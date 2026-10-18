"""Finite positive DTOs for selected native decision-information reports."""

import unittest

from backend import projection


BASE = {"tick": 7, "company_id": 2}


def spec(name, *fields):
    return {"name": name, "result_fields": ["tick", "company_id", *fields]}


class DecisionProjectionTest(unittest.TestCase):
    def test_payment_rates_preserves_all_native_samples_and_negative_money(self):
        samples = [{"transit_periods": (i + 1) * 4,
                    "display_time": (i + 1) * 10,
                    "income": -i, "callback_id": 999} for i in range(20)]
        raw = {**BASE, "cargo_id": 3, "amount": 10, "distance_tiles": 20,
               "money_unit": "native_money", "time_unit": "economy_days",
               "income_scope": "base_delivery", "samples": samples,
               "private_profit_formula": "hidden"}
        fields = ("cargo_id", "amount", "distance_tiles", "money_unit",
                  "time_unit", "income_scope", "samples")
        projected = projection.observation(raw, spec("cargo.payment_rates", *fields))
        self.assertEqual(len(projected["samples"]), 20)
        self.assertEqual(projected["samples"][-1], {
            "transit_periods": 80, "display_time": 200, "income": -19})
        self.assertEqual(projected["samples"][0]["income"], 0)
        self.assertNotIn("private_profit_formula", projected)
        invalid = projection.observation({**raw, "cargo_id": True,
            "samples": [{**samples[0], "income": True}]},
            spec("cargo.payment_rates", *fields))
        self.assertNotIn("cargo_id", invalid)
        self.assertEqual(invalid["samples"], [{"transit_periods": 4,
                                               "display_time": 10}])

    def test_bridge_catalog_preserves_thirteen_rows_and_null_vs_zero_speed(self):
        rows = [{"bridge_type": i, "material": "Béton" if i == 0 else f"Type {i}",
                 "material_truncated": False, "minimum_span_tiles": 0,
                 "maximum_span_tiles": 12 + i,
                 "maximum_speed": None if i == 0 else 0 if i == 1 else 100 + i,
                 "internal_price_multiplier": 45} for i in range(13)]
        raw = {**BASE, "transports": ["rail", "road"],
               "span_unit": "interior_tiles_excluding_heads",
               "speed_unit": "kmh_ish", "items": rows, "private": True}
        projected = projection.observation(raw, spec("catalog.bridges",
            "transports", "span_unit", "speed_unit", "items"))
        self.assertEqual(len(projected["items"]), 13)
        self.assertEqual(projected["items"][0]["material"], "Béton")
        self.assertIsNone(projected["items"][0]["maximum_speed"])
        self.assertEqual(projected["items"][1]["maximum_speed"], 0)
        self.assertNotIn("private", projected)
        self.assertTrue(all("internal_price_multiplier" not in row
                            for row in projected["items"]))
        invalid = projection.observation({**raw, "items": [{**rows[0],
            "maximum_speed": True, "bridge_type": True}]},
            spec("catalog.bridges", "items"))
        self.assertNotIn("maximum_speed", invalid["items"][0])
        self.assertNotIn("bridge_type", invalid["items"][0])

    def test_purchase_info_retains_applicable_fields_without_inventing_others(self):
        performance = {"designed_year": 1950, "life_calendar_days": 9000,
                       "reliability_percent": 76, "empty_weight_tonnes": 64,
                       "power_hp": 1600, "maximum_tractive_effort_newtons": 123456,
                       "powered_wagon_power_hp": 500,
                       "powered_wagon_extra_weight_tonnes": 12,
                       "raw_callbacks": [123]}
        raw = {**BASE, "engine_id": 42, "vehicle_type": "rail",
               "speed_unit": "kmh_ish", "performance": performance,
               "raw_purchase_price": 1234}
        fields = ("engine_id", "vehicle_type", "speed_unit", "performance")
        projected = projection.observation(raw, spec("engine.purchase_info", *fields))
        self.assertEqual(projected["performance"], {
            key: value for key, value in performance.items() if key != "raw_callbacks"})
        self.assertNotIn("raw_purchase_price", projected)
        ship = projection.observation({**raw, "vehicle_type": "ship",
            "performance": {"designed_year": 1960, "life_calendar_days": 7000,
                            "reliability_percent": 85, "ocean_speed": 20,
                            "canal_speed": 0}},
            spec("engine.purchase_info", *fields))
        self.assertEqual(ship["performance"], {"designed_year": 1960,
            "life_calendar_days": 7000, "reliability_percent": 85,
            "ocean_speed": 20, "canal_speed": 0})
        self.assertNotIn("power_hp", ship["performance"])
        wagon = projection.observation({**raw, "engine_id": True,
            "performance": {"empty_weight_tonnes": 17,
                            "power_hp": True, "ocean_speed": None}},
            spec("engine.purchase_info", *fields))
        self.assertNotIn("engine_id", wagon)
        self.assertEqual(wagon["performance"], {"empty_weight_tonnes": 17})

    def test_vehicle_status_uses_catalog_allowlist_and_foreign_redaction(self):
        handle = {"id": 5, "generation": 9, "engine_instance_id": "boot"}
        raw = {**BASE, "vehicle_id": handle, "owned_by_actor": False,
               "can_refit_now": True,
               "status": {"kind": "heading_to_station", "text": "En route à Montréal",
                          "text_truncated": False, "raw_order_flags": 123},
               "parts": [{"position": 0, "refittable_cargo_ids": [1]}]}
        fields = ("vehicle_id", "owned_by_actor", "can_refit_now", "status", "parts")
        projected = projection.observation(raw, spec("vehicle.details", *fields))
        self.assertEqual(projected["status"], {"kind": "heading_to_station",
            "text": "En route à Montréal", "text_truncated": False})
        self.assertNotIn("can_refit_now", projected)
        self.assertNotIn("refittable_cargo_ids", projected["parts"][0])
        old_spec = spec("vehicle.details", "vehicle_id", "owned_by_actor")
        self.assertNotIn("status", projection.observation(raw, old_spec))
        invalid = projection.observation({**raw, "status": {
            "kind": "other", "text": "", "text_truncated": 0}},
            spec("vehicle.details", "status"))
        self.assertEqual(invalid["status"], {"kind": "other", "text": ""})


if __name__ == "__main__":
    unittest.main()
