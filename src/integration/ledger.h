/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file ledger.h Live request deduplication and save-bound checkpoint receipts. */

#ifndef INTEGRATION_LEDGER_H
#define INTEGRATION_LEDGER_H

#include "../3rdparty/nlohmann/json.hpp"
#include <cstdint>
#include <filesystem>
#include <memory>
#include <optional>
#include <string>

namespace Integration {

struct LedgerFence {
	std::string world_id;
	std::string world_epoch;
	std::string boot_id;
	bool operator==(const LedgerFence &) const = default;
};

/** Backend assigns a permanent monotonic per-world sequence when accepting a request. */
struct RequestIdentity {
	uint64_t request_sequence = 0;
	std::string actor_id;
	std::string request_id;
	bool operator==(const RequestIdentity &) const = default;
};

/** Authenticated bridge binding; immutable for every retry. */
struct RequestBinding {
	uint32_t company = 0;
	std::string mode; ///< "text" or "visual".
	bool operator==(const RequestBinding &) const = default;
};

enum class RequestState { Reserved, Queued, Executed, Rejected, Uncertain, RolledBack };

struct LedgerEntry {
	RequestIdentity identity;
	RequestBinding binding;
	std::string canonical_payload;
	RequestState state = RequestState::Reserved;
	nlohmann::json outcome = nullptr;
	uint64_t executed_tick = 0;
	uint64_t executed_frame = 0;
	std::string executed_boot_id; ///< Native network frames are comparable only within one engine boot.
	bool checkpoint_committed = false;
};

enum class AdmissionStatus { Admitted, Duplicate, Conflict, Retired, Fenced, Recovering, Full, Invalid, Unavailable };

struct LedgerAdmission {
	AdmissionStatus status = AdmissionStatus::Unavailable;
	std::optional<LedgerEntry> entry;
	std::string error;
};

/** Direct published predecessor identity, with no recursive ancestry or live counters. */
struct CheckpointParent {
	std::string checkpoint_id;
	std::string save_checksum;
	uint64_t saved_tick = 0;
	bool operator==(const CheckpointParent &) const = default;
};

struct CheckpointMetadata {
	std::string checkpoint_id;
	uint64_t saved_tick = 0;
	uint64_t saved_frame = 0;
	uint64_t retired_through = 0;
	std::string save_path;
	std::string save_checksum; ///< Private local corruption detection, not authentication.
	std::string saved_boot_id; ///< Qualifies saved_frame; game ticks remain comparable across boots.
	std::optional<CheckpointParent> parent_checkpoint; ///< Null for a first generation or a legacy v2 sidecar.
	bool operator==(const CheckpointMetadata &) const = default;
};

struct CheckpointTicket {
	std::string checkpoint_id;
	uint64_t saved_tick = 0;
	uint64_t saved_frame = 0;
	bool operator==(const CheckpointTicket &) const = default;
};

struct LedgerConfig {
	std::filesystem::path directory; ///< Dedicated private checkpoint directory.
	size_t max_requests = 8192; ///< Unretired window, never a lifetime limit.
	size_t max_payload_bytes = 65536;
	size_t max_outcome_bytes = 65536;
	size_t max_manifest_bytes = 32 * 1024 * 1024;
};

/**
 * Single simulation-thread live ledger. Reserve/completion/acknowledgement do NO disk IO.
 * Backend owns permanent public request identity, accepted-key durability and history.
 * Request sequences identify retries; they never imply execution order.
 *
 * Open verifies the latest save-bound sidecar and fences admissions. The engine loads the
 * exact save named by Checkpoint(), then calls ReconcileLoadedCheckpoint. Backend recovery
 * calls ReconcileAccepted for its accepted identities; absent keys become Uncertain and
 * never queue or replay. FinishRecovery opens fresh admissions. ResolveUncertain records
 * explicit rejection or rollback, never fabricated completion.
 *
 * AckThrough asserts every accepted sequence in that prefix has a durable terminal backend
 * record. Retirement additionally requires an engine terminal receipt included in a durable
 * checkpoint. Old sequence retries return Retired. Missing sequences may be skipped because
 * they had no engine effect. Backend must never acknowledge a prefix containing pending
 * outbox actions; a late request in a retired prefix is always refused.
 *
 * BeginCheckpoint requires no pending bridge commands. Hold bridge admission while copying
 * the game snapshot and calling BeginCheckpoint; ordinary simulation need not remain paused
 * while saving. Captured receipts are independent of subsequent live mutations. Save to a
 * NEW generation, then CompleteCheckpoint fsyncs save+parent and atomically publishes the
 * sidecar pointer. Keep the previous generation until publication. Only checkpointing does
 * disk IO. Unix durability is supported; other platforms fail closed at Open().
 * Native frame numbers retain their original boot provenance and are compared only within
 * that boot. Saved game ticks, world/epoch fences and exact save identity span restarts.
 * New sidecars retain only their direct published predecessor identity. The backend may use
 * that evidence to reconcile a save published before its checkpoint acknowledgement; it
 * must independently verify the loaded generation, counters and authoritative receipts.
 */
class RequestLedger {
public:
	RequestLedger(LedgerConfig config, LedgerFence fence);
	~RequestLedger();
	RequestLedger(const RequestLedger &) = delete;
	RequestLedger &operator=(const RequestLedger &) = delete;

	bool Open();
	bool Healthy() const;
	bool RecoveryRequired() const;
	const std::string &LastError() const;
	const LedgerFence &Fence() const;
	const CheckpointMetadata &Checkpoint() const;
	size_t Size() const;
	uint64_t RetiredThrough() const;

	bool ReconcileLoadedCheckpoint(const CheckpointMetadata &loaded);
	LedgerAdmission ReconcileAccepted(const RequestIdentity &, const RequestBinding &, const nlohmann::json &payload);
	bool FinishRecovery(); ///< Backend must first durably reconcile all accepted identities.

	LedgerAdmission Reserve(const RequestIdentity &, const RequestBinding &, const nlohmann::json &payload, const LedgerFence &);
	std::optional<LedgerEntry> Lookup(const RequestIdentity &) const;
	bool MarkQueued(const RequestIdentity &);
	bool MarkExecuted(const RequestIdentity &, uint64_t tick, uint64_t frame, const nlohmann::json &outcome);
	bool MarkRejected(const RequestIdentity &, uint64_t tick, uint64_t frame, const nlohmann::json &outcome);
	bool ResolveUncertain(const RequestIdentity &, bool rolled_back, const nlohmann::json &outcome);
	bool AckThrough(uint64_t request_sequence); ///< Also permitted during recovery after loaded-save reconciliation.

	std::optional<CheckpointTicket> BeginCheckpoint(uint64_t saved_tick, uint64_t saved_frame);
	bool CompleteCheckpoint(const CheckpointTicket &, const std::filesystem::path &save_path);
	void CancelCheckpoint(const CheckpointTicket &);

private:
	struct Impl;
	std::unique_ptr<Impl> impl;
};

} // namespace Integration

#endif /* INTEGRATION_LEDGER_H */
