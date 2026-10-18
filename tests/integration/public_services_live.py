"""Opt-in four passenger services through the real public ASGI HTTP boundary.

Use a built integration engine, OpenGFX and the pinned backend environment::

    OPENTTD_TEST_BINARY=/path/to/openttd OPENTTD_TEST_BASESET=/path/to/baseset \
      OPENTTD_TEST_KEEP=1 /path/to/venv/bin/python \
      tests/integration/public_services_live.py -v

OPENTTD_TEST_PORT defaults to 15978, OPENTTD_TEST_API_PORT to 18086 and
OPENTTD_TEST_ROUTE_TIMEOUT to 180 seconds. These must be unused loopback ports.
All construction, finance, purchase, orders and observations use BackendClient.
Private RPC is restricted to lifecycle/readiness; no running world is reused.
The stock regression fixture supplies its preexisting company 0.
The retained public-services-result.json contains no bearer credential.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from urllib.error import URLError
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.client import BackendClient, BackendError
from backend.store import Store
from engine_harness import LiveWorld, TERMINAL
from fixture_plans import PASSENGER_ROUTES


def runtime_fingerprints(binary):
    paths = [Path(binary).resolve(), ROOT / "backend/requirements.txt",
             ROOT / "backend/schemas/engine-catalog-v1.json", *sorted((ROOT / "backend").glob("*.py"))]
    return {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


class LifecycleWorld(LiveWorld):
    """Keep the fixture runner from accidentally bypassing the public API."""

    allowed_console_commands = frozenset({"unpause", "quit"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.private_calls = []

    def rpc(self, kind, **fields):
        if kind not in {"hello", "checkpoint.status"}:
            raise AssertionError(f"Fixture test forbids private gameplay RPC {kind}")
        self.private_calls.append(kind)
        return super().rpc(kind, **fields)

    def console(self, command):
        if command not in self.allowed_console_commands:
            raise AssertionError(f"Fixture test forbids console gameplay command {command}")
        return super().console(command)


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"),
                     "Set integration binary and OpenGFX assets for public HTTP acceptance")
class PublicPassengerServices(unittest.TestCase):
    def setUp(self):
        self.world = LifecycleWorld(os.environ["OPENTTD_TEST_BINARY"], os.environ["OPENTTD_TEST_BASESET"],
                                    port=int(os.environ.get("OPENTTD_TEST_PORT", "15978")))
        self.backend = self.backend_log = None
        self.stage = "startup"
        self.evidence = {"result": "incomplete", "transport": "asgi/http", "modes": {},
                         "actions": [], "public_request_counts": {}}
        self.addCleanup(self.close)
        self.evidence["runtime_fingerprints"] = runtime_fingerprints(self.world.binary)
        self.evidence["fixture_sha256"] = hashlib.sha256(self.world.fixture.read_bytes()).hexdigest()
        print(f"Public services artifacts: {self.world.directory}", flush=True)
        self.world.start()
        database = self.world.directory / "backend.sqlite3"
        store = Store(database, world_id=self.world.world_id)
        try:
            token = store.provision(self.world.world_id, "public-services-test", 0,
                                    mode="text", kind="agent", display_name="Public services acceptance",
                                    world_epoch=self.world.epoch)
        finally:
            store.close()
        port = int(os.environ.get("OPENTTD_TEST_API_PORT", "18086"))
        self.client = BackendClient(f"http://127.0.0.1:{port}", token, timeout=5)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("OPENTTD_INTEGRATION_")}
        self.backend_log = (self.world.directory / "backend.log").open("wb", buffering=0)
        self.backend = subprocess.Popen([
            sys.executable, "-m", "backend", "serve", "--database", str(database),
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
        path = self.world.directory / "public-services-result.json"
        path.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"Public services evidence: {path}", flush=True)
        self.world.close()

    def request(self, method, path, body=None):
        key = method + " " + path.split("/actions/")[0]
        counts = self.evidence["public_request_counts"]
        counts[key] = counts.get(key, 0) + 1
        return self.client.request(method, path, body)

    def ready(self):
        if self.backend.poll() is not None:
            self.fail((self.world.directory / "backend.log").read_text(errors="replace")[-5000:])
        try:
            capabilities = self.request("GET", "/v1/capabilities")
            if not capabilities.get("engine_connected"):
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
        return self.request("POST", "/v1/observations", {"query": name, "parameters": parameters or {}})["observation"]

    def action(self, name, parameters=None, *, exact_body=None):
        body = copy.deepcopy(exact_body) if exact_body is not None else {
            "request_id": uuid.uuid4().hex,
            "action": {"name": name, "parameters": copy.deepcopy(parameters or {})}}
        reply = self.request("POST", "/v1/actions", body)
        deadline = time.monotonic() + 15
        while reply["status"] not in TERMINAL:
            if time.monotonic() >= deadline:
                self.fail(f"Public action did not terminate: {name}: {reply}")
            time.sleep(0.025)
            reply = self.request("GET", "/v1/actions/" + body["request_id"])
        self.assertEqual(reply["status"], "completed", reply)
        self.assertTrue(reply.get("result", {}).get("success"), reply)
        self.evidence["actions"].append({"request": body, "terminal": reply})
        return reply["result"], body, reply

    def handle(self, value):
        self.assertEqual(set(value), {"id", "generation", "engine_instance_id"}, value)
        self.assertIs(type(value["id"]), int)
        self.assertIs(type(value["generation"]), int)
        self.assertEqual(value["engine_instance_id"], self.world.instance)
        return copy.deepcopy(value)

    def orders(self, vehicle):
        return self.observe("orders", {"vehicle_id": vehicle})

    def test_public_four_modes_load_deliver_and_modify(self):
        before = self.observe("company")
        available = before["maximum_loan"] - before["loan"]
        if available:
            self.action("company.loan.increase", {"amount": available})
        self.evidence["company_after_ordinary_finance"] = self.observe("company")
        vehicles = {}
        for plan in PASSENGER_ROUTES:
            mode = plan["mode"]
            self.stage = mode + " construction/purchase/orders"
            evidence = self.evidence["modes"][mode] = {"construction": []}
            for name, parameters in plan["construction"]:
                outcome, _, _ = self.action(name, parameters)
                self.assertGreater(outcome["cost"], 0, outcome)
                evidence["construction"].append({"name": name, "cost": outcome["cost"]})
            purchase, exact_body, receipt = self.action("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
            self.assertGreater(purchase["cost"], 0)
            vehicle = self.handle(purchase["vehicle_id"])
            evidence.update(vehicle_id=vehicle, purchase_cost=purchase["cost"])
            vehicles[mode] = vehicle
            if mode == "road":
                before_retry = self.observe("company")["balance"]
                before_count = len(self.observe("vehicles", {"limit": 64})["items"])
                retried, _, retried_receipt = self.action("vehicle.build", exact_body=exact_body)
                self.assertEqual(retried, purchase)
                self.assertEqual(retried_receipt["request_id"], receipt["request_id"])
                self.assertEqual(retried_receipt["status"], receipt["status"])
                self.assertEqual(self.observe("company")["balance"], before_retry)
                self.assertEqual(len(self.observe("vehicles", {"limit": 64})["items"]), before_count)
                evidence["duplicate_retry"] = {"request_id": exact_body["request_id"],
                                               "same_terminal_outcome": True,
                                               "balance_unchanged": True, "vehicle_count_unchanged": True}
            if "wagon_engine" in plan:
                wagon_purchase, _, _ = self.action("vehicle.build", {"tile": plan["depot"], "engine_id": plan["wagon_engine"]})
                self.assertGreater(wagon_purchase["cost"], 0)
                wagon = self.handle(wagon_purchase["vehicle_id"])
                self.action("train.wagon.move", {"vehicle_id": wagon, "destination_vehicle_id": vehicle})
                evidence.update(wagon_id=wagon, wagon_purchase_cost=wagon_purchase["cost"])
            station_ids = []
            for destination in plan["destinations"]:
                order_version = self.orders(vehicle)["order_version"]
                self.action("order.insert", {"vehicle_id": vehicle, "order_version": order_version,
                                             "destination": destination})
                tile = self.observe("map.region", {"x": destination % 256, "y": destination // 256,
                                                    "width": 1, "height": 1})["tiles"][0]
                station_ids.append(self.handle(tile["station_id"])["id"])
            orders = self.orders(vehicle)
            self.assertEqual(len(orders["orders"]), 2, orders)
            self.assertEqual([o["destination_id"] for o in orders["orders"]], station_ids)
            evidence["orders"] = orders

        road = vehicles["road"]
        original = self.orders(road)
        self.action("order.move", {"vehicle_id": road, "order_version": original["order_version"],
                                   "position": 0, "target_position": 1})
        changed = self.orders(road)
        self.assertEqual([o["destination_id"] for o in changed["orders"]],
                         [o["destination_id"] for o in reversed(original["orders"])])
        self.assertNotEqual(changed["orders"][0]["destination_id"], original["orders"][0]["destination_id"])
        order_version = self.orders(road)["order_version"]
        self.action("order.move", {"vehicle_id": road, "order_version": order_version,
                                   "position": 0, "target_position": 1})
        restored = self.orders(road)
        self.assertEqual(restored["orders"], original["orders"])
        self.evidence["ordinary_order_modification"] = {"before": original, "changed": changed, "restored": restored}
        for vehicle in vehicles.values():
            self.action("vehicle.start_stop", {"vehicle_id": vehicle})

        self.stage = "four public passenger services loading and earning revenue"
        loaded, earned, previous, last = set(), set(), {}, {}
        sampling_started = time.monotonic()
        deadline = sampling_started + float(os.environ.get("OPENTTD_TEST_ROUTE_TIMEOUT", "180"))
        while time.monotonic() < deadline and (len(loaded) < 4 or len(earned) < 4):
            observation = self.observe("vehicles", {"limit": 32})
            self.evidence.setdefault("sampling_first_tick", observation["tick"])
            self.evidence["sampling_last_tick"] = observation["tick"]
            snapshots = observation["items"]
            for mode, handle in vehicles.items():
                vehicle = next(item for item in snapshots if item["vehicle_id"] == handle)
                self.handle(vehicle["vehicle_id"])
                self.assertFalse(vehicle["crashed"], vehicle)
                self.assertFalse(vehicle["stopped"], vehicle)
                last[mode] = vehicle
                evidence = self.evidence["modes"][mode]
                if any(part["cargo_id"] == 0 and part["load"] > 0 for part in vehicle["parts"]) and mode not in loaded:
                    loaded.add(mode)
                    evidence["loaded_vehicle"] = copy.deepcopy(vehicle)
                # With no mutations/refits/replacements during sampling, a
                # positive successive operating-profit delta proves delivery
                # revenue. Sum both year buckets to avoid first-year reset.
                profit = vehicle["profit_this_year"] + vehicle["profit_last_year"]
                prior = previous.get(mode)
                if mode in loaded and prior is not None and profit > prior["profit"] and mode not in earned:
                    earned.add(mode)
                    evidence["delivery"] = {"income_scope": "whole_vehicle_operating_profit", "profit_before": prior["profit"], "profit_after": profit,
                                            "positive_profit_delta": profit - prior["profit"],
                                            "vehicle_before": prior["vehicle"], "vehicle_after": copy.deepcopy(vehicle)}
                    print(f"Public {mode}: observed passenger loading and service income", flush=True)
                previous[mode] = {"profit": profit, "vehicle": copy.deepcopy(vehicle)}
            if len(loaded) < 4 or len(earned) < 4:
                time.sleep(0.2)
        self.evidence.update(loaded_modes=sorted(loaded), earned_modes=sorted(earned), last_vehicles=last, sampling_seconds=time.monotonic() - sampling_started)
        # The stock fixture uses 74 calendar ticks/day. New vehicles cannot
        # reach a second annual profit-bucket rollover within this bound.
        self.evidence["sampling_tick_span"] = self.evidence["sampling_last_tick"] - self.evidence["sampling_first_tick"]
        self.assertLess(self.evidence["sampling_tick_span"], 365 * 74)
        self.assertEqual(loaded, set(vehicles), self.evidence)
        self.assertEqual(earned, set(vehicles), self.evidence)
        self.evidence["final_company"] = self.observe("company")
        self.evidence["runtime_fingerprints_after"] = runtime_fingerprints(self.world.binary)
        self.evidence["runtime_files_unchanged"] = self.evidence["runtime_fingerprints_after"] == self.evidence["runtime_fingerprints"]
        if not self.evidence["runtime_files_unchanged"]:
            self.evidence["result"] = "failed_runtime_fingerprint_mismatch"
        self.assertTrue(self.evidence["runtime_files_unchanged"], "Runtime files changed during acceptance")
        self.evidence["result"] = "passed"


if __name__ == "__main__":
    unittest.main()
