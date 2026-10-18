"""Broker recovery contract tests against the current C++ bridge wire shape.

The engine fixture models envelopes and state transitions; it does not run the
simulation. The Unix socket framing is exercised separately in test_bridge.
"""

import copy
import os
import tempfile
import unittest

from backend.errors import ServiceError
from backend.checkpoint import CheckpointPolicy
from backend.catalog import (CATALOG, CATALOG_ACTION_NAMES, CATALOG_QUERY_NAMES,
                             CATALOG_HASH_ALGORITHM, CATALOG_HASH)
from backend.service import Service
from backend.store import Store


class WireEngine:
    def __init__(self):
        self.calls = []
        self.instance = "boot-1"
        self.recovery_required = True
        self.checkpoint = {"checkpoint_id": "", "saved_tick": 0, "retired_through": 0,
                           "save_checksum": "", "recovery_required": True,
                           "status": "idle", "load_verified": True}
        self.live = {}
        self.live_checkpointed = set()
        self.reconciled = {}
        self.roster = []
        self.pending_chat = []
        self.event_pages = {}
        self.wrong_world = False
        self.chat_status = "completed"
        self.finish_faults = []
        self.checkpoint_status_reply_status = "completed"
        self.capabilities = {
            **{key: copy.deepcopy(value) for key, value in CATALOG.items()
               if key not in ("actions", "queries")},
            "actions": list(CATALOG_ACTION_NAMES), "queries": list(CATALOG_QUERY_NAMES),
            "catalog_hash_algorithm": CATALOG_HASH_ALGORITHM, "catalog_hash": CATALOG_HASH,
        }

    def call(self, request):
        self.calls.append(copy.deepcopy(request))
        kind = request["type"]
        result, status = {}, "completed"
        if kind == "hello":
            result = {"engine_instance_id": self.instance, "world_epoch": "initial",
                      "role": "world", "recovery_required": self.recovery_required,
                      "capabilities": copy.deepcopy(self.capabilities)}
        elif kind == "checkpoint.status":
            result = dict(self.checkpoint, recovery_required=self.recovery_required)
            status = self.checkpoint_status_reply_status
        elif kind == "checkpoint.create":
            if self.checkpoint["status"] == "saving":
                status = "rejected"
            else:
                self.checkpoint["status"] = "saving"
                status = "queued"
            result = {"checkpoint_id": "save-next"}
        elif kind == "recovery.reconcile":
            if not self.recovery_required:
                status = "rejected"  # RequestLedger refuses reconciliation when ready.
                result = {"error": {"code": "recovery_not_open"}}
            else:
                recovered = self.reconciled.get(request["request_sequence"], "uncertain")
                if isinstance(recovered, dict):
                    status = recovered["status"]
                    result = recovered.get("result", {
                        "request_id": request["request_id"],
                        "request_sequence": request["request_sequence"],
                        "checkpoint_state": "uncheckpointed"})
                else:
                    status = recovered
                    result = {"request_id": request["request_id"],
                              "request_sequence": request["request_sequence"],
                              "checkpoint_state": "uncheckpointed"}
        elif kind == "recovery.finish":
            fault = self.finish_faults.pop(0) if self.finish_faults else None
            if fault == "rejected":
                status = "rejected"
            elif fault == "lost_reply":
                self.recovery_required = False
                raise ServiceError("engine_unavailable", status=503)
            else:
                self.recovery_required = False
                result = {"ready": True}
        elif kind == "session.bind":
            result = {"bound": True}
        elif kind == "action.submit":
            self.live[request["request_sequence"]] = "queued"
            status = "queued"
            result = {"request_id": request["request_id"],
                      "request_sequence": request["request_sequence"]}
        elif kind == "action.status":
            status = self.live.get(request["request_sequence"], "uncertain")
            result = {"request_id": request["request_id"],
                      "request_sequence": request["request_sequence"], "ok": status == "completed",
                      "checkpoint_state": "checkpointed" if request["request_sequence"] in self.live_checkpointed
                                          else "uncheckpointed"}
        elif kind == "chat.publish":
            status = self.chat_status
            self.live[request["request_sequence"]] = status
            result = {"ok": status == "completed", "message_id": request["message"]["message_id"]}
        elif kind == "action.ack":
            result = {"retired_through": request["through_sequence"]}
        elif kind == "chat.roster":
            result = self.roster  # NativeRoster() returns a bare array.
        elif kind == "chat.pending":
            result = {"messages": self.pending_chat, "pending_count": len(self.pending_chat),
                      "has_more": False}
        elif kind == "chat.ack":
            result = {"ok": True, "acknowledged": len(request["parameters"]["ingress"])}
        elif kind == "events.poll":
            result = self.event_pages.get(request["after"], {"events": [], "cursor": request["after"],
                                                              "gap": False, "oldest_event_id": 1})
        else:
            raise AssertionError(f"Unmodelled bridge method: {kind}")
        reply = {"protocol_version": 1, "type": kind + ".result",
                 "correlation_id": request["correlation_id"], "status": status,
                 "result": result, "world_id": "other" if self.wrong_world else request["world_id"],
                 "world_epoch": request["world_epoch"], "engine_instance_id": self.instance,
                 "tick": 12}
        return reply

    def calls_of(self, kind):
        return [call for call in self.calls if call["type"] == kind]


class NativeSocialSpy:
    def __init__(self):
        self.roster = None
        self.admitted = []
        self.delivered = []

    def update_native_roster(self, _world_id, roster):
        self.roster = roster

    def admit_native(self, _world_id, message):
        self.admitted.append(message)
        return {"disposition": "accepted"}

    def mark_delivered(self, message_id, tick=None):
        self.delivered.append((message_id, tick))

    def mark_uncertain(self, message_id):
        raise AssertionError(f"Unexpected uncertain chat delivery: {message_id}")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class BrokerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "world.sqlite")
        self.store = Store(self.path, world_id="world-a")
        self.token = self.store.provision("world-a", "alice", 1, world_epoch="initial")
        self.principal = self.store.authenticate(self.token)
        self.engine = WireEngine()
        self.service = Service(self.store, self.engine, autostart=False, world_id="world-a")

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.temp.cleanup()

    def accept(self, request_id="r1", token=None):
        return self.service.handle("POST", "/v1/actions", token or self.token, {
            "request_id": request_id, "action": {"name": "road.build", "parameters": {"start": 1, "end": 2}}})

    def recover(self):
        hello = self.service._hello(self.principal, refresh=True)
        self.service._recover(self.principal, hello)
        return hello

    def saved_visual_request(self):
        """A visual action whose previous completion is in the loaded save."""
        self.recover()
        token = self.store.provision("world-a", "visual", 2, mode="visual")
        visual = self.store.authenticate(token)
        payload = {"action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
                   "preconditions": {}}
        self.store.accept_request(visual, "visual-1", payload)
        self.store.begin_intent_capture(visual, "visual-1", "worker-1")
        self.store.persist_visual_intent(visual, "visual-1", {
            "request_id": "visual-1", "image_id": "image-1", "world_epoch": "initial"})
        self.store.update_request(visual, "visual-1", "queued", result={"engine_instance_id": "boot-1"})
        self.store.update_request(visual, "visual-1", "completed", result={"ok": True})
        self.store.apply_checkpoint("world-a", {"world_epoch": "initial", "save_id": "save-visual",
            "retired_through": 0, "tick": 20, "save_checksum": "12345678901234567890"},
            [{"request_sequence": 1, "status": "completed"}])
        self.engine.instance = "boot-2"
        self.engine.recovery_required = True
        self.engine.checkpoint = {"checkpoint_id": "save-visual", "saved_tick": 20,
                                  "retired_through": 0, "save_checksum": "12345678901234567890",
                                  "status": "completed", "load_verified": True}
        return visual

    def published_successor_before_db_commit(self):
        """G is in SQLite; a new engine loaded native-published G+1."""
        self.recover()
        self.accept("saved")
        self.store.update_request(self.principal, "saved", "completed", result={"ok": True})
        old_checksum = "12345678901234567890"
        new_checksum = "98765432109876543210"
        # SQLite observed a live retired prefix of 1 for G. The native G
        # metadata had saved prefix 0; that immutable value is not in the
        # three-field parent DTO and must not be compared to SQLite's prefix.
        self.store.apply_checkpoint("world-a", {
            "world_epoch": "initial", "save_id": "save-g", "retired_through": 1,
            "tick": 20, "save_checksum": old_checksum,
        }, [{"request_sequence": 1, "status": "completed"}])
        self.accept("inflight")
        self.store.update_request(self.principal, "inflight", "queued",
                                  result={"engine_instance_id": "boot-1"})
        self.engine.instance = "boot-2"
        self.engine.recovery_required = True
        self.engine.checkpoint = {
            "checkpoint_id": "save-g-next", "saved_tick": 25,
            "checkpoint_retired_through": 1, "retired_through": 1,
            "save_checksum": new_checksum, "status": "completed", "load_verified": True,
            "parent_checkpoint": {"checkpoint_id": "save-g", "saved_tick": 20,
                                  "save_checksum": old_checksum},
        }
        self.engine.reconciled[2] = {"status": "completed", "result": {
            "request_id": "inflight", "request_sequence": 2,
            "checkpoint_state": "checkpointed", "ok": True}}
        return copy.deepcopy(self.engine.checkpoint)

    def assert_successor_stays_fenced(self):
        finished_before = len(self.engine.calls_of("recovery.finish"))
        with self.assertRaises(ServiceError):
            self.recover()
        self.assertEqual(self.store.latest_checkpoint("world-a")["save_id"], "save-g")
        self.assertEqual(len(self.engine.calls_of("recovery.finish")), finished_before)
        self.assertTrue(self.engine.recovery_required)

    def test_first_hello_flat_checkpoint_and_single_world_fences(self):
        hello = self.recover()
        self.assertEqual(hello["engine_instance_id"], "boot-1")
        with self.store.lock:
            state = self.store.connection.execute(
                "SELECT recovery_state FROM worlds WHERE id=?", ("world-a",)).fetchone()[0]
        self.assertEqual(state, "ready")
        self.assertNotIn("actor", self.engine.calls_of("checkpoint.status")[0])
        self.assertEqual(len(self.engine.calls_of("recovery.finish")), 1)
        self.engine.wrong_world = True
        with self.assertRaises(ServiceError) as caught:
            self.service._hello(self.principal, refresh=True)
        self.assertEqual(caught.exception.code, "world_recovering")
        with self.assertRaises(ServiceError):
            self.store.provision("world-b", "bob", 2)

    def test_catalog_hash_mismatch_clears_cached_hello_before_broker_dispatch(self):
        self.recover()
        self.accept("historical")
        self.store.update_request(self.principal, "historical", "completed",
                                  result={"success": True, "cost": 0})
        historical = self.store.get_request(self.principal, "historical")
        self.accept("new")
        before = {kind: len(self.engine.calls_of(kind)) for kind in
                  ("checkpoint.status", "recovery.reconcile", "action.submit", "checkpoint.create")}
        self.assertIsNotNone(self.service._hello_value)
        self.engine.capabilities["catalog_hash"] = "0" * len(CATALOG_HASH)
        with self.assertRaises(ServiceError) as caught:
            self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(caught.exception.code, "catalog_mismatch")
        self.assertIsNone(self.service._hello_value)
        self.assertEqual({kind: len(self.engine.calls_of(kind)) for kind in before}, before)
        self.assertEqual(self.store.get_request(self.principal, "historical"), historical)
        self.assertEqual(self.store.get_request(self.principal, "new")["status"], "accepted")

    def test_pending_request_has_sequence_and_ack_waits_for_checkpoint(self):
        self.recover()
        self.accept()
        record = self.store.get_request(self.principal, "r1")
        self.assertEqual(record["request_sequence"], 1)
        hello = self.service._hello(self.principal)
        self.service._process_record(self.principal, record, hello)
        self.assertEqual(self.engine.calls_of("action.submit")[0]["request_sequence"], 1)
        self.assertEqual(self.store.terminal_ack_prefix("world-a"), 0)
        self.engine.live[1] = "completed"
        self.service._process_record(self.principal, self.store.get_request(self.principal, "r1"), hello)
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(self.engine.calls_of("action.ack"), [])
        self.engine.checkpoint = {"checkpoint_id": "save-1", "saved_tick": 12,
                                  "retired_through": 0, "save_checksum": "12345678901234567890",
                                  "status": "completed", "load_verified": True}
        self.engine.live_checkpointed.add(1)
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(self.engine.calls_of("action.ack")[-1]["through_sequence"], 1)

    def test_backend_restart_same_engine_uses_live_status_not_recovery_reconcile(self):
        self.recover()
        self.accept()
        self.service._process_record(self.principal, self.store.get_request(self.principal, "r1"))
        self.assertEqual(self.store.get_request(self.principal, "r1")["status"], "queued")
        self.service.close()
        self.store.close()
        self.store = Store(self.path, world_id="world-a")
        self.service = Service(self.store, self.engine, autostart=False, world_id="world-a")
        self.engine.live[1] = "completed"
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(self.store.get_request(self.principal, "r1")["status"], "completed")
        self.assertEqual(self.engine.calls_of("recovery.reconcile"), [])
        self.assertTrue(self.engine.calls_of("action.status"))

    def test_backend_only_restart_adopts_native_successor_with_same_live_engine(self):
        self.recover()
        self.accept("saved")
        self.store.update_request(self.principal, "saved", "completed", result={"ok": True})
        self.store.apply_checkpoint("world-a", {
            "world_epoch": "initial", "save_id": "save-g", "retired_through": 0,
            "tick": 20, "save_checksum": "12345678901234567890",
        }, [{"request_sequence": 1, "status": "completed"}])
        self.engine.checkpoint = {
            "checkpoint_id": "save-g-next", "saved_tick": 25,
            "checkpoint_retired_through": 0, "retired_through": 0,
            "save_checksum": "98765432109876543210", "status": "completed",
            "load_verified": True,
            "parent_checkpoint": {"checkpoint_id": "save-g", "saved_tick": 20,
                                  "save_checksum": "12345678901234567890"},
        }
        self.service.close()
        self.store.close()
        self.store = Store(self.path, world_id="world-a")
        self.service = Service(self.store, self.engine, autostart=False, world_id="world-a")
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(self.store.latest_checkpoint("world-a")["save_id"], "save-g-next")
        self.assertEqual(self.engine.calls_of("recovery.reconcile"), [])

    def test_new_engine_adopts_verified_direct_successor_after_native_publication(self):
        self.published_successor_before_db_commit()
        self.recover()
        checkpoint = self.store.latest_checkpoint("world-a")
        self.assertEqual((checkpoint["save_id"], checkpoint["tick"], checkpoint["save_checksum"]),
                         ("save-g-next", 25, "98765432109876543210"))
        self.assertEqual(self.store.get_engine_state("world-a"),
                         {"engine_instance_id": "boot-2", "retired_through": 1})
        self.assertEqual([call["request_sequence"] for call in self.engine.calls_of("recovery.reconcile")], [2])
        record = self.store.get_request(self.principal, "inflight")
        self.assertEqual((record["status"], record["checkpoint_state"]),
                         ("completed", "checkpointed"))
        self.assertFalse(self.engine.recovery_required)

    def test_unlinked_or_invalid_successor_stays_fenced(self):
        original = self.published_successor_before_db_commit()
        cases = {
            "missing parent": lambda cp: cp.pop("parent_checkpoint"),
            "null parent": lambda cp: cp.update(parent_checkpoint=None),
            "extra parent field": lambda cp: cp["parent_checkpoint"].update(retired_through=0),
            "typed parent": lambda cp: cp["parent_checkpoint"].update(saved_tick=True),
            "wrong parent id": lambda cp: cp["parent_checkpoint"].update(checkpoint_id="other-save"),
            "wrong parent tick": lambda cp: cp["parent_checkpoint"].update(saved_tick=19),
            "wrong parent checksum": lambda cp: cp["parent_checkpoint"].update(save_checksum="other"),
            "older current tick": lambda cp: cp.update(saved_tick=19),
            "unverified load": lambda cp: cp.update(load_verified=False),
            "boolean live prefix": lambda cp: cp.update(retired_through=True),
            "boolean saved prefix": lambda cp: cp.update(checkpoint_retired_through=True),
            "missing saved prefix": lambda cp: cp.pop("checkpoint_retired_through"),
            "saved prefix beyond live": lambda cp: cp.update(checkpoint_retired_through=2),
            "live prefix beyond DB": lambda cp: cp.update(retired_through=3),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                checkpoint = copy.deepcopy(original)
                mutate(checkpoint)
                self.engine.checkpoint = checkpoint
                self.assert_successor_stays_fenced()

    def test_rejected_native_finish_rescans_idempotently_without_resubmission(self):
        self.published_successor_before_db_commit()
        finishes_before = len(self.engine.calls_of("recovery.finish"))
        self.engine.finish_faults = ["rejected"]
        with self.assertRaises(ServiceError):
            self.recover()
        self.assertEqual(self.store.latest_checkpoint("world-a")["save_id"], "save-g-next")
        self.assertEqual(len(self.engine.calls_of("recovery.reconcile")), 1)
        before = self.store.get_request(self.principal, "inflight")
        self.assertTrue(self.engine.recovery_required)
        self.recover()
        reconciles = self.engine.calls_of("recovery.reconcile")
        self.assertEqual(len(reconciles), 2)
        self.assertEqual([{key: value for key, value in call.items() if key != "correlation_id"}
                          for call in reconciles],
                         [{key: value for key, value in reconciles[0].items() if key != "correlation_id"}] * 2)
        after = self.store.get_request(self.principal, "inflight")
        self.assertEqual((after["status"], after["checkpoint_state"], after["result"]),
                         (before["status"], before["checkpoint_state"], before["result"]))
        self.assertEqual(self.engine.calls_of("action.submit"), [])
        self.assertEqual(len(self.engine.calls_of("recovery.finish")), finishes_before + 2)
        self.assertFalse(self.engine.recovery_required)

    def test_backend_restart_after_lost_native_finish_reply_does_not_resubmit(self):
        self.published_successor_before_db_commit()
        self.engine.finish_faults = ["lost_reply"]
        with self.assertRaises(ServiceError):
            self.recover()
        self.assertEqual(self.store.latest_checkpoint("world-a")["save_id"], "save-g-next")
        reconciles = len(self.engine.calls_of("recovery.reconcile"))
        self.service.close()
        self.store.close()
        self.store = Store(self.path, world_id="world-a")
        self.service = Service(self.store, self.engine, autostart=False, world_id="world-a")
        self.recover()
        self.assertEqual(len(self.engine.calls_of("recovery.reconcile")), reconciles)
        self.assertEqual(self.engine.calls_of("action.submit"), [])
        self.assertEqual(self.store.get_engine_state("world-a")["engine_instance_id"], "boot-2")

    def test_new_boot_can_rebind_interrupted_verified_successor_recovery(self):
        self.published_successor_before_db_commit()
        self.engine.reconciled[2] = {"status": "completed", "result": {}}
        with self.assertRaises(ServiceError):
            self.recover()
        self.assertEqual(self.store.latest_checkpoint("world-a")["save_id"], "save-g")
        self.engine.instance = "boot-3"
        self.engine.reconciled[2] = {"status": "completed", "result": {
            "request_id": "inflight", "request_sequence": 2,
            "checkpoint_state": "checkpointed", "ok": True}}
        self.recover()
        self.assertEqual(self.store.latest_checkpoint("world-a")["save_id"], "save-g-next")
        self.assertEqual(self.store.get_engine_state("world-a")["engine_instance_id"], "boot-3")

    def test_failed_checkpoint_status_cannot_start_another_save(self):
        clock = FakeClock()
        self.service.checkpoints = CheckpointPolicy(clock=clock)
        self.recover()
        clock.advance(31)
        self.engine.checkpoint_status_reply_status = "rejected"
        with self.assertRaises(ServiceError):
            self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(self.engine.calls_of("checkpoint.create"), [])

    def test_existing_dispatched_world_rejects_fresh_engine_without_verified_save(self):
        self.recover()
        self.accept()
        self.service._process_record(self.principal, self.store.get_request(self.principal, "r1"))
        self.engine.instance = "fresh-no-save"
        self.engine.recovery_required = False
        with self.assertRaises(ServiceError) as caught:
            self.recover()
        self.assertEqual(caught.exception.code, "loaded_checkpoint_unverified")
        with self.assertRaises(ServiceError) as admission:
            self.accept("new")
        self.assertEqual(admission.exception.code, "world_recovering")
        self.assertEqual(len(self.engine.calls_of("action.submit")), 1)

    def test_empty_new_backend_can_start_against_fresh_engine(self):
        self.engine.recovery_required = False
        self.engine.instance = "fresh-world"
        self.recover()
        self.assertEqual(self.store.get_engine_state("world-a")["engine_instance_id"], "fresh-world")
        self.assertEqual(self.accept("first")[0], 202)

    def test_verified_empty_native_checkpoint_cannot_erase_dispatched_db_request(self):
        self.accept("sent")
        # Model a committed dispatch marker with no DB checkpoint or recorded
        # engine instance. An empty native load cannot prove that work rolled back.
        self.store.update_request(self.principal, "sent", "queued",
                                  result={"engine_instance_id": "previous-boot"})
        self.assertIsNone(self.store.get_engine_state("world-a")["engine_instance_id"])
        self.assertTrue(self.store.has_dispatched_requests("world-a"))
        self.engine.instance = "fresh-boot"
        self.engine.recovery_required = True
        self.engine.checkpoint["load_verified"] = True
        with self.assertRaises(ServiceError) as caught:
            self.recover()
        self.assertEqual(caught.exception.code, "loaded_checkpoint_unverified")
        self.assertIsNone(self.store.latest_checkpoint("world-a"))
        self.assertEqual(self.store.get_request(self.principal, "sent")["status"], "queued")
        self.assertEqual(self.engine.calls_of("recovery.reconcile"), [])
        self.assertEqual(self.engine.calls_of("recovery.finish"), [])

    def test_periodic_quiescence_drains_queued_before_create_and_releases_after_save(self):
        clock = FakeClock()
        self.service.checkpoints = CheckpointPolicy(clock=clock)
        self.recover()
        self.accept("first")
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(self.store.get_request(self.principal, "first")["status"], "queued")
        clock.advance(31)
        self.accept("second")
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertTrue(self.service.checkpoints.quiescing)
        self.assertEqual(len(self.engine.calls_of("action.submit")), 1)
        self.assertEqual(self.engine.calls_of("checkpoint.create"), [])
        self.engine.live[1] = "completed"
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(len(self.engine.calls_of("checkpoint.create")), 1)
        self.assertEqual(len(self.engine.calls_of("action.submit")), 1)
        self.assertTrue(self.service.checkpoints.saving)
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertEqual(len(self.engine.calls_of("checkpoint.create")), 1)
        self.engine.checkpoint = {"checkpoint_id": "save-next", "saved_tick": 15,
                                  "retired_through": 0, "save_checksum": "12345678901234567890",
                                  "status": "completed", "load_verified": True}
        self.engine.live_checkpointed.add(1)
        self.service._process_world("world-a", self.store.pending_requests())
        self.assertFalse(self.service.checkpoints.quiescing)
        self.assertEqual(len(self.engine.calls_of("action.submit")), 2)
        self.assertEqual(self.store.get_request(self.principal, "first")["checkpoint_state"], "checkpointed")

    def test_overdue_checkpoint_blocks_new_admission_but_preserves_retry_receipt(self):
        clock = FakeClock()
        self.service.checkpoints = CheckpointPolicy(clock=clock)
        accepted = self.accept("existing")
        clock.advance(121)
        self.assertTrue(self.service.checkpoints.admission_blocked)
        self.assertEqual(self.accept("existing"), accepted)
        with self.assertRaises(ServiceError) as blocked:
            self.accept("new")
        self.assertEqual((blocked.exception.code, blocked.exception.status),
                         ("world_checkpoint_unavailable", 503))

    def test_new_engine_reconciles_queued_and_corrects_uncheckpointed_completion(self):
        self.recover()
        # The new engine loads the last verified save, which predates both
        # actions. A fresh engine without that generation must stay fenced.
        self.store.apply_checkpoint("world-a", {"world_epoch": "initial", "save_id": "save-0",
            "retired_through": 0, "tick": 0, "save_checksum": "12345678901234567890"}, [])
        self.accept("queued")
        self.service._process_record(self.principal, self.store.get_request(self.principal, "queued"))
        self.accept("completed")
        self.service._process_record(self.principal, self.store.get_request(self.principal, "completed"))
        self.assertEqual(self.store.get_request(self.principal, "completed")["status"], "queued")
        self.engine.live[2] = "completed"
        self.service._process_record(self.principal, self.store.get_request(self.principal, "completed"))
        self.assertEqual(self.store.get_request(self.principal, "completed")["status"], "completed")
        self.engine.instance = "boot-2"
        self.engine.recovery_required = True
        self.engine.checkpoint = {"checkpoint_id": "save-0", "saved_tick": 0,
                                  "retired_through": 0, "save_checksum": "12345678901234567890",
                                  "status": "completed", "load_verified": True}
        self.engine.reconciled = {1: "uncertain", 2: "uncertain"}
        self.recover()
        self.assertEqual(self.store.get_request(self.principal, "queued")["status"], "uncertain")
        corrected = self.store.get_request(self.principal, "completed")
        self.assertEqual((corrected["status"], corrected["checkpoint_state"]), ("uncertain", "uncertain"))
        self.assertEqual([call["request_sequence"] for call in self.engine.calls_of("recovery.reconcile")], [1, 2])

    def test_sparse_receipt_marks_only_listed_request_durable(self):
        self.accept("first")
        self.accept("second")
        first, second = (self.store.get_request(self.principal, name) for name in ("first", "second"))
        for name in ("first", "second"):
            self.store.update_request(self.principal, name, "completed", result={"ok": True})
        self.store.apply_checkpoint("world-a", {"world_epoch": "initial", "save_id": "save-1",
            "retired_through": 0, "tick": 20, "save_sha256": "a" * 64},
            [{"request_sequence": second["request_sequence"], "status": "completed"}])
        self.assertEqual(self.store.get_request(self.principal, "first")["checkpoint_state"], "uncheckpointed")
        self.assertEqual(self.store.get_request(self.principal, "second")["checkpoint_state"], "checkpointed")
        self.assertEqual(self.store.terminal_ack_prefix("world-a"), 2)
        self.assertEqual(first["request_sequence"], 1)

    def test_live_checkpoint_queries_each_terminal_key_for_sparse_receipts(self):
        self.recover()
        for name in ("first", "second"):
            self.accept(name)
            self.service._process_record(self.principal, self.store.get_request(self.principal, name))
            sequence = self.store.get_request(self.principal, name)["request_sequence"]
            self.engine.live[sequence] = "completed"
            self.service._process_record(self.principal, self.store.get_request(self.principal, name))
        self.engine.checkpoint = {"checkpoint_id": "save-live", "saved_tick": 31,
                                  "retired_through": 0, "save_checksum": "12345678901234567890"}
        self.engine.live_checkpointed.add(2)
        before = len(self.engine.calls_of("action.status"))
        self.service._checkpoint_sync(self.service._hello(self.principal))
        queried = self.engine.calls_of("action.status")[before:]
        self.assertEqual([call["request_sequence"] for call in queried], [1, 2])
        self.assertEqual(self.store.get_request(self.principal, "first")["checkpoint_state"], "uncheckpointed")
        self.assertEqual(self.store.get_request(self.principal, "second")["checkpoint_state"], "checkpointed")
        self.assertEqual(self.store.latest_checkpoint("world-a")["save_id"], "save-live")

    def test_live_ack_after_save_does_not_change_immutable_checkpoint_prefix(self):
        self.recover()
        self.accept("saved")
        self.store.update_request(self.principal, "saved", "completed", result={"ok": True})
        self.engine.live[1] = "completed"
        self.engine.live_checkpointed.add(1)
        # The save captured prefix 0, then the live engine acknowledged 1.
        self.engine.checkpoint = {
            "checkpoint_id": "save-live-ahead", "saved_tick": 31,
            "checkpoint_retired_through": 0, "retired_through": 1,
            "save_checksum": "12345678901234567890", "status": "completed",
            "load_verified": True, "parent_checkpoint": None,
        }
        self.service._checkpoint_sync(self.service._hello(self.principal))
        checkpoint = self.store.latest_checkpoint("world-a")
        self.assertEqual((checkpoint["watermark"], checkpoint["retired_through"]), (0, 0))
        self.assertEqual(self.store.get_engine_state("world-a")["retired_through"], 1)
        self.assertEqual(self.store.get_request(self.principal, "saved")["checkpoint_state"],
                         "checkpointed")

    def test_recovered_receipt_reads_nested_engine_checkpoint_state(self):
        self.recover()
        self.accept()
        self.service._process_record(self.principal, self.store.get_request(self.principal, "r1"))
        self.store.apply_checkpoint("world-a", {"world_epoch": "initial", "save_id": "save-1",
            "retired_through": 0, "tick": 20, "save_checksum": "9876543210987654321"}, [])
        self.engine.instance = "boot-2"
        self.engine.recovery_required = True
        # EntryReply() carries checkpoint_state inside result; CheckpointInfo()
        # carries a flat save manifest but no sparse receipt array.
        self.engine.reconciled[1] = {"status": "completed", "result": {
            "request_id": "r1", "request_sequence": 1, "checkpoint_state": "checkpointed", "ok": True}}
        self.engine.checkpoint = {"checkpoint_id": "save-1", "saved_tick": 20,
                                  "retired_through": 0, "save_checksum": "9876543210987654321",
                                  "status": "completed", "load_verified": True}
        self.recover()
        record = self.store.get_request(self.principal, "r1")
        self.assertEqual((record["status"], record["checkpoint_state"]),
                         ("completed", "checkpointed"))

    def test_visual_semantic_recovery_and_checkpoint_status_need_no_intent(self):
        self.recover()
        token = self.store.provision("world-a", "visual", 2, mode="visual")
        actor = self.store.authenticate(token)
        payload = {"action": {"name": "road.build", "parameters": {"start": 1, "end": 2}},
                   "preconditions": {}}
        self.store.accept_request(actor, "visual-semantic", payload)
        self.store.update_request(actor, "visual-semantic", "queued",
                                  result={"engine_instance_id": "boot-1"})
        self.store.apply_checkpoint("world-a", {"world_epoch": "initial", "save_id": "save-1",
            "retired_through": 0, "tick": 20, "save_checksum": "9876543210987654321"}, [])
        self.engine.instance = "boot-2"
        self.engine.recovery_required = True
        self.engine.reconciled[1] = {"status": "completed", "result": {
            "request_id": "visual-semantic", "request_sequence": 1,
            "checkpoint_state": "uncheckpointed", "success": True}}
        self.engine.checkpoint = {"checkpoint_id": "save-1", "saved_tick": 20,
                                  "retired_through": 0, "save_checksum": "9876543210987654321",
                                  "status": "completed", "load_verified": True}
        self.recover()
        restored = self.store.get_request(actor, "visual-semantic")
        self.assertEqual((restored["status"], restored["intent_state"]), ("completed", "none"))
        self.assertIsNone(restored["visual_intent"])
        reconciled = self.engine.calls_of("recovery.reconcile")[-1]
        self.assertEqual(reconciled["action"], payload["action"])
        self.assertNotIn("visual_intent", reconciled)

        self.engine.live[1] = "completed"
        self.engine.live_checkpointed.add(1)
        self.engine.checkpoint = {"checkpoint_id": "save-2", "saved_tick": 31,
                                  "retired_through": 0, "save_checksum": "12345678901234567890",
                                  "status": "completed", "load_verified": True}
        before = len(self.engine.calls_of("action.status"))
        self.service._checkpoint_sync(self.service._hello(actor))
        queried = self.engine.calls_of("action.status")[before:]
        self.assertEqual(len(queried), 1)
        self.assertNotIn("visual_intent", queried[0])
        self.assertEqual(self.store.get_request(actor, "visual-semantic")["checkpoint_state"], "checkpointed")

    def test_empty_protocol_rejection_cannot_erase_saved_visual_completion(self):
        visual = self.saved_visual_request()
        # This is the old C++ visual-mode gate's rejected RPC shape. It has
        # no EntryReply identity, so it is not a gameplay rejection receipt.
        self.engine.reconciled[1] = {"status": "rejected", "result": {}}
        finished_before = len(self.engine.calls_of("recovery.finish"))
        with self.assertRaises(ServiceError) as caught:
            self.recover()
        self.assertEqual(caught.exception.code, "recovery_receipt_unverified")
        record = self.store.get_request(visual, "visual-1")
        self.assertEqual((record["status"], record["checkpoint_state"]),
                         ("completed", "checkpointed"))
        self.assertEqual(len(self.engine.calls_of("recovery.finish")), finished_before)

    def test_mismatched_recovery_receipt_identity_cannot_correct_saved_visual_result(self):
        visual = self.saved_visual_request()
        for bad_result in ({"request_id": "someone-else", "request_sequence": 1},
                           {"request_id": "visual-1", "request_sequence": 2},
                           {"request_id": "visual-1", "request_sequence": True}):
            with self.subTest(receipt=bad_result):
                self.engine.reconciled[1] = {"status": "uncertain", "result": bad_result}
                finished_before = len(self.engine.calls_of("recovery.finish"))
                with self.assertRaises(ServiceError) as caught:
                    self.recover()
                self.assertEqual(caught.exception.code, "recovery_receipt_unverified")
                record = self.store.get_request(visual, "visual-1")
                self.assertEqual((record["status"], record["checkpoint_state"]),
                                 ("completed", "checkpointed"))
                self.assertEqual(len(self.engine.calls_of("recovery.finish")), finished_before)

    def test_visual_result_and_event_projection_never_forward_raw_fields(self):
        visual_token = self.store.provision("world-a", "visual", 2, mode="visual")
        visual = self.store.authenticate(visual_token)
        visual_action = {"action": {"name": "tool.apply", "parameters": {"tool": "road.build",
            "view_id": "view-1", "screen_target": {"x": 2, "y": 3}}},
            "preconditions": {"renderer_generation": "g1", "view_revision": "r1"}}
        with self.assertRaises(ServiceError) as denied:
            self.service.handle("POST", "/v1/actions", visual_token,
                                {"request_id": "v1", "action": visual_action["action"],
                                 "preconditions": {}})
        self.assertEqual(denied.exception.code, "unsupported_action")
        # An imported or contaminated durable row still cannot disclose raw
        # engine data through public retry/status and event projections.
        self.store.accept_request(visual, "v1", visual_action)
        self.store.update_request(visual, "v1", "rejected", result={"money": 900, "save_path": "/secret"},
                                  error={"code": "stale", "detail": "hidden tile"})
        self.store.append_event(visual, "action.status", {"request_id": "v1", "money": 900})
        expected = {"request_id": "v1", "status": "rejected",
                    "checkpoint_state": "uncheckpointed", "result": {},
                    "error": {"code": "stale"}}
        self.assertEqual(self.service.handle("GET", "/v1/actions/v1", visual_token)[1], expected)
        self.assertTrue(all(event["data"] == expected
                            for event in self.service.events(visual_token)["events"]))

    def test_private_event_is_quarantined_and_later_cursor_advances(self):
        self.recover()
        self.engine.event_pages[0] = {"events": [
            {"event_id": 1, "type": "chat.message", "data": {"body": "private secret"}},
            {"event_id": 2, "type": "action.completed", "result": {"ok": True}}],
            "cursor": 2, "gap": False, "oldest_event_id": 1}
        hello = self.service._hello(self.principal)
        self.service._poll_events(self.principal, hello)
        with self.store.lock:
            offset = self.store.connection.execute("SELECT cursor FROM broker_offsets WHERE world_id=?",
                                                   ("world-a",)).fetchone()[0]
            quarantined = self.store.connection.execute("SELECT COUNT(*) FROM broker_quarantine").fetchone()[0]
        self.assertEqual((offset, quarantined), (2, 1))
        self.assertEqual(self.service.events(self.token)["events"], [])
        self.service._poll_events(self.principal, hello)
        self.assertEqual(self.engine.calls_of("events.poll")[-1]["after"], 2)

    def test_native_chat_uses_bare_roster_wrapped_pending_and_nested_ack(self):
        spy = NativeSocialSpy()
        self.service.social = spy
        self.recover()
        self.engine.roster = [{"public_key": "a" * 64, "company_id": 1, "label": "Alice", "kind": "human"}]
        self.engine.pending_chat = [{"type": "chat.received", "event_id": "boot-1:1", "data": {
            "message_id": "boot-1:1", "body": "hello", "tick": 5, "scope": "world",
            "sender": self.engine.roster[0]}}]
        self.service._native_chat(self.principal, self.service._hello(self.principal))
        self.assertEqual(spy.roster, self.engine.roster)
        self.assertEqual(spy.admitted[0]["body"], "hello")
        self.assertEqual(self.engine.calls_of("chat.ack")[-1]["parameters"],
                         {"ingress": [{"message_id": "boot-1:1", "accepted": True}]})

    def test_internal_chat_uses_shared_sequence_and_exact_delivery_message(self):
        spy = NativeSocialSpy()
        self.service.social = spy
        self.recover()
        delivery = {"message_id": "msg-1", "delivery_request_id": "msg-1", "body": "hello",
                    "sender": {"label": "Bot", "kind": "agent", "company_id": 1},
                    "recipient_public_keys": []}
        record = self.store.reserve_internal("world-a", "chat:msg-1", {
            "rpc_type": "chat.publish", "message_id": "msg-1", "delivery": delivery})
        self.assertEqual(record["request_sequence"], 1)
        actor = self.store.get_principal(record["run_id"])
        self.service._process_record(actor, record, self.service._hello(self.principal))
        submitted = self.engine.calls_of("chat.publish")[-1]
        self.assertNotIn("actor", submitted)
        self.assertEqual(submitted["request_sequence"], 1)
        self.assertEqual(submitted["message"], delivery)
        self.assertEqual(set(submitted["message"]), {"message_id", "delivery_request_id", "body",
                                                    "sender", "recipient_public_keys"})
        self.assertEqual(self.store.get_internal("world-a", "chat:msg-1")["status"], "completed")
        self.assertEqual(spy.delivered, [("msg-1", 12)])

    def test_internal_chat_status_uses_request_type_sentinel(self):
        spy = NativeSocialSpy()
        self.service.social = spy
        self.recover()
        delivery = {"message_id": "msg-2", "delivery_request_id": "msg-2", "body": "hello",
                    "sender": {"label": "Bot", "kind": "agent", "company_id": 1},
                    "recipient_public_keys": []}
        record = self.store.reserve_internal("world-a", "chat:msg-2", {
            "rpc_type": "chat.publish", "message_id": "msg-2", "delivery": delivery})
        actor = self.store.get_principal(record["run_id"])
        self.engine.chat_status = "queued"
        self.service._process_record(actor, record, self.service._hello(self.principal))
        self.assertEqual(self.store.get_internal("world-a", "chat:msg-2")["status"], "queued")
        self.engine.live[1] = "completed"
        self.service._process_record(actor, self.store.get_internal("world-a", "chat:msg-2"),
                                     self.service._hello(self.principal))
        status_request = self.engine.calls_of("action.status")[-1]
        self.assertNotIn("actor", status_request)
        self.assertEqual(status_request["request_type"], "chat.publish")
        self.assertEqual(status_request["message"], delivery)
        self.assertEqual(self.store.get_internal("world-a", "chat:msg-2")["status"], "completed")
        self.assertEqual(spy.delivered, [("msg-2", 12)])


if __name__ == "__main__":
    unittest.main()
