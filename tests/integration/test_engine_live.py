"""Genuine engine acceptance checks, opt-in with explicit binary and assets.

Run from the repository root:
  OPENTTD_TEST_BINARY=/path/to/openttd OPENTTD_TEST_BASESET=/path/to/baseset \
    python3 -m unittest discover -s tests/integration -v

OPENTTD_TEST_LIBRARY_PATH adds a local dependency prefix at subprocess launch.
OPENTTD_TEST_KEEP=1 retains private logs, saves and receipts for investigation.
"""

import copy
import os
from pathlib import Path
import time
import unittest

from engine_harness import LiveWorld
from fixture_plans import MODES, PASSENGER_ROUTES


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"), "Set OPENTTD_TEST_BINARY and OPENTTD_TEST_BASESET for live engine acceptance tests")
class LiveEngineTests(unittest.TestCase):
    def setUp(self):
        binary = Path(os.environ["OPENTTD_TEST_BINARY"])
        baseset = Path(os.environ["OPENTTD_TEST_BASESET"])
        self.assertTrue(binary.is_file(), f"Engine binary missing: {binary}")
        self.assertTrue(baseset.is_dir(), f"Base graphics directory missing: {baseset}")
        self.world = LiveWorld(binary, baseset, port=int(os.environ.get("OPENTTD_TEST_PORT", "13978")))
        self.addCleanup(self.world.close)
        self.world.start()
        self.world.finish_recovery()
        self.world.actor = self.world.bind(0)

    def success(self, name, parameters=None, **kwargs):
        reply = self.world.action(name, parameters, **kwargs)
        self.assertEqual(reply["status"], "completed", reply)
        self.assertTrue(reply["result"].get("success"), reply)
        return reply["result"]

    def finance(self):
        company = self.world.observe("company")
        available = company["maximum_loan"] - company["loan"]
        if available:
            self.success("company.loan.increase", {"amount": available})

    def test_four_modes_have_real_construction_and_purchase_outcomes(self):
        self.finance()
        purchased = {}
        for plan in MODES:
            with self.subTest(mode=plan["mode"]):
                before = self.world.observe("company")["balance"]
                built = self.success(plan["build"], plan["parameters"])
                self.assertGreater(built["cost"], 0)
                after = self.world.observe("company")["balance"]
                self.assertEqual(before - after, built["cost"], (before, after, built))
                vehicle = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
                self.assertGreater(vehicle["cost"], 0)
                self.assertEqual(after - self.world.observe("company")["balance"], vehicle["cost"])
                self.assertIn("vehicle_id", vehicle)
                purchased[plan["mode"]] = vehicle["vehicle_id"]
        vehicles = self.world.observe("vehicles", {"limit": 64})["items"]
        for mode, handle in purchased.items():
            observed = next(item for item in vehicles if item["vehicle_id"] == handle)
            self.assertEqual(observed["vehicle_type"], mode)
            self.assertTrue(observed["stopped"])
            self.assertEqual(observed["order_count"], 0)

    def test_duplicate_requests_do_not_charge_or_allocate_twice(self):
        plan = MODES[0]
        self.success(plan["build"], plan["parameters"])
        before = self.world.observe("company")["balance"]
        action, queued = self.world.enqueue("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
        self.assertEqual(queued["status"], "queued", queued)
        original = self.world.wait(action, queued)
        self.assertEqual(original["status"], "completed", original)
        duplicate = self.world.rpc("action.submit", **action.fields())
        self.assertEqual(duplicate["result"], original["result"])
        self.assertEqual(self.world.observe("company")["balance"], before - original["result"]["cost"])
        self.assertEqual(len(self.world.observe("vehicles")["items"]), 1)
        conflict = action.fields()
        conflict["action"] = {"name": "vehicle.build", "parameters": {"tile": plan["depot"], "engine_id": plan["engine"] + 1}}
        changed = self.world.rpc("action.submit", **conflict)
        self.assertEqual(changed["status"], "rejected", changed)
        self.assertEqual(changed["error"]["code"], "request_conflict")

    def test_four_passenger_services_deliver_and_earn_revenue(self):
        self.finance()
        vehicles = {}
        for plan in PASSENGER_ROUTES:
            with self.subTest(construction=plan["mode"]):
                for name, parameters in plan["construction"]:
                    self.assertGreater(self.success(name, parameters)["cost"], 0)
                vehicle = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})["vehicle_id"]
                if "wagon_engine" in plan:
                    wagon = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": plan["wagon_engine"]})["vehicle_id"]
                    self.success("train.wagon.move", {"vehicle_id": wagon, "destination_vehicle_id": vehicle})
                for destination in plan["destinations"]:
                    order_version = self.world.observe("orders", {"vehicle_id": vehicle})["order_version"]
                    self.success("order.insert", {"vehicle_id": vehicle, "order_version": order_version,
                                                   "destination": destination})
                orders = self.world.observe("orders", {"vehicle_id": vehicle})["orders"]
                self.assertEqual(len(orders), 2)
                station_ids = []
                for destination in plan["destinations"]:
                    tile = self.world.observe("map.region", {"x": destination % 256, "y": destination // 256, "width": 1, "height": 1})["tiles"][0]
                    station_ids.append(tile["station_id"]["id"])
                self.assertEqual([order["destination_id"] for order in orders], station_ids)
                vehicles[plan["mode"]] = vehicle

        self.assertEqual(set(vehicles), {"road", "rail", "ship", "aircraft"})
        for vehicle in vehicles.values():
            self.success("vehicle.start_stop", {"vehicle_id": vehicle})
        earned = set()
        loaded = set()
        last = {}
        previous_profit = {mode: 0 for mode in vehicles}
        deadline = time.monotonic() + float(os.environ.get("OPENTTD_TEST_ROUTE_TIMEOUT", "150"))
        while time.monotonic() < deadline and (len(earned) < len(vehicles) or len(loaded) < len(vehicles)):
            snapshots = self.world.observe("vehicles", {"limit": 32})["items"]
            for mode, handle in vehicles.items():
                vehicle = next(item for item in snapshots if item["vehicle_id"] == handle)
                last[mode] = vehicle
                self.assertFalse(vehicle["crashed"], vehicle)
                if any(part["load"] > 0 for part in vehicle["parts"]):
                    loaded.add(mode)
                # A service can deliver passengers while its net profit remains
                # negative. During this new vehicle's first year transition,
                # summing the two year buckets also avoids treating a reset as
                # income. With no refits/replacements/actions during sampling,
                # a positive operating-profit delta proves real delivery income.
                profit = vehicle["profit_this_year"] + vehicle["profit_last_year"]
                if profit > previous_profit[mode]:
                    earned.add(mode)
                previous_profit[mode] = profit
            if len(earned) < len(vehicles) or len(loaded) < len(vehicles):
                time.sleep(0.2)
        self.assertEqual(earned, set(vehicles), {"earned": sorted(earned), "observed_loading": sorted(loaded), "last_vehicles": last})
        self.assertEqual(loaded, set(vehicles), {"observed_loading": sorted(loaded), "last_vehicles": last})

    def test_both_dock_halves_are_safe_to_observe(self):
        self.success("water.dock.build", {"tile": 4542})
        tile = self.world.observe("map.region", {"x": 190, "y": 17, "width": 1, "height": 1})["tiles"][0]
        self.assertEqual(tile["tile"], 4542)
        self.assertIn("dock_direction", tile)
        dx, dy = ((-1, 0), (0, 1), (1, 0), (0, -1))[tile["dock_direction"]]
        water = self.world.observe("map.region", {"x": 190 + dx, "y": 17 + dy, "width": 1, "height": 1})["tiles"][0]
        self.assertEqual(water["station_id"], tile["station_id"])
        self.assertNotIn("dock_direction", water)

    def test_other_company_and_reused_vehicle_handles_are_rejected(self):
        plan = MODES[0]
        self.success(plan["build"], plan["parameters"])
        original = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})["vehicle_id"]
        other = self.world.bind(1)
        unauthorized = self.world.action("vehicle.start_stop", {"vehicle_id": original}, actor=other)
        self.assertEqual(unauthorized["status"], "rejected", unauthorized)
        self.assertTrue(self.world.observe("vehicles")["items"][0]["stopped"])
        self.success("vehicle.sell", {"vehicle_id": original})
        replacement = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})["vehicle_id"]
        self.assertEqual(replacement["id"], original["id"])
        self.assertNotEqual(replacement["generation"], original["generation"])
        stale = self.world.action("vehicle.sell", {"vehicle_id": original})
        self.assertEqual(stale["status"], "rejected", stale)
        vehicles = self.world.observe("vehicles")["items"]
        self.assertEqual([item["vehicle_id"] for item in vehicles], [replacement])

    def test_checkpoint_restart_restores_receipts_and_world_effects(self):
        plan = MODES[0]
        self.success(plan["build"], plan["parameters"])
        action, queued = self.world.enqueue("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
        original = self.world.wait(action, queued)
        self.assertEqual(original["status"], "completed", original)
        rename, queued = self.world.enqueue("vehicle.rename", {"vehicle_id": original["result"]["vehicle_id"], "name": "Checkpoint vehicle"})
        renamed = self.world.wait(rename, queued)
        self.assertEqual(renamed["status"], "completed", renamed)
        # Establish enough pre-restart uptime to exercise a subsequent save's
        # frame bookkeeping instead of accidentally catching up during launch.
        time.sleep(0.6)
        checkpoint = self.world.checkpoint_now()
        self.assertTrue(Path(checkpoint["save_path"]).is_file())
        self.world.restart_checkpoint()
        restored = self.world.rpc("action.status", **action.fields())
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(restored["result"]["cost"], original["result"]["cost"])
        self.assertEqual(restored["result"]["checkpoint_state"], "checkpointed")
        duplicate = self.world.rpc("action.submit", **action.fields())
        self.assertEqual(duplicate["status"], "completed", duplicate)
        # Keep the original target handle and canonical payload on a historical
        # retry, even though a new command would need a fresh observed handle.
        duplicate_rename = self.world.rpc("action.submit", **rename.fields())
        self.assertEqual(duplicate_rename["status"], "completed", duplicate_rename)
        self.assertEqual(duplicate_rename["result"]["request_id"], rename.request_id)
        self.assertEqual(duplicate_rename["result"]["checkpoint_state"], "checkpointed")
        self.assertEqual(len(self.world.observe("vehicles")["items"]), 1)
        changed = copy.deepcopy(action.fields())
        changed["action"]["parameters"]["engine_id"] += 1
        conflict = self.world.rpc("action.submit", **changed)
        self.assertEqual(conflict["error"]["code"], "request_conflict")
        following = self.world.checkpoint_now()
        self.assertNotEqual(following["checkpoint_id"], checkpoint["checkpoint_id"])

    def test_handle_from_rolled_back_boot_cannot_control_replacement_vehicle(self):
        plan = MODES[0]
        self.success(plan["build"], plan["parameters"])
        self.assertEqual(self.world.observe("vehicles")["items"], [])
        self.world.checkpoint_now()
        previous_instance = self.world.instance
        previous_actor = self.world.actor
        old_action, queued = self.world.enqueue("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})
        completed = self.world.wait(old_action, queued)
        self.assertEqual(completed["status"], "completed", completed)
        original = completed["result"]["vehicle_id"]

        # Restore the real earlier save. The purchase was accepted after that
        # checkpoint, so recovery must record uncertainty and explicit rollback.
        self.world.restart_checkpoint()
        self.assertNotEqual(self.world.instance, previous_instance)
        uncertain = self.world.rpc("action.status", **old_action.fields())
        self.assertEqual(uncertain["status"], "uncertain", uncertain)
        self.assertEqual(uncertain["result"]["request_id"], old_action.request_id)
        resolved = self.world.rpc("recovery.resolve", **old_action.fields(), resolution="resolved_rolled_back")
        self.assertEqual(resolved["status"], "completed", resolved)
        rollback = self.world.rpc("action.status", **old_action.fields())
        self.assertEqual(rollback["status"], "resolved_rolled_back", rollback)
        self.assertEqual(self.world.observe("vehicles")["items"], [])

        self.world.actor = self.world.bind(0)
        self.assertNotEqual(self.world.actor["run_id"], previous_actor["run_id"])
        replacement = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})["vehicle_id"]
        self.assertEqual(replacement["id"], original["id"])
        # This is a fresh accepted request in the current engine envelope, not
        # a retry of the rolled-back purchase or an old private RPC frame.
        stale = self.world.action("vehicle.start_stop", {"vehicle_id": original})
        vehicles = self.world.observe("vehicles")["items"]
        self.assertEqual(stale["status"], "rejected", {"old_handle": original, "replacement_handle": replacement, "reply": stale, "vehicles": vehicles})
        self.assertEqual([item["vehicle_id"] for item in vehicles], [replacement])
        self.assertTrue(vehicles[0]["stopped"], vehicles)
        self.success("vehicle.start_stop", {"vehicle_id": replacement})
        self.assertFalse(self.world.observe("vehicles")["items"][0]["stopped"])


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"), "Set OPENTTD_TEST_BINARY and OPENTTD_TEST_BASESET for live engine acceptance tests")
class VisualRecoveryTests(unittest.TestCase):
    def test_visual_recovery_retains_explicit_rejection_after_checkpoint_restart(self):
        world = LiveWorld(os.environ["OPENTTD_TEST_BINARY"], os.environ["OPENTTD_TEST_BASESET"],
                          port=int(os.environ.get("OPENTTD_TEST_PORT", "13978")))
        self.addCleanup(world.close)
        # Keep the real startup recovery window open; the context manager would
        # finish it before an accepted backend request could be reconciled.
        world.start()
        observer = world.bind(0, identity="recovery-observer")
        visual = world.bind(0, identity="visual-recovery", mode="visual")
        before = world.observe("vehicles", actor=observer)["items"]
        self.assertEqual(before, [])

        # This is historical accepted input, never an executable submission.
        # Recovery must preserve uncertainty without requiring a live renderer
        # or manufacturing a successful UI command receipt.
        accepted = {
            "request_sequence": 1,
            "request_id": "visual-history-missing",
            "actor": visual,
            "action": {"name": "ui.apply", "parameters": {}},
            "visual_intent": {},
        }

        def receipt(reply, status):
            self.assertEqual(reply["status"], status, reply)
            result = reply["result"]
            self.assertEqual(result.get("request_id"), accepted["request_id"], reply)
            self.assertEqual(result.get("request_sequence"), accepted["request_sequence"], reply)
            self.assertNotIn("success", result, reply)
            return result

        receipt(world.rpc("recovery.reconcile", **accepted), "uncertain")
        world.finish_recovery()
        for altered_field in ("action", "visual_intent"):
            with self.subTest(resolve_conflict=altered_field):
                changed = copy.deepcopy(accepted)
                if altered_field == "action":
                    changed["action"]["parameters"] = {"image_id": "different-history"}
                else:
                    changed["visual_intent"] = {"image_id": "different-history"}
                conflict = world.rpc("recovery.resolve", **changed, resolution="rejected")
                self.assertEqual(conflict["status"], "rejected", conflict)
                self.assertEqual(conflict["error"]["code"], "request_conflict", conflict)
                receipt(world.rpc("action.status", **accepted), "uncertain")
        resolved = world.rpc("recovery.resolve", **accepted, resolution="rejected")
        self.assertEqual(resolved["status"], "completed", resolved)
        rejected = receipt(world.rpc("action.status", **accepted), "rejected")
        self.assertEqual(rejected.get("resolution"), "rejected", rejected)

        # Permitting the private recovery operations must retain the visual
        # actor's restriction against structured world observations.
        forbidden = world.rpc("observation.query", actor=visual, query={"name": "vehicles", "parameters": {}})
        self.assertEqual(forbidden["status"], "rejected", forbidden)
        self.assertEqual(forbidden["error"]["code"], "mode_restricted", forbidden)

        checkpoint = world.checkpoint_now()
        previous_instance = world.instance
        world.stop()
        world.start(checkpoint["save_path"])
        self.assertNotEqual(world.instance, previous_instance)
        restored = receipt(world.rpc("recovery.reconcile", **accepted), "rejected")
        self.assertEqual(restored.get("resolution"), "rejected", restored)
        self.assertEqual(restored.get("checkpoint_state"), "checkpointed", restored)
        changed = copy.deepcopy(accepted)
        changed["visual_intent"] = {"image_id": "different-history"}
        conflict = world.rpc("recovery.reconcile", **changed)
        self.assertEqual(conflict["status"], "rejected", conflict)
        self.assertEqual(conflict["error"]["code"], "request_conflict", conflict)
        world.finish_recovery()
        self.assertEqual(receipt(world.rpc("action.status", **accepted), "rejected"), restored)
        self.assertEqual(world.observe("vehicles", actor=observer)["items"], before)


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"), "Set OPENTTD_TEST_BINARY and OPENTTD_TEST_BASESET for live engine acceptance tests")
class NewWorldBootstrapTests(unittest.TestCase):
    def test_operator_can_create_and_bind_a_company_in_a_new_world(self):
        with LiveWorld(os.environ["OPENTTD_TEST_BINARY"], os.environ["OPENTTD_TEST_BASESET"],
                       port=int(os.environ.get("OPENTTD_TEST_PORT", "13978")), generate=True) as world:
            company = world.observe("company")
            self.assertEqual(company["company_id"], 0)
            self.assertGreater(company["balance"], 0)
            region = world.observe("map.region", {"x": 1, "y": 1, "width": 1, "height": 1})
            self.assertEqual((region["map_width"], region["map_height"]), (64, 64))
            self.assertEqual(world.observe("vehicles")["items"], [])
            rename = world.action("company.rename", {"name": "Fresh company"})
            self.assertEqual(rename["status"], "completed", rename)
            self.assertEqual(world.observe("company")["name"], "Fresh company")
