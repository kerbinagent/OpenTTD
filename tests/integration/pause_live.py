"""Opt-in public pause/status and native-policy gameplay contract.

Uses a disposable regression authority and real HTTP backend, without a renderer.
The operator pauses and resumes only this fixture's native authority. This is technical
validation, separate from autonomous business play. Run with the same explicit
binary/assets environment as shared_capture_live.py; default ports are 25978/18106.
"""

from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unittest
import uuid

from fixture_plans import MODES
from public_services_live import LifecycleWorld
import shared_capture_live as shared


class PauseWorld(LifecycleWorld):
    allowed_console_commands = LifecycleWorld.allowed_console_commands | {"pause"}


class SharedPauseLive(shared.SharedCaptureActuators):
    world_type = PauseWorld

    def setUp(self):
        super().setUp()
        self.evidence["shared_fixture_source_sha256"] = self.evidence["test_source_sha256"]
        self.evidence["test_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        helper = Path(__file__).parent / "public_services_live.py"
        self.evidence["lifecycle_source_sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()

    def assert_pause(self, value):
        self.assertEqual(set(value), {"paused", "reasons", "command_policy", "revision"})
        self.assertIs(type(value["paused"]), bool)
        self.assertIs(type(value["revision"]), int)
        self.assertGreaterEqual(value["revision"], 1)

    def snapshot(self, *, audit=False):
        value = self.request("GET", "/v1/world/status", audit=audit)
        self.assertEqual(set(value), {"world_id", "world_epoch", "engine_instance_id", "pause", "event_cursor"})
        self.assertEqual(value["world_id"], self.world.world_id)
        self.assertEqual(value["world_epoch"], self.world.epoch)
        self.assertEqual(value["engine_instance_id"], self.world.instance)
        self.assert_pause(value["pause"])
        self.evidence.setdefault("pause_snapshots", []).append({"audit": audit, "value": value})
        return value

    def matching_transition(self, event, snapshot, paused):
        self.assertEqual(set(event), {"id", "type", "data"})
        data = event["data"]
        required = {"world_id", "world_epoch", "engine_instance_id", "pause"}
        self.assertTrue(required <= set(data))
        self.assertLessEqual(set(data), required | {"baseline", "history_gap"})
        for field in ("world_id", "world_epoch", "engine_instance_id"):
            self.assertEqual(data[field], snapshot[field])
        self.assert_pause(data["pause"])
        return (data["pause"]["paused"] is paused and
                (not paused or "manual" in data["pause"]["reasons"]) and
                data["pause"]["revision"] > snapshot["pause"]["revision"])

    def pause_events(self, snapshot, *, paused, audit=False):
        cursor = snapshot["event_cursor"]
        def poll():
            nonlocal cursor
            page = self.request("GET", "/v1/events", query={"after": cursor, "limit": 100}, audit=audit)
            cursor = page["cursor"]
            for event in page["events"]:
                if event["type"] != "world.status":
                    continue
                self.evidence.setdefault("pause_events", []).append({"audit": audit, "event": event})
                if self.matching_transition(event, snapshot, paused):
                    return event
        return self.wait("public pause transition", poll)

    def sse_transition(self, snapshot, *, paused):
        deadline = time.monotonic() + 10
        with closing(self.player.iter_events(snapshot["event_cursor"], reconnect=False,
                                            stop=lambda: time.monotonic() >= deadline)) as stream:
            for count, event in enumerate(stream):
                self.assertLess(count, 100, "Bounded fixture event history")
                if event["type"] == "world.status" and self.matching_transition(event, snapshot, paused):
                    self.evidence.setdefault("sse_subscriptions", []).append({
                        "from_snapshot": snapshot, "received": event})
                    return event
        self.fail("SSE pause transition was not delivered")

    def test_pause_status_events_native_policy_and_checkpoint(self):
        self.evidence["scope"] = "Technical shared pause metadata, native allowed actions and durable retry; no autonomous business claim"
        self.evidence["not_covered"] = ["other saved pause policies (covered by native unit cases)", "native queued-before-pause publication race", "renderer GUI command capture", "process restart and event-ring overflow", "automatic stream recovery and expired cursor"]
        initial = self.snapshot()
        initial_text = self.snapshot(audit=True)
        self.assertFalse(initial["pause"]["paused"])
        self.assertEqual(initial["pause"]["command_policy"], "no_construction")
        self.assertEqual(initial["pause"], initial_text["pause"])
        capabilities = self.request("GET", "/v1/capabilities")
        self.assertEqual(capabilities["world_status"], {"endpoint": "/v1/world/status", "event": "world.status"})

        rail = next(plan for plan in MODES if plan["mode"] == "rail")
        road = next(plan for plan in MODES if plan["mode"] == "road")
        self.action(road["build"], road["parameters"])
        vehicle, _, _ = self.action("vehicle.build", {"tile": road["depot"], "engine_id": road["engine"]})
        def rows():
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
                return connection.execute("SELECT COUNT(*),MAX(request_sequence) FROM action_requests").fetchone()
        self.evidence["operator_console"] = [{"command": "pause"}]
        self.world.console("pause")
        paused = self.wait("paused authority", lambda: (value if (value := self.snapshot())["pause"]["paused"] else None))
        self.assertIn("manual", paused["pause"]["reasons"])
        # Native Shift quoting remains available while construction is paused,
        # without inserting an action or consuming its sequence.
        before = rows()
        quote = self.request("POST", "/v1/actions/estimate", {"action": {"name": rail["build"], "parameters": rail["parameters"]}})
        self.assertTrue(quote["estimate"]["success"])
        self.assertEqual(set(quote["estimate"]), {"success", "cost", "advisory", "test_and_execute_may_differ"})
        self.assertEqual(before, rows())
        self.evidence["advisory_quote"] = quote
        self.pause_events(initial, paused=True)
        self.pause_events(initial_text, paused=True, audit=True)
        self.sse_transition(initial, paused=True)
        self.expected_error("POST", "/v1/observations", {"query": "company", "parameters": {}}, status=403)
        _, forbidden_body, rejected = self.action(rail["build"], rail["parameters"], success=False)
        identity = self.ledger(forbidden_body["request_id"])
        _, _, retried = self.action(exact_body=forbidden_body, success=False)
        self.assertEqual({key: value for key, value in retried.items() if key != "checkpoint_state"},
                         {key: value for key, value in rejected.items() if key != "checkpoint_state"})
        self.action("company.rename", {"name": "Paused visual company"})
        text_body = {"request_id": uuid.uuid4().hex, "action": {"name": "company.rename", "parameters": {"name": "Paused text company"}}}
        text_receipt = self.request("POST", "/v1/actions", text_body, audit=True)
        deadline = time.monotonic() + 15
        while text_receipt["status"] not in shared.TERMINAL:
            self.assertLess(time.monotonic(), deadline, text_receipt)
            time.sleep(0.05)
            text_receipt = self.request("GET", "/v1/actions/" + text_body["request_id"], audit=True)
        self.assertEqual(text_receipt["status"], "completed", text_receipt)
        self.evidence["text_paused_action"] = {"request": text_body, "terminal": text_receipt}

        # Native vehicle management remains permitted, while construction does not.
        self.action("vehicle.build", {"tile": road["depot"], "engine_id": road["engine"]}, success=False)
        self.action("vehicle.start_stop", {"vehicle_id": vehicle["vehicle_id"]})

        # Periodic checkpoint publication must keep working during a long pause.
        state = self.world.rpc("checkpoint.status")["result"]
        first = state.get("checkpoint", state)["checkpoint_id"]
        def newer_checkpoint():
            state = self.world.rpc("checkpoint.status")["result"]
            checkpoint = state.get("checkpoint", state)
            if checkpoint.get("checkpoint_id") != first and checkpoint.get("status") == "completed":
                self.assertIsInstance(checkpoint["checkpoint_id"], str)
                self.assertTrue(checkpoint["checkpoint_id"])
                checksum = 14695981039346656037
                for byte in Path(checkpoint["save_path"]).read_bytes():
                    checksum = ((checksum ^ byte) * 1099511628211) & ((1 << 64) - 1)
                self.assertEqual(str(checksum), checkpoint["save_checksum"])
                self.assertFalse(checkpoint["recovery_required"])
                return checkpoint
            time.sleep(0.2)
        self.evidence["checkpoint_while_paused"] = self.wait("periodic checkpoint while paused", newer_checkpoint, timeout=50)
        self.assertTrue(self.snapshot()["pause"]["paused"])
        self.action("company.rename", {"name": "Paused after checkpoint"})
        before_resume = self.snapshot()
        self.evidence["operator_console"].append({"command": "unpause"})
        self.world.console("unpause")
        resumed = self.wait("resumed authority", lambda: (value if not (value := self.snapshot())["pause"]["paused"] else None))
        self.pause_events(before_resume, paused=False)
        self.sse_transition(before_resume, paused=False)
        self.assertGreater(resumed["pause"]["revision"], before_resume["pause"]["revision"])
        _, _, after_resume_retry = self.action(exact_body=forbidden_body, success=False)
        self.assertEqual({key: value for key, value in after_resume_retry.items() if key != "checkpoint_state"},
                         {key: value for key, value in rejected.items() if key != "checkpoint_state"})
        after_identity = self.ledger(forbidden_body["request_id"])
        self.assertEqual(after_identity["request_sequence"], identity["request_sequence"])
        self.assertEqual(after_identity["payload"], identity["payload"])
        self.action(rail["build"], copy.deepcopy(rail["parameters"]))
        self.assertEqual(self.evidence["runtime_fingerprints"], shared.runtime_fingerprints(self.world.binary))
        self.evidence["result"] = "pass"


if __name__ == "__main__":
    unittest.main(defaultTest="SharedPauseLive.test_pause_status_events_native_policy_and_checkpoint")
