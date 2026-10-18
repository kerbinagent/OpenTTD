import os
import sqlite3
import hashlib
import json
import tempfile
import threading
import unittest

from backend.errors import ServiceError
from backend.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.db")
        self.store = Store(self.path, max_pending_company=2, max_pending_world=3, event_retention=2)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def principal(self, world="world", identity="alice", company=1, mode="text"):
        token = self.store.provision(world, identity, company, mode=mode)
        return token, self.store.authenticate(token)

    def assert_code(self, code, fn):
        with self.assertRaises(ServiceError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_auth_is_scoped_and_revocation_is_effective(self):
        token, alice = self.principal()
        _, bob = self.principal(identity="bob", company=2)
        _, other_world = self.principal(world="other", identity="alice", company=1)
        self.assertEqual(self.store.authenticate(token), alice)
        self.assertNotEqual(alice, bob)
        self.assertNotEqual(alice, other_world)
        self.assert_code("unauthorized", lambda: self.store.authenticate("invalid"))
        self.assertTrue(self.store.company_exists("world", 1))
        self.assertFalse(self.store.company_exists("world", 3))
        self.store.revoke_membership("world", "alice", 1)
        self.assert_code("unauthorized", lambda: self.store.authenticate(token))
        self.assertEqual(self.store.get_principal(alice.run_id), alice)
        self.assert_code("unauthorized", lambda: self.store.validate_principal(alice))
        self.assertEqual(self.store.principals_for_world("world"), [bob])

    def test_provision_validation_epoch_and_rotation(self):
        for args, code in [
            ({"company_id": -1}, "invalid_company"),
            ({"company_id": 15}, "invalid_company"),
            ({"company_id": True}, "invalid_company"),
            ({"mode": "other"}, "invalid_mode"),
            ({"kind": "other"}, "invalid_kind"),
        ]:
            self.assert_code(code, lambda args=args: self.store.provision(
                "world", "alice", 1, **args
            ) if "company_id" not in args else self.store.provision(
                "world", "alice", args["company_id"]
            ))
        old_token, principal = self.principal()
        self.assert_code("epoch_conflict", lambda: self.store.provision(
            "world", "bob", 2, world_epoch="new-epoch"
        ))
        new_token = self.store.rotate_credential(principal)
        self.assertNotEqual(old_token, new_token)
        self.assert_code("unauthorized", lambda: self.store.authenticate(old_token))
        self.assertEqual(self.store.authenticate(new_token), principal)
        self.assertEqual(self.store.get_principal(principal.run_id), principal)
        self.store.revoke_membership("world", "alice", 1)
        self.assert_code("unauthorized", lambda: self.store.rotate_credential(principal))

    def test_retry_binding_and_terminal_transition(self):
        _, first = self.principal()
        record, new = self.store.accept_request(first, "r1", {"b": 2, "a": 1})
        self.assertTrue(new)
        self.assertEqual(record["status"], "accepted")
        record, new = self.store.accept_request(first, "r1", {"a": 1, "b": 2})
        self.assertFalse(new)
        self.assert_code("idempotency_conflict", lambda: self.store.accept_request(first, "r1", {"a": 2}))
        _, second = self.principal(mode="visual")
        self.assert_code("idempotency_conflict", lambda: self.store.accept_request(second, "r1", {"a": 1, "b": 2}))
        self.assert_code("not_found", lambda: self.store.get_request(second, "r1"))
        self.store.update_request(first, "r1", "queued", result={"engine_instance_id": "e1"})
        done = self.store.update_request(first, "r1", "completed", result={"ok": True}, engine_sequence=3, tick=4)
        self.assertEqual(done["result"], {"ok": True})
        self.assert_code("invalid_transition", lambda: self.store.update_request(first, "r1", "queued"))
        self.assert_code("invalid_transition", lambda: self.store.update_request(first, "r1", "completed", result={"ok": False}))

    def test_capacity_is_transactional_and_duplicate_is_free(self):
        _, a = self.principal()
        _, b = self.principal(identity="bob", company=1)
        _, c = self.principal(identity="carol", company=2)
        barrier = threading.Barrier(2)
        outcomes = []

        def accept(principal, request_id):
            barrier.wait()
            try:
                outcomes.append(self.store.accept_request(principal, request_id, {})[1])
            except Exception as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=accept, args=(a, "a")),
                   threading.Thread(target=accept, args=(b, "b"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes, [True, True])
        self.assertFalse(self.store.accept_request(a, "a", {})[1])
        self.assert_code("company_capacity", lambda: self.store.accept_request(a, "a2", {}))
        self.store.accept_request(c, "c", {})
        _, d = self.principal(identity="dave", company=3)
        self.assert_code("world_capacity", lambda: self.store.accept_request(d, "d", {}))
        self.store.update_request(a, "a", "completed")
        self.assertTrue(self.store.accept_request(d, "d", {})[1])

    def test_restart_recovers_requests_and_hashed_token(self):
        token, principal = self.principal()
        self.store.accept_request(principal, "job", {"cmd": "buy"})
        self.store.update_request(principal, "job", "queued", result={"engine_instance_id": "e"})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.authenticate(token), principal)
        self.assertEqual(self.store.pending_requests()[0]["result"], {"engine_instance_id": "e"})
        with open(self.path, "rb") as db_file:
            self.assertNotIn(token.encode(), db_file.read())
        self.assertEqual(self.store.get_request(principal, "job")["status"], "queued")
        self.store.revoke_membership("world", "alice", 1)
        self.assertEqual(self.store.update_request(principal, "job", "rejected", error={"code": "revoked"})["status"], "rejected")

    def test_private_cursor_and_retention(self):
        _, alice = self.principal()
        _, bob = self.principal(identity="bob")
        initial = self.store.events(alice)["cursor"]
        first = self.store.append_event(alice, "notice", {"n": 1})
        self.store.append_event(alice, "notice", {"n": 2})
        self.store.append_event(alice, "notice", {"n": 3})
        self.store.append_event(bob, "private", {"n": 99})
        self.assert_code("resync_required", lambda: self.store.events(alice, after=initial))
        self.assertEqual([event["data"]["n"] for event in self.store.events(alice)["events"]], [2, 3])
        page = self.store.events(alice, after=first, limit=1)
        self.assertEqual([event["data"]["n"] for event in page["events"]], [2])
        self.assertEqual(self.store.events(alice, after=page["cursor"])["events"][0]["data"], {"n": 3})
        self.assert_code("invalid_cursor", lambda: self.store.events(bob, after=first))
        self.assertEqual(len(self.store.events(bob)["events"]), 1)

    def test_event_projection_can_share_a_transaction(self):
        _, principal = self.principal()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store._transaction() as db:
                self.store.append_event_in_transaction(db, principal, "notice", {"n": 1})
                raise RuntimeError("rollback")
        self.assertEqual(self.store.events(principal)["events"], [])
        with self.store._transaction() as db:
            self.store.append_event_in_transaction(db, principal, "notice", {"n": 2})
        self.assertEqual(self.store.events(principal)["events"][0]["data"], {"n": 2})

    def test_terminal_event_is_atomic_and_wakes_waiter(self):
        _, principal = self.principal()
        self.store.accept_request(principal, "job", {})
        cursor = self.store.events(principal)["cursor"]
        self.assert_code("invalid_payload", lambda: self.store.update_request(
            principal, "job", "completed", event={"bad": object()}
        ))
        self.assertEqual(self.store.get_request(principal, "job")["status"], "accepted")
        observed = []

        def wait():
            observed.append(self.store.wait_for_events(principal, cursor, timeout=1))

        thread = threading.Thread(target=wait)
        thread.start()
        self.store.update_request(principal, "job", "completed",
                                  event={"request_id": "job", "status": "completed"})
        thread.join(timeout=2)
        self.assertEqual(observed, [True])
        self.assertEqual(self.store.events(principal, after=cursor)["events"][0]["data"]["status"], "completed")

    def test_checkpoint_manifest_is_monotonic_and_marks_only_durable_results(self):
        _, principal = self.principal()
        self.store.accept_request(principal, "one", {})
        self.store.accept_request(principal, "two", {})
        self.store.update_request(principal, "one", "completed", engine_sequence=5)
        self.store.update_request(principal, "two", "completed", engine_sequence=8)
        digest = "a" * 64
        one = self.store.get_request(principal, "one")["request_sequence"]
        two = self.store.get_request(principal, "two")["request_sequence"]
        manifest_data = {"world_epoch": "initial", "save_id": "save1",
                         "retired_through": 2, "tick": 100, "save_sha256": digest}
        receipt_one = {"request_sequence": one, "state": "executed", "checkpoint_committed": True}
        manifest = self.store.apply_checkpoint("world", manifest_data, [receipt_one])
        self.assertEqual(self.store.latest_checkpoint("world"), manifest)
        self.assertEqual(self.store.get_request(principal, "one")["checkpoint_state"], "checkpointed")
        self.assertEqual(self.store.get_request(principal, "two")["checkpoint_state"], "uncheckpointed")
        self.assertEqual(self.store.apply_checkpoint("world", manifest_data, [receipt_one]), manifest)
        self.assert_code("checkpoint_conflict", lambda: self.store.apply_checkpoint(
            "world", {**manifest_data, "retired_through": 3}, [receipt_one]))
        self.assert_code("watermark_regression", lambda: self.store.apply_checkpoint(
            "world", {**manifest_data, "save_id": "save2", "retired_through": 1}, []))
        self.assert_code("receipt_conflict", lambda: self.store.apply_checkpoint(
            "world", {**manifest_data, "save_id": "save2"},
            [{"request_sequence": two, "status": "rejected"}]))
        self.store.apply_checkpoint("world", {**manifest_data, "save_id": "save2"},
                                    [{"request_sequence": two, "status": "completed"}])
        self.assertEqual(self.store.get_request(principal, "two")["checkpoint_state"], "checkpointed")

    def test_sparse_nonterminal_receipt_does_not_claim_durability(self):
        _, principal = self.principal()
        request = self.store.accept_request(principal, "pending", {})[0]
        self.store.apply_checkpoint("world", {"world_epoch": "initial", "save_id": "save1",
                                              "retired_through": 0, "tick": 5},
                                    [{"request_sequence": request["request_sequence"], "state": "reserved"}])
        self.assertEqual(self.store.get_request(principal, "pending")["checkpoint_state"], "uncheckpointed")

    def test_checkpoint_opaque_checksum_and_multiple_receipts_return_manifest(self):
        _, principal = self.principal()
        first = self.store.accept_request(principal, "one", {})[0]
        second = self.store.accept_request(principal, "two", {})[0]
        self.store.update_request(principal, "one", "completed")
        self.store.update_request(principal, "two", "rejected")
        manifest_data = {"world_epoch": "initial", "save_id": "save-fnv",
                         "retired_through": 0, "tick": 15, "save_checksum": "1a2b3c4d"}
        receipts = [{"request_sequence": first["request_sequence"], "state": "executed"},
                    {"request_sequence": second["request_sequence"], "state": "rejected"}]
        manifest = self.store.apply_checkpoint("world", manifest_data, receipts)
        self.assertEqual(manifest["save_id"], "save-fnv")
        self.assertEqual(manifest["save_checksum"], "1a2b3c4d")
        self.assertIsNone(manifest["save_sha256"])
        self.assertEqual(self.store.latest_checkpoint("world"), manifest)
        self.assertEqual(self.store.apply_checkpoint("world", manifest_data, receipts), manifest)
        self.assertEqual(self.store.get_request(principal, "one")["checkpoint_state"], "checkpointed")
        self.assertEqual(self.store.get_request(principal, "two")["checkpoint_state"], "checkpointed")
        self.assert_code("checkpoint_conflict", lambda: self.store.apply_checkpoint(
            "world", {**manifest_data, "save_checksum": "different"}, receipts
        ))

    def test_late_terminal_receipt_merges_into_existing_manifest(self):
        _, principal = self.principal()
        pending = self.store.accept_request(principal, "late", {})[0]
        manifest = self.store.apply_checkpoint("world", {
            "world_epoch": "initial", "save_id": "save-before-poll",
            "retired_through": 0, "tick": 20, "save_checksum": "fnv-check",
        }, [])
        self.assertEqual(self.store.get_request(principal, "late")["checkpoint_state"], "uncheckpointed")
        self.store.update_request(principal, "late", "completed", result={"success": True})
        receipt = {"request_sequence": pending["request_sequence"], "status": "completed"}
        self.assertEqual(self.store.merge_checkpoint_receipts(
            "world", "save-before-poll", [receipt]), manifest)
        self.assertEqual(self.store.get_request(principal, "late")["checkpoint_state"], "checkpointed")
        self.assertEqual(self.store.merge_checkpoint_receipts(
            "world", "save-before-poll", [receipt]), manifest)
        self.assert_code("receipt_conflict", lambda: self.store.merge_checkpoint_receipts(
            "world", "save-before-poll", [{**receipt, "result": {"different": True}}]
        ))
        self.assert_code("invalid_checkpoint", lambda: self.store.merge_checkpoint_receipts(
            "world", "save-before-poll", [{"request_sequence": pending["request_sequence"], "status": "queued"}]
        ))

    def test_monotonic_sequence_retry_prefix_and_restart(self):
        _, alice = self.principal()
        _, bob = self.principal(identity="bob", company=2)
        _, carol = self.principal(identity="carol", company=3)
        barrier = threading.Barrier(3)
        results = []

        def write(principal, request_id):
            barrier.wait()
            results.append(self.store.accept_request(principal, request_id, {})[0]["request_sequence"])

        workers = [threading.Thread(target=write, args=(p, rid))
                   for p, rid in [(alice, "a"), (bob, "b"), (carol, "c")]]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sorted(results), [1, 2, 3])
        self.assertEqual(self.store.max_request_sequence("world"), 3)
        self.assertFalse(self.store.accept_request(alice, "a", {})[1])
        self.assertEqual(self.store.max_request_sequence("world"), 3)
        ledger = self.store.accepted_requests("world")
        self.assertEqual([row["request_sequence"] for row in ledger], [1, 2, 3])
        self.assertEqual(len(self.store.accepted_requests("world", after_sequence=1)), 2)
        self.store.update_request(self.store.get_principal(ledger[0]["run_id"]), ledger[0]["request_id"], "completed")
        self.assertEqual(self.store.terminal_ack_prefix("world"), 1)
        self.store.update_request(self.store.get_principal(ledger[2]["run_id"]), ledger[2]["request_id"], "rejected")
        self.assertEqual(self.store.terminal_ack_prefix("world"), 1)
        self.store.update_request(self.store.get_principal(ledger[1]["run_id"]), ledger[1]["request_id"], "completed")
        self.assertEqual(self.store.terminal_ack_prefix("world"), 3)
        self.store.close()
        self.store = Store(self.path)
        _, dave = self.principal(identity="dave", company=4)
        self.assertEqual(self.store.accept_request(dave, "d", {})[0]["request_sequence"], 4)

    def test_unretired_window_persists_and_is_shared_with_chat(self):
        _, principal = self.principal()
        self.store.close()
        self.store = Store(self.path, max_pending_company=10, max_pending_world=10, max_unretired=2)
        first = self.store.accept_request(principal, "one", {})[0]
        self.store.accept_request(principal, "two", {})
        self.store.update_request(principal, "one", "completed")
        self.store.update_request(principal, "two", "completed")
        self.assertEqual(first["request_sequence"], 1)
        self.assert_code("unretired_capacity", lambda: self.store.accept_request(principal, "three", {}))
        self.assert_code("unretired_capacity", lambda: self.store.reserve_internal(
            "world", "message", {"rpc_type": "chat.publish", "message_id": "message", "delivery": {}}
        ))
        self.assertFalse(self.store.accept_request(principal, "one", {})[1])
        self.assertEqual(self.store.observe_engine("world", "engine-a", 1),
                         {"engine_instance_id": "engine-a", "retired_through": 1})
        self.assertEqual(self.store.reserve_internal(
            "world", "message", {"rpc_type": "chat.publish", "message_id": "message", "delivery": {}}
        )["request_sequence"], 3)
        self.store.close()
        self.store = Store(self.path, max_unretired=2)
        self.assertEqual(self.store.get_engine_state("world"),
                         {"engine_instance_id": "engine-a", "retired_through": 1})
        self.assert_code("restore_behind", lambda: self.store.observe_engine("world", "engine-b", 0))
        self.assert_code("invalid_engine_state", lambda: self.store.observe_engine("world", "engine-b", 4))

    def test_checkpoint_ack_prefix_needs_sparse_receipt_or_safe_local_rejection(self):
        _, principal = self.principal()
        a = self.store.accept_request(principal, "a", {})[0]
        self.store.accept_request(principal, "b", {})
        self.store.update_request(principal, "a", "completed")
        self.store.update_request(principal, "b", "rejected", error={"code": "unsupported_action"})
        self.assertEqual(self.store.checkpoint_ack_prefix("world"), 0)
        self.store.apply_checkpoint("world", {"world_epoch": "initial", "save_id": "save1",
                                              "retired_through": 0, "tick": 5},
                                    [{"request_sequence": a["request_sequence"], "status": "completed"}])
        self.assertEqual(self.store.checkpoint_ack_prefix("world"), 2)
        c = self.store.accept_request(principal, "c", {})[0]
        self.store.update_request(principal, "c", "queued")
        self.store.update_request(principal, "c", "rejected", error={"code": "unsupported_action"})
        self.assertTrue(self.store.get_request(principal, "c")["engine_dispatched"])
        self.assertEqual(self.store.checkpoint_ack_prefix("world"), 2)
        self.store.merge_checkpoint_receipts("world", "save1", [
            {"request_sequence": c["request_sequence"], "status": "rejected"}
        ])
        self.assertEqual(self.store.checkpoint_ack_prefix("world"), 3)

    def test_recovery_fence_and_correction(self):
        _, alice = self.principal()
        self.store.accept_request(alice, "first", {})
        self.store.update_request(alice, "first", "completed")
        self.store.accept_request(alice, "second", {})
        self.store.begin_recovery("world", "engine-new")
        self.store.close()
        self.store = Store(self.path, max_pending_company=2, max_pending_world=3)
        self.assert_code("world_recovering", lambda: self.store.accept_request(alice, "third", {}))
        self.assertFalse(self.store.accept_request(alice, "second", {})[1])
        self.assert_code("recovery_incomplete", lambda: self.store.finish_recovery("world"))
        corrected = self.store.resolve_recovery_request(alice, "first", "uncertain", error={"code": "lost"})
        self.assertEqual(corrected["checkpoint_state"], "uncertain")
        self.assertEqual(self.store.terminal_ack_prefix("world"), 0)
        resolved = self.store.resolve_recovery_request(alice, "first", "resolved_rolled_back")
        self.assertEqual(resolved["status"], "resolved_rolled_back")
        self.assertEqual(self.store.terminal_ack_prefix("world"), 1)
        self.store.finish_recovery("world", scan_completed=True)
        self.assertEqual(self.store.accept_request(alice, "third", {})[0]["request_sequence"], 3)
        self.assert_code("recovery_conflict", lambda: self.store.resolve_recovery_request(
            alice, "first", "uncertain"))

    def test_interrupted_recovery_rebind_requires_literal_verified_checkpoint(self):
        _, principal = self.principal()
        self.store.accept_request(principal, "one", {})
        self.store.begin_recovery("world", "first-target")
        self.store.close()
        self.store = Store(self.path)
        self.store.begin_recovery("world", "first-target")
        self.assert_code("recovery_conflict", lambda: self.store.begin_recovery(
            "world", "second-target"))
        self.assert_code("recovery_conflict", lambda: self.store.begin_recovery(
            "world", "second-target", verified_checkpoint=1))
        self.store.begin_recovery("world", "second-target", verified_checkpoint=True)
        self.store.close()
        self.store = Store(self.path)
        self.assert_code("recovery_conflict", lambda: self.store.begin_recovery(
            "world", "third-target"))
        self.store.begin_recovery("world", "third-target", verified_checkpoint=True)
        self.assertEqual(tuple(self.store.connection.execute(
            "SELECT recovery_state,recovery_instance_id FROM worlds WHERE id='world'"
        ).fetchone()), ("recovering", "third-target"))

    def test_recovery_finish_persists_engine_state_atomically_or_stays_fenced(self):
        _, principal = self.principal()
        self.store.accept_request(principal, "one", {})
        self.store.accept_request(principal, "two", {})
        self.store.observe_engine("world", "old-engine", 1)
        self.store.begin_recovery("world", "new-engine")
        bad_finishes = [
            ("recovery_incomplete", lambda: self.store.finish_recovery(
                "world", engine_instance_id="new-engine", retired_through=2)),
            ("invalid_recovery", lambda: self.store.finish_recovery(
                "world", scan_completed=True, engine_instance_id="new-engine")),
            ("invalid_recovery", lambda: self.store.finish_recovery(
                "world", scan_completed=True, retired_through=2)),
            ("invalid_recovery", lambda: self.store.finish_recovery(
                "world", scan_completed=True, engine_instance_id="", retired_through=2)),
            ("invalid_recovery", lambda: self.store.finish_recovery(
                "world", scan_completed=True, engine_instance_id="new-engine", retired_through=True)),
            ("recovery_conflict", lambda: self.store.finish_recovery(
                "world", scan_completed=True, engine_instance_id="wrong-engine", retired_through=2)),
            ("restore_behind", lambda: self.store.finish_recovery(
                "world", scan_completed=True, engine_instance_id="new-engine", retired_through=0)),
            ("invalid_engine_state", lambda: self.store.finish_recovery(
                "world", scan_completed=True, engine_instance_id="new-engine", retired_through=3)),
        ]
        for code, attempt in bad_finishes:
            self.assert_code(code, attempt)
            self.assertEqual(tuple(self.store.connection.execute(
                "SELECT recovery_state,engine_instance_id,retired_through FROM worlds WHERE id='world'"
            ).fetchone()), ("recovering", "old-engine", 1))
        self.store.finish_recovery("world", scan_completed=True,
                                   engine_instance_id="new-engine", retired_through=2)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.connection.execute(
            "SELECT recovery_state FROM worlds WHERE id='world'"
        ).fetchone()[0], "ready")
        self.assertEqual(self.store.get_engine_state("world"),
                         {"engine_instance_id": "new-engine", "retired_through": 2})

    def test_single_world_guard(self):
        self.principal()
        self.assertEqual(self.store.enforce_single_world("world"), "world")
        self.assert_code("world_scope", lambda: self.store.provision("other", "bob", 2))

    def test_internal_chat_reservation_shares_sequence_and_has_no_public_admission(self):
        _, alice = self.principal()
        self.assertEqual(self.store.accept_request(alice, "action", {})[0]["request_sequence"], 1)
        payload = {"rpc_type": "chat.publish", "message_id": "message-1", "delivery": {"body": "hi"}}
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store._transaction() as db:
                self.store.reserve_internal_in_transaction(db, "world", "message-1", payload)
                raise RuntimeError("rollback")
        self.assertEqual(self.store.max_request_sequence("world"), 1)
        internal = self.store.reserve_internal("world", "message-1", payload)
        self.assertEqual(internal["request_sequence"], 2)
        self.assertEqual(self.store.reserve_internal("world", "message-1", payload), internal)
        self.assertEqual(self.store.max_request_sequence("world"), 2)
        self.assert_code("idempotency_conflict", lambda: self.store.reserve_internal(
            "world", "message-1", {**payload, "delivery": {"body": "changed"}}
        ))
        self.assertEqual(self.store.get_internal("world", "message-1"), internal)
        backend_principal = self.store.get_principal(internal["run_id"])
        self.assertEqual(self.store.principals_for_world("world"), [alice])
        self.assertFalse(self.store.company_exists("world", 255))
        self.assert_code("forbidden", lambda: self.store.accept_request(backend_principal, "other", {}))
        self.store.update_request(backend_principal, "message-1", "completed")
        self.assertEqual(self.store.terminal_ack_prefix("world"), 0)
        self.store.update_request(alice, "action", "completed")
        self.assertEqual(self.store.terminal_ack_prefix("world"), 2)

    def test_old_draft_sequence_migration(self):
        legacy_path = os.path.join(self.tmp.name, "legacy.db")
        db = sqlite3.connect(legacy_path)
        db.executescript("""
            CREATE TABLE worlds (id TEXT PRIMARY KEY, epoch TEXT NOT NULL);
            INSERT INTO worlds VALUES ('old','initial');
            CREATE TABLE action_requests (
              world_id TEXT, identity_id TEXT, request_id TEXT, run_id TEXT,
              company_id INTEGER, mode TEXT, status TEXT, payload TEXT,
              result TEXT, error TEXT, engine_sequence INTEGER, tick INTEGER,
              checkpoint_state TEXT, created_at REAL, updated_at REAL,
              PRIMARY KEY(world_id,identity_id,request_id));
            INSERT INTO action_requests VALUES
              ('old','alice','b','run',1,'text','completed','{}',NULL,NULL,NULL,NULL,'uncheckpointed',2,2),
              ('old','alice','a','run',1,'text','completed','{}',NULL,NULL,NULL,NULL,'uncheckpointed',1,1);
            CREATE TABLE checkpoints (
              id INTEGER PRIMARY KEY AUTOINCREMENT, world_id TEXT, world_epoch TEXT,
              save_id TEXT, watermark INTEGER, tick INTEGER, save_sha256 TEXT,
              created_at REAL, UNIQUE(world_id,world_epoch,save_id));
            INSERT INTO checkpoints(world_id,world_epoch,save_id,watermark,tick,created_at)
              VALUES('old','initial','legacy-save',0,1,1);
        """)
        db.close()
        old = Store(legacy_path)
        try:
            self.assertEqual([(row["request_id"], row["request_sequence"])
                              for row in old.accepted_requests("old")], [("a", 1), ("b", 2)])
            self.assertEqual(old.max_request_sequence("old"), 2)
            self.assertIsNone(old.latest_checkpoint("old")["save_checksum"])
            self.assertTrue(all(row["engine_dispatched"] for row in old.accepted_requests("old")))
        finally:
            old.close()

    def test_visual_capture_marker_survives_restart_and_cannot_recapture(self):
        token = self.store.provision("world", "viewer", 1, mode="visual")
        principal = self.store.authenticate(token)
        self.store.accept_request(principal, "gesture", {
            "action": {"name": "ui.apply", "parameters": {"image_id": "image-1", "gesture": "click", "x": 1, "y": 2}},
            "preconditions": {},
        })
        capturing = self.store.begin_intent_capture(principal, "gesture", "worker-1")
        self.assertEqual(capturing["intent_state"], "capturing")
        self.assertEqual(capturing["expected_worker_instance_id"], "worker-1")
        self.assertIsNone(capturing["visual_intent"])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_request(principal, "gesture")["intent_state"], "capturing")
        self.assert_code("invalid_transition", lambda: self.store.begin_intent_capture(principal, "gesture", "worker-2"))
        self.store.update_request(principal, "gesture", "accepted", result={"fake": "outcome"})
        uncertain = self.store.mark_intent_uncertain(principal, "gesture")
        self.assertEqual((uncertain["intent_state"], uncertain["status"], uncertain["checkpoint_state"]),
                         ("uncertain", "uncertain", "uncertain"))
        self.assertIsNone(uncertain["result"])
        self.assert_code("invalid_transition", lambda: self.store.begin_intent_capture(principal, "gesture", "worker-2"))

    def test_visual_bundle_binding_size_and_immutability(self):
        token = self.store.provision("world", "viewer", 1, mode="visual")
        principal = self.store.authenticate(token)
        self.store.accept_request(principal, "gesture", {
            "action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
            "preconditions": {},
        })
        bundle = {"request_id": "gesture", "image_id": "image-1", "world_epoch": "initial",
                  "worker_instance_id": "worker", "authority_instance_id": "authority", "commands": []}
        self.assert_code("invalid_transition", lambda: self.store.persist_visual_intent(principal, "gesture", bundle))
        self.store.begin_intent_capture(principal, "gesture", "worker-1")
        for changed in ({**bundle, "request_id": "other"}, {**bundle, "image_id": "other"},
                        {**bundle, "world_epoch": "other"}):
            self.assert_code("invalid_intent", lambda changed=changed:
                             self.store.persist_visual_intent(principal, "gesture", changed))
        self.assert_code("visual_intent_limit", lambda: self.store.persist_visual_intent(
            principal, "gesture", {**bundle, "padding": "x" * 48000}
        ))
        record = self.store.persist_visual_intent(principal, "gesture", bundle)
        canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(record["visual_intent_sha256"], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertEqual(record["visual_intent"], bundle)
        self.assertEqual(self.store.persist_visual_intent(principal, "gesture", dict(reversed(list(bundle.items())))), record)
        self.assert_code("visual_intent_conflict", lambda: self.store.persist_visual_intent(
            principal, "gesture", {**bundle, "commands": [{"command": 1}]}
        ))

    def test_visual_ui_release_ack_is_monotonic_and_restarts_unsynced(self):
        token = self.store.provision("world", "viewer", 1, mode="visual")
        principal = self.store.authenticate(token)
        self.store.accept_request(principal, "gesture", {
            "action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
            "preconditions": {},
        })
        self.store.begin_intent_capture(principal, "gesture", "worker-1")
        self.store.persist_visual_intent(principal, "gesture", {
            "request_id": "gesture", "image_id": "image-1", "world_epoch": "initial",
        })
        self.store.update_request(principal, "gesture", "completed")
        self.assertEqual(len(self.store.unsynced_visual_requests("world")), 1)
        self.assert_code("invalid_transition", lambda: self.store.mark_ui_state(principal, "gesture", "acked"))
        released = self.store.mark_ui_state(principal, "gesture", "released")
        self.assertEqual(released["ui_state"], "released")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.unsynced_visual_requests("world")[0]["ui_state"], "released")
        self.assertEqual(self.store.mark_ui_state(principal, "gesture", "acked")["ui_state"], "acked")
        self.assertEqual(self.store.unsynced_visual_requests("world"), [])
        self.assert_code("invalid_transition", lambda: self.store.mark_ui_state(principal, "gesture", "released"))

    def test_visual_capture_abort_is_atomic_safe_local_terminal_and_cleanup_visible(self):
        token = self.store.provision("world", "viewer", 1, mode="visual")
        principal = self.store.authenticate(token)
        self.store.accept_request(principal, "gesture", {
            "action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
            "preconditions": {},
        })
        self.assert_code("invalid_intent", lambda: self.store.begin_intent_capture(principal, "gesture", ""))
        self.store.begin_intent_capture(principal, "gesture", "worker-before-rpc")
        self.assert_code("invalid_payload", lambda: self.store.abort_visual_capture(
            principal, "gesture", event={"bad": object()}
        ))
        self.assertEqual(self.store.get_request(principal, "gesture")["intent_state"], "capturing")
        aborted = self.store.abort_visual_capture(principal, "gesture",
                                                  event={"request_id": "gesture", "status": "rejected"})
        self.assertEqual((aborted["intent_state"], aborted["status"], aborted["error"]),
                         ("aborted", "rejected", {"code": "visual_capture_aborted"}))
        self.assertEqual(aborted["expected_worker_instance_id"], "worker-before-rpc")
        self.assertIsNone(aborted["result"])
        self.assertIsNone(aborted["visual_intent"])
        self.assertEqual(self.store.checkpoint_ack_prefix("world"), 1)
        self.assertEqual(len(self.store.unsynced_visual_requests("world")), 1)
        self.assertEqual(len(self.store.events(principal)["events"]), 1)
        self.assertEqual(self.store.abort_visual_capture(principal, "gesture"), aborted)
        self.assertEqual(len(self.store.events(principal)["events"]), 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.unsynced_visual_requests("world")[0]["intent_state"], "aborted")
        self.store.mark_ui_state(principal, "gesture", "released")
        self.store.mark_ui_state(principal, "gesture", "acked")
        self.assertEqual(self.store.unsynced_visual_requests("world"), [])

    def test_visual_capture_abort_loses_race_to_persisted_or_queued_bundle(self):
        token = self.store.provision("world", "viewer", 1, mode="visual")
        principal = self.store.authenticate(token)
        self.store.accept_request(principal, "gesture", {
            "action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
            "preconditions": {},
        })
        self.store.begin_intent_capture(principal, "gesture", "worker-1")
        bundle = {"request_id": "gesture", "image_id": "image-1", "world_epoch": "initial"}
        self.store.persist_visual_intent(principal, "gesture", bundle)
        self.assert_code("invalid_transition", lambda: self.store.abort_visual_capture(principal, "gesture"))
        self.assertEqual(self.store.get_request(principal, "gesture")["visual_intent"], bundle)
        self.store.update_request(principal, "gesture", "queued")
        self.assert_code("invalid_transition", lambda: self.store.abort_visual_capture(principal, "gesture"))
        self.assertEqual(self.store.get_request(principal, "gesture")["status"], "queued")

    def test_visual_abort_commits_after_revocation_without_projecting_event(self):
        token = self.store.provision("world", "viewer", 1, mode="visual")
        principal = self.store.authenticate(token)
        self.store.accept_request(principal, "gesture", {
            "action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
            "preconditions": {},
        })
        self.store.begin_intent_capture(principal, "gesture", "worker-1")
        self.store.revoke_membership("world", "viewer", 1)
        aborted = self.store.abort_visual_capture(principal, "gesture",
                                                  event={"request_id": "gesture", "status": "rejected"})
        self.assertEqual((aborted["intent_state"], aborted["status"]), ("aborted", "rejected"))
        self.assertEqual(self.store.checkpoint_ack_prefix("world"), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM projected_events WHERE run_id=?", (principal.run_id,)
        ).fetchone()[0], 0)

    def test_unsynced_visual_scan_pages_past_sixteen_blocked_rows(self):
        token = self.store.provision("world", "viewer", 1, mode="visual")
        principal = self.store.authenticate(token)
        for number in range(17):
            request_id = f"gesture-{number}"
            self.store.accept_request(principal, request_id, {
                "action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
                "preconditions": {},
            })
            self.store.begin_intent_capture(principal, request_id, "old-worker")
            self.store.abort_visual_capture(principal, request_id)
        first = self.store.unsynced_visual_requests("world", limit=16)
        self.assertEqual(len(first), 16)
        later = self.store.unsynced_visual_requests("world", limit=16,
                                                   after_sequence=first[-1]["request_sequence"])
        self.assertEqual([row["request_id"] for row in later], ["gesture-16"])
        self.assertEqual(self.store.unsynced_visual_requests(
            "world", after_sequence=later[-1]["request_sequence"]), [])
        self.assert_code("invalid_limit", lambda: self.store.unsynced_visual_requests(
            "world", after_sequence=-1))


if __name__ == "__main__":
    unittest.main()
