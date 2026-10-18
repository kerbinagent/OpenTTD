/*
 * This file is part of OpenTTD.
 * OpenTTD is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 2.
 */

/** @file test_integration_ledger.cpp Request identity, rollback fences and durable save-bound receipts. */

#include "../stdafx.h"
#include "../3rdparty/catch2/catch.hpp"
#include "../integration/ledger.h"
#include <filesystem>
#include <fstream>

#ifndef _WIN32
#include <sys/stat.h>
#include <unistd.h>
#endif

#include "../safeguards.h"

#ifndef _WIN32
namespace {

using namespace Integration;
using Json = nlohmann::json;

struct LedgerFixture {
	std::filesystem::path directory;
	LedgerFence fence{"world", "epoch", "boot-a"};
	RequestBinding binding{2, "text"};
	Json payload{{"action", "road.build"}, {"parameters", {{"tile", 12}}}};

	LedgerFixture()
	{
		std::array<char, 40> name{};
		std::string pattern = "/tmp/openttd-ledger-XXXXXX";
		std::copy(pattern.begin(), pattern.end(), name.begin());
		auto *created = mkdtemp(name.data());
		if (created == nullptr) throw std::runtime_error("Cannot create ledger test directory");
		this->directory = created;
	}
	~LedgerFixture() { std::error_code ec; std::filesystem::remove_all(this->directory, ec); }

	LedgerConfig Config(size_t max_requests = 8192) const { return {this->directory, max_requests}; }
	RequestIdentity Key(uint64_t sequence) const { return {sequence, "actor", Json(sequence).dump()}; }
	void Start(RequestLedger &ledger) const
	{
		REQUIRE(ledger.Open());
		REQUIRE(ledger.ReconcileLoadedCheckpoint(ledger.Checkpoint()));
		REQUIRE(ledger.FinishRecovery());
	}
	void Write(const std::filesystem::path &path, const std::string &bytes) const
	{
		std::ofstream file(path, std::ios::binary | std::ios::trunc);
		REQUIRE(file.good());
		file << bytes;
		file.close();
		REQUIRE(file.good());
	}
	Json ReadJournal() const
	{
		std::ifstream file(this->directory / "ledger.json", std::ios::binary);
		REQUIRE(file.good());
		return Json::parse(std::string(std::istreambuf_iterator<char>(file), {}));
	}
	/** Keep fixture integrity valid so historical/invalid schema tests reach the schema reader. */
	void RewriteJournal(Json envelope) const
	{
		uint64_t checksum = 14695981039346656037ULL;
		for (unsigned char byte : envelope.at("journal").dump()) checksum = (checksum ^ byte) * 1099511628211ULL;
		envelope["checksum"] = Json(checksum).dump();
		this->Write(this->directory / "ledger.json", envelope.dump());
	}
	CheckpointMetadata Save(RequestLedger &ledger, uint64_t tick, uint64_t frame, const std::string &generation) const
	{
		auto ticket = ledger.BeginCheckpoint(tick, frame);
		REQUIRE(ticket);
		auto path = this->directory / (generation + ".sav");
		this->Write(path, "fixture world " + generation);
		REQUIRE(ledger.CompleteCheckpoint(*ticket, path));
		return ledger.Checkpoint();
	}
};

} // namespace

TEST_CASE("Integration ledger deduplicates identity and binding without action-path IO", "[integration][ledger]")
{
	LedgerFixture fixture;
	RequestLedger ledger(fixture.Config(), fixture.fence);
	REQUIRE(ledger.Open());
	CHECK(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Recovering);
	REQUIRE(ledger.ReconcileLoadedCheckpoint({}));
	REQUIRE(ledger.FinishRecovery());
	REQUIRE(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
	CHECK_FALSE(std::filesystem::exists(fixture.directory / "ledger.json"));
	CHECK(ledger.Reserve(fixture.Key(1), fixture.binding, Json::parse(R"({"parameters":{"tile":12},"action":"road.build"})"), fixture.fence).status == AdmissionStatus::Duplicate);
	CHECK(ledger.Reserve(fixture.Key(1), {3, "text"}, fixture.payload, fixture.fence).status == AdmissionStatus::Conflict);
	CHECK(ledger.Reserve(fixture.Key(1), {2, "visual"}, fixture.payload, fixture.fence).status == AdmissionStatus::Conflict);
	CHECK(ledger.Reserve(fixture.Key(1), fixture.binding, {{"changed", true}}, fixture.fence).status == AdmissionStatus::Conflict);
	auto changed_sequence = fixture.Key(1);
	changed_sequence.request_sequence = 2;
	CHECK(ledger.Reserve(changed_sequence, fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Conflict);
	auto changed_actor = fixture.Key(1);
	changed_actor.actor_id = "different";
	CHECK(ledger.Reserve(changed_actor, fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Conflict);
	for (const auto &stale : {LedgerFence{"old", "epoch", "boot-a"}, LedgerFence{"world", "old", "boot-a"}, LedgerFence{"world", "epoch", "old"}}) {
		CHECK(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, stale).status == AdmissionStatus::Fenced);
	}
	REQUIRE(ledger.MarkQueued(fixture.Key(1)));
	CHECK_FALSE(ledger.Lookup(fixture.Key(1))->checkpoint_committed);
	CHECK_FALSE(ledger.BeginCheckpoint(20, 30));
	REQUIRE(ledger.MarkExecuted(fixture.Key(1), 20, 30, {{"success", true}}));
	CHECK_FALSE(ledger.Lookup(fixture.Key(1))->checkpoint_committed);
	CHECK_FALSE(ledger.AckThrough(1));
	CHECK_FALSE(ledger.BeginCheckpoint(19, 30));
	CHECK_FALSE(ledger.BeginCheckpoint(20, 29));
	CHECK_FALSE(std::filesystem::exists(fixture.directory / "ledger.json"));
	fixture.Save(ledger, 20, 30, "first");
	CHECK(ledger.Lookup(fixture.Key(1))->checkpoint_committed);
	REQUIRE(ledger.AckThrough(1));
	CHECK(ledger.Size() == 0);
	CHECK(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Retired);
}

TEST_CASE("Integration ledger restarts from save receipts and never replays uncertain requests", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata saved;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		REQUIRE(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(1), 10, 100, {{"result", "in save"}}));
		saved = fixture.Save(ledger, 10, 100, "first");
		REQUIRE(ledger.Reserve(fixture.Key(3), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(3), 20, 200, {{"result", "after save"}}));
		REQUIRE(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkQueued(fixture.Key(2)));
	}
	fixture.fence.boot_id = "boot-b";
	RequestLedger ledger(fixture.Config(), fixture.fence);
	REQUIRE(ledger.Open());
	CHECK(ledger.Checkpoint() == saved);
	CHECK_FALSE(ledger.FinishRecovery());
	REQUIRE(ledger.ReconcileLoadedCheckpoint(saved));
	CHECK(ledger.ReconcileAccepted(fixture.Key(1), fixture.binding, fixture.payload).status == AdmissionStatus::Duplicate);
	CHECK(ledger.Lookup(fixture.Key(1))->checkpoint_committed);
	for (uint64_t sequence : {2, 3}) {
		CHECK(ledger.ReconcileAccepted(fixture.Key(sequence), fixture.binding, fixture.payload).status == AdmissionStatus::Admitted);
		CHECK(ledger.Lookup(fixture.Key(sequence))->state == RequestState::Uncertain);
	}
	REQUIRE(ledger.FinishRecovery());
	CHECK_FALSE(ledger.MarkQueued(fixture.Key(2)));
	CHECK_FALSE(ledger.MarkExecuted(fixture.Key(3), 20, 200, {{"result", "after save"}}));
	CHECK(ledger.Reserve(fixture.Key(3), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Duplicate);
	CHECK_FALSE(ledger.AckThrough(3));
	REQUIRE(ledger.ResolveUncertain(fixture.Key(2), false, {{"reason", "cancelled"}}));
	REQUIRE(ledger.ResolveUncertain(fixture.Key(3), true, {{"reason", "rolled back"}}));
	CHECK(ledger.Lookup(fixture.Key(3))->state == RequestState::RolledBack);
	CHECK_FALSE(ledger.Lookup(fixture.Key(3))->checkpoint_committed);
	fixture.Save(ledger, 30, 300, "second");
	REQUIRE(ledger.AckThrough(3));
	CHECK(ledger.Size() == 0);
}

TEST_CASE("Integration ledger checkpoint captures sparse receipts independently of later actions", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata saved;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		REQUIRE(ledger.Reserve(fixture.Key(8), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(8), 10, 100, {{"ok", true}}));
		auto ticket = ledger.BeginCheckpoint(10, 100);
		REQUIRE(ticket);
		REQUIRE(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(2), 11, 101, {{"ok", true}}));
		auto save = fixture.directory / "first.sav";
		fixture.Write(save, "captured world");
		REQUIRE(ledger.CompleteCheckpoint(*ticket, save));
		CHECK(ledger.Lookup(fixture.Key(8))->checkpoint_committed);
		CHECK_FALSE(ledger.Lookup(fixture.Key(2))->checkpoint_committed);
		CHECK_FALSE(ledger.AckThrough(8));
		saved = ledger.Checkpoint();
	}
	RequestLedger restarted(fixture.Config(), {"world", "epoch", "boot-b"});
	REQUIRE(restarted.Open());
	REQUIRE(restarted.ReconcileLoadedCheckpoint(saved));
	CHECK(restarted.Lookup(fixture.Key(8))->state == RequestState::Executed);
	CHECK_FALSE(restarted.Lookup(fixture.Key(2)));
	CHECK(restarted.ReconcileAccepted(fixture.Key(2), fixture.binding, fixture.payload).entry->state == RequestState::Uncertain);
}

TEST_CASE("Integration ledger retention is a reusable checkpoint window", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata saved;
	{
		RequestLedger ledger(fixture.Config(1), fixture.fence);
		fixture.Start(ledger);
		for (uint64_t sequence = 1; sequence <= 12; ++sequence) {
			REQUIRE(ledger.Reserve(fixture.Key(sequence), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
			CHECK(ledger.Reserve(fixture.Key(sequence + 1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Full);
			REQUIRE(ledger.MarkRejected(fixture.Key(sequence), sequence, sequence, {{"error", "invalid construction"}}));
			saved = fixture.Save(ledger, sequence, sequence, Json(sequence).dump());
			REQUIRE(ledger.AckThrough(sequence));
			CHECK(ledger.Size() == 0);
		}
		/* Missing 13/14 were backend-only rejections; trusted acknowledgement skips them. */
		REQUIRE(ledger.AckThrough(14));
		saved = fixture.Save(ledger, 13, 13, "retired");
	}
	RequestLedger restarted(fixture.Config(1), {"world", "epoch", "boot-b"});
	fixture.Start(restarted);
	CHECK(restarted.RetiredThrough() == 14);
	CHECK(restarted.Reserve(fixture.Key(13), fixture.binding, fixture.payload, restarted.Fence()).status == AdmissionStatus::Retired);
	CHECK(restarted.Reserve(fixture.Key(15), fixture.binding, fixture.payload, restarted.Fence()).status == AdmissionStatus::Admitted);
}

TEST_CASE("Integration ledger fails closed on checkpoint corruption and publication failure", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata saved;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		saved = fixture.Save(ledger, 10, 100, "first");
	}
	SECTION("sidecar corruption") { fixture.Write(fixture.directory / "ledger.json", R"({"checksum":"0","journal":{}})"); }
	SECTION("save corruption") { fixture.Write(saved.save_path, "a different world"); }
	SECTION("missing save") { std::filesystem::remove(saved.save_path); }
	SECTION("missing published sidecar") { std::filesystem::remove(fixture.directory / "ledger.json"); }
	SECTION("world epoch mismatch") { fixture.fence.world_epoch = "different"; }
	SECTION("mismatched loaded metadata") {
		RequestLedger ledger(fixture.Config(), fixture.fence);
		REQUIRE(ledger.Open());
		saved.saved_tick++;
		CHECK_FALSE(ledger.ReconcileLoadedCheckpoint(saved));
		CHECK_FALSE(ledger.Healthy());
		return;
	}
	SECTION("publication cannot replace old sidecar") {
		{
			RequestLedger ledger(fixture.Config(), fixture.fence);
			fixture.Start(ledger);
			auto ticket = ledger.BeginCheckpoint(20, 200);
			REQUIRE(ticket);
			std::filesystem::create_directory(fixture.directory / "ledger.json.tmp");
			fixture.Write(fixture.directory / "second.sav", "next world generation");
			CHECK_FALSE(ledger.CompleteCheckpoint(*ticket, fixture.directory / "second.sav"));
			CHECK_FALSE(ledger.Healthy());
			CHECK(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Unavailable);
		}
		std::filesystem::remove(fixture.directory / "ledger.json.tmp");
		RequestLedger restarted(fixture.Config(), {"world", "epoch", "boot-b"});
		REQUIRE(restarted.Open());
		CHECK(restarted.Checkpoint() == saved);
		return;
	}
	RequestLedger corrupt(fixture.Config(), fixture.fence);
	CHECK_FALSE(corrupt.Open());
	CHECK_FALSE(corrupt.Healthy());
	CHECK(corrupt.RecoveryRequired());
}

TEST_CASE("Integration ledger retires restored receipts before importing recovery backlog", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata saved;
	{
		RequestLedger ledger(fixture.Config(1), fixture.fence);
		fixture.Start(ledger);
		REQUIRE(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(1), 1, 1, {{"ok", true}}));
		saved = fixture.Save(ledger, 1, 1, "first");
		REQUIRE(ledger.AckThrough(1));
		REQUIRE(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		/* Crash before publishing another checkpoint; saved window still contains key 1. */
	}
	RequestLedger restarted(fixture.Config(1), {"world", "epoch", "boot-b"});
	REQUIRE(restarted.Open());
	CHECK_FALSE(restarted.AckThrough(1));
	REQUIRE(restarted.ReconcileLoadedCheckpoint(saved));
	CHECK(restarted.ReconcileAccepted(fixture.Key(2), fixture.binding, fixture.payload).status == AdmissionStatus::Full);
	REQUIRE(restarted.AckThrough(1));
	CHECK(restarted.ReconcileAccepted(fixture.Key(1), fixture.binding, fixture.payload).status == AdmissionStatus::Retired);
	REQUIRE(restarted.ReconcileAccepted(fixture.Key(2), fixture.binding, fixture.payload).status == AdmissionStatus::Admitted);
	REQUIRE(restarted.FinishRecovery());
	CHECK(restarted.Lookup(fixture.Key(2))->state == RequestState::Uncertain);
}

TEST_CASE("Integration ledger reserves serialized checkpoint capacity and validates UTF8", "[integration][ledger]")
{
	LedgerFixture fixture;
	auto config = fixture.Config();
	config.max_manifest_bytes = 40000;
	config.max_payload_bytes = 1024;
	config.max_outcome_bytes = 4000;
	RequestLedger ledger(config, fixture.fence);
	fixture.Start(ledger);
	auto invalid = fixture.Key(1);
	invalid.actor_id = std::string(1, static_cast<char>(0xFF));
	CHECK(ledger.Reserve(invalid, fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Invalid);
	REQUIRE(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
	CHECK(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Full);
	REQUIRE(ledger.MarkExecuted(fixture.Key(1), 1, 1, {{"small", true}}));
	CHECK(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Full);
	/* Only checkpointed outcomes release reserved headroom, preserving recovery capacity. */
	fixture.Save(ledger, 1, 1, "first-budget");
	REQUIRE(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
	REQUIRE(ledger.MarkRejected(fixture.Key(2), 1, 1, std::string(3990, 'x')));
	fixture.Save(ledger, 1, 1, "within-budget");
	CHECK(ledger.Healthy());
	CHECK(std::filesystem::file_size(fixture.directory / "ledger.json") <= config.max_manifest_bytes);
	LedgerFixture invalid_fixture;
	auto invalid_fence = invalid_fixture.fence;
	invalid_fence.world_epoch = std::string(1, static_cast<char>(0xFF));
	RequestLedger invalid_ledger(invalid_fixture.Config(), invalid_fence);
	CHECK_FALSE(invalid_ledger.Open());
}

TEST_CASE("Integration ledger reserves crash recovery headroom for uncheckpointed outcomes", "[integration][ledger]")
{
	LedgerFixture fixture;
	auto config = fixture.Config();
	config.max_manifest_bytes = 42000;
	config.max_payload_bytes = 1024;
	config.max_outcome_bytes = 3000;
	CheckpointMetadata saved;
	{
		RequestLedger ledger(config, fixture.fence);
		fixture.Start(ledger);
		saved = fixture.Save(ledger, 0, 0, "empty");
		for (uint64_t sequence : {1, 2}) {
			REQUIRE(ledger.Reserve(fixture.Key(sequence), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
			REQUIRE(ledger.MarkExecuted(fixture.Key(sequence), 1, 1, {{"small", true}}));
		}
		CHECK(ledger.Reserve(fixture.Key(3), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Full);
	}
	RequestLedger recovered(config, {"world", "epoch", "boot-b"});
	REQUIRE(recovered.Open());
	REQUIRE(recovered.ReconcileLoadedCheckpoint(saved));
	for (uint64_t sequence : {1, 2}) {
		REQUIRE(recovered.ReconcileAccepted(fixture.Key(sequence), fixture.binding, fixture.payload).status == AdmissionStatus::Admitted);
	}
	REQUIRE(recovered.FinishRecovery());
	CHECK(recovered.Lookup(fixture.Key(1))->state == RequestState::Uncertain);
	CHECK(recovered.Lookup(fixture.Key(2))->state == RequestState::Uncertain);
}

TEST_CASE("Integration ledger qualifies native frames by boot across repeated checkpoints", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata saved;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		REQUIRE(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(1), 10, 1000, {{"ok", true}}));
		saved = fixture.Save(ledger, 10, 1000, "boot-a");
		CHECK(saved.saved_boot_id == "boot-a");
		CHECK(ledger.Lookup(fixture.Key(1))->executed_boot_id == "boot-a");
	}
	fixture.fence.boot_id = "boot-b";
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		REQUIRE(ledger.Open());
		REQUIRE(ledger.ReconcileLoadedCheckpoint(saved));
		REQUIRE(ledger.FinishRecovery());
		/* Native frames reset, while saved game ticks and original receipt provenance remain. */
		CHECK_FALSE(ledger.BeginCheckpoint(9, 0));
		auto reset_frame = ledger.BeginCheckpoint(10, 0);
		REQUIRE(reset_frame);
		ledger.CancelCheckpoint(*reset_frame);
		CHECK(ledger.Lookup(fixture.Key(1))->executed_frame == 1000);
		CHECK(ledger.Lookup(fixture.Key(1))->executed_boot_id == "boot-a");
		REQUIRE(ledger.Reserve(fixture.Key(2), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(2), 11, 2, {{"ok", true}}));
		CHECK_FALSE(ledger.BeginCheckpoint(11, 1));
		saved = fixture.Save(ledger, 11, 2, "boot-b");
		CHECK(saved.saved_boot_id == "boot-b");
		CHECK_FALSE(ledger.BeginCheckpoint(11, 1));
	}
	fixture.fence.boot_id = "boot-c";
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		REQUIRE(ledger.Open());
		REQUIRE(ledger.ReconcileLoadedCheckpoint(saved));
		REQUIRE(ledger.FinishRecovery());
		CHECK(ledger.Lookup(fixture.Key(1))->executed_frame == 1000);
		CHECK(ledger.Lookup(fixture.Key(1))->executed_boot_id == "boot-a");
		CHECK(ledger.Lookup(fixture.Key(2))->executed_boot_id == "boot-b");
		CHECK_FALSE(ledger.BeginCheckpoint(10, 0));
		saved = fixture.Save(ledger, 11, 0, "boot-c");
		CHECK(saved.saved_boot_id == "boot-c");
	}
	RequestLedger final_load(fixture.Config(), {"world", "epoch", "boot-d"});
	REQUIRE(final_load.Open());
	CHECK(final_load.Checkpoint() == saved);
	CHECK(final_load.Lookup(fixture.Key(1))->executed_frame == 1000);
	CHECK(final_load.Lookup(fixture.Key(2))->executed_frame == 2);
}

TEST_CASE("Integration ledger enforces exclusive ownership and fails closed on save failure", "[integration][ledger]")
{
	LedgerFixture fixture;
	RequestLedger ledger(fixture.Config(), fixture.fence);
	fixture.Start(ledger);
	RequestLedger competitor(fixture.Config(), fixture.fence);
	CHECK_FALSE(competitor.Open());
	struct stat state;
	REQUIRE(stat(fixture.directory.c_str(), &state) == 0);
	CHECK((state.st_mode & 0777) == 0700);
	auto ticket = ledger.BeginCheckpoint(0, 0);
	REQUIRE(ticket);
	CHECK_FALSE(ledger.CompleteCheckpoint(*ticket, fixture.directory / "missing.sav"));
	CHECK_FALSE(ledger.Healthy());
	CHECK_FALSE(ledger.FinishRecovery());
}

TEST_CASE("Integration ledger checkpoints retain only their direct published predecessor", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata first, second, third;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		first = fixture.Save(ledger, 1, 1, "first");
		CHECK_FALSE(first.parent_checkpoint);
		REQUIRE(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(1), 2, 2, {{"effect", "in successor save"}}));
		second = fixture.Save(ledger, 2, 2, "second");
		REQUIRE(second.parent_checkpoint);
		CHECK((*second.parent_checkpoint == CheckpointParent{first.checkpoint_id, first.save_checksum, first.saved_tick}));
		third = fixture.Save(ledger, 3, 3, "third");
		REQUIRE(third.parent_checkpoint);
		CHECK((*third.parent_checkpoint == CheckpointParent{second.checkpoint_id, second.save_checksum, second.saved_tick}));
		const auto envelope = fixture.ReadJournal();
		CHECK(envelope.at("journal").at("version") == 3);
		const auto &parent = envelope.at("journal").at("checkpoint").at("parent_checkpoint");
		CHECK(parent.size() == 3);
		CHECK_FALSE(parent.contains("parent_checkpoint"));
		CHECK_FALSE(parent.contains("retired_through"));
	}
	/* The current save is independently verified; its parent identity is evidence,
	 * not a request to load older saves or traverse ancestry from disk. */
	std::filesystem::remove(first.save_path);
	std::filesystem::remove(second.save_path);
	RequestLedger restarted(fixture.Config(), {"world", "epoch", "boot-b"});
	REQUIRE(restarted.Open());
	CHECK(restarted.Checkpoint() == third);
	REQUIRE(restarted.ReconcileLoadedCheckpoint(third));
	CHECK(restarted.Lookup(fixture.Key(1))->checkpoint_committed);
}

TEST_CASE("Integration ledger cancellation and failed publication cannot advance checkpoint parents", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata first, second;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		first = fixture.Save(ledger, 1, 1, "first");
		auto cancelled = ledger.BeginCheckpoint(2, 2);
		REQUIRE(cancelled);
		fixture.Write(fixture.directory / "orphan.sav", "unpublished cancelled generation");
		ledger.CancelCheckpoint(*cancelled);
		CHECK(ledger.Checkpoint() == first);
		second = fixture.Save(ledger, 3, 3, "second");
		REQUIRE(second.parent_checkpoint);
		CHECK(second.parent_checkpoint->checkpoint_id == first.checkpoint_id);
		CHECK(second.parent_checkpoint->checkpoint_id != cancelled->checkpoint_id);
		auto failed = ledger.BeginCheckpoint(4, 4);
		REQUIRE(failed);
		std::filesystem::create_directory(fixture.directory / "ledger.json.tmp");
		fixture.Write(fixture.directory / "failed.sav", "unpublished failed generation");
		CHECK_FALSE(ledger.CompleteCheckpoint(*failed, fixture.directory / "failed.sav"));
		CHECK_FALSE(ledger.Healthy());
		CHECK(ledger.Checkpoint() == second);
	}
	std::filesystem::remove(fixture.directory / "ledger.json.tmp");
	RequestLedger restarted(fixture.Config(), {"world", "epoch", "boot-b"});
	REQUIRE(restarted.Open());
	CHECK(restarted.Checkpoint() == second);
	REQUIRE(restarted.Checkpoint().parent_checkpoint);
	CHECK(restarted.Checkpoint().parent_checkpoint->checkpoint_id == first.checkpoint_id);
}

TEST_CASE("Integration ledger reads version two checkpoints without inventing parent evidence", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata legacy;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		REQUIRE(ledger.Reserve(fixture.Key(1), fixture.binding, fixture.payload, fixture.fence).status == AdmissionStatus::Admitted);
		REQUIRE(ledger.MarkExecuted(fixture.Key(1), 1, 1, {{"legacy", true}}));
		legacy = fixture.Save(ledger, 1, 1, "legacy");
	}
	auto envelope = fixture.ReadJournal();
	envelope["journal"]["version"] = 2U;
	envelope["journal"]["checkpoint"].erase("parent_checkpoint");
	fixture.RewriteJournal(std::move(envelope));
	RequestLedger upgraded(fixture.Config(), {"world", "epoch", "boot-b"});
	REQUIRE(upgraded.Open());
	CHECK(upgraded.Checkpoint() == legacy);
	CHECK_FALSE(upgraded.Checkpoint().parent_checkpoint);
	CHECK(upgraded.Lookup(fixture.Key(1))->checkpoint_committed);
	CHECK(fixture.ReadJournal().at("journal").at("version") == 2);
	REQUIRE(upgraded.ReconcileLoadedCheckpoint(legacy));
	REQUIRE(upgraded.FinishRecovery());
	const auto successor = fixture.Save(upgraded, 2, 0, "successor");
	REQUIRE(successor.parent_checkpoint);
	CHECK((*successor.parent_checkpoint == CheckpointParent{legacy.checkpoint_id, legacy.save_checksum, legacy.saved_tick}));
	CHECK(fixture.ReadJournal().at("journal").at("version") == 3);
}

TEST_CASE("Integration ledger rejects malformed or recursive checkpoint lineage", "[integration][ledger]")
{
	LedgerFixture fixture;
	CheckpointMetadata current;
	{
		RequestLedger ledger(fixture.Config(), fixture.fence);
		fixture.Start(ledger);
		fixture.Save(ledger, 1, 1, "first");
		current = fixture.Save(ledger, 2, 2, "second");
	}
	auto envelope = fixture.ReadJournal();
	auto &checkpoint = envelope["journal"]["checkpoint"];
	SECTION("missing required version three parent field") { checkpoint.erase("parent_checkpoint"); }
	SECTION("recursive ancestry") { checkpoint["parent_checkpoint"]["parent_checkpoint"] = nullptr; }
	SECTION("self parent") { checkpoint["parent_checkpoint"]["checkpoint_id"] = current.checkpoint_id; }
	SECTION("parent from a future game tick") { checkpoint["parent_checkpoint"]["saved_tick"] = current.saved_tick + 1; }
	SECTION("parent identifier exceeds bound") { checkpoint["parent_checkpoint"]["checkpoint_id"] = std::string(513, 'x'); }
	SECTION("empty parent checksum") { checkpoint["parent_checkpoint"]["save_checksum"] = ""; }
	SECTION("parent checksum exceeds bound") { checkpoint["parent_checkpoint"]["save_checksum"] = std::string(129, 'x'); }
	SECTION("version two cannot contain a new parent field") { envelope["journal"]["version"] = 2U; }
	SECTION("unknown future sidecar version") { envelope["journal"]["version"] = 4U; }
	fixture.RewriteJournal(std::move(envelope));
	RequestLedger rejected(fixture.Config(), {"world", "epoch", "boot-b"});
	CHECK_FALSE(rejected.Open());
	CHECK_FALSE(rejected.Healthy());
	CHECK(rejected.RecoveryRequired());
}
#endif
