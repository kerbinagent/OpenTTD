/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file actions.h Company-scoped, allowlisted integration actions and observations. */

#ifndef INTEGRATION_ACTIONS_H
#define INTEGRATION_ACTIONS_H

#include "../3rdparty/nlohmann/json.hpp"
#include "../command_type.h"
#include "../network/network_type.h"

#include <optional>

namespace Integration {

using Json = nlohmann::json;

/** Admission result. Accepted means queued, never authoritative completion. */
struct ActionResult {
	bool accepted = false;
	std::string error;
	std::string code;
	std::optional<Json> prequeue_result = std::nullopt; ///< Already-computed normal native test failure.
};

/**
 * Validate a public action and submit one normal typed gameplay command.
 * Call only on the simulation thread with a valid authenticated _current_company.
 * The bridge must correlate submission and actual execution, and enforce quotas,
 * observation mode, request deduplication and snapshot/target preconditions.
 */
ActionResult DispatchAction(const std::string &action, const Json &parameters);

/** Native Shift test feedback for one typed action; never queued or executed.
 * Returns {estimate: scalar feedback}, or {error: {code}} if no test ran.
 * Must run on the simulation thread under the authenticated company scope. */
Json EstimateAction(const std::string &action, const Json &parameters);

/** Revalidate entity handles immediately before authoritative command publication. */
ActionResult ValidateActionTargets(const std::string &action, const Json &parameters);

/**
 * Capture private target incarnations/tiles before an action is queued.
 * Returns an array, or the usual {"error": ...} on validation failure.
 * This internal lease must never be included in public responses or events.
 */
Json CaptureActionTargets(const std::string &action, const Json &parameters);

/** Validate explicit handles and a previously captured private target lease. */
ActionResult ValidateActionTargets(const std::string &action, const Json &parameters, const Json &captured_targets);

/**
 * Read a bounded snapshot under the current company on the simulation thread.
 * These are text-mode observations. The bridge must reject visual sessions
 * before calling this function and must not forward its payload to them.
 * Validation failures are returned as {"error": {"code", "message"}}.
 */
Json QueryState(const std::string &query, const Json &parameters);

/** Stable action/query names and their accepted parameter names. */
Json CapabilityMatrix();

/** Closed single-target actuator metadata schema; never a world observation. */
Json ControlReferenceSchema();

/** Resolve one explicitly selected current target, returning only handles/control tokens. */
Json ResolveControlReference(const Json &parameters);

/** Decode authoritative command acknowledgements; public consumers apply their mode projection. */
Json FormatCommandResult(Commands command, const CommandCost &cost, const CommandDataBuffer &return_data, const Json &action = Json::object());

/** Server-owned pool generation bounds captured after a renderer's source frame. */
struct TargetWatermarks {
	uint64_t vehicle = 0;
	uint64_t station = 0;
	uint64_t depot = 0;
	uint64_t company = 0;
	uint64_t group = 0;
	uint64_t town = 0;
	uint64_t sign = 0;
	uint64_t object = 0;
	uint64_t engine = 0;
	uint64_t story_page = 0;
	uint64_t story_element = 0;
	uint64_t goal = 0;
	uint64_t question = 0;
};

/** Private native capture allowlist derived from the public action registry. */
bool IsAllowedNativeCommand(Commands command);
bool IsNativeCommandForAction(const std::string &action, Commands command);

/** Inspect typed native references under the company context against frame bounds. */
ActionResult InspectNativeTargets(Commands command, const CommandDataBuffer &data, const TargetWatermarks &watermarks, ClientID owner = ClientID::Invalid);

/**
 * Return the sorted, unique construction intent footprint, bounded by the map.
 * Diagnostic compatibility helper; production capture uses the compact proof.
 * Call against the parked renderer frame or the authoritative simulation thread.
 * Throws std::runtime_error for malformed, unsupported or oversized intents.
 * Native bytes and this inspection data are private to the integration bridge.
 */
std::vector<TileIndex> NativeIntentTiles(Commands command, const CommandDataBuffer &data);

/** Private stable operation/order/consist proof; throws if its exact DTO exceeds 16 KiB. */
Json NativeIntentState(Commands command, const CommandDataBuffer &data, ClientID owner = ClientID::Invalid);

/** Private protocol v2 proof, independent of tile/part count on the wire. */
struct NativeIntentProof {
	Json tile_proof; ///< Exactly {count, digest}; fixed BLAKE2b-256.
	Json state; ///< Stable logical operation proof, at most 16 KiB.
};

/** Decode/collect native geometry once, against a parked renderer frame. */
NativeIntentProof CaptureNativeIntentProof(Commands command, const CommandDataBuffer &data, ClientID owner = ClientID::Invalid);

/** Recompute geometry/state and inspect every relevant incarnation before publication. */
ActionResult VerifyNativeIntentProof(Commands command, const CommandDataBuffer &data, const TargetWatermarks &watermarks,
	const Json &tile_proof, const Json &state, ClientID owner = ClientID::Invalid);

} // namespace Integration

#endif /* INTEGRATION_ACTIONS_H */
