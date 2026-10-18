/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 2.
 */

/** @file visual_commands.cpp Validate parked native UI intents against completed frames. */

#include "../stdafx.h"
#include "visual_commands.h"
#include "renderer.h"
#include "actions_story.h"
#include "integration_bridge.h"
#include "../base_station_base.h"
#include "../company_base.h"
#include "../depot_base.h"
#include "../engine_base.h"
#include "../group.h"
#include "../goal_base.h"
#include "../object_base.h"
#include "../signs_base.h"
#include "../story_base.h"
#include "../town.h"
#include "../debug.h"
#include "../map_func.h"
#include "../network/network.h"
#include "../network/network_base.h"
#include "../network/network_internal.h"
#include "../network/network_server.h"
#include "../settings_type.h"
#include "../timer/timer_game_tick.h"
#include "../vehicle_base.h"
#include <deque>

#include "../safeguards.h"

namespace Integration {
namespace {

using Clock = std::chrono::steady_clock;
struct FrameWatermarks {
	uint32_t frame;
	TargetWatermarks pools;
	Clock::time_point completed;
};
std::deque<FrameWatermarks> completed_frames;

ActionResult Failure(const char *code)
{
	return {false, "Visual action unavailable", code};
}

uint64_t Number(const Json &object, const char *key, uint64_t maximum)
{
	const auto &value = object.at(key);
	if (!value.is_number_unsigned() || value.get<uint64_t>() > maximum) throw std::invalid_argument("Invalid intent number");
	return value.get<uint64_t>();
}

CommandDataBuffer DecodeHex(const Json &value, size_t maximum)
{
	if (!value.is_string()) throw std::invalid_argument("Invalid intent buffer");
	const auto &text = value.get_ref<const std::string &>();
	if (text.size() % 2 != 0 || text.size() > maximum * 2) throw std::invalid_argument("Intent buffer limit");
	auto digit = [](char c) -> uint8_t {
		if (c >= '0' && c <= '9') return c - '0';
		if (c >= 'a' && c <= 'f') return c - 'a' + 10;
		if (c >= 'A' && c <= 'F') return c - 'A' + 10;
		throw std::invalid_argument("Invalid intent hex");
	};
	CommandDataBuffer bytes;
	bytes.reserve(text.size() / 2);
	for (size_t i = 0; i < text.size(); i += 2) bytes.push_back((digit(text[i]) << 4) | digit(text[i + 1]));
	return bytes;
}

const FrameWatermarks *SourceFrame(const Json &bundle)
{
	uint32_t frame = static_cast<uint32_t>(Number(bundle, "source_frame", UINT32_MAX));
	if (frame > _frame_counter) return nullptr;
	auto found = std::ranges::find_if(completed_frames, [frame](const auto &entry) { return entry.frame == frame; });
	if (found == completed_frames.end()) return nullptr;
	/* Parking cannot keep a native client at the server's lag timeout. */
	const int64_t lag_budget = static_cast<int64_t>(_settings_client.network.max_lag_time) * MILLISECONDS_PER_TICK - 2000;
	const auto ttl = std::chrono::milliseconds(std::clamp<int64_t>(lag_budget, 0, MAX_RENDERER_PARK_MS));
	const int64_t frame_budget = std::max<int64_t>(0, static_cast<int64_t>(_settings_client.network.max_lag_time) - 2000 / MILLISECONDS_PER_TICK);
	if (ttl.count() == 0 || static_cast<uint64_t>(_frame_counter - frame) > static_cast<uint64_t>(frame_budget) || Clock::now() - found->completed > ttl) return nullptr;
	return &*found;
}

bool CurrentOwner(const VisualCommand &prepared)
{
	auto rejected = [](const char *code) {
		Debug(Facility::Misc, Severity::Notice, "[integration] visual owner rejected: {}", code);
		return false;
	};
	const auto &key = prepared.bundle.at("owner").at("public_key");
	if (!key.is_string() || key.get_ref<const std::string &>().empty() || key.get_ref<const std::string &>().size() > 128) return rejected("owner_key_invalid");
	const auto *info = NetworkClientInfo::GetByClientID(prepared.owner);
	const auto *socket = NetworkClientSocket::GetByClientID(prepared.owner);
	if (info == nullptr) return rejected("owner_info_missing");
	if (socket == nullptr) return rejected("owner_socket_missing");
	if (socket->status != NetworkClientSocket::ClientStatus::Active) return rejected("owner_not_active");
	if (info->client_playas != prepared.company) return rejected("owner_company_changed");
	if (info->public_key != key.get_ref<const std::string &>()) return rejected("owner_key_changed");
	return true;
}

} // namespace

void CompletedFrame()
{
	if (!Enabled()) return;
	if (!completed_frames.empty() && completed_frames.back().frame >= _frame_counter) completed_frames.clear();
	completed_frames.push_back({_frame_counter, {Vehicle::GetLastInstanceGeneration(), BaseStation::GetLastInstanceGeneration(),
		Depot::GetLastInstanceGeneration(), Company::GetLastInstanceGeneration(), Group::GetLastInstanceGeneration(),
		Town::GetLastInstanceGeneration(), Sign::GetLastInstanceGeneration(), Object::GetLastInstanceGeneration(),
		Engine::GetLastInstanceGeneration(), StoryPage::GetLastInstanceGeneration(), StoryPageElement::GetLastInstanceGeneration(),
		Goal::GetLastInstanceGeneration(), LastStoryQuestionGeneration()}, Clock::now()});
	if (completed_frames.size() > 2048) completed_frames.pop_front();
}

ActionResult ValidateVisualCommand(const VisualCommand &prepared)
{
	try {
		const auto &bundle = prepared.bundle;
		if (!Enabled() || _settings_client.network.frame_freq != 0 || bundle.at("authority_instance_id") != InstanceId()) return Failure("visual_context_stale");
		const auto *source = SourceFrame(bundle);
		if (source == nullptr) return Failure("visual_frame_expired");
		if (!CurrentOwner(prepared)) return Failure("visual_owner_stale");
		const auto *company = Company::GetIfValid(prepared.company);
		if (company == nullptr || company->GetInstanceGeneration() > source->pools.company) return Failure("visual_context_stale");
		if (prepared.ui_only) {
			if (!bundle.contains("tile_proof") || !bundle.at("tile_proof").is_null() || bundle.contains("state")) return Failure("invalid_visual_intent");
			return {true, {}, {}};
		}
		if (!IsAllowedNativeCommand(prepared.command)) return Failure("visual_command_unavailable");
		if (!bundle.contains("state") || !bundle.contains("tile_proof")) return Failure("invalid_visual_intent");
		auto proof = VerifyNativeIntentProof(prepared.command, prepared.data, source->pools, bundle.at("tile_proof"), bundle.at("state"), prepared.owner);
		if (!proof.accepted) return Failure("visual_target_stale");
		return {true, {}, {}};
	} catch (const std::exception &) { return Failure("invalid_visual_intent"); }
}

ActionResult PrepareVisualCommand(const Json &bundle, const Json &actor, std::string_view request_id, VisualCommand &prepared)
{
	try {
		if (!bundle.is_object() || Number(bundle, "version", 2) != 2 || bundle.at("request_id") != request_id ||
			!bundle.at("image_id").is_string() || bundle.at("image_id").get_ref<const std::string &>().empty() ||
			!bundle.at("worker_instance_id").is_string() || bundle.at("worker_instance_id").get_ref<const std::string &>().empty() ||
			actor.at("mode") != "visual" || !bundle.at("world_epoch").is_string()) return Failure("invalid_visual_intent");
		prepared.bundle = bundle;
		if (bundle.dump().size() > 48000) return Failure("visual_intent_limit");
		prepared.company = CompanyID(static_cast<uint8_t>(Number(actor, "company_id", MAX_COMPANIES - 1)));
		prepared.owner = static_cast<ClientID>(Number(bundle.at("owner"), "client_id", UINT32_MAX));
		if (prepared.owner == ClientID::Invalid || prepared.owner == ClientID::Server) return Failure("visual_owner_unavailable");
		const auto &commands = bundle.at("commands");
		if (!commands.is_array() || commands.size() > 1) return Failure("visual_sequence_unavailable");
		prepared.ui_only = commands.empty();
		if (!prepared.ui_only) {
			const auto &command = commands.front();
			if (Number(command, "company_id", MAX_COMPANIES - 1) != prepared.company.base()) return Failure("visual_context_stale");
			prepared.command = static_cast<Commands>(Number(command, "command", UINT16_MAX));
			prepared.error = StringID(static_cast<uint16_t>(Number(command, "error", UINT16_MAX)));
			prepared.callback = static_cast<uint8_t>(Number(command, "callback", UINT8_MAX));
			if (!IsAllowedNativeCommand(prepared.command)) return Failure("visual_command_unavailable");
			prepared.data = DecodeHex(command.at("data"), 8192);
			auto normalized = prepared.data;
			if (!NormalizeNativeCommand(prepared.command, prepared.callback, normalized, prepared.owner) || normalized != prepared.data) return Failure("invalid_visual_intent");
		}
		return ValidateVisualCommand(prepared);
	} catch (const std::exception &) { return Failure("invalid_visual_intent"); }
}

} // namespace Integration
