/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file ledger.cpp Live deduplication with save-bound durable checkpoint receipts. */

#include "../stdafx.h"
#include "ledger.h"
#include "../3rdparty/fmt/format.h"
#include <filesystem>

#ifndef _WIN32
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

#include "../safeguards.h"

namespace Integration {

using Json = nlohmann::json;

namespace {

constexpr uint64_t CHECKSUM_OFFSET = 14695981039346656037ULL;

uint64_t ChecksumBytes(std::string_view bytes, uint64_t checksum = CHECKSUM_OFFSET)
{
	for (unsigned char byte : bytes) checksum = (checksum ^ byte) * 1099511628211ULL;
	return checksum;
}

bool ValidIdentifier(const std::string &value)
{
	if (value.empty() || value.size() > 256 || !std::all_of(value.begin(), value.end(), [](unsigned char c) { return c >= 32 && c != 127; })) return false;
	try { Json(value).dump(); } catch (const std::exception &) { return false; }
	return true;
}

bool Terminal(RequestState state)
{
	return state == RequestState::Executed || state == RequestState::Rejected || state == RequestState::RolledBack;
}

constexpr size_t MANIFEST_OVERHEAD = 32768; ///< Includes fully escaped generation path, identities and envelope.

const char *StateName(RequestState state)
{
	switch (state) {
		case RequestState::Reserved: return "reserved";
		case RequestState::Queued: return "queued";
		case RequestState::Executed: return "executed";
		case RequestState::Rejected: return "rejected";
		case RequestState::Uncertain: return "uncertain";
		case RequestState::RolledBack: return "rolled_back";
	}
	throw std::runtime_error("Invalid request state");
}

RequestState ParseState(const std::string &state)
{
	if (state == "executed") return RequestState::Executed;
	if (state == "rejected") return RequestState::Rejected;
	if (state == "uncertain") return RequestState::Uncertain;
	if (state == "rolled_back") return RequestState::RolledBack;
	throw std::runtime_error("Invalid checkpoint request state");
}

Json EncodeEntry(const LedgerEntry &entry)
{
	return {{"request_sequence", entry.identity.request_sequence}, {"actor_id", entry.identity.actor_id}, {"request_id", entry.identity.request_id},
			{"company", entry.binding.company}, {"mode", entry.binding.mode}, {"payload", entry.canonical_payload},
			{"state", StateName(entry.state)}, {"outcome", entry.outcome}, {"executed_tick", entry.executed_tick},
			{"executed_frame", entry.executed_frame}, {"executed_boot_id", entry.executed_boot_id}, {"checkpoint_committed", entry.checkpoint_committed}};
}

uint64_t ReadUnsigned(const Json &json, const char *key)
{
	const auto &value = json.at(key);
	if (!value.is_number_unsigned()) throw std::runtime_error("Invalid unsigned checkpoint field");
	return value.get<uint64_t>();
}

Json EncodeCheckpoint(const CheckpointMetadata &checkpoint)
{
	Json parent = nullptr;
	if (checkpoint.parent_checkpoint) {
		const auto &previous = *checkpoint.parent_checkpoint;
		parent = {{"checkpoint_id", previous.checkpoint_id}, {"save_checksum", previous.save_checksum}, {"saved_tick", previous.saved_tick}};
	}
	return {{"checkpoint_id", checkpoint.checkpoint_id}, {"saved_tick", checkpoint.saved_tick},
			{"saved_frame", checkpoint.saved_frame}, {"retired_through", checkpoint.retired_through},
			{"save_path", checkpoint.save_path}, {"save_checksum", checkpoint.save_checksum}, {"saved_boot_id", checkpoint.saved_boot_id},
			{"parent_checkpoint", std::move(parent)}};
}

CheckpointMetadata DecodeCheckpoint(const Json &json, uint64_t version)
{
	if (!json.is_object() || json.size() != (version == 2 ? 7 : 8)) throw std::runtime_error("Invalid checkpoint metadata");
	CheckpointMetadata checkpoint{json.at("checkpoint_id").get<std::string>(), ReadUnsigned(json, "saved_tick"),
			ReadUnsigned(json, "saved_frame"), ReadUnsigned(json, "retired_through"),
			json.at("save_path").get<std::string>(), json.at("save_checksum").get<std::string>(), json.at("saved_boot_id").get<std::string>(), std::nullopt};
	if (version == 3 && !json.at("parent_checkpoint").is_null()) {
		const auto &parent = json.at("parent_checkpoint");
		if (!parent.is_object() || parent.size() != 3) throw std::runtime_error("Invalid checkpoint parent identity");
		checkpoint.parent_checkpoint = CheckpointParent{parent.at("checkpoint_id").get<std::string>(),
				parent.at("save_checksum").get<std::string>(), ReadUnsigned(parent, "saved_tick")};
		const auto &previous = *checkpoint.parent_checkpoint;
		if (previous.checkpoint_id.empty() || previous.checkpoint_id.size() > 512 ||
				std::ranges::any_of(previous.checkpoint_id, [](unsigned char c) { return c < 32 || c == 127; }) ||
				!ValidIdentifier(previous.save_checksum) || previous.save_checksum.size() > 128 ||
				previous.checkpoint_id == checkpoint.checkpoint_id || previous.saved_tick > checkpoint.saved_tick) {
			throw std::runtime_error("Inconsistent checkpoint parent identity");
		}
	}
	return checkpoint;
}

#ifndef _WIN32
/** Stream saves without allocating their size; optionally make the completed generation durable. */
std::optional<std::string> VerifySave(const std::filesystem::path &path, bool sync)
{
	int fd = open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
	if (fd < 0) return std::nullopt;
	struct stat before, after;
	bool ok = fstat(fd, &before) == 0 && S_ISREG(before.st_mode) && before.st_size > 0;
	uint64_t checksum = CHECKSUM_OFFSET;
	std::array<char, 65536> buffer;
	while (ok) {
		ssize_t count = read(fd, buffer.data(), buffer.size());
		if (count < 0 && errno == EINTR) continue;
		if (count < 0) { ok = false; break; }
		if (count == 0) break;
		checksum = ChecksumBytes(std::string_view(buffer.data(), static_cast<size_t>(count)), checksum);
	}
	if (ok) ok = fstat(fd, &after) == 0 && before.st_size == after.st_size && before.st_mtime == after.st_mtime;
	if (ok && sync) ok = fsync(fd) == 0;
	close(fd);
	if (!ok) return std::nullopt;
	if (sync) {
		int parent_fd = open(path.parent_path().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		if (parent_fd < 0) return std::nullopt;
		ok = fsync(parent_fd) == 0;
		close(parent_fd);
		if (!ok) return std::nullopt;
	}
	return fmt::format("{}", checksum);
}
#endif

} // namespace

struct RequestLedger::Impl {
	struct Data {
		std::map<uint64_t, LedgerEntry> entries;
		uint64_t retired_through = 0;
		size_t reserved_bytes = 0;
	};
	struct Pending {
		CheckpointTicket ticket;
		Data snapshot;
	};

	LedgerConfig config;
	LedgerFence fence;
	Data data;
	CheckpointMetadata checkpoint;
	std::optional<Pending> pending;
	uint64_t generation = 0;
	std::string error;
	bool healthy = false;
	bool recovery_required = true;
	bool save_reconciled = false;
	bool opened = false;
	int directory_fd = -1;
	int lock_fd = -1;

	Impl(LedgerConfig config, LedgerFence fence) : config(std::move(config)), fence(std::move(fence)) {}
	~Impl()
	{
#ifndef _WIN32
		if (this->lock_fd >= 0) close(this->lock_fd);
		if (this->directory_fd >= 0) close(this->directory_fd);
#endif
	}

	bool Fail(std::string message)
	{
		this->healthy = false;
		this->recovery_required = true;
		this->error = std::move(message);
		return false;
	}

	bool Valid(const RequestIdentity &identity, const RequestBinding &binding) const
	{
		return identity.request_sequence != 0 && ValidIdentifier(identity.actor_id) && ValidIdentifier(identity.request_id) &&
				(binding.mode == "text" || binding.mode == "visual");
	}

	LedgerEntry *Find(const RequestIdentity &identity)
	{
		auto found = this->data.entries.find(identity.request_sequence);
		return found != this->data.entries.end() && found->second.identity == identity ? &found->second : nullptr;
	}

	/** Keep a state-independent reservation until checkpointed, so crash recovery always fits. */
	size_t EntryCharge(const LedgerEntry &entry) const
	{
		auto encoded = EncodeEntry(entry);
		if (Terminal(entry.state) && entry.checkpoint_committed) return encoded.dump().size() + 8;
		encoded["state"] = "rolled_back";
		encoded["outcome"] = nullptr;
		encoded["executed_tick"] = UINT64_MAX;
		encoded["executed_frame"] = UINT64_MAX;
		encoded["executed_boot_id"] = std::string(256, '\\');
		encoded["checkpoint_committed"] = false;
		return encoded.dump().size() + this->config.max_outcome_bytes + 8;
	}

	LedgerAdmission Admit(const RequestIdentity &identity, const RequestBinding &binding, const Json &payload, bool recovering)
	{
		if (!this->healthy) return {AdmissionStatus::Unavailable, {}, this->error};
		if (!this->Valid(identity, binding)) return {AdmissionStatus::Invalid, {}, "Invalid request identity or mode"};
		std::string canonical;
		try { canonical = payload.dump(); } catch (const std::exception &) { return {AdmissionStatus::Invalid, {}, "Invalid JSON payload"}; }
		if (canonical.size() > this->config.max_payload_bytes) return {AdmissionStatus::Invalid, {}, "Payload exceeds bound"};
		if (identity.request_sequence <= this->data.retired_through) return {AdmissionStatus::Retired, {}, "Request belongs to retired prefix; consult backend history"};
		auto found = this->data.entries.find(identity.request_sequence);
		if (found != this->data.entries.end()) {
			if (found->second.identity != identity || found->second.binding != binding || found->second.canonical_payload != canonical) {
				return {AdmissionStatus::Conflict, {}, "Sequence, identity, company, mode or payload conflict"};
			}
			return {AdmissionStatus::Duplicate, found->second, {}};
		}
		for (const auto &[sequence, entry] : this->data.entries) {
			if (entry.identity.actor_id == identity.actor_id && entry.identity.request_id == identity.request_id) {
				return {AdmissionStatus::Conflict, {}, "Public request identity already has a sequence"};
			}
		}
		if (this->recovery_required && !recovering) return {AdmissionStatus::Recovering, {}, "Backend recovery is incomplete"};
		if (this->data.entries.size() >= this->config.max_requests) return {AdmissionStatus::Full, {}, "Unretired request window is full"};
		LedgerEntry entry;
		entry.identity = identity;
		entry.binding = binding;
		entry.canonical_payload = std::move(canonical);
		entry.state = recovering ? RequestState::Uncertain : RequestState::Reserved;
		size_t charge = this->EntryCharge(entry);
		if (this->config.max_manifest_bytes < MANIFEST_OVERHEAD || charge > this->config.max_manifest_bytes - MANIFEST_OVERHEAD ||
				this->data.reserved_bytes > this->config.max_manifest_bytes - MANIFEST_OVERHEAD - charge) {
			return {AdmissionStatus::Full, {}, "Checkpoint receipt byte budget is full"};
		}
		this->data.reserved_bytes += charge;
		this->data.entries.emplace(identity.request_sequence, entry);
		return {AdmissionStatus::Admitted, std::move(entry), {}};
	}

	bool Record(const RequestIdentity &identity, uint64_t tick, uint64_t frame, const Json &outcome, RequestState state)
	{
		if (!this->healthy || this->recovery_required) return false;
		auto *entry = this->Find(identity);
		if (entry == nullptr) return false;
		if (entry->state == state) return entry->executed_tick == tick && entry->executed_frame == frame && entry->outcome == outcome;
		if (entry->state != RequestState::Reserved && entry->state != RequestState::Queued) return false;
		try {
			if (outcome.dump().size() > this->config.max_outcome_bytes) return this->Fail("Authoritative outcome exceeds bound");
		} catch (const std::exception &) { return this->Fail("Invalid authoritative outcome JSON"); }
		size_t old_charge = this->EntryCharge(*entry);
		entry->state = state;
		entry->executed_tick = tick;
		entry->executed_frame = frame;
		entry->executed_boot_id = this->fence.boot_id;
		entry->outcome = outcome;
		this->data.reserved_bytes = this->data.reserved_bytes - old_charge + this->EntryCharge(*entry);
		return true;
	}

	Json Encode(const Data &data, const CheckpointMetadata &checkpoint) const
	{
		Json entries = Json::array();
		for (const auto &[sequence, entry] : data.entries) {
			entries.push_back(EncodeEntry(entry));
		}
		return {{"version", 3U}, {"world_id", this->fence.world_id}, {"world_epoch", this->fence.world_epoch},
				{"generation", this->generation}, {"checkpoint", EncodeCheckpoint(checkpoint)}, {"requests", std::move(entries)}};
	}

	void Decode(const Json &json)
	{
		if (!json.is_object() || json.size() != 6 || (ReadUnsigned(json, "version") != 2 && ReadUnsigned(json, "version") != 3) ||
				json.at("world_id").get<std::string>() != this->fence.world_id || json.at("world_epoch").get<std::string>() != this->fence.world_epoch) {
			throw std::runtime_error("Checkpoint format, world or epoch mismatch");
		}
		this->generation = ReadUnsigned(json, "generation");
		this->checkpoint = DecodeCheckpoint(json.at("checkpoint"), ReadUnsigned(json, "version"));
		if (this->checkpoint.checkpoint_id.empty() || this->checkpoint.checkpoint_id.size() > 512 ||
				this->checkpoint.save_path.size() > 4096 || !std::filesystem::path(this->checkpoint.save_path).is_absolute() || this->checkpoint.save_checksum.empty() ||
				!ValidIdentifier(this->checkpoint.saved_boot_id)) throw std::runtime_error("Invalid save generation reference");
		this->data.retired_through = this->checkpoint.retired_through;
		const auto &entries = json.at("requests");
		if (!entries.is_array() || entries.size() > this->config.max_requests) throw std::runtime_error("Checkpoint request bound exceeded");
		for (const auto &item : entries) {
			if (!item.is_object() || item.size() != 12) throw std::runtime_error("Malformed checkpoint receipt");
			LedgerEntry entry;
			entry.identity = {ReadUnsigned(item, "request_sequence"), item.at("actor_id").get<std::string>(), item.at("request_id").get<std::string>()};
			auto company = ReadUnsigned(item, "company");
			if (company > UINT32_MAX) throw std::runtime_error("Invalid company binding");
			entry.binding = {static_cast<uint32_t>(company), item.at("mode").get<std::string>()};
			if (!this->Valid(entry.identity, entry.binding) || entry.identity.request_sequence <= this->data.retired_through) throw std::runtime_error("Invalid receipt identity");
			entry.canonical_payload = item.at("payload").get<std::string>();
			if (entry.canonical_payload.size() > this->config.max_payload_bytes || Json::parse(entry.canonical_payload).dump() != entry.canonical_payload) throw std::runtime_error("Invalid canonical payload");
			entry.state = ParseState(item.at("state").get<std::string>());
			entry.outcome = item.at("outcome");
			entry.executed_tick = ReadUnsigned(item, "executed_tick");
			entry.executed_frame = ReadUnsigned(item, "executed_frame");
			entry.executed_boot_id = item.at("executed_boot_id").get<std::string>();
			entry.checkpoint_committed = item.at("checkpoint_committed").get<bool>();
			if (entry.outcome.dump().size() > this->config.max_outcome_bytes || entry.executed_tick > this->checkpoint.saved_tick ||
					(entry.executed_boot_id == this->checkpoint.saved_boot_id && entry.executed_frame > this->checkpoint.saved_frame) ||
					(Terminal(entry.state) && !ValidIdentifier(entry.executed_boot_id)) ||
					(entry.state == RequestState::Uncertain && (!entry.executed_boot_id.empty() || entry.executed_tick != 0 || entry.executed_frame != 0 || !entry.outcome.is_null())) ||
					entry.checkpoint_committed != Terminal(entry.state)) throw std::runtime_error("Inconsistent checkpoint receipt");
			for (const auto &[sequence, prior] : this->data.entries) {
				if (prior.identity.actor_id == entry.identity.actor_id && prior.identity.request_id == entry.identity.request_id) throw std::runtime_error("Duplicate public request identity");
			}
			if (!this->data.entries.emplace(entry.identity.request_sequence, entry).second) throw std::runtime_error("Duplicate request sequence");
			size_t charge = this->EntryCharge(entry);
			if (this->config.max_manifest_bytes < MANIFEST_OVERHEAD || charge > this->config.max_manifest_bytes - MANIFEST_OVERHEAD ||
					this->data.reserved_bytes > this->config.max_manifest_bytes - MANIFEST_OVERHEAD - charge) throw std::runtime_error("Checkpoint receipt reservation budget exceeded");
			this->data.reserved_bytes += charge;
		}
	}

	bool Publish(const Data &snapshot, const CheckpointMetadata &checkpoint)
	{
#ifdef _WIN32
		return this->Fail("Durable integration checkpoints are unsupported on this platform");
#else
		std::string bytes;
		try {
			Json journal = this->Encode(snapshot, checkpoint);
			bytes = Json{{"checksum", fmt::format("{}", ChecksumBytes(journal.dump()))}, {"journal", std::move(journal)}}.dump();
		} catch (const std::exception &) { return this->Fail("Cannot encode checkpoint sidecar"); }
		if (bytes.size() > this->config.max_manifest_bytes) return this->Fail("Checkpoint sidecar exceeds size bound");
		int fd = openat(this->directory_fd, "ledger.json.tmp", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
		if (fd < 0) return this->Fail("Cannot create checkpoint sidecar temporary file");
		size_t offset = 0;
		bool ok = true;
		while (offset < bytes.size()) {
			ssize_t written = write(fd, bytes.data() + offset, bytes.size() - offset);
			if (written < 0 && errno == EINTR) continue;
			if (written <= 0) { ok = false; break; }
			offset += static_cast<size_t>(written);
		}
		if (ok) ok = fsync(fd) == 0;
		if (close(fd) != 0) ok = false;
		if (!ok || renameat(this->directory_fd, "ledger.json.tmp", this->directory_fd, "ledger.json") != 0) {
			unlinkat(this->directory_fd, "ledger.json.tmp", 0);
			return this->Fail("Cannot write or publish checkpoint sidecar");
		}
		if (fsync(this->directory_fd) != 0) return this->Fail("Checkpoint publication is uncertain: directory fsync failed");
		/* Detect a missing published sidecar on later startup rather than silently resetting. */
		if (pwrite(this->lock_fd, "C", 1, 0) != 1 || fsync(this->lock_fd) != 0) return this->Fail("Cannot persist checkpoint initialization marker");
		return true;
#endif
	}
};

RequestLedger::RequestLedger(LedgerConfig config, LedgerFence fence) : impl(std::make_unique<Impl>(std::move(config), std::move(fence))) {}
RequestLedger::~RequestLedger() = default;

bool RequestLedger::Open()
{
	auto &self = *this->impl;
	if (self.opened) return self.healthy;
	self.opened = true;
	if (!ValidIdentifier(self.fence.world_id) || !ValidIdentifier(self.fence.world_epoch) || !ValidIdentifier(self.fence.boot_id) ||
			self.config.directory.empty() || self.config.max_requests == 0 || self.config.max_payload_bytes == 0 || self.config.max_outcome_bytes == 0 ||
			self.config.max_manifest_bytes == 0 || self.config.max_payload_bytes > self.config.max_manifest_bytes ||
			self.config.max_outcome_bytes > self.config.max_manifest_bytes || self.config.max_manifest_bytes > std::numeric_limits<size_t>::max() / 4) return self.Fail("Invalid ledger configuration or fence");
#ifdef _WIN32
	return self.Fail("Durable integration checkpoints are unsupported on this platform");
#else
	std::error_code ec;
	std::filesystem::create_directories(self.config.directory, ec);
	if (ec) return self.Fail("Cannot create checkpoint directory");
	self.directory_fd = open(self.config.directory.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
	if (self.directory_fd < 0) return self.Fail("Cannot open private checkpoint directory");
	struct stat directory_stat;
	if (fstat(self.directory_fd, &directory_stat) != 0 || directory_stat.st_uid != geteuid() || fchmod(self.directory_fd, 0700) != 0) return self.Fail("Checkpoint directory is not privately owned");
	self.lock_fd = openat(self.directory_fd, "ledger.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
	if (self.lock_fd < 0 || flock(self.lock_fd, LOCK_EX | LOCK_NB) != 0) return self.Fail("Checkpoint directory is locked or unavailable");
	if (unlinkat(self.directory_fd, "ledger.json.tmp", 0) != 0 && errno != ENOENT) return self.Fail("Cannot clear interrupted checkpoint temporary file");
	int fd = openat(self.directory_fd, "ledger.json", O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
	if (fd < 0) {
		if (errno != ENOENT) return self.Fail("Cannot open checkpoint sidecar");
		char initialized;
		ssize_t marker = pread(self.lock_fd, &initialized, 1, 0);
		if (marker != 0) return self.Fail("Published checkpoint sidecar is missing");
		self.healthy = true;
		return true;
	}
	struct stat file_stat;
	if (fstat(fd, &file_stat) != 0 || !S_ISREG(file_stat.st_mode) || file_stat.st_size <= 0 ||
			static_cast<uint64_t>(file_stat.st_size) > self.config.max_manifest_bytes) { close(fd); return self.Fail("Invalid checkpoint sidecar file"); }
	std::string bytes(static_cast<size_t>(file_stat.st_size), '\0');
	size_t offset = 0;
	while (offset < bytes.size()) {
		ssize_t count = read(fd, bytes.data() + offset, bytes.size() - offset);
		if (count < 0 && errno == EINTR) continue;
		if (count <= 0) break;
		offset += static_cast<size_t>(count);
	}
	close(fd);
	if (offset != bytes.size()) return self.Fail("Cannot read entire checkpoint sidecar");
	try {
		Json envelope = Json::parse(bytes);
		if (!envelope.is_object() || envelope.size() != 2 || envelope.at("checksum").get<std::string>() !=
				fmt::format("{}", ChecksumBytes(envelope.at("journal").dump()))) throw std::runtime_error("Checkpoint checksum mismatch");
		self.Decode(envelope.at("journal"));
		auto checksum = VerifySave(self.checkpoint.save_path, false);
		if (!checksum || *checksum != self.checkpoint.save_checksum) throw std::runtime_error("Save generation is missing or corrupt");
	} catch (const std::exception &e) { return self.Fail(std::string("Invalid integration checkpoint: ") + e.what()); }
	self.healthy = true;
	return true;
#endif
}

bool RequestLedger::Healthy() const { return this->impl->healthy; }
bool RequestLedger::RecoveryRequired() const { return this->impl->recovery_required; }
const std::string &RequestLedger::LastError() const { return this->impl->error; }
const LedgerFence &RequestLedger::Fence() const { return this->impl->fence; }
const CheckpointMetadata &RequestLedger::Checkpoint() const { return this->impl->checkpoint; }
size_t RequestLedger::Size() const { return this->impl->data.entries.size(); }
uint64_t RequestLedger::RetiredThrough() const { return this->impl->data.retired_through; }

bool RequestLedger::ReconcileLoadedCheckpoint(const CheckpointMetadata &loaded)
{
	auto &self = *this->impl;
	if (!self.healthy) return false;
	if (loaded != self.checkpoint) return self.Fail("Loaded save does not match checkpoint generation");
	self.save_reconciled = true;
	return true;
}

LedgerAdmission RequestLedger::ReconcileAccepted(const RequestIdentity &identity, const RequestBinding &binding, const Json &payload)
{
	auto &self = *this->impl;
	if (!self.recovery_required || !self.save_reconciled) return {AdmissionStatus::Recovering, {}, "Not reconciling backend accepted requests"};
	return self.Admit(identity, binding, payload, true);
}

bool RequestLedger::FinishRecovery()
{
	auto &self = *this->impl;
	if (!self.healthy || !self.save_reconciled) return false;
	self.recovery_required = false;
	return true;
}

LedgerAdmission RequestLedger::Reserve(const RequestIdentity &identity, const RequestBinding &binding, const Json &payload, const LedgerFence &fence)
{
	if (fence != this->impl->fence) return {AdmissionStatus::Fenced, {}, "World, epoch or boot fence is stale"};
	return this->impl->Admit(identity, binding, payload, false);
}

std::optional<LedgerEntry> RequestLedger::Lookup(const RequestIdentity &identity) const
{
	if (!this->impl->healthy) return std::nullopt;
	auto *entry = this->impl->Find(identity);
	return entry == nullptr ? std::nullopt : std::optional<LedgerEntry>(*entry);
}

bool RequestLedger::MarkQueued(const RequestIdentity &identity)
{
	auto &self = *this->impl;
	if (!self.healthy || self.recovery_required) return false;
	auto *entry = self.Find(identity);
	if (entry == nullptr || (entry->state != RequestState::Reserved && entry->state != RequestState::Queued)) return false;
	size_t old_charge = self.EntryCharge(*entry);
	entry->state = RequestState::Queued;
	self.data.reserved_bytes = self.data.reserved_bytes - old_charge + self.EntryCharge(*entry);
	return true;
}

bool RequestLedger::MarkExecuted(const RequestIdentity &identity, uint64_t tick, uint64_t frame, const Json &outcome)
{
	return this->impl->Record(identity, tick, frame, outcome, RequestState::Executed);
}

bool RequestLedger::MarkRejected(const RequestIdentity &identity, uint64_t tick, uint64_t frame, const Json &outcome)
{
	return this->impl->Record(identity, tick, frame, outcome, RequestState::Rejected);
}

bool RequestLedger::ResolveUncertain(const RequestIdentity &identity, bool rolled_back, const Json &outcome)
{
	auto &self = *this->impl;
	if (!self.healthy || self.recovery_required) return false;
	auto *entry = self.Find(identity);
	if (entry == nullptr || entry->state != RequestState::Uncertain) return false;
	try { if (outcome.dump().size() > self.config.max_outcome_bytes) return false; } catch (const std::exception &) { return false; }
	size_t old_charge = self.EntryCharge(*entry);
	entry->state = rolled_back ? RequestState::RolledBack : RequestState::Rejected;
	entry->outcome = outcome;
	entry->executed_boot_id = self.fence.boot_id;
	entry->checkpoint_committed = false;
	self.data.reserved_bytes = self.data.reserved_bytes - old_charge + self.EntryCharge(*entry);
	return true;
}

bool RequestLedger::AckThrough(uint64_t request_sequence)
{
	auto &self = *this->impl;
	if (!self.healthy || !self.save_reconciled || request_sequence < self.data.retired_through) return false;
	auto end = self.data.entries.upper_bound(request_sequence);
	for (auto it = self.data.entries.begin(); it != end; ++it) {
		if (!Terminal(it->second.state) || !it->second.checkpoint_committed) return false;
	}
	for (auto it = self.data.entries.begin(); it != end; ++it) self.data.reserved_bytes -= self.EntryCharge(it->second);
	self.data.entries.erase(self.data.entries.begin(), end);
	self.data.retired_through = request_sequence;
	return true;
}

std::optional<CheckpointTicket> RequestLedger::BeginCheckpoint(uint64_t saved_tick, uint64_t saved_frame)
{
	auto &self = *this->impl;
	if (!self.healthy || self.recovery_required || self.pending || saved_tick < self.checkpoint.saved_tick ||
			(self.checkpoint.saved_boot_id == self.fence.boot_id && saved_frame < self.checkpoint.saved_frame)) return std::nullopt;
	for (const auto &[sequence, entry] : self.data.entries) {
		if (entry.state == RequestState::Reserved || entry.state == RequestState::Queued || entry.executed_tick > saved_tick ||
				(entry.executed_boot_id == self.fence.boot_id && entry.executed_frame > saved_frame)) return std::nullopt;
	}
	if (self.generation == UINT64_MAX) { self.Fail("Checkpoint generation exhausted"); return std::nullopt; }
	CheckpointTicket ticket{fmt::format("{}:{}", self.fence.boot_id, ++self.generation), saved_tick, saved_frame};
	self.pending = Impl::Pending{ticket, self.data};
	for (auto &[sequence, entry] : self.pending->snapshot.entries) entry.checkpoint_committed = Terminal(entry.state);
	return ticket;
}

bool RequestLedger::CompleteCheckpoint(const CheckpointTicket &ticket, const std::filesystem::path &save_path)
{
	auto &self = *this->impl;
	if (!self.healthy || self.recovery_required || !self.pending || ticket != self.pending->ticket) return false;
	if (save_path.empty()) return self.Fail("Checkpoint save path is empty");
	std::error_code ec;
	auto absolute = std::filesystem::absolute(save_path, ec).lexically_normal();
	if (ec || absolute.string().size() > 4096) return self.Fail("Cannot resolve checkpoint save path within size bound");
	if (!self.checkpoint.save_path.empty()) {
		if (absolute == std::filesystem::path(self.checkpoint.save_path) || std::filesystem::equivalent(absolute, self.checkpoint.save_path, ec)) return self.Fail("Checkpoint requires a new save generation");
	}
#ifdef _WIN32
	return self.Fail("Durable integration checkpoints are unsupported on this platform");
#else
	auto checksum = VerifySave(absolute, true);
	if (!checksum) return self.Fail("Completed checkpoint save is invalid or cannot be fsynced");
	CheckpointMetadata checkpoint{ticket.checkpoint_id, ticket.saved_tick, ticket.saved_frame,
			self.pending->snapshot.retired_through, absolute.string(), *checksum, self.fence.boot_id, std::nullopt};
	if (!self.checkpoint.checkpoint_id.empty()) {
		checkpoint.parent_checkpoint = CheckpointParent{self.checkpoint.checkpoint_id, self.checkpoint.save_checksum, self.checkpoint.saved_tick};
	}
	if (!self.Publish(self.pending->snapshot, checkpoint)) return false;
	for (const auto &[sequence, captured] : self.pending->snapshot.entries) {
		auto found = self.data.entries.find(sequence);
		if (found != self.data.entries.end() && found->second.state == captured.state && found->second.outcome == captured.outcome &&
				found->second.executed_tick == captured.executed_tick && found->second.executed_frame == captured.executed_frame &&
				found->second.executed_boot_id == captured.executed_boot_id) {
			size_t old_charge = self.EntryCharge(found->second);
			found->second.checkpoint_committed = captured.checkpoint_committed;
			self.data.reserved_bytes = self.data.reserved_bytes - old_charge + self.EntryCharge(found->second);
		}
	}
	self.checkpoint = std::move(checkpoint);
	self.pending.reset();
	return true;
#endif
}

void RequestLedger::CancelCheckpoint(const CheckpointTicket &ticket)
{
	if (this->impl->pending && this->impl->pending->ticket == ticket) this->impl->pending.reset();
}

} // namespace Integration
