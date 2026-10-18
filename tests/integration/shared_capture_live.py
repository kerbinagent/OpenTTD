"""Opt-in shared typed actuators with visual perception through public HTTP.

This technical contract test starts a disposable stock-regression authority and
the real ASGI backend, deliberately without a renderer. It is not autonomous
image-perception or business-success acceptance. Player inputs come only from
the fixed regression plan, creation receipts and explicit control references.
A separate text credential supplies read-only assertions, never action inputs.

    OPENTTD_TEST_BINARY=/path/to/openttd OPENTTD_TEST_BASESET=/path/to/baseset \
      OPENTTD_TEST_KEEP=1 /path/to/venv/bin/python \
      tests/integration/shared_capture_live.py -v

OPENTTD_TEST_PORT defaults to 25978 and OPENTTD_TEST_API_PORT to 18106.
Both must be unused loopback ports. No existing world is reused.
"""

from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from urllib.error import URLError
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.catalog import action_spec
from backend.client import BackendClient, BackendError
from backend.store import Store
from engine_harness import TERMINAL
from fixture_plans import PASSENGER_ROUTES
from public_services_live import LifecycleWorld, runtime_fingerprints


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"),
                     "Set matching integration binary and OpenGFX assets for shared-capture HTTP acceptance")
class SharedCaptureActuators(unittest.TestCase):
    def setUp(self):
        self.world = LifecycleWorld(os.environ["OPENTTD_TEST_BINARY"], os.environ["OPENTTD_TEST_BASESET"],
                                    port=int(os.environ.get("OPENTTD_TEST_PORT", "25978")))
        self.backend = self.backend_log = None
        self.stage = "startup"
        self.evidence = {"result": "incomplete", "transport": "asgi/http", "renderer_configured": False,
                         "scope": "technical shared-actuator contract, not autonomous image/business play",
                         "not_covered": ["native duplicate-target ambiguity", "cross-company target authorization",
                                         "engine/backend restart recovery", "autonomous image perception and business income"],
                         "capability_assertion": "Relevant action availability and canonical mode membership; no public parameter-schema endpoint",
                         "actions": [], "references": [], "expected_http_errors": [],
                         "public_request_counts": {}}
        self.receipts = {}
        self.reference_after = 0.0
        self.addCleanup(self.close)
        self.evidence["runtime_fingerprints"] = runtime_fingerprints(self.world.binary)
        self.evidence["fixture_sha256"] = hashlib.sha256(self.world.fixture.read_bytes()).hexdigest()
        self.evidence["test_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        print(f"Shared capture artifacts: {self.world.directory}", flush=True)
        self.world.start()
        self.database = self.world.directory / "backend.sqlite3"
        store = Store(self.database, world_id=self.world.world_id)
        try:
            tokens = {mode: store.provision(self.world.world_id, "shared-capture-" + mode, 0,
                                            mode=mode, kind="agent", display_name="Shared capture " + mode,
                                            world_epoch=self.world.epoch)
                      for mode in ("visual", "text")}
        finally:
            store.close()
        port = int(os.environ.get("OPENTTD_TEST_API_PORT", "18106"))
        origin = f"http://127.0.0.1:{port}"
        self.player = BackendClient(origin, tokens["visual"], timeout=5)
        self.auditor = BackendClient(origin, tokens["text"], timeout=5)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("OPENTTD_INTEGRATION_") and key != "OPENTTD_RENDERER_IMAGE_DIR"}
        self.backend_log = (self.world.directory / "backend.log").open("wb", buffering=0)
        # No renderer command-line option, process, socket or image directory.
        self.backend = subprocess.Popen([
            sys.executable, "-m", "backend", "serve", "--database", str(self.database),
            "--world", self.world.world_id, "--engine-socket", str(self.world.socket_path),
            "--transport", "asgi", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=self.backend_log,
            stderr=subprocess.STDOUT, start_new_session=True)
        self.wait("backend recovery readiness", lambda: not self.world.rpc("checkpoint.status").get("result", {}).get("recovery_required", True), timeout=30)
        self.wait("public backend ready", self.ready, timeout=30)
        self.evidence.update(world_id=self.world.world_id, world_epoch=self.world.epoch,
                             engine_instance_id=self.world.instance)

    def close(self):
        if self.backend and self.backend.poll() is None:
            self.backend.terminate()
            try:
                self.backend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.backend.kill()
                self.backend.wait(timeout=3)
        if self.backend_log:
            self.backend_log.close()
        self.evidence["private_runner_rpc_methods"] = sorted(set(self.world.private_calls))
        self.evidence["last_stage"] = self.stage
        path = self.world.directory / "shared-capture-result.json"
        path.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"Shared capture evidence: {path}", flush=True)
        self.world.close()

    def request(self, method, path, body=None, *, audit=False, query=None):
        role = "audit" if audit else "player"
        route = "/v1/actions/:id" if path.startswith("/v1/actions/") else path
        key = f"{role} {method} {route}"
        counts = self.evidence["public_request_counts"]
        counts[key] = counts.get(key, 0) + 1
        try:
            return (self.auditor if audit else self.player).request(method, path, body, query=query)
        except BackendError as error:
            self.evidence.setdefault("http_errors", []).append({
                "role": role, "method": method, "path": path, "body": body,
                "status": error.status, "code": error.code, "message": error.message})
            if hasattr(error, "add_note"):
                error.add_note(f"HTTP {error.status} {error.code} at {method} {path}")
            raise

    def ready(self):
        if self.backend.poll() is not None:
            self.fail((self.world.directory / "backend.log").read_text(errors="replace")[-5000:])
        try:
            if not self.request("GET", "/v1/capabilities").get("engine_connected"):
                return False
            return self.observe("company")
        except (URLError, OSError, BackendError):
            return False

    def wait(self, label, predicate, timeout=15):
        self.stage = label
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"Timeout: {label}; artifacts: {self.world.directory}")

    def observe(self, name, parameters=None):
        return self.request("POST", "/v1/observations", {"query": name, "parameters": parameters or {}},
                            audit=True)["observation"]

    def expected_error(self, method, path, body=None, *, status, code=None, query=None):
        with self.assertRaises(BackendError) as caught:
            self.request(method, path, body, query=query)
        error = caught.exception
        self.assertEqual(error.status, status)
        if code is not None:
            self.assertEqual(error.code, code)
        self.evidence["expected_http_errors"].append({"method": method, "path": path, "body": body,
                                                       "status": error.status, "code": error.code})

    def handle(self, value):
        self.assertEqual(set(value), {"id", "generation", "engine_instance_id"}, value)
        self.assertIs(type(value["id"]), int)
        self.assertIs(type(value["generation"]), int)
        self.assertGreater(value["generation"], 0)
        self.assertEqual(value["engine_instance_id"], self.world.instance)
        return copy.deepcopy(value)

    def assert_receipt(self, reply):
        self.assertLessEqual(set(reply), {"request_id", "status", "checkpoint_state", "tick", "result", "error"}, reply)
        if "result" in reply:
            outcome = reply["result"]
            self.assertLessEqual(set(outcome), {"success", "cost", "error", "vehicle_id", "station_id", "depot_id",
                                                "group_id", "sign_id", "town_id"}, outcome)
            for key in ("vehicle_id", "station_id", "depot_id", "group_id", "sign_id", "town_id"):
                if key in outcome:
                    self.handle(outcome[key])
            if "cost" in outcome:
                self.assertIs(type(outcome["cost"]), int)
            if "error" in outcome:
                self.assertLessEqual(set(outcome["error"]), {"code", "message", "game_message", "string_id"})
        if "error" in reply:
            self.assertLessEqual(set(reply["error"]), {"code", "message", "game_message", "string_id"})

    def action(self, name=None, parameters=None, *, exact_body=None, success=True):
        body = copy.deepcopy(exact_body) if exact_body is not None else {
            "request_id": uuid.uuid4().hex,
            "action": {"name": name, "parameters": copy.deepcopy(parameters or {})}}
        self.assertNotIn("image_id", body["action"]["parameters"])
        self.stage = body["action"]["name"]
        reply = self.request("POST", "/v1/actions", body)
        self.assert_receipt(reply)
        deadline = time.monotonic() + 15
        while reply["status"] not in TERMINAL:
            if time.monotonic() >= deadline:
                self.fail(f"Public action did not terminate: {body}: {reply}")
            time.sleep(0.025)
            reply = self.request("GET", "/v1/actions/" + body["request_id"])
            self.assert_receipt(reply)
        self.evidence["actions"].append({"request": body, "terminal": reply})
        self.receipts[body["request_id"]] = reply
        self.assertEqual(reply["status"], "completed" if success else "rejected", reply)
        if success:
            self.assertTrue(reply.get("result", {}).get("success"), reply)
        return reply.get("result", {}), body, reply

    def reference(self, selector, *, order_token=False):
        # Own-primary order tokens and explicit name/unit scans share the
        # native process-global one-second text gate. This isolated runner
        # spaces them deliberately rather than retrying ambiguous failures.
        gated = order_token or "name" in selector or "unit_number" in selector
        if gated:
            delay = self.reference_after - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        reply = self.request("POST", "/v1/control/reference", selector)
        if gated:
            self.reference_after = time.monotonic() + 1.1
        expected = {"kind", "engine_id"} if selector["kind"] == "engine" else {"kind", "reference"}
        if order_token:
            expected.add("order_version")
        self.assertEqual(set(reply), expected, reply)
        self.assertEqual(reply["kind"], selector["kind"])
        if "reference" in reply:
            self.handle(reply["reference"])
        if order_token:
            self.assertIsInstance(reply["order_version"], str)
            self.assertTrue(reply["order_version"])
        self.evidence["references"].append({"selector": selector, "response": reply})
        return reply

    def ledger(self, request_id):
        # Operator assertion only. Never read credentials or feed values into
        # player actions, and never mutate the backend database.
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                "SELECT mode,payload,result,request_sequence,engine_dispatched,intent_state,visual_intent,"
                "expected_worker_instance_id FROM action_requests WHERE world_id=? AND identity_id=? AND request_id=?",
                (self.world.world_id, "shared-capture-visual", request_id)).fetchall()
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        row["payload"] = json.loads(row["payload"])
        row["result"] = json.loads(row["result"]) if row["result"] else None
        self.assertEqual(row["mode"], "visual")
        self.assertEqual(row["intent_state"], "none")
        self.assertIsNone(row["visual_intent"])
        self.assertIsNone(row["expected_worker_instance_id"])
        return row

    def test_visual_typed_route_without_renderer_and_normal_guards(self):
        plan = PASSENGER_ROUTES[0]
        self.assertEqual(plan["mode"], "road")
        needed = {name for name, _ in plan["construction"]} | {
            "company.loan.increase", "vehicle.build", "vehicle.sell", "vehicle.start_stop", "order.insert"}
        for audit in (False, True):
            caps = self.request("GET", "/v1/capabilities", audit=audit)
            self.assertTrue(caps["engine_connected"], caps)
            available = {row["action"] for row in caps["actions"] if row["available"]}
            self.assertLessEqual(needed, available)
            if not audit:
                self.assertEqual(caps["observation"], "screenshots")
                self.assertEqual(caps["view_controls"], [])
                self.assertEqual(caps["tools"], [])
        for name in needed:
            self.assertLessEqual({"text", "visual"}, set(action_spec(name)["modes"]))
        self.expected_error("POST", "/v1/view/images", {}, status=503, code="renderer_unavailable")
        for query in ("company", "vehicles", "orders", "map.region", "catalog.engines", "display.text"):
            self.expected_error("POST", "/v1/observations", {"query": query}, status=403, code="forbidden")
            self.expected_error("GET", "/v1/observations", status=403, code="forbidden", query={"query": query})

        self.action("company.loan.increase", {"mode": "max"})
        created = {}
        for name, parameters in plan["construction"]:
            outcome, _, _ = self.action(name, parameters)
            self.assertGreater(outcome["cost"], 0)
            if name == "road.depot.build":
                created[parameters["tile"]] = self.handle(outcome["depot_id"])
            elif name == "road.stop.build":
                created[parameters["tile"]] = self.handle(outcome["station_id"])
        for tile in (plan["depot"], *plan["destinations"]):
            kind = "depot" if tile == plan["depot"] else "station"
            self.assertEqual(self.reference({"kind": kind, "tile": tile})["reference"], created[tile])
        self.assertEqual(self.reference({"kind": "engine", "id": plan["engine"]})["engine_id"], plan["engine"])

        purchase, purchase_body, original = self.action("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
        self.assertGreater(purchase["cost"], 0)
        vehicle = self.handle(purchase["vehicle_id"])
        initial = self.reference({"kind": "vehicle", "id": vehicle["id"]}, order_token=True)
        self.assertEqual(initial["reference"], vehicle)
        # The regression fixture has no existing road vehicles: the first
        # purchase is the displayed Bus 1, independent of the native pool ID.
        numbered = self.reference({"kind": "vehicle", "vehicle_type": "road", "unit_number": 1}, order_token=True)
        self.assertEqual(numbered, initial)
        row = self.ledger(purchase_body["request_id"])
        self.assertTrue({"capacity", "mail_capacity", "cargo_capacities"} <= set(row["result"]), row)
        self.assertGreater(row["result"]["capacity"], 0)
        count_before = len(self.observe("vehicles", {"limit": 100})["items"])
        retry, _, retried = self.action(exact_body=purchase_body)
        self.assertEqual(retry, purchase)
        self.assertEqual(retried["request_id"], original["request_id"])
        self.assertEqual(retried["status"], original["status"])
        self.assertEqual(len(self.observe("vehicles", {"limit": 100})["items"]), count_before)
        self.assertEqual(self.ledger(purchase_body["request_id"])["request_sequence"], row["request_sequence"])
        conflict = copy.deepcopy(purchase_body)
        conflict["action"]["parameters"]["engine_id"] += 1
        self.expected_error("POST", "/v1/actions", conflict, status=409)
        self.evidence["purchase_retry"] = {"same_terminal_outcome": True, "single_request_row": True,
                                            "vehicle_count_unchanged": True, "intent_state": row["intent_state"],
                                            "engine_dispatched_boolean": bool(row["engine_dispatched"]),
                                            "native_capacity_fields_present_but_not_public": True}

        # Exercise a real recycled pool slot before starting any service.
        spare, spare_body, _ = self.action("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
        stale = self.handle(spare["vehicle_id"])
        self.action("vehicle.sell", {"vehicle_id": stale})
        replacement, _, _ = self.action("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
        fresh = self.handle(replacement["vehicle_id"])
        self.assertEqual(fresh["id"], stale["id"])
        self.assertNotEqual(fresh["generation"], stale["generation"])
        historical, _, _ = self.action(exact_body=spare_body)
        self.assertEqual(historical, spare)
        self.action("vehicle.sell", {"vehicle_id": stale}, success=False)
        self.assertEqual(self.reference({"kind": "vehicle", "id": stale["id"]}, order_token=True)["reference"], fresh)
        self.assertEqual({item["vehicle_id"]["id"] for item in self.observe("vehicles", {"limit": 100})["items"]},
                         {vehicle["id"], fresh["id"]})
        self.action("vehicle.sell", {"vehicle_id": fresh})
        self.evidence["recycled_handle"] = {"old": stale, "replacement": fresh, "old_handle_rejected": True}

        first, second = plan["destinations"]
        self.action("order.insert", {"vehicle_id": vehicle, "order_version": initial["order_version"], "destination": first})
        self.action("order.insert", {"vehicle_id": vehicle, "order_version": initial["order_version"], "destination": second}, success=False)
        self.assertEqual(len(self.observe("orders", {"vehicle_id": vehicle})["orders"]), 1)
        current = self.reference({"kind": "vehicle", "id": vehicle["id"]}, order_token=True)
        self.assertNotEqual(current["order_version"], initial["order_version"])
        self.action("order.insert", {"vehicle_id": vehicle, "order_version": current["order_version"], "destination": second})
        orders = self.observe("orders", {"vehicle_id": vehicle})["orders"]
        self.assertEqual([order["destination_id"] for order in orders], [created[tile]["id"] for tile in plan["destinations"]])
        failure, _, _ = self.action("road.stop.build", {"tile": plan["depot"], "direction": 2, "stop_type": "bus"}, success=False)
        self.assertFalse(failure["success"])
        self.assertEqual(failure["cost"], 0)
        self.assertTrue(failure["error"]["message"])
        self.assertNotEqual(failure["error"]["message"], "The game rejected the action")

        for selector in ({"kind": "vehicles", "id": 0}, {"kind": "vehicle", "id": vehicle["id"], "limit": 10},
                         {"kind": "station", "id": created[first]["id"], "tile": first}):
            self.expected_error("POST", "/v1/control/reference", selector, status=400, code="invalid_request")
        # The deleted spare has no current occupant. This is unavailable-target
        # handling, not cross-company authorization or native name ambiguity.
        self.expected_error("POST", "/v1/control/reference", {"kind": "vehicle", "id": fresh["id"]},
                            status=409, code="control_reference_unavailable")
        self.action("vehicle.start_stop", {"vehicle_id": vehicle})
        audited = next(item for item in self.observe("vehicles", {"limit": 100})["items"] if item["vehicle_id"] == vehicle)
        self.assertFalse(audited["stopped"])
        self.assertFalse(audited["crashed"])
        self.evidence["audited_service"] = {"vehicle_id": vehicle, "orders": orders, "running": True}

        event_receipts = {}
        cursor = None
        for _ in range(10):
            page = self.request("GET", "/v1/events", query={"after": cursor, "limit": 100} if cursor else {"limit": 100})
            for event in page["events"]:
                if event["type"] == "action.status":
                    self.assert_receipt(event["data"])
                    if event["data"]["status"] in TERMINAL:
                        event_receipts[event["data"]["request_id"]] = event["data"]
            cursor = page["cursor"]
            if not page["events"]:
                break
        else:
            self.fail("Public event pagination did not terminate within bounded test history")
        self.assertLessEqual(set(self.receipts), set(event_receipts))
        for request_id, receipt in self.receipts.items():
            self.assertEqual(event_receipts[request_id].get("result"), receipt.get("result"))
        self.evidence["projected_terminal_event_count"] = len(event_receipts)
        self.evidence["runtime_fingerprints_after"] = runtime_fingerprints(self.world.binary)
        self.evidence["runtime_files_unchanged"] = self.evidence["runtime_fingerprints_after"] == self.evidence["runtime_fingerprints"]
        self.assertTrue(self.evidence["runtime_files_unchanged"], "Runtime files changed during acceptance")
        self.evidence["result"] = "passed"


if __name__ == "__main__":
    unittest.main()
