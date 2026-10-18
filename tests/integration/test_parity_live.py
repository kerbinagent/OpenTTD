"""Opt-in native acceptance for versioned orders and management/report parity.

Use the same binary/assets environment as test_engine_live.py. These tests own
isolated fixture servers; no generic action helper supplies missing versions.
"""

import json
import os
import unittest

from engine_harness import LiveWorld
from fixture_plans import MODES, PASSENGER_ROUTES


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"),
                     "Set integration binary and OpenGFX assets for native parity acceptance")
class LiveParityTests(unittest.TestCase):
    def setUp(self):
        self.world = LiveWorld(os.environ["OPENTTD_TEST_BINARY"], os.environ["OPENTTD_TEST_BASESET"],
                               port=int(os.environ.get("OPENTTD_TEST_PORT", "13975")))
        self.addCleanup(self.world.close)
        self.world.start()
        self.world.finish_recovery()
        self.world.actor = self.world.bind(0)

    def success(self, name, parameters):
        reply = self.world.action(name, parameters)
        self.assertEqual(reply["status"], "completed", reply)
        self.assertTrue(reply["result"].get("success"), reply)
        return reply["result"]

    def orders(self, vehicle):
        return self.world.observe("orders", {"vehicle_id": vehicle})

    def bus(self):
        plan = MODES[0]
        self.assertGreater(self.success(plan["build"], plan["parameters"])["cost"], 0)
        purchase = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": 116})
        self.assertGreater(purchase["cost"], 0)
        return purchase["vehicle_id"]

    def test_order_versions_shared_conditions_timetable_and_checkpoint_retry(self):
        plan = PASSENGER_ROUTES[0]
        for name, parameters in plan["construction"]:
            self.assertGreater(self.success(name, parameters)["cost"], 0)
        vehicle = self.success("vehicle.build", {"tile": plan["depot"], "engine_id": plan["engine"]})["vehicle_id"]
        empty = self.orders(vehicle)
        self.assertEqual(empty["orders"], [])
        before_cash = self.world.observe("company")["balance"]
        missing, reply = self.world.enqueue("order.insert", {"vehicle_id": vehicle,
                                                             "destination": plan["destinations"][0]})
        self.assertEqual(reply["status"], "rejected", reply)
        self.assertEqual(reply["error"]["code"], "invalid_targets", reply)
        self.assertEqual(self.orders(vehicle)["order_version"], empty["order_version"])
        self.assertEqual(self.world.observe("company")["balance"], before_cash)
        for destination in plan["destinations"]:
            version = self.orders(vehicle)["order_version"]
            self.success("order.insert", {"vehicle_id": vehicle, "order_version": version,
                                           "destination": destination})
        ordinary = self.orders(vehicle)
        stale, reply = self.world.enqueue("order.move", {"vehicle_id": vehicle,
                                                         "order_version": empty["order_version"],
                                                         "position": 0, "target_position": 1})
        self.assertEqual(reply["status"], "rejected", reply)
        self.assertEqual(reply["error"]["code"], "invalid_targets", reply)
        self.assertEqual(self.orders(vehicle)["orders"], ordinary["orders"])
        self.assertEqual(self.world.observe("company")["balance"], before_cash)

        # The version fences the sharing cohort as well as positional contents.
        clone = self.success("vehicle.clone", {"tile": plan["depot"], "vehicle_id": vehicle,
                                               "share_orders": True})["vehicle_id"]
        shared = self.orders(vehicle)
        self.assertEqual(shared["shared_count"], 2)
        self.assertEqual({v["id"] for v in shared["shared_vehicles"]}, {vehicle["id"], clone["id"]})
        self.assertNotEqual(shared["order_version"], ordinary["order_version"])
        _, rejected = self.world.enqueue("order.delete", {"vehicle_id": vehicle,
                                                          "order_version": ordinary["order_version"], "position": 0})
        self.assertEqual(rejected["status"], "rejected", rejected)
        version = self.orders(vehicle)["order_version"]
        self.success("order.modify", {"vehicle_id": vehicle, "order_version": version,
                                       "position": 0, "field": "load", "value": "full"})
        self.assertEqual(self.orders(clone)["orders"][0]["load_policy"], "full")
        version = self.orders(clone)["order_version"]
        self.success("order.unshare", {"vehicle_id": clone, "order_version": version, "mode": "keep"})
        self.assertEqual(self.orders(clone)["shared_count"], 1)
        self.assertEqual(self.orders(vehicle)["shared_count"], 1)

        version = self.orders(clone)["order_version"]
        self.success("order.conditional.insert", {"vehicle_id": clone, "order_version": version,
                                                   "position": 1, "target_position": 0,
                                                   "variable": "load_percentage", "comparator": "lt", "value": 50})
        conditional = self.orders(clone)
        self.assertEqual(conditional["orders"][1]["kind"], "conditional")
        self.assertEqual(conditional["orders"][1]["condition"],
                         {"variable": "load_percentage", "comparator": "lt", "value": 50, "target_position": 0})
        version = self.orders(clone)["order_version"]
        self.success("order.modify", {"vehicle_id": clone, "order_version": version,
                                       "position": 1, "field": "condition_value", "value": 75})
        for field, value in (("wait_ticks", 20), ("travel_ticks", 40), ("speed_limit", 25)):
            version = self.orders(clone)["order_version"]
            self.success("timetable.set", {"vehicle_id": clone, "order_version": version,
                                            "position": 0, "field": field, "value": value})
        timetable = self.world.observe("timetable", {"vehicle_id": clone})
        self.assertEqual([timetable["orders"][0][field] for field in ("wait_ticks", "travel_ticks", "speed_limit")],
                         [20, 40, 25])
        version = self.orders(clone)["order_version"]
        self.success("timetable.autofill", {"vehicle_id": clone, "order_version": version,
                                             "enabled": True, "preserve_wait_time": True})
        self.assertTrue(self.world.observe("timetable", {"vehicle_id": clone})["autofill"])
        version = self.orders(clone)["order_version"]
        reset, initial = self.world.enqueue("timetable.autofill", {"vehicle_id": clone,
                                                                   "order_version": version, "enabled": False})
        original = self.world.wait(reset, initial)
        self.assertEqual(original["status"], "completed", original)
        self.assertFalse(self.world.observe("timetable", {"vehicle_id": clone})["autofill"])
        self.world.checkpoint_now()
        self.world.restart_checkpoint()
        replay = self.world.rpc("action.submit", **reset.fields())
        self.assertEqual(replay["status"], "completed", replay)
        self.assertEqual(replay["result"]["request_id"], reset.request_id)
        self.assertEqual(replay["result"]["checkpoint_state"], "checkpointed")
        fresh = next(v["vehicle_id"] for v in self.world.observe("vehicles")["items"]
                     if v["vehicle_id"]["id"] == clone["id"])
        restored = self.world.observe("timetable", {"vehicle_id": fresh})
        self.assertEqual(restored["orders"][1]["condition"]["value"], 75)
        self.assertEqual(restored["orders"][0]["wait_ticks"], 20)
        self.assertFalse(restored["autofill"])
        self.assertNotEqual(restored["order_version"], version)
        # Historical rejected inputs also retain their outcomes after recovery.
        self.assertEqual(self.world.rpc("action.status", **missing.fields())["status"], "rejected")
        self.assertEqual(self.world.rpc("action.status", **stale.fields())["status"], "rejected")

    def test_management_signs_service_and_appearance_persist(self):
        vehicle = self.bus()
        parent = self.success("group.create", {"vehicle_type": "road"})["group_id"]
        child = self.success("group.create", {"vehicle_type": "road", "parent_group_id": parent})["group_id"]
        self.success("group.rename", {"group_id": child, "name": "Parity buses"})
        self.success("group.vehicle.move", {"vehicle_id": vehicle, "group_id": child})
        self.success("group.protection.set", {"group_id": parent, "enabled": True, "recursive": True})
        self.success("group.livery.set", {"group_id": child, "channel": "primary", "colour": "blue"})
        groups = self.world.observe("groups")["items"]
        observed = next(g for g in groups if g["group_id"] == child)
        self.assertEqual(observed["name"], "Parity buses")
        self.assertEqual(observed["parent_group_id"], parent)
        self.assertTrue(observed["replace_protection"])
        self.assertTrue(observed["livery"]["primary_override"])
        self.assertEqual(self.world.observe("group.stats", {"scope": "group", "group_id": child})["vehicle_count"], 1)
        self.assertEqual(self.world.observe("group.stats", {"scope": "default", "vehicle_type": "road"})["vehicle_count"], 0)
        self.success("vehicle.service_interval.set", {"vehicle_id": vehicle, "mode": "custom",
                                                       "interval": 200, "percent": False})
        service = self.world.observe("vehicles")["items"][0]
        self.assertTrue(service["service_custom"])
        self.assertFalse(service["service_percent"])
        self.assertEqual(service["service_interval"], 200)

        sign = self.success("sign.place", {"tile": 40529, "name": "Parity sign"})["sign_id"]
        self.success("sign.rename", {"sign_id": sign, "name": "Moved parity sign"})
        self.success("sign.move", {"sign_id": sign, "tile": 40530})
        observed_sign = next(s for s in self.world.observe("signs")["items"] if s["sign_id"] == sign)
        self.assertEqual(observed_sign["name"], "Moved parity sign")
        self.assertEqual(observed_sign["location"]["tile"], 40530)
        self.success("company.president.rename", {"name": "Parity president"})
        self.success("company.livery.set", {"scheme": "bus", "channel": "primary", "colour": "blue"})
        appearance = self.world.observe("company.appearance")
        self.assertEqual(appearance["president_name"], "Parity president")
        bus_livery = next(l for l in appearance["liveries"] if l["scheme"] == "bus")
        self.assertEqual(bus_livery["primary"], "blue")
        self.assertFalse(bus_livery["primary_inherited"])
        face = appearance["manager_face"]
        component = next(c for c in face["components"] if c["active"] and c["value_count"] > 1)
        face_value = (component["value"] + 1) % component["value_count"]
        self.success("company.manager_face.component.set", {"expected_style_id": face["style_id"],
                                                              "component_id": component["component_id"],
                                                              "value": face_value})
        self.world.checkpoint_now()
        self.world.restart_checkpoint()
        restored = self.world.observe("vehicles")["items"][0]
        self.assertEqual(restored["service_interval"], 200)
        self.assertTrue(restored["service_custom"])
        restored_group = next(g for g in self.world.observe("groups")["items"] if g["group_id"]["id"] == child["id"])
        self.assertEqual(restored_group["name"], "Parity buses")
        self.assertEqual(restored["group_id"], restored_group["group_id"])
        self.assertTrue(restored_group["replace_protection"])
        appearance = self.world.observe("company.appearance")
        self.assertEqual(appearance["president_name"], "Parity president")
        restored_component = next(c for c in appearance["manager_face"]["components"]
                                  if c["component_id"] == component["component_id"])
        self.assertEqual(restored_component["value"], face_value)
        fresh_sign = next(s for s in self.world.observe("signs")["items"] if s["sign_id"]["id"] == sign["id"])
        self.assertEqual(fresh_sign["location"]["tile"], 40530)
        self.success("sign.delete", {"sign_id": fresh_sign["sign_id"]})
        self.assertFalse(any(s["sign_id"]["id"] == sign["id"] for s in self.world.observe("signs")["items"]))

    def test_bounded_reports_map_paging_and_native_prequeue_failure(self):
        before = self.world.observe("company")
        world = self.world.observe("world")
        self.assertEqual(world["map"]["width"], 256)
        self.assertEqual(world["timekeeping"]["ticks_per_day"], 74)
        report = self.world.observe("company.report")
        self.assertEqual(report["company"]["id"], 0)
        self.assertEqual(report["loan"], before["loan"])
        self.assertTrue(all("year" in q and "quarter" in q for q in report["quarter_history"]))
        self.assertIn("construction", report["yearly_expenses"][0]["categories"])
        town = self.world.observe("towns", {"limit": 1})["items"][0]["town"]
        authority = self.world.observe("town.authority", {"town_id": town})
        self.assertEqual(authority["town_id"], town)
        self.assertEqual(len(authority["actions"]), 8)
        site_parameters = {"tile": 40529, "direction": 2, "stop_type": "bus"}
        site = self.world.observe("station.site", {"build_action": "road.stop.build", "build_parameters": site_parameters})
        self.assertEqual(site["feasibility"]["status"], "query_test_passed")
        self.assertFalse(site["feasibility"]["cash_checked"])
        self.assertFalse(site["feasibility"]["reserved"])
        self.assertFalse(site["feasibility"]["build_result_guaranteed"])
        self.assertTrue(any(c["cargo_id"] == 0 and c["preview_threshold_met"] for c in site["accepted_cargo"]))
        self.assertEqual(self.world.observe("company")["balance"], before["balance"])
        self.assertNotIn("station_id", self.world.observe("map.region", {"x": 81, "y": 158, "width": 1, "height": 1})["tiles"][0])

        seen = []
        after = -1
        while True:
            page = self.world.observe("map.region", {"x": 10, "y": 10, "width": 64, "height": 64,
                                                     "after_offset": after, "limit": 100})
            self.assertLessEqual(len(page["tiles"]), 100)
            self.assertTrue(page["tiles"])
            self.assertEqual(page["page_tick"], page["tick"])
            self.assertLess(len(json.dumps(page).encode()), 65536)
            seen.extend(t["tile"] for t in page["tiles"])
            if page["complete"]:
                self.assertIsNone(page["next_after_offset"])
                break
            self.assertGreater(page["next_after_offset"], after)
            after = page["next_after_offset"]
        self.assertEqual(seen, [(10 + y) * 256 + 10 + x for y in range(64) for x in range(64)])

        plan = MODES[0]
        self.success(plan["build"], plan["parameters"])
        before_cash = self.world.observe("company")["balance"]
        before_tile = self.world.observe("map.region", {"x": plan["depot"] % 256, "y": plan["depot"] // 256,
                                                       "width": 1, "height": 1})["tiles"]
        blocked_parameters = {"tile": plan["depot"], "direction": 2, "stop_type": "bus"}
        failure, rejected = self.world.enqueue("road.stop.build", blocked_parameters)
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(rejected["error"]["code"], "gameplay_rejected", rejected)
        self.assertFalse(rejected["result"]["success"])
        self.assertEqual(rejected["result"]["cost"], 0)
        self.assertTrue(rejected["result"]["error"]["message"])
        self.assertNotEqual(rejected["result"]["error"]["message"], "The game rejected the action")
        self.assertEqual(self.world.observe("company")["balance"], before_cash)
        self.assertEqual(self.world.observe("map.region", {"x": plan["depot"] % 256, "y": plan["depot"] // 256,
                                                          "width": 1, "height": 1})["tiles"], before_tile)
        # Rejection is terminal in the initial reply; exact retries and recovery
        # retain its native human-visible error without scheduling a command.
        replay = self.world.rpc("action.submit", **failure.fields())
        self.assertEqual(replay["result"], rejected["result"])
        self.world.checkpoint_now()
        self.world.restart_checkpoint()
        restored = self.world.rpc("action.submit", **failure.fields())
        self.assertEqual(restored["status"], "rejected", restored)
        self.assertEqual(restored["result"]["error"], rejected["result"]["error"])
        self.assertEqual(restored["result"]["cost"], 0)
        self.assertEqual(restored["result"]["checkpoint_state"], "checkpointed")
