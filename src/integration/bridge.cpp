/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file bridge.cpp Bounded, private local IPC serviced on the simulation thread. */

#include "../stdafx.h"
#include "integration_bridge.h"
#include "actions.h"
#include "chat.h"
#include "ledger.h"
#include "renderer.h"
#include "visual_commands.h"
#include "../command_func.h"
#include "../company_base.h"
#include "../company_cmd.h"
#include "../company_func.h"
#include "../debug.h"
#include "../network/network.h"
#include "../network/network_base.h"
#include "../network/network_client.h"
#include "../network/network_func.h"
#include "../network/network_internal.h"
#include "../openttd.h"
#include "../rev.h"
#include "../saveload/saveload.h"
#include "../saveload/saveload_func.h"
#include "../settings_type.h"
#include "../timer/timer_game_tick.h"
#include "../3rdparty/monocypher/monocypher.h"
#include <cstdlib>
#include <deque>
#include <filesystem>
#include <random>
#ifndef _WIN32
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#endif

#include "../safeguards.h"

namespace Integration {
namespace {

constexpr size_t MAX_FRAME = 65536;
constexpr size_t MAX_CLIENTS = 16;
constexpr size_t MAX_EVENTS = 4096;
constexpr size_t MAX_SESSIONS = 8192;
using Clock = std::chrono::steady_clock;

std::string Environment(const char *name, const char *fallback = "")
{
	const char *value = std::getenv(name);
	return value == nullptr ? fallback : value;
}

std::string NewId()
{
	std::random_device random;
	return fmt::format("{:08x}{:08x}{:08x}{:08x}", random(), random(), random(), random());
}

struct PendingAction {
	RequestIdentity identity;
	Json actor;
	Json action;
	Json captured_targets;
	std::optional<VisualCommand> visual;
	unsigned queued = 0;
	std::optional<Json> prequeue_failure = std::nullopt;
};

bool UsesImageInput(const PendingAction &pending)
{
	return pending.action.at("name") == "ui.apply";
}

struct Client {
	int fd = -1;
	std::string input;
	std::string output;
	size_t sent = 0;
	Clock::time_point opened = Clock::now();
};

struct State {
	bool initialized = false;
	bool configured = false;
	bool load_reconciled = false;
	bool world_replaced = false;
	int listener = -1;
	std::string socket_path;
	std::string world = Environment("OPENTTD_INTEGRATION_WORLD_ID", "world");
	std::string epoch = Environment("OPENTTD_INTEGRATION_WORLD_EPOCH", "initial");
	std::string instance = NewId();
	std::filesystem::path directory;
	std::unique_ptr<RequestLedger> ledger;
	std::map<std::string, Json> sessions;
	std::map<std::string, uint64_t> company_generations;
	std::map<uint64_t, PendingAction> pending;
	uint64_t submitting = 0;
	uint64_t executing = 0;
	std::vector<Client> clients;
	std::deque<Json> events;
	uint64_t next_event = 1;
	Json pause_facts;
	uint64_t pause_revision = 0;
	std::optional<CheckpointTicket> checkpoint;
	std::filesystem::path checkpoint_path;
	std::string checkpoint_status = "idle";
	std::optional<uint32_t> company_creation;
};

State &GetState()
{
	static State state;
	return state;
}

/** Shared control metadata only; native command categories remain authoritative. */
Json PauseFacts()
{
	Json reasons = Json::array();
	static constexpr std::pair<PauseMode, const char *> names[] = {
		{PauseMode::Normal, "manual"},
		{PauseMode::SaveLoad, "saving_loading"},
		{PauseMode::Join, "joining"},
		{PauseMode::Error, "error"},
		{PauseMode::ActiveClients, "insufficient_clients"},
		{PauseMode::GameScript, "game_script"},
		{PauseMode::LinkGraph, "link_graph"},
	};
	for (const auto &[mode, name] : names) if (_pause_mode.Test(mode)) reasons.push_back(name);
	/* CommandDuringPause is bookkeeping, not an additional pause cause. */
	if (_pause_mode.Any() && reasons.empty()) reasons.push_back("other");
	static constexpr const char *policies[] = {"no_actions", "no_construction", "no_landscaping", "all_actions"};
	const size_t policy = std::min<size_t>(to_underlying(_settings_game.construction.command_pause_level), std::size(policies) - 1);
	return {{"paused", _pause_mode.Any()}, {"reasons", std::move(reasons)}, {"command_policy", policies[policy]}};
}

/** No listener/ledger initialization; the first observation is a baseline. */
Json ObservePauseStatus()
{
	auto &s = GetState();
	Json facts = PauseFacts();
	const bool changed = facts != s.pause_facts;
	const bool initial = s.pause_revision == 0;
	if (changed) {
		/* Exhaustion cannot wrap and make old revisions appear current. */
		if (s.pause_revision == UINT64_MAX) { s.world_replaced = true; return Json(); }
		s.pause_facts = std::move(facts);
		++s.pause_revision;
	}
	Json result = s.pause_facts;
	result["revision"] = s.pause_revision;
	if (changed && !initial && Enabled()) PublishEvent({{"type", "world.status"}, {"data", {{"pause", result}}}});
	return result;
}

std::string loaded_path;
uint64_t loaded_tick = 0;

bool ValidString(const Json &value, size_t maximum = 256)
{
	if (!value.is_string()) return false;
	const auto &s = value.get_ref<const std::string &>();
	return !s.empty() && s.size() <= maximum && std::all_of(s.begin(), s.end(), [](unsigned char c) { return c >= 32 && c != 127; });
}

uint64_t Unsigned(const Json &object, const char *key, uint64_t maximum = UINT64_MAX)
{
	const auto &value = object.at(key);
	if (!value.is_number_unsigned()) throw std::invalid_argument("unsigned field required");
	auto number = value.get<uint64_t>();
	if (number > maximum) throw std::invalid_argument("field out of bounds");
	return number;
}

Json Error(const char *code)
{
	return {{"code", code}, {"message", "Integration request unavailable"}};
}

Json Reply(const Json &request, const char *status, Json result = Json::object(), const char *error = nullptr)
{
	auto &s = GetState();
	Json response = {{"protocol_version", 1}, {"type", request.value("type", std::string("invalid")) + ".result"},
		{"correlation_id", request.value("correlation_id", std::string())}, {"status", status}, {"result", std::move(result)},
		{"world_id", s.world}, {"world_epoch", s.epoch}, {"engine_instance_id", s.instance}, {"tick", TimerGameTick::counter}};
	if (error != nullptr) response["error"] = Error(error);
	return response;
}

bool AuthoritativeReady()
{
	return _game_mode == GameMode::Normal && _networking && _network_server && !RendererIsActive();
}

Json ReadActor(const Json &request)
{
	const auto &actor = request.at("actor");
	if (!actor.is_object() || actor.size() != 4 || !ValidString(actor.at("identity_id")) || !ValidString(actor.at("run_id")) ||
			!actor.at("mode").is_string() || (actor.at("mode") != "text" && actor.at("mode") != "visual")) throw std::invalid_argument("invalid actor");
	Unsigned(actor, "company_id", MAX_COMPANIES - 1);
	return actor;
}

bool Bound(const Json &actor)
{
	auto &s = GetState();
	const auto &run = actor.at("run_id").get_ref<const std::string &>();
	auto found = s.sessions.find(run);
	auto generation = s.company_generations.find(run);
	auto *company = Company::GetIfValid(CompanyID(actor.at("company_id").get<uint8_t>()));
	return found != s.sessions.end() && found->second == actor && company != nullptr &&
		generation != s.company_generations.end() && generation->second == company->GetInstanceGeneration();
}

RequestIdentity Identity(const Json &request, const Json &actor)
{
	if (!ValidString(request.at("request_id"))) throw std::invalid_argument("invalid request id");
	uint64_t sequence = Unsigned(request, "request_sequence");
	if (sequence == 0) throw std::invalid_argument("zero request sequence");
	return {sequence, actor.at("identity_id").get<std::string>(), request.at("request_id").get<std::string>()};
}

RequestBinding Binding(const Json &actor)
{
	return {actor.at("company_id").get<uint32_t>(), actor.at("mode").get<std::string>()};
}

Json Canonical(const Json &request, const Json &actor, const char *kind)
{
	Json payload = {{"type", kind}, {"actor", actor}};
	if (std::string_view(kind) == "chat.publish") payload["message"] = request.at("message");
	else {
		payload["action"] = request.at("action");
		if (request.at("action").value("name", std::string()) == "ui.apply") payload["visual_intent"] = request.at("visual_intent");
	}
	return payload;
}

Json ServiceActor()
{
	return {{"identity_id", "backend-chat"}, {"company_id", 255U}, {"run_id", "backend-chat"}, {"mode", "text"}};
}

Json RequestActor(const Json &request)
{
	return request.value("request_type", std::string()) == "chat.publish" ? ServiceActor() : ReadActor(request);
}

const char *Lifecycle(RequestState state)
{
	switch (state) {
		case RequestState::Reserved: case RequestState::Queued: return "queued";
		case RequestState::Executed: return "completed";
		case RequestState::Rejected: return "rejected";
		case RequestState::Uncertain: return "uncertain";
		case RequestState::RolledBack: return "resolved_rolled_back";
	}
	return "uncertain";
}

Json EntryReply(const Json &request, const LedgerEntry &entry)
{
	Json result = entry.outcome.is_null() ? Json::object() : entry.outcome;
	result["request_id"] = entry.identity.request_id;
	result["request_sequence"] = entry.identity.request_sequence;
	result["checkpoint_state"] = entry.checkpoint_committed ? "checkpointed" : "uncheckpointed";
	result["executed_boot_id"] = entry.executed_boot_id;
	Json reply = Reply(request, Lifecycle(entry.state), std::move(result));
	if (entry.state == RequestState::Rejected && entry.outcome.contains("error")) reply["error"] = entry.outcome.at("error");
	return reply;
}

Json AdmissionReply(const Json &request, const LedgerAdmission &admission)
{
	if (admission.entry) return EntryReply(request, *admission.entry);
	switch (admission.status) {
		case AdmissionStatus::Retired: return Reply(request, "retired", {}, "retired_request");
		case AdmissionStatus::Conflict: return Reply(request, "rejected", {}, "request_conflict");
		case AdmissionStatus::Full: return Reply(request, "rejected", {}, "request_window_full");
		case AdmissionStatus::Recovering: return Reply(request, "rejected", {}, "recovery_required");
		default: return Reply(request, "rejected", {}, "ledger_unavailable");
	}
}

void Reject(PendingAction &pending, const char *code, const Json *native_failure = nullptr)
{
	auto &s = GetState();
	if (UsesImageInput(pending)) code = "visual_action_unavailable";
	Json outcome = native_failure != nullptr && !UsesImageInput(pending) ?
		*native_failure : Json{{"error", Error(code)}};
	if (s.ledger && !s.ledger->MarkRejected(pending.identity, TimerGameTick::counter, _frame_counter, outcome)) s.world_replaced = true;
	Json event = {{"type", "action.status"}, {"actor", pending.actor}, {"request_id", pending.identity.request_id},
		{"request_sequence", pending.identity.request_sequence}, {"status", "rejected"}, {"error", outcome.at("error")}};
	if (native_failure != nullptr && !UsesImageInput(pending)) event["result"] = outcome;
	PublishEvent(event);
}

#ifndef _WIN32
bool PrivateDirectory(const std::filesystem::path &directory)
{
	if (directory.empty() || !directory.is_absolute()) return false;
	std::error_code error;
	bool created = std::filesystem::create_directories(directory, error);
	if (error) return false;
	struct stat st;
	if (lstat(directory.c_str(), &st) != 0 || !S_ISDIR(st.st_mode) || st.st_uid != geteuid()) return false;
	if (created && chmod(directory.c_str(), 0700) != 0) return false;
	return created || (st.st_mode & 0077) == 0;
}

void WriteSessions(const CheckpointTicket &ticket)
{
	auto &s = GetState();
	Json sessions = Json::array();
	for (const auto &[run, actor] : s.sessions) sessions.push_back({{"actor", actor}, {"active", Bound(actor)}});
	std::string bytes = Json{{"checkpoint_id", ticket.checkpoint_id}, {"sessions", std::move(sessions)}}.dump();
	if (bytes.size() > 8 * 1024 * 1024) throw std::runtime_error("session checkpoint too large");
	auto path = s.directory / ("sessions-" + ticket.checkpoint_id + ".json");
	int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
	if (fd < 0) throw std::runtime_error("session checkpoint unavailable");
	size_t done = 0;
	bool ok = true;
	while (done < bytes.size()) {
		ssize_t count = write(fd, bytes.data() + done, bytes.size() - done);
		if (count < 0 && errno == EINTR) continue;
		if (count <= 0) { ok = false; break; }
		done += count;
	}
	if (ok) ok = fsync(fd) == 0;
	close(fd);
	int directory_fd = open(s.directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (directory_fd < 0) ok = false;
	else { if (fsync(directory_fd) != 0) ok = false; close(directory_fd); }
	if (!ok) throw std::runtime_error("session checkpoint sync failed");
}

bool LoadSessions(const CheckpointMetadata &checkpoint)
{
	auto &s = GetState();
	auto path = s.directory / ("sessions-" + checkpoint.checkpoint_id + ".json");
	int fd = open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
	if (fd < 0) return false;
	struct stat st;
	if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077) != 0 || st.st_size < 1 || st.st_size > 8 * 1024 * 1024) { close(fd); return false; }
	std::string bytes(static_cast<size_t>(st.st_size), '\0');
	size_t done = 0;
	while (done < bytes.size()) {
		ssize_t count = read(fd, bytes.data() + done, bytes.size() - done);
		if (count < 0 && errno == EINTR) continue;
		if (count <= 0) { close(fd); return false; }
		done += count;
	}
	close(fd);
	try {
		Json json = Json::parse(bytes);
		if (json.at("checkpoint_id") != checkpoint.checkpoint_id || !json.at("sessions").is_array() || json.at("sessions").size() > MAX_SESSIONS) return false;
		std::map<std::string, Json> sessions;
		std::map<std::string, bool> active;
		for (const auto &row : json.at("sessions")) {
			const auto &actor = row.at("actor");
			ReadActor({{"actor", actor}});
			if (!sessions.emplace(actor.at("run_id").get<std::string>(), actor).second) return false;
			active[actor.at("run_id").get<std::string>()] = row.at("active").get<bool>();
		}
		s.sessions = std::move(sessions);
		s.company_generations.clear();
		for (const auto &[run, actor] : s.sessions) {
			const Company *company = Company::GetIfValid(CompanyID(actor.at("company_id").get<uint8_t>()));
			s.company_generations[run] = company == nullptr || !active.at(run) ? 0 : company->GetInstanceGeneration();
		}
		return true;
	} catch (const std::exception &) { return false; }
}
#endif

void ReconcileLoaded()
{
	auto &s = GetState();
	if (!s.ledger || s.load_reconciled || !s.ledger->Healthy() || !AuthoritativeReady()) return;
	const auto &checkpoint = s.ledger->Checkpoint();
	if (checkpoint.checkpoint_id.empty()) {
		s.load_reconciled = s.ledger->ReconcileLoadedCheckpoint({});
		return;
	}
#ifndef _WIN32
	std::error_code error;
	if (!loaded_path.empty() && std::filesystem::path(loaded_path).is_absolute() && loaded_tick == checkpoint.saved_tick && std::filesystem::equivalent(loaded_path, checkpoint.save_path, error) && !error && LoadSessions(checkpoint)) {
		s.load_reconciled = s.ledger->ReconcileLoadedCheckpoint(checkpoint);
	}
#endif
}

void Initialize()
{
	auto &s = GetState();
	if (s.initialized) return;
	s.initialized = true;
	s.socket_path = Environment("OPENTTD_INTEGRATION_SOCKET");
	if (s.socket_path.empty()) return;
#ifndef _WIN32
	s.directory = Environment("OPENTTD_INTEGRATION_STATE_DIR");
	if (!PrivateDirectory(s.directory) || s.world.empty() || s.world.size() > 256 || s.epoch.empty() || s.epoch.size() > 256) return;
	std::filesystem::path socket_path(s.socket_path);
	if (!socket_path.is_absolute() || !PrivateDirectory(socket_path.parent_path())) return;
	sockaddr_un address{};
	if (s.socket_path.size() >= sizeof(address.sun_path)) return;
	address.sun_family = AF_UNIX;
	std::copy(s.socket_path.begin(), s.socket_path.end(), address.sun_path);
	struct stat st;
	if (lstat(s.socket_path.c_str(), &st) == 0) {
		if (!S_ISSOCK(st.st_mode) || st.st_uid != geteuid()) return;
		int probe = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
		if (probe < 0) return;
		int result = connect(probe, reinterpret_cast<sockaddr *>(&address), sizeof(address));
		int saved_errno = errno;
		close(probe);
		if (result == 0 || saved_errno != ECONNREFUSED) return;
		if (unlink(s.socket_path.c_str()) != 0) return;
	}
	s.listener = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
	if (s.listener < 0) return;
	if (bind(s.listener, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0 || chmod(s.socket_path.c_str(), 0600) != 0 || listen(s.listener, MAX_CLIENTS) != 0) {
		close(s.listener); s.listener = -1; return;
	}
	s.configured = true;
	if (!RendererIsActive()) {
		s.ledger = std::make_unique<RequestLedger>(LedgerConfig{s.directory}, LedgerFence{s.world, s.epoch, s.instance});
		s.ledger->Open();
	}
#endif
}

Json CheckpointInfo()
{
	auto &s = GetState();
	if (!s.ledger) return Json::object();
	const auto &checkpoint = s.ledger->Checkpoint();
	Json parent = nullptr;
	if (checkpoint.parent_checkpoint) {
		const auto &metadata = *checkpoint.parent_checkpoint;
		parent = {{"checkpoint_id", metadata.checkpoint_id}, {"save_checksum", metadata.save_checksum}, {"saved_tick", metadata.saved_tick}};
	}
	return {{"checkpoint_id", checkpoint.checkpoint_id}, {"saved_tick", checkpoint.saved_tick}, {"saved_frame", checkpoint.saved_frame},
		{"saved_boot_id", checkpoint.saved_boot_id},
		{"parent_checkpoint", std::move(parent)}, {"checkpoint_retired_through", checkpoint.retired_through},
		{"retired_through", s.ledger->RetiredThrough()}, {"save_path", checkpoint.save_path}, {"save_checksum", checkpoint.save_checksum},
		{"status", s.checkpoint_status}, {"load_verified", s.load_reconciled}, {"recovery_required", s.ledger->RecoveryRequired()}};
}

Json VisualActionSpec()
{
	Json coordinate = {{"type", "integer"}, {"minimum", 0}, {"maximum", 4095}};
	auto branch = [&](const char *gesture, bool drag, bool text) {
		Json required = {"image_id", "gesture", "x", "y"};
		Json properties = {{"image_id", {{"type", "string"}, {"minLength", 1}, {"maxLength", 256}}},
			{"gesture", {{"type", "string"}, {"enum", {gesture}}}}, {"x", coordinate}, {"y", coordinate}};
		if (text) {
			required.push_back("text");
			properties["text"] = {{"type", "string"}, {"maxLength", 4096}};
		} else if (std::string_view(gesture) != "move") {
			properties["ctrl"] = {{"type", "boolean"}};
			properties["button"] = {{"type", "string"}, {"enum", drag ? Json::array({"left"}) : Json::array({"left", "right"})}};
		}
		if (drag) {
			required.push_back("end_x"); required.push_back("end_y");
			properties["end_x"] = coordinate; properties["end_y"] = coordinate;
		}
		return Json{{"type", "object"}, {"additionalProperties", false}, {"required", std::move(required)}, {"properties", std::move(properties)}};
	};
	auto control = [&](const char *name, bool orientation) {
		Json schema = branch("control", false, false);
		auto &properties = schema["properties"];
		properties.erase("button"); properties.erase("ctrl");
		schema["required"].push_back("control");
		properties["control"] = {{"type", "string"}, {"enum", {name}}};
		if (orientation) {
			schema["required"].push_back("orientation");
			properties["orientation"] = {{"type", "string"}, {"enum", {"terminal_ne", "terminal_se", "terminal_sw", "terminal_nw", "drive_through_ne_sw", "drive_through_se_nw"}}};
		}
		return schema;
	};
	return {{"name", "ui.apply"}, {"modes", {"visual"}}, {"target_kind", "screenshot"}, {"result_fields", Json::array()},
		{"parameters", {{"anyOf", Json::array({branch("click", false, false), branch("drag", true, false), branch("move", false, false), branch("text", false, true), control("orders.go_to", false), control("road_stop.orientation", true)})}}}};
}

/** One frozen schema document supplies discovery, export and its identity. */
const Json &NativeCatalog()
{
	static const Json catalog = [] {
		Json value = CapabilityMatrix();
		value["actions"].push_back(VisualActionSpec());
		return value;
	}();
	return catalog;
}

const std::string &CatalogHash()
{
	static const std::string hash = [] {
		auto bytes = NativeCatalog().dump();
		std::array<uint8_t, 32> digest{};
		crypto_blake2b(digest.data(), digest.size(), reinterpret_cast<const uint8_t *>(bytes.data()), bytes.size());
		static constexpr char digits[] = "0123456789abcdef";
		std::string value; value.reserve(64);
		for (auto byte : digest) { value.push_back(digits[byte >> 4]); value.push_back(digits[byte & 15]); }
		return value;
	}();
	return hash;
}

Json CompactCapabilities(bool visual)
{
	static const Json compact = [] {
		Json value = NativeCatalog();
		for (const char *kind : {"actions", "queries"}) {
			Json names = Json::array();
			for (const auto &entry : value.at(kind)) names.push_back(entry.at("name"));
			value[kind] = std::move(names);
		}
		value["catalog_hash_algorithm"] = "blake2b-256";
		value["catalog_hash"] = CatalogHash();
		return value;
	}();
	Json result = compact;
	if (visual) {
		result["queries"] = Json::array();
		result["renderer_controls"] = {"view.control", "window.open", "image.request"};
	}
	return result;
}

bool ValidVisualParameters(const Json &parameters)
{
	if (!parameters.is_object() || !ValidString(parameters.at("image_id")) || !parameters.at("gesture").is_string()) return false;
	for (auto it = parameters.begin(); it != parameters.end(); ++it) {
		if (it.key() != "image_id" && it.key() != "gesture" && it.key() != "x" && it.key() != "y" && it.key() != "end_x" && it.key() != "end_y" && it.key() != "button" && it.key() != "ctrl" && it.key() != "text" && it.key() != "control" && it.key() != "orientation") return false;
	}
	Unsigned(parameters, "x", 4095);
	Unsigned(parameters, "y", 4095);
	if (parameters.at("gesture") == "control") {
		for (const char *field : {"button", "ctrl", "end_x", "end_y", "text"}) if (parameters.contains(field)) return false;
		if (!parameters.contains("control") || !parameters.at("control").is_string()) return false;
		if (parameters.at("control") == "orders.go_to") return !parameters.contains("orientation");
		if (parameters.at("control") != "road_stop.orientation" || !parameters.contains("orientation")) return false;
		for (const char *orientation : {"terminal_ne", "terminal_se", "terminal_sw", "terminal_nw", "drive_through_ne_sw", "drive_through_se_nw"}) if (parameters.at("orientation") == orientation) return true;
		return false;
	}
	if (parameters.contains("control") || parameters.contains("orientation")) return false;
	if (parameters.contains("ctrl") && !parameters.at("ctrl").is_boolean()) return false;
	if (parameters.at("gesture") == "text") {
		if (parameters.contains("button") || parameters.contains("end_x") || parameters.contains("end_y") || parameters.contains("ctrl") || !parameters.at("text").is_string()) return false;
		const auto &text = parameters.at("text").get_ref<const std::string &>();
		return text.size() <= 4096 && std::ranges::none_of(text, [](unsigned char c) { return c < 32 || c == 127; });
	}
	if (parameters.contains("text")) return false;
	if (parameters.at("gesture") == "drag") {
		Unsigned(parameters, "end_x", 4095);
		Unsigned(parameters, "end_y", 4095);
		if (parameters.contains("button") && parameters.at("button") != "left") return false;
	} else {
		if (parameters.contains("end_x") || parameters.contains("end_y")) return false;
		if (parameters.at("gesture") == "move") return !parameters.contains("button") && !parameters.contains("ctrl");
		if (parameters.at("gesture") != "click") return false;
	}
	return !parameters.contains("button") || parameters.at("button") == "left" || parameters.at("button") == "right";
}

Json Handle(const Json &request)
{
	auto &s = GetState();
	if (!request.is_object() || request.value("protocol_version", 0) != 1 || !ValidString(request.at("type"), 80) || !ValidString(request.at("correlation_id"))) return Reply(Json::object(), "rejected", {}, "invalid_envelope");
	if (request.at("world_id") != s.world || request.at("world_epoch") != s.epoch) return Reply(request, "rejected", {}, "world_fence");
	std::string type = request.at("type").get<std::string>();
	bool visual = request.contains("actor") && request.at("actor").is_object() && request.at("actor").value("mode", std::string()) == "visual";
	if (type == "hello") {
		Json capabilities = CompactCapabilities(visual || RendererIsActive());
		/* Discovery remains available during recovery; a replaced/unready world
		 * cannot supply current authoritative metadata under its previous fence.
		 * Renderer hello describes only its private replica, never the authority. */
		const bool pause_available = RendererIsActive() || (AuthoritativeReady() && s.ledger && s.ledger->Healthy() &&
			!s.world_replaced && s.load_reconciled && !s.ledger->RecoveryRequired());
		Json result = {{"engine_instance_id", s.instance}, {"world_epoch", s.epoch}, {"role", RendererIsActive() ? "renderer" : "world"},
			{"capabilities", std::move(capabilities)}, {"native_max_lag_ticks", _settings_client.network.max_lag_time}, {"baseline_hash", std::string(_openttd_revision_hash)},
			{"recovery_required", s.ledger ? s.ledger->RecoveryRequired() : false}, {"pause", pause_available ? ObservePauseStatus() : Json()}};
		if (RendererIsActive()) {
			const NetworkClientInfo *native = _networking && !_network_server && MyClient::IsConnected() ? NetworkClientInfo::GetByClientID(_network_own_client_id) : nullptr;
			result["native_public_key"] = native == nullptr ? "" : native->public_key;
		}
		return Reply(request, "completed", std::move(result));
	}
	if (request.value("engine_instance_id", std::string()) != s.instance) return Reply(request, "rejected", {}, "engine_fence");
	if (visual && type != "session.bind" && type != "view.control" && type != "window.open" && type != "image.request" && type != "control.reference" && type != "action.estimate" && type != "action.submit" && type != "action.status" && type != "ui.capture" && type != "ui.status" && type != "ui.release" && type != "recovery.reconcile" && type != "recovery.resolve") return Reply(request, "rejected", {}, "mode_restricted");
	if (type == "catalog.describe") {
		if (RendererIsActive() || request.contains("actor")) return Reply(request, "rejected", {}, "catalog_operator_only");
		if (!ValidString(request.at("name"), 128) || !request.at("kind").is_string()) return Reply(request, "rejected", {}, "invalid_catalog_request");
		std::string kind = request.at("kind").get<std::string>();
		if (kind != "action" && kind != "query") return Reply(request, "rejected", {}, "invalid_catalog_request");
		for (const auto &entry : NativeCatalog().at(kind == "action" ? "actions" : "queries")) {
			if (entry.at("name") != request.at("name")) continue;
			Json result = {{"entry", entry}, {"version", NativeCatalog().at("version")}, {"catalog_hash_algorithm", "blake2b-256"}, {"catalog_hash", CatalogHash()}};
			if (result.dump().size() > MAX_FRAME - 2048) return Reply(request, "rejected", {}, "catalog_entry_too_large");
			return Reply(request, "completed", std::move(result));
		}
		return Reply(request, "rejected", {}, "unknown_catalog_entry");
	}
	if (type == "session.bind") {
		Json actor = ReadActor(request);
		if (!Company::IsValidID(CompanyID(actor.at("company_id").get<uint8_t>()))) return Reply(request, "rejected", {}, "company_unavailable");
		std::string run = actor.at("run_id").get<std::string>();
		auto found = s.sessions.find(run);
		if (found != s.sessions.end() && found->second != actor) return Reply(request, "rejected", {}, "binding_conflict");
		if (found != s.sessions.end() && !Bound(actor)) return Reply(request, "rejected", {}, "stale_company_binding");
		if (s.sessions.size() >= MAX_SESSIONS && found == s.sessions.end()) return Reply(request, "rejected", {}, "binding_limit");
		if (s.checkpoint) return Reply(request, "rejected", {}, "checkpoint_busy");
		if (RendererIsActive()) {
			uint32_t lag = request.contains("authority_max_lag_ticks") ? static_cast<uint32_t>(Unsigned(request, "authority_max_lag_ticks", UINT16_MAX)) : 0;
			if (request.contains("authority_max_lag_ticks") && lag == 0) return Reply(request, "rejected", {}, "invalid_authority_budget");
			ConfigureRenderer(CompanyID(actor.at("company_id").get<uint8_t>()), run, request.value("authority_instance_id", std::string()), s.epoch, lag);
		}
		s.sessions[run] = actor;
		s.company_generations[run] = Company::Get(CompanyID(actor.at("company_id").get<uint8_t>()))->GetInstanceGeneration();
		return Reply(request, "completed", {{"bound", true}});
	}
	if (RendererIsActive()) {
		if (type != "view.control" && type != "window.open" && type != "image.request" && type != "ui.capture" && type != "ui.status" && type != "ui.release") return Reply(request, "rejected", {}, "renderer_read_only");
		Json actor = ReadActor(request);
		if (!Bound(actor)) return Reply(request, "rejected", {}, "session_unbound");
		Json parameters = request.value("parameters", Json::object());
		if (type == "window.open") parameters["action"] = "open_window";
		Json result;
		if (type == "ui.capture") result = RendererUICapture(parameters);
		else if (type == "ui.status") result = RendererUIStatus(parameters);
		else if (type == "ui.release") result = RendererUIRelease(parameters);
		else result = type == "image.request" ? RendererCapture(parameters) : RendererControl(parameters);
		return result.contains("error") ? Reply(request, "rejected", {}, result.at("error").at("code").get<std::string>().c_str()) : Reply(request, "completed", std::move(result));
	}
	if (!AuthoritativeReady() || !s.ledger || !s.ledger->Healthy() || s.world_replaced) return Reply(request, "rejected", {}, "world_unavailable");
	if (type == "checkpoint.status") return Reply(request, "completed", CheckpointInfo());
	if (type == "recovery.reconcile") {
		Json actor = RequestActor(request);
		auto admission = s.ledger->ReconcileAccepted(Identity(request, actor), Binding(actor), Canonical(request, actor, request.value("request_type", std::string("action.submit")).c_str()));
		return AdmissionReply(request, admission);
	}
	if (type == "recovery.finish") return s.load_reconciled && s.ledger->FinishRecovery() ? Reply(request, "completed", {{"ready", true}}) : Reply(request, "rejected", {}, "loaded_checkpoint_unverified");
	if (type == "recovery.resolve") {
		Json actor = RequestActor(request);
		auto identity = Identity(request, actor);
		auto entry = s.ledger->Lookup(identity);
		if (!entry || entry->binding != Binding(actor) || entry->canonical_payload != Canonical(request, actor, request.value("request_type", std::string("action.submit")).c_str()).dump()) return Reply(request, "rejected", {}, "request_conflict");
		bool rollback = request.value("resolution", std::string()) == "resolved_rolled_back";
		bool rejected = request.value("resolution", std::string()) == "rejected";
		if (!rollback && !rejected) return Reply(request, "rejected", {}, "invalid_resolution");
		return s.ledger->ResolveUncertain(identity, rollback, {{"resolution", rollback ? "resolved_rolled_back" : "rejected"}}) ? Reply(request, "completed") : Reply(request, "rejected", {}, "resolution_unavailable");
	}
	if (type == "action.ack") {
		uint64_t through = request.contains("through_sequence") ? Unsigned(request, "through_sequence") : Unsigned(request, "request_sequence");
		return s.ledger->AckThrough(through) ? Reply(request, "completed", {{"retired_through", s.ledger->RetiredThrough()}}) : Reply(request, "rejected", {}, "checkpoint_required");
	}
	if (type == "company.ensure") {
		if (request.contains("actor")) return Reply(request, "rejected", {}, "operator_context_required");
		uint32_t company = Unsigned(request, "company_id", MAX_COMPANIES - 1);
		if (Company::IsValidID(CompanyID(company))) {
			if (s.company_creation == company) s.company_creation.reset();
			return Reply(request, "completed", {{"company_id", company}});
		}
		if (!request.value("create", false) || s.ledger->RecoveryRequired()) return Reply(request, "rejected", {}, "company_unavailable");
		if (s.company_creation) return *s.company_creation == company ? Reply(request, "queued", {{"company_id", company}}) : Reply(request, "rejected", {}, "company_creation_busy");
		for (uint32_t id = 0; id < company; ++id) if (!Company::IsValidID(CompanyID(id))) return Reply(request, "rejected", {}, "company_creation_order");
		if (Company::GetNumItems() >= _settings_client.network.max_companies) return Reply(request, "rejected", {}, "company_limit");
		AutoRestoreBackup current(_current_company, COMPANY_SPECTATOR);
		if (!Command<Commands::CompanyControl>::Post(CompanyCtrlAction::New, CompanyID::Invalid(), CompanyRemoveReason::None, static_cast<ClientID>(UINT32_MAX - 1))) return Reply(request, "rejected", {}, "company_creation_failed");
		s.company_creation = company;
		return Reply(request, "queued", {{"company_id", company}});
	}
	if (type == "company.access") {
		/* Operator RPC only: public sessions never receive this method or its inputs. */
		if (request.contains("actor")) return Reply(request, "rejected", {}, "operator_context_required");
		CompanyID company(static_cast<uint8_t>(Unsigned(request, "company_id", MAX_COMPANIES - 1)));
		const Company *current_company = Company::GetIfValid(company);
		if (current_company == nullptr || s.ledger->RecoveryRequired() || s.checkpoint) return Reply(request, "rejected", {}, "company_unavailable");
		if (!request.at("allow_any").is_boolean()) return Reply(request, "rejected", {}, "invalid_company_access");
		bool allow_any = request.at("allow_any").get<bool>();
		std::vector<std::pair<CompanyAllowListCtrlAction, std::string>> changes;
		std::set<std::string> keys;
		size_t requested = 1;
		for (const char *field : {"add_keys", "remove_keys"}) {
			const Json &values = request.contains(field) ? request.at(field) : Json::array();
			if (!values.is_array() || values.size() > 31 || requested + values.size() > 32) return Reply(request, "rejected", {}, "company_access_limit");
			requested += values.size();
			for (const Json &value : values) {
				if (!value.is_string()) return Reply(request, "rejected", {}, "invalid_company_key");
				std::string key = value.get<std::string>();
				if (key.size() != 64 || !std::all_of(key.begin(), key.end(), [](unsigned char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'); })) return Reply(request, "rejected", {}, "invalid_company_key");
				for (char &c : key) if (c >= 'A' && c <= 'F') c += 'a' - 'A';
				if (!keys.insert(key).second) return Reply(request, "rejected", {}, "conflicting_company_keys");
				bool adding = std::string_view(field) == "add_keys";
				if (current_company->allow_list.Contains(key) != adding) changes.emplace_back(adding ? CompanyAllowListCtrlAction::AddKey : CompanyAllowListCtrlAction::RemoveKey, std::move(key));
			}
		}
		/* Apply the join policy after its explicit key edits. */
		if (current_company->allow_any != allow_any) changes.emplace_back(allow_any ? CompanyAllowListCtrlAction::AllowAny : CompanyAllowListCtrlAction::AllowListed, "");
		if (changes.empty()) return Reply(request, "completed", {{"company_id", company.base()}, {"commands_queued", 0}, {"requested_commands", requested}});
		if (PendingLocalCommands() + changes.size() > 32) return Reply(request, "rejected", {}, "operator_queue_full");
		AutoRestoreBackup company_context(_current_company, company);
		size_t queued = 0;
		for (const auto &[action, key] : changes) {
			if (!Command<Commands::CompanyAllowListControl>::Post(action, key)) return Reply(request, queued == 0 ? "rejected" : "queued",
				{{"company_id", company.base()}, {"commands_queued", queued}, {"requested_commands", changes.size()}, {"partial", queued != 0}}, "company_access_partial");
			++queued;
		}
		return Reply(request, "queued", {{"company_id", company.base()}, {"commands_queued", queued}, {"requested_commands", changes.size()}});
	}
	if (type == "events.poll") {
		uint64_t after = request.contains("after") ? Unsigned(request, "after") : 0;
		size_t limit = request.contains("limit") ? Unsigned(request, "limit", 128) : 64;
		Json events = Json::array();
		bool gap = !s.events.empty() && after < s.events.front().at("event_id").get<uint64_t>() - 1;
		size_t bytes = 0;
		for (const auto &event : s.events) if (event.at("event_id").get<uint64_t>() > after && events.size() < limit) {
			size_t count = event.dump().size();
			if (bytes + count > 48000) break;
			bytes += count; events.push_back(event);
		}
		uint64_t cursor = events.empty() ? after : events.back().at("event_id").get<uint64_t>();
		return Reply(request, "completed", {{"events", std::move(events)}, {"cursor", cursor}, {"gap", gap}, {"oldest_event_id", s.events.empty() ? s.next_event : s.events.front().at("event_id").get<uint64_t>()}});
	}
	if (type == "chat.pending" || type == "chat.ack") {
		Json result = type == "chat.pending" ? ChatPending(request.value("parameters", request)) : ChatAcknowledge(request.value("parameters", request));
		return result.contains("error") ? Reply(request, "rejected", {}, "chat_request_rejected") : Reply(request, "completed", std::move(result));
	}
	if (type == "roster" || type == "chat.roster") return Reply(request, "completed", NativeRoster());
	if (type == "checkpoint.create") {
		if (s.checkpoint || !s.pending.empty() || s.company_creation || IsSaveInProgress()) return Reply(request, "rejected", {}, "checkpoint_busy");
		auto ticket = s.ledger->BeginCheckpoint(TimerGameTick::counter, _frame_counter);
		if (!ticket) return Reply(request, "rejected", {}, "checkpoint_unavailable");
		s.checkpoint = ticket;
		s.checkpoint_status = "saving";
		s.checkpoint_path = s.directory / ("world-" + ticket->checkpoint_id + ".sav");
		try {
#ifndef _WIN32
			WriteSessions(*ticket);
#endif
			if (SaveOrLoad(s.checkpoint_path.string(), SaveLoadOperation::Save, DetailedFileType::GameFile, Subdirectory::None, true) != SaveLoadResult::Ok) SaveFinished(false);
		} catch (const std::exception &) { SaveFinished(false); }
		return Reply(request, s.checkpoint_status == "failed" ? "rejected" : "queued", {{"checkpoint_id", ticket->checkpoint_id}});
	}
	if (type == "chat.publish") {
		Json actor = ServiceActor();
		auto identity = Identity(request, actor);
		auto admission = s.ledger->Reserve(identity, {255, "text"}, Canonical(request, actor, "chat.publish"), s.ledger->Fence());
		if (admission.status != AdmissionStatus::Admitted) return AdmissionReply(request, admission);
		Json result = DeliverChat(request.at("message"));
		bool recorded = result.value("ok", false) ? s.ledger->MarkExecuted(identity, TimerGameTick::counter, _frame_counter, result) :
			s.ledger->MarkRejected(identity, TimerGameTick::counter, _frame_counter, {{"error", Error("chat_delivery_rejected")}});
		if (!recorded) { s.world_replaced = true; return Reply(request, "uncertain", {}, "receipt_unavailable"); }
		return EntryReply(request, *s.ledger->Lookup(identity));
	}
	if (type == "action.estimate") {
		Json actor = ReadActor(request);
		if (!Bound(actor)) return Reply(request, "rejected", {}, "session_unbound");
		/* No Reserve call exists on this path to enforce the recovery fence. */
		if (s.ledger->RecoveryRequired()) return Reply(request, "rejected", {}, "world_recovering");
		if (s.checkpoint) return Reply(request, "rejected", {}, "checkpoint_busy");
		const Json &action = request.at("action");
		if (!action.is_object() || action.size() != 2 || !action.contains("name") || !action.at("name").is_string() ||
				!action.contains("parameters") || !action.at("parameters").is_object() ||
				(request.contains("preconditions") && (!request.at("preconditions").is_object() || !request.at("preconditions").empty())) ||
				request.contains("request_id") || request.contains("request_sequence") || request.contains("visual_intent")) return Reply(request, "rejected", {}, "invalid_parameters");
		if (action.dump().size() > 60000) return Reply(request, "rejected", {}, "request_too_large");
		AutoRestoreBackup current(_current_company, CompanyID(actor.at("company_id").get<uint8_t>()));
		Json result = EstimateAction(action.at("name").get<std::string>(), action.at("parameters"));
		return result.contains("estimate") ? Reply(request, "completed", std::move(result.at("estimate"))) :
			Reply(request, "rejected", {}, result.at("error").at("code").get<std::string>().c_str());
	}
	if (type == "control.reference") {
		Json actor = ReadActor(request);
		if (!Bound(actor)) return Reply(request, "rejected", {}, "session_unbound");
		AutoRestoreBackup current(_current_company, CompanyID(actor.at("company_id").get<uint8_t>()));
		try { return Reply(request, "completed", ResolveControlReference(request.at("parameters"))); }
		catch (const std::exception &) { return Reply(request, "rejected", {}, "control_reference_unavailable"); }
	}
	if (type == "observation.query") {
		Json actor = ReadActor(request);
		if (!Bound(actor) || actor.at("mode") != "text") return Reply(request, "rejected", {}, "mode_restricted");
		AutoRestoreBackup current(_current_company, CompanyID(actor.at("company_id").get<uint8_t>()));
		Json query = request.at("query");
		std::string name = query.is_string() ? query.get<std::string>() : query.at("name").get<std::string>();
		Json parameters = query.is_object() ? query.value("parameters", Json::object()) : request.value("parameters", Json::object());
		Json result = QueryState(name, parameters);
		return result.contains("error") ? Reply(request, "rejected", {}, "query_rejected") : Reply(request, "completed", std::move(result));
	}
	if (type == "action.submit" || type == "action.status") {
		bool chat_status = type == "action.status" && request.value("request_type", std::string()) == "chat.publish";
		Json actor = chat_status ? ServiceActor() : ReadActor(request);
		if (!chat_status && !Bound(actor)) return Reply(request, "rejected", {}, "session_unbound");
		if (type == "action.submit" && actor.at("mode") != "visual" && request.at("action").value("name", std::string()) == "ui.apply") return Reply(request, "rejected", {}, "mode_restricted");
		auto identity = Identity(request, actor);
		if (type == "action.status") {
			auto entry = s.ledger->Lookup(identity);
			if (!entry) return Reply(request, identity.request_sequence <= s.ledger->RetiredThrough() ? "retired" : "uncertain");
			Json canonical = Json::parse(entry->canonical_payload);
			if (entry->binding != Binding(actor) || canonical.at("actor") != actor ||
					(request.contains("action") && canonical.at("action") != request.at("action")) ||
					(request.contains("visual_intent") && canonical.at("visual_intent") != request.at("visual_intent")) ||
					(request.contains("message") && canonical.at("message") != request.at("message"))) return Reply(request, "rejected", {}, "request_conflict");
			return EntryReply(request, *entry);
		}
		if (s.checkpoint) return Reply(request, "rejected", {}, "checkpoint_busy");
		auto admission = s.ledger->Reserve(identity, Binding(actor), Canonical(request, actor, "action.submit"), s.ledger->Fence());
		if (admission.status != AdmissionStatus::Admitted) return AdmissionReply(request, admission);
		auto [it, inserted] = s.pending.emplace(identity.request_sequence, PendingAction{identity, actor, request.at("action"), Json::array(), std::nullopt, 0});
		auto &pending = it->second;
		if (_settings_client.network.frame_freq != 0) {
			Reject(pending, "frame_frequency_unsupported"); s.pending.erase(it); return EntryReply(request, *s.ledger->Lookup(identity));
		}
		ActionResult dispatched;
		try {
			AutoRestoreBackup current(_current_company, CompanyID(actor.at("company_id").get<uint8_t>()));
			if (UsesImageInput(pending)) {
				const auto &parameters = pending.action.at("parameters");
				if (!ValidVisualParameters(parameters) || request.at("visual_intent").at("image_id") != parameters.at("image_id")) {
					Reject(pending, "visual_action_unavailable"); s.pending.erase(it); return EntryReply(request, *s.ledger->Lookup(identity));
				}
				VisualCommand prepared;
				dispatched = request.at("visual_intent").at("world_epoch") == s.epoch ?
					PrepareVisualCommand(request.at("visual_intent"), actor, identity.request_id, prepared) : ActionResult{false, {}, "visual_context_stale"};
				if (dispatched.accepted && parameters.at("gesture") == "control" && !prepared.ui_only) {
					dispatched = {false, {}, "visual_control_posted_command"};
				}
				if (!dispatched.accepted) {
					Debug(Facility::Misc, Severity::Notice, "[integration] visual preparation rejected: {}", dispatched.code);
					Reject(pending, "visual_action_unavailable"); s.pending.erase(it); return EntryReply(request, *s.ledger->Lookup(identity));
				}
				if (prepared.ui_only) {
					if (!s.ledger->MarkExecuted(identity, TimerGameTick::counter, _frame_counter, {{"success", true}, {"ui_only", true}})) s.world_replaced = true;
					s.pending.erase(it);
					return EntryReply(request, *s.ledger->Lookup(identity));
				}
				pending.visual = std::move(prepared);
				const auto &intent = *pending.visual;
				dispatched.accepted = QueueVisualNativeCommand(intent.command, intent.error, intent.callback, intent.company, intent.data, intent.owner, identity.request_sequence);
				if (!dispatched.accepted || pending.queued != 1) { Reject(pending, "visual_action_unavailable"); if (pending.queued == 0) s.pending.erase(it); }
				return EntryReply(request, *s.ledger->Lookup(identity));
			}
			pending.captured_targets = CaptureActionTargets(pending.action.at("name").get<std::string>(), pending.action.value("parameters", Json::object()));
			if (!pending.captured_targets.is_array()) {
				Reject(pending, "invalid_targets"); s.pending.erase(it); return EntryReply(request, *s.ledger->Lookup(identity));
			}
			s.submitting = identity.request_sequence;
			dispatched = DispatchAction(pending.action.at("name").get<std::string>(), pending.action.value("parameters", Json::object()));
			s.submitting = 0;
		} catch (const std::exception &) { s.submitting = 0; dispatched = {false, {}, "invalid_action"}; }
		if (!dispatched.accepted || pending.queued != 1) {
			const Json *native_failure = !dispatched.accepted && pending.queued == 0 && dispatched.prequeue_result ?
				&*dispatched.prequeue_result : nullptr;
			Reject(pending, dispatched.code.empty() ? "action_not_queued" : dispatched.code.c_str(), native_failure);
			if (pending.queued == 0) s.pending.erase(it);
		}
		return EntryReply(request, *s.ledger->Lookup(identity));
	}
	return Reply(request, "rejected", {}, "unsupported_method");
}

} // namespace

std::string_view InstanceId() { return GetState().instance; }

Json CurrentPauseStatus() { return ObservePauseStatus(); }

void PauseChanged() { if (TracksPlayerPresentations()) ObservePauseStatus(); }

bool Enabled() { return GetState().configured && !RendererIsActive() && _networking && _network_server; }

bool TracksPlayerPresentations()
{
	if (RendererIsActive()) return true;
	const char *socket = std::getenv("OPENTTD_INTEGRATION_SOCKET");
	return _networking && _network_server && socket != nullptr && *socket != '\0';
}

uint64_t SubmissionToken() { return GetState().submitting; }

void CommandQueued(uint64_t token)
{
	auto &s = GetState();
	auto found = s.pending.find(token);
	if (found == s.pending.end()) return;
	++found->second.queued;
	if (found->second.queued == 1 && s.ledger) s.ledger->MarkQueued(found->second.identity);
}

void CommandPostTestFailed(Commands command, const CommandCost &cost)
{
	auto &s = GetState();
	if (!cost.Failed() || s.submitting == 0 || !AuthoritativeReady() || s.world_replaced) return;
	auto found = s.pending.find(s.submitting);
	if (found == s.pending.end() || UsesImageInput(found->second) ||
			found->second.queued != 0 || found->second.prequeue_failure ||
			!Bound(found->second.actor)) return;
	try {
		const auto &action = found->second.action.at("name").get_ref<const std::string &>();
		if (!IsNativeCommandForAction(action, command)) return;
		Json outcome = FormatCommandResult(command, cost, {});
		/* This test cost was never charged: only later authoritative execution can charge. */
		outcome["cost"] = 0;
		const auto &message = outcome.at("error").at("message").get_ref<const std::string &>();
		if (message.size() > 1024 || outcome.dump().size() > 4096) return;
		found->second.prequeue_failure = std::move(outcome);
	} catch (const std::exception &) {
		/* Malformed/unprojectable game feedback retains the generic rejection. */
	}
}

std::optional<Json> TakeSubmittedPostFailure()
{
	auto &s = GetState();
	if (s.submitting == 0) return std::nullopt;
	auto found = s.pending.find(s.submitting);
	if (found == s.pending.end() || UsesImageInput(found->second) ||
			found->second.queued != 0) return std::nullopt;
	auto failure = std::move(found->second.prequeue_failure);
	found->second.prequeue_failure.reset();
	return failure;
}

bool ValidateQueuedCommand(uint64_t token, Commands command)
{
	auto &s = GetState();
	auto found = s.pending.find(token);
	if (found == s.pending.end()) return false;
	auto entry = s.ledger->Lookup(found->second.identity);
	if (!entry || entry->state != RequestState::Queued) { s.pending.erase(found); return false; }
	if (!AuthoritativeReady() || !Bound(found->second.actor) || _settings_client.network.frame_freq != 0 || s.world_replaced) {
		Reject(found->second, "stale_context"); s.pending.erase(found); return false;
	}
	if (_pause_mode.Any() && !IsCommandAllowedWhilePaused(command)) { Reject(found->second, "world_paused"); s.pending.erase(found); return false; }
	AutoRestoreBackup current(_current_company, CompanyID(found->second.actor.at("company_id").get<uint8_t>()));
	const auto &action = found->second.action;
	auto result = found->second.visual ? ValidateVisualCommand(*found->second.visual) :
		ValidateActionTargets(action.at("name").get<std::string>(), action.value("parameters", Json::object()), found->second.captured_targets);
	if (!result.accepted) {
		if (found->second.visual) Debug(Facility::Misc, Severity::Notice, "[integration] visual publication rejected: {}", result.code);
		Reject(found->second, result.code.empty() ? "stale_target" : result.code.c_str()); s.pending.erase(found); return false;
	}
	return true;
}

void BeginCommand(uint64_t token) { GetState().executing = token; }
void EndCommand() { GetState().executing = 0; }

std::optional<uint64_t> ExecutingQuestionGeneration(CompanyID company, uint16_t question_id)
{
	auto &s = GetState();
	auto found = s.pending.find(s.executing);
	if (s.executing == 0 || found == s.pending.end()) return std::nullopt;
	const auto &pending = found->second;
	if (UsesImageInput(pending) ||
			pending.actor.at("company_id") != company.base() || pending.action.at("name") != "goal.question.answer") return std::nullopt;
	for (const auto &target : pending.captured_targets) {
		if (target.value("kind", std::string()) != "story_family") continue;
		const auto &handle = target.at("state").at("question_id");
		if (handle.at("id") == question_id && handle.at("engine_instance_id") == s.instance) return handle.at("generation").get<uint64_t>();
	}
	/* This is an exact typed answer, but its validated selection is unavailable.
	 * Zero cannot be an issuance: keep the consumer from retiring another row. */
	return uint64_t{0};
}

void CommandCompleted(Commands command, const CommandCost &cost, const CommandDataBuffer &returned)
{
	auto &s = GetState();
	if (s.executing == 0) return;
	auto found = s.pending.find(s.executing);
	if (found == s.pending.end()) return;
	Json result;
	try { result = UsesImageInput(found->second) ? Json{{"success", cost.Succeeded()}} : FormatCommandResult(command, cost, returned, found->second.action); }
	catch (const std::exception &) { result = {{"status", cost.Succeeded() ? "completed" : "rejected"}}; }
	bool recorded = cost.Succeeded() ? s.ledger->MarkExecuted(found->second.identity, TimerGameTick::counter, _frame_counter, result) :
		s.ledger->MarkRejected(found->second.identity, TimerGameTick::counter, _frame_counter, result);
	if (!recorded) {
		s.world_replaced = true;
		PublishEvent({{"type", "action.status"}, {"actor", found->second.actor}, {"request_id", found->second.identity.request_id},
			{"request_sequence", found->second.identity.request_sequence}, {"status", "uncertain"}});
		s.pending.erase(found);
		return;
	}
	PublishEvent({{"type", "action.status"}, {"actor", found->second.actor}, {"request_id", found->second.identity.request_id},
		{"request_sequence", found->second.identity.request_sequence}, {"status", cost.Succeeded() ? "completed" : "rejected"}, {"result", result}});
	s.pending.erase(found);
}

void PublishEvent(const Json &event)
{
	auto &s = GetState();
	if (!s.configured || RendererIsActive() || !event.is_object()) return;
	std::string type = event.value("type", std::string());
	if (!type.starts_with("action.") && !type.starts_with("checkpoint.") && !type.starts_with("world.")) return;
	if (event.dump().size() > 48000 || s.next_event == UINT64_MAX) return;
	Json copy = event;
	copy["event_id"] = s.next_event++;
	copy["tick"] = TimerGameTick::counter;
	copy["engine_instance_id"] = s.instance;
	s.events.push_back(std::move(copy));
	if (s.events.size() > MAX_EVENTS) s.events.pop_front();
}

void WorldLoaded(std::string_view filename)
{
	loaded_path = filename;
	loaded_tick = TimerGameTick::counter;
	auto &s = GetState();
	if (s.load_reconciled) s.world_replaced = true;
}

void SaveFinished(bool success)
{
	auto &s = GetState();
	if (!s.checkpoint) return;
	auto ticket = *s.checkpoint;
	if (success && s.ledger->CompleteCheckpoint(ticket, s.checkpoint_path)) {
		s.checkpoint_status = "completed";
		PublishEvent({{"type", "checkpoint.complete"}, {"data", CheckpointInfo()}});
	} else {
		s.ledger->CancelCheckpoint(ticket);
		s.checkpoint_status = "failed";
		PublishEvent({{"type", "checkpoint.failed"}});
	}
	s.checkpoint.reset();
}

void Tick()
{
	Initialize();
	auto &s = GetState();
	if (!s.configured) return;
	ObservePauseStatus();
	ReconcileLoaded();
	if (s.company_creation && Company::IsValidID(CompanyID(*s.company_creation))) s.company_creation.reset();
#ifndef _WIN32
	auto deadline = Clock::now() + std::chrono::milliseconds(2);
	size_t bytes = 0, requests = 0, queries = 0;
	while (s.clients.size() < MAX_CLIENTS && Clock::now() < deadline) {
		int fd = accept4(s.listener, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
		if (fd < 0) break;
		struct ucred credential{};
		socklen_t length = sizeof(credential);
		if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credential, &length) != 0 || credential.uid != geteuid()) { close(fd); continue; }
		Client client;
		client.fd = fd;
		s.clients.push_back(std::move(client));
	}
	for (auto &client : s.clients) {
		if (Clock::now() >= deadline || bytes >= 128 * 1024 || requests >= 32) break;
		if (Clock::now() - client.opened > std::chrono::seconds(5)) { close(client.fd); client.fd = -1; continue; }
		if (client.output.empty()) {
			std::array<char, 4096> buffer;
			while (client.input.find('\n') == std::string::npos && bytes < 128 * 1024 && client.input.size() <= MAX_FRAME && Clock::now() < deadline) {
				ssize_t count = recv(client.fd, buffer.data(), std::min(buffer.size(), 128 * 1024 - bytes), 0);
				if (count == 0) { close(client.fd); client.fd = -1; break; }
				if (count < 0) { if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) { close(client.fd); client.fd = -1; } break; }
				bytes += count; client.input.append(buffer.data(), count);
				if (client.input.find('\n') != std::string::npos) break;
			}
			if (client.fd < 0) continue;
			if (client.input.size() > MAX_FRAME) { close(client.fd); client.fd = -1; continue; }
			auto newline = client.input.find('\n');
			if (newline == std::string::npos) continue;
			Json request = Json::object(), reply;
			try {
				if (newline + 1 != client.input.size()) throw std::invalid_argument("one frame required");
				unsigned nesting = 0;
				bool quoted = false, escaped = false;
				for (char c : client.input) {
					if (quoted) {
						if (escaped) escaped = false;
						else if (c == '\\') escaped = true;
						else if (c == '"') quoted = false;
					} else if (c == '"') quoted = true;
					else if (c == '{' || c == '[') { if (++nesting > 64) throw std::invalid_argument("nesting limit"); }
					else if (c == '}' || c == ']') { if (nesting == 0) throw std::invalid_argument("unbalanced frame"); --nesting; }
				}
				request = Json::parse(client.input.begin(), client.input.begin() + newline, nullptr, true, false);
				if (request.value("type", std::string()) == "observation.query" && queries++ >= 2) continue;
				reply = Handle(request);
			} catch (const std::exception &) {
				if (!request.is_object() || !request.value("type", Json()).is_string() || !request.value("correlation_id", Json()).is_string()) request = Json::object();
				reply = Reply(request, "rejected", {}, "invalid_request");
			}
			++requests;
			try {
				client.output = reply.dump();
				if (client.output.size() >= MAX_FRAME) client.output = Reply(request, "rejected", {}, "response_limit").dump();
				client.output.push_back('\n');
			} catch (const std::exception &) { close(client.fd); client.fd = -1; continue; }
		}
		while (client.sent < client.output.size() && bytes < 128 * 1024 && Clock::now() < deadline) {
			ssize_t count = send(client.fd, client.output.data() + client.sent, std::min(client.output.size() - client.sent, 128 * 1024 - bytes), MSG_NOSIGNAL);
			if (count < 0) { if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) { close(client.fd); client.fd = -1; } break; }
			if (count == 0) break;
			client.sent += count;
			bytes += count;
		}
		if (client.fd >= 0 && client.sent == client.output.size()) { close(client.fd); client.fd = -1; }
	}
	std::erase_if(s.clients, [](const Client &client) { return client.fd < 0; });
#endif
}

} // namespace Integration
