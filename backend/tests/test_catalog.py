import copy
import unittest

from backend.capabilities import matrix
from backend.catalog import (
    CATALOG, CATALOG_ACTION_NAMES, CATALOG_HASH, CATALOG_HASH_ALGORITHM,
    CATALOG_QUERY_NAMES, action_spec, query_spec, validate_parameters,
    verify_engine_capabilities,
)
from backend.errors import ServiceError
from backend import projection


class CatalogTest(unittest.TestCase):
    def test_compact_engine_capabilities_match_catalog_identity(self):
        self.assertEqual(CATALOG_HASH_ALGORITHM, "blake2b-256")
        self.assertEqual(CATALOG_HASH,
                         "083a13031161b652d3892f756d1dc5a097f8b1991383174a075ce2fa09ff85a3")
        capabilities = {key: copy.deepcopy(value) for key, value in CATALOG.items()
                        if key not in ("actions", "queries")}
        capabilities.update(actions=list(CATALOG_ACTION_NAMES), queries=list(CATALOG_QUERY_NAMES),
                            catalog_hash_algorithm=CATALOG_HASH_ALGORITHM,
                            catalog_hash=CATALOG_HASH)
        self.assertIsNone(verify_engine_capabilities(capabilities))

        mutations = {
            "missing hash": lambda c: c.pop("catalog_hash"),
            "wrong hash": lambda c: c.update(catalog_hash="0" * 64),
            "wrong algorithm": lambda c: c.update(catalog_hash_algorithm="sha256"),
            "boolean version": lambda c: c.update(version=True),
            "missing action": lambda c: c["actions"].pop(),
            "duplicate action": lambda c: c["actions"].append(c["actions"][0]),
            "reordered actions": lambda c: c["actions"].reverse(),
            "missing query": lambda c: c["queries"].pop(),
            "duplicate query": lambda c: c["queries"].append(c["queries"][0]),
            "reordered queries": lambda c: c["queries"].reverse(),
            "full action spec": lambda c: c["actions"].__setitem__(0, CATALOG["actions"][0]),
            "changed metadata": lambda c: c["limits"].update(max_batch_actions=999),
            "extra field": lambda c: c.update(private=True),
        }
        for label, mutate in mutations.items():
            candidate = copy.deepcopy(capabilities)
            mutate(candidate)
            with self.subTest(label=label), self.assertRaises(ServiceError) as raised:
                verify_engine_capabilities(candidate)
            self.assertEqual(raised.exception.code, "catalog_mismatch")
            self.assertEqual(raised.exception.status, 503)

    def test_catalog_is_engine_generated_surface(self):
        self.assertEqual(CATALOG["version"], 1)
        self.assertEqual(len(CATALOG["actions"]), 104)
        self.assertEqual(len(CATALOG["queries"]), 60)
        self.assertEqual(action_spec("ui.apply")["modes"], ["visual"])
        self.assertEqual(sum(spec["modes"] == ["text", "visual"] for spec in CATALOG["actions"]), 103)
        self.assertIsNone(action_spec("tool.apply"))
        self.assertIsNone(query_spec("raw.command"))

    def test_land_info_uses_shared_expensive_observation_admission(self):
        self.assertEqual(CATALOG["limits"]["land_info_interval_ms"], 1000)
        self.assertIn("land information",
                      CATALOG["conventions"]["observation_intervals"])
        convention = CATALOG["conventions"]["land_info"]
        self.assertIn("query-local callback PSA writes", convention)
        self.assertIn("no funds are checked or reserved", convention)
        self.assertEqual(query_spec("land.info")["modes"], ["text"])

    def test_decision_estimate_and_station_site_actual_catalog_contract(self):
        expected_queries = {
            "cargo.payment_rates": ("cargo_id", 63,
                ["tick", "company_id", "cargo_id", "amount", "distance_tiles",
                 "money_unit", "time_unit", "income_scope", "samples"]),
            "engine.purchase_info": ("engine_id", 63999,
                ["tick", "company_id", "engine_id", "vehicle_type",
                 "speed_unit", "performance"]),
        }
        for name, (key, maximum, fields) in expected_queries.items():
            with self.subTest(name=name):
                query = query_spec(name)
                self.assertEqual(query["modes"], ["text"])
                self.assertEqual(query["result_fields"], fields)
                self.assertEqual(query["parameters"], {"type": "object",
                    "required": [key], "additionalProperties": False,
                    "properties": {key: {"type": "integer", "minimum": 0,
                                         "maximum": maximum}}})
                self.assertEqual(validate_parameters(query, {key: maximum}),
                                 {key: maximum})
                for invalid in ({key: True}, {key: maximum + 1}, {key: 0, "private": 1}):
                    with self.assertRaises(ServiceError):
                        validate_parameters(query, invalid)
        bridges = query_spec("catalog.bridges")
        self.assertEqual(bridges["modes"], ["text"])
        self.assertEqual(bridges["parameters"], {"type": "object", "required": [],
            "additionalProperties": False, "properties": {}})
        self.assertEqual(bridges["result_fields"], ["tick", "company_id",
            "transports", "span_unit", "speed_unit", "items"])
        self.assertEqual(validate_parameters(bridges, {}), {})
        with self.assertRaises(ServiceError):
            validate_parameters(bridges, {"limit": 1})

        details = query_spec("vehicle.details")
        self.assertEqual(details["result_fields"].count("status"), 1)
        site = query_spec("station.site")
        self.assertEqual(site["modes"], ["text"])
        branches = site["parameters"]["anyOf"]
        self.assertEqual(len(branches), 4)
        for branch in branches:
            self.assertEqual(branch["required"], ["build_action", "build_parameters"])
            self.assertIs(branch["additionalProperties"], False)
            action = branch["properties"]["build_action"]["enum"][0]
            parameters = branch["properties"]["build_parameters"]
            self.assertEqual(parameters, action_spec(action)["parameters"])
            self.assertEqual({"spec_class", "spec_index"} <=
                             set(parameters["properties"]),
                             action in ("rail.station.build", "road.stop.build"))
        self.assertEqual({branch["properties"]["build_action"]["enum"][0]
                          for branch in branches}, {"rail.station.build",
                          "road.stop.build", "water.dock.build", "airport.build"})
        rail = {"build_action": "rail.station.build", "build_parameters": {
            "tile": 1, "axis": "x", "tracks": 1, "length": 64,
            "spec_class": 0, "spec_index": 0}}
        self.assertEqual(validate_parameters(site, rail), rail)
        for invalid in ({**rail, "build_parameters": {**rail["build_parameters"],
                         "length": 65}},
                        {"build_action": "water.dock.build", "build_parameters": {
                            "tile": 1, "spec_class": 0}}):
            with self.assertRaises(ServiceError):
                validate_parameters(site, invalid)

        estimate = CATALOG["conventions"]["action_estimate"]
        self.assertEqual(estimate["endpoint"], "/v1/actions/estimate")
        self.assertEqual(estimate["modes"], ["text", "visual"])
        self.assertEqual(estimate["result_fields"], ["success", "cost", "error",
                                                      "advisory", "test_and_execute_may_differ"])
        limits = CATALOG["limits"]
        self.assertEqual({key: limits[key] for key in (
            "estimate_interval_ms", "estimate_result_bytes",
            "station_site_interval_ms", "station_site_name_bytes")}, {
                "estimate_interval_ms": 1000, "estimate_result_bytes": 4096,
                "station_site_interval_ms": 1000, "station_site_name_bytes": 64})

    def test_actual_custom_news_catalog_has_closed_cursor_and_disjoint_text_kind(self):
        news = query_spec("news.custom")
        self.assertEqual(news["modes"], ["text"])
        self.assertEqual(news["target_kind"], "none")
        self.assertEqual(news["result_fields"], ["tick", "company_id",
            "history_scope", "history_start", "latest", "gap", "items",
            "next", "complete"])
        params = news["parameters"]
        self.assertEqual((params["type"], params["required"],
                          params["additionalProperties"]), ("object", [], False))
        self.assertEqual(set(params["properties"]), {"after", "limit"})
        self.assertEqual(params["properties"]["limit"], {
            "type": "integer", "minimum": 1, "maximum": 32})
        cursor = params["properties"]["after"]
        self.assertEqual(cursor["required"], ["id", "generation",
                                               "engine_instance_id"])
        self.assertIs(cursor["additionalProperties"], False)
        self.assertEqual(cursor["properties"], {
            "id": {"type": "integer", "minimum": 0,
                   "maximum": 0x7FFFFFFFFFFFFFFF},
            "generation": {"type": "integer", "minimum": 1,
                           "maximum": 0xFFFFFFFFFFFFFFFF},
            "engine_instance_id": {"type": "string", "minLength": 1,
                                   "maxLength": 256}})
        after = {"id": 0x7FFFFFFFFFFFFFFF,
                 "generation": 0xFFFFFFFFFFFFFFFF,
                 "engine_instance_id": "b" * 256}
        self.assertEqual(validate_parameters(news, {}), {})
        self.assertEqual(validate_parameters(news, {"after": after, "limit": 32}),
                         {"after": after, "limit": 32})
        for invalid in ({"after": {**after, "id": True}},
                        {"after": {**after, "id": 1 << 63}},
                        {"after": {**after, "generation": 0}},
                        {"after": {**after, "private": 1}},
                        {"after": {"id": 0, "generation": 1}},
                        {"limit": 0}, {"limit": 33}, {"company_id": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ServiceError):
                validate_parameters(news, invalid)

        display = query_spec("display.text")
        branches = display["parameters"]["anyOf"]
        self.assertEqual(len(branches), 14)
        news_branches = [branch for branch in branches
                         if branch["properties"]["kind"]["enum"] ==
                         ["news_custom_text"]]
        self.assertEqual(len(news_branches), 2)
        self.assertTrue(all(branch["additionalProperties"] is False
                            for branch in news_branches))
        self.assertEqual({tuple(branch["required"]) for branch in news_branches}, {
            ("kind", "target"),
            ("kind", "target", "after_byte", "expected_content_version")})
        for branch in news_branches:
            target = branch["properties"]["target"]
            self.assertEqual(target["properties"]["id"], {
                "type": "integer", "minimum": 1,
                "maximum": 0x7FFFFFFFFFFFFFFF})
            self.assertEqual(target["properties"]["generation"],
                             cursor["properties"]["generation"])
            self.assertEqual(target["properties"]["engine_instance_id"],
                             cursor["properties"]["engine_instance_id"])
            self.assertEqual(branch["properties"]["limit"], {
                "type": "integer", "minimum": 1, "maximum": 2048})
            self.assertEqual(branch["properties"]["expected_content_version"], {
                "type": "string", "minLength": 64, "maxLength": 64})
        first = {"kind": "news_custom_text", "target": {**after, "id": 1}}
        self.assertEqual(validate_parameters(display, first), first)
        continued = {**first, "after_byte": 1,
                     "expected_content_version": "a" * 64}
        self.assertEqual(validate_parameters(display, continued), continued)
        for invalid in ({**first, "target": {**after, "id": 0}},
                        {**first, "after_byte": 1},
                        {**continued, "after_byte": True},
                        {**first, "private": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ServiceError):
                validate_parameters(display, invalid)
        old_town = next(branch for branch in branches if "town_text" in
                        branch["properties"]["kind"]["enum"] and
                        "after_byte" not in branch["required"])
        self.assertEqual(old_town["properties"]["target"]["properties"]["id"], {
            "type": "integer", "minimum": 0, "maximum": 63999})
        self.assertEqual({key: CATALOG["limits"][key] for key in (
            "custom_news_rows", "custom_news_encoded_bytes",
            "custom_news_item_encoded_bytes", "custom_news_page_rows")}, {
                "custom_news_rows": 1024,
                "custom_news_encoded_bytes": 4194304,
                "custom_news_item_encoded_bytes": 65536,
                "custom_news_page_rows": 32})
        self.assertIn("live native names/language",
                      CATALOG["conventions"]["custom_news"])

    def test_shared_actions_leave_observations_text_only_and_reference_selectors_closed(self):
        self.assertTrue(all(spec["modes"] == ["text"] for spec in CATALOG["queries"]))
        reference = CATALOG["conventions"]["control_reference"]
        self.assertEqual(reference["result_fields"],
                         ["kind", "reference", "order_version", "page", "page_version", "engine_id"])
        valid = [
            {"kind": "vehicle", "id": 3},
            {"kind": "vehicle", "vehicle_type": "road", "unit_number": 1},
            {"kind": "station", "tile": {"x": 2, "y": 3}},
            {"kind": "depot", "tile": 10},
            {"kind": "company", "id": 14},
            {"kind": "question", "id": 65535},
            {"kind": "engine", "vehicle_type": "rail", "name": "Kirby Paul Tank"},
        ]
        for body in valid:
            with self.subTest(body=body):
                self.assertEqual(validate_parameters(reference, body), body)
        for body in (
            {"kind": "vehicle", "id": True},
            {"kind": "vehicle", "id": 3, "generation": 1, "engine_instance_id": "old"},
            {"kind": "vehicle", "vehicle_type": "road", "unit_number": 0},
            {"kind": "company", "id": 15},
            {"kind": "station", "tile": {"x": 2, "y": 3, "width": 4}},
            {"kind": "engine", "vehicle_type": "rail", "name": ""},
            {"kind": "vehicle", "after_id": 0, "limit": 100},
        ):
            with self.subTest(body=body), self.assertRaises(ServiceError):
                validate_parameters(reference, body)

    def test_version_pattern_is_enforced_before_forwarding(self):
        spec = {"parameters": {"type": "object", "required": ["version"],
                "additionalProperties": False, "properties": {
                    "version": {"type": "string", "minLength": 69, "maxLength": 69,
                                "pattern": "^swh1:[0-9a-f]{64}$"}}}}
        version = "swh1:" + "a" * 64
        self.assertEqual(validate_parameters(spec, {"version": version}), {"version": version})
        for invalid in ("x" * 69, "swh1:" + "A" * 64, version + "\n", version[:-1]):
            with self.subTest(invalid=invalid), self.assertRaises(ServiceError):
                validate_parameters(spec, {"version": invalid})

    def test_parameter_shape_and_handles(self):
        build = action_spec("road.build")
        self.assertEqual(validate_parameters(build, {"start": {"x": 2, "y": 4}, "end": 17}), {
            "start": {"x": 2, "y": 4}, "end": 17})
        insert = action_spec("order.insert")
        # Shape validation deliberately cannot assert whether this generation is still live.
        example = {"vehicle_id": {"id": 23, "generation": 9999, "engine_instance_id": "boot-a"},
                   "position": 0, "destination_id": 5, "order_type": "go_to_station"}
        # Use a required handle from a simpler canonical action.
        vehicle = action_spec("vehicle.start_stop")
        self.assertEqual(validate_parameters(vehicle, {"vehicle_id": example["vehicle_id"]}),
                         {"vehicle_id": example["vehicle_id"]})
        for bad in (
            {"start": True, "end": 17},
            {"start": 1, "end": 17, "secret": 1},
            {"start": {"x": True, "y": 2}, "end": 17},
            {"start": {"x": 0, "y": 0, "debug": 1}, "end": 17},
            {"start": -1, "end": 17},
            {"start": 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ServiceError) as raised:
                validate_parameters(build, bad)
            self.assertEqual(raised.exception.code, "invalid_request")
        for bad_handle in (
            {"id": 23, "generation": 9999},
            {"id": 23, "generation": 9999, "engine_instance_id": ""},
            {"id": 23, "generation": 9999, "engine_instance_id": "x" * 257},
            {"id": 23, "generation": 9999, "engine_instance_id": 123},
            {"id": 23, "generation": 9999, "engine_instance_id": "boot-a", "private": 1},
        ):
            with self.subTest(handle=bad_handle), self.assertRaises(ServiceError) as raised:
                validate_parameters(vehicle, {"vehicle_id": bad_handle})
            self.assertEqual(raised.exception.code, "invalid_request")
        # The backend checks the opaque field's shape; only C++ can compare it
        # with the running engine instance and live entity generation.
        self.assertEqual(validate_parameters(vehicle, {"vehicle_id": {
            "id": 23, "generation": 9999, "engine_instance_id": "other-boot"}})["vehicle_id"]["engine_instance_id"],
            "other-boot")
        self.assertIsNotNone(insert)

    def test_string_enum_and_length(self):
        spec = action_spec("road.stop.build")
        base = {"tile": 1, "direction": 0, "stop_type": "bus"}
        self.assertEqual(validate_parameters(spec, base), base)
        with self.assertRaises(ServiceError):
            validate_parameters(spec, {**base, "stop_type": "helicopter"})
        rename = action_spec("vehicle.rename")
        name_schema = rename["parameters"]["properties"]["name"]
        if "maxLength" in name_schema:
            with self.assertRaises(ServiceError):
                validate_parameters(rename, {"vehicle_id": {"id": 1, "generation": 2, "engine_instance_id": "boot-a"},
                                             "name": "x" * (name_schema["maxLength"] + 1)})

    def test_nested_observation_projection(self):
        raw = {
            "tick": 8, "company_id": 1, "engine_secret": "private",
            "items": [{"vehicle_id": {"id": 3, "generation": 4, "engine_instance_id": "boot-a", "pointer": 123},
                       "location": {"tile": 31, "x": 3, "y": 4, "hidden": 1},
                       "vehicle_type": "ship", "order_count": 2,
                       "parts": [{"cargo_id": 5, "capacity": 12, "load": 3,
                                  "vehicle_id": {"id": 3, "generation": 4, "engine_instance_id": "boot-a",
                                                 "private": True},
                                  "raw_buffer": "secret"}],
                       "debug": {"state": "secret"}}],
            "next_after_id": None,
        }
        actual = projection.observation(raw, query_spec("vehicles"))
        self.assertEqual(actual["items"][0]["parts"][0]["load"], 3)
        self.assertEqual(actual["items"][0]["vehicle_id"],
                         {"id": 3, "generation": 4, "engine_instance_id": "boot-a"})
        self.assertEqual(actual["items"][0]["parts"][0]["vehicle_id"],
                         {"id": 3, "generation": 4, "engine_instance_id": "boot-a"})
        self.assertEqual(actual["items"][0]["location"], {"tile": 31, "x": 3, "y": 4})
        self.assertIsNone(actual["next_after_id"])
        self.assertNotIn("engine_secret", actual)
        self.assertNotIn("debug", actual["items"][0])
        self.assertNotIn("raw_buffer", actual["items"][0]["parts"][0])

    def test_map_region_page_and_bridge_tile_fields(self):
        page = projection.observation({
            "tick": 12, "company_id": 1, "map_width": 256, "map_height": 256,
            "region": {"x": 4, "y": 5, "width": 2, "height": 1, "private": 1},
            "tiles": [
                {"tile": 1284, "x": 4, "y": 5, "type": 2,
                 "transport": "rail", "tunnel": False,
                 "other_end": {"tile": 1290, "x": 10, "y": 5, "private": 1},
                 "direction": 0, "station_track": 0, "crossing_track": 0,
                 "lock_direction": 0, "dock_direction": 0, "private": 1},
                {"tile": 1285, "x": 5, "y": 5,
                 "transport": True, "tunnel": 0,
                 "other_end": {"tile": True, "x": 6, "y": 5},
                 "direction": False, "station_track": True,
                 "crossing_track": False, "lock_direction": True,
                 "dock_direction": False},
            ],
            "next_after_offset": 0, "complete": False, "page_tick": 12,
            "private": "hidden",
        }, {"name": "map.region", "result_fields": [
            "tick", "company_id", "map_width", "map_height", "region",
            "tiles", "next_after_offset", "complete", "page_tick"]})
        self.assertEqual(page["region"], {"x": 4, "y": 5, "width": 2, "height": 1})
        self.assertEqual(page["tiles"][0], {
            "tile": 1284, "x": 4, "y": 5, "type": 2,
            "transport": "rail", "tunnel": False,
            "other_end": {"tile": 1290, "x": 10, "y": 5},
            "direction": 0, "station_track": 0, "crossing_track": 0,
            "lock_direction": 0, "dock_direction": 0,
        })
        self.assertEqual(page["tiles"][1], {"tile": 1285, "x": 5, "y": 5,
                                             "other_end": {"x": 6, "y": 5}})
        self.assertEqual(page["next_after_offset"], 0)
        self.assertIs(page["complete"], False)
        self.assertEqual(page["page_tick"], 12)
        self.assertNotIn("private", page)

        fenced = projection.observation({
            "region": {"x": True, "y": 5, "width": False, "height": 1},
            "next_after_offset": False, "complete": 1, "page_tick": True,
            "tiles": [],
        }, {"name": "map.region", "result_fields": [
            "region", "next_after_offset", "complete", "page_tick", "tiles"]})
        self.assertEqual(fenced, {"region": {"y": 5, "height": 1}, "tiles": []})
        terminal = projection.observation({"next_after_offset": None, "complete": True},
                                          {"name": "map.region", "result_fields": [
                                              "next_after_offset", "complete"]})
        self.assertEqual(terminal, {"next_after_offset": None, "complete": True})

    def test_aircraft_catalog_fields_preserve_zero_without_widening_projection(self):
        raw = {"tick": 12, "company_id": 1, "next_after_id": None,
               "private": "hidden", "items": [
                   {"engine_id": 1, "vehicle_type": "aircraft", "capacity": 0,
                    "mail_capacity": 18, "aircraft_subtype": 0, "range": 0,
                    "private": "hidden"},
                   {"engine_id": 2, "vehicle_type": "rail"},
                   {"engine_id": 3, "vehicle_type": "aircraft", "capacity": True,
                    "mail_capacity": "18", "aircraft_subtype": False, "range": "0",
                    "private": "hidden"},
               ]}
        self.assertEqual(projection.observation(raw, query_spec("catalog.engines")), {
            "tick": 12, "company_id": 1, "next_after_id": None, "items": [
                {"engine_id": 1, "vehicle_type": "aircraft", "capacity": 0,
                 "mail_capacity": 18, "aircraft_subtype": 0, "range": 0},
                {"engine_id": 2, "vehicle_type": "rail"},
                {"engine_id": 3, "vehicle_type": "aircraft"},
            ]})

    def test_planning_fields_and_result_field_boundary(self):
        company = projection.observation({
            "balance": 100, "loan": 10, "maximum_loan": 300, "loan_interval": 20,
            "quarter_history": [{"quarter": 1, "income": 9, "expenses": 3, "company_value": 80,
                                 "private": "secret"}],
            "infrastructure": {"rail": 2, "road": 7, "airports": 1, "secret": 4},
            "password": "secret",
        }, query_spec("company"))
        self.assertEqual(company["balance"], 100)
        self.assertEqual(company["infrastructure"], {"rail": 2, "road": 7, "airports": 1})
        self.assertEqual(company["quarter_history"], [
            {"quarter": 1, "income": 9, "expenses": 3, "company_value": 80}])
        orders = projection.observation({
            "vehicle_id": {"id": 1, "generation": 2}, "current_order": 0,
            "orders": [{"position": 0, "type": 1, "destination_id": 9,
                        "load": 2, "unload": 0, "non_stop": 1, "refit_cargo_id": 3, "secret": 5}],
        }, query_spec("orders"))
        self.assertEqual(orders["orders"][0]["destination_id"], 9)
        self.assertNotIn("secret", orders["orders"][0])
        engines = projection.observation({
            "items": [{"engine_id": 4, "price": 200, "capacities": [{"cargo_id": 1, "capacity": 40,
                                                                     "secret": 8}],
                       "refittable_cargo_ids": [1, 2], "hidden": 3}],
        }, query_spec("catalog.engines"))
        self.assertEqual(engines["items"][0]["capacities"], [{"cargo_id": 1, "capacity": 40}])
        result = projection.outcome({"success": True, "cost": 12, "vehicle_id": {"id": 1, "generation": 2},
                                     "raw": "secret"}, action_spec("road.build"))
        self.assertEqual(result, {"success": True, "cost": 12})

    def test_engine_instance_handle_on_every_public_result_surface(self):
        handle = {"id": 7, "generation": 9, "engine_instance_id": "boot-a"}
        noisy = {**handle, "native_pointer": 123}
        station = projection.observation({
            "items": [{"station_id": noisy, "name": "West", "location": {"tile": 10, "x": 2, "y": 3}}],
        }, query_spec("stations"))
        self.assertEqual(station["items"][0]["station_id"], handle)
        region = projection.observation({
            "tiles": [{"tile": 10, "x": 2, "y": 3, "type": 5, "station_id": noisy}],
        }, query_spec("map.region"))
        self.assertEqual(region["tiles"][0]["station_id"], handle)
        orders = projection.observation({
            "vehicle_id": noisy, "current_order": 0, "orders": [],
        }, query_spec("orders"))
        self.assertEqual(orders["vehicle_id"], handle)
        outcome = projection.outcome({
            "success": True, "vehicle_id": noisy, "cost": 10,
        }, action_spec("vehicle.build"))
        self.assertEqual(outcome["vehicle_id"], handle)
        self.assertEqual(outcome["success"], True)
        # Projection is a positive field copy, not a handle normalizer. Old
        # stored receipts stay byte-for-byte equivalent at the handle level.
        historical = {"id": 7, "generation": 9}
        old = projection.outcome({"success": True, "vehicle_id": historical},
                                 action_spec("vehicle.build"))
        self.assertEqual(old["vehicle_id"], historical)
        self.assertNotIn("engine_instance_id", old["vehicle_id"])

    def test_management_group_dtos_are_positive_and_typed(self):
        handle = {"id": 4, "generation": 8, "engine_instance_id": "boot-a"}
        groups_spec = {"name": "groups", "result_fields":
                       ["tick", "company_id", "items", "next_after_id"]}
        groups = projection.observation({
            "tick": 12, "company_id": 1, "next_after_id": None,
            "secret": "private", "items": [
                {"group_id": {**handle, "pointer": 42}, "parent_group_id": None,
                 "name": "North", "vehicle_type": "rail", "replace_protection": False,
                 "wagon_removal": True, "vehicle_count": 0, "profit_last_year": 0,
                 "livery": {"primary": 0, "secondary": 7, "primary_override": False,
                            "secondary_override": True, "secret": 1}, "secret": "private"},
                {"group_id": handle, "parent_group_id": {**handle, "pointer": 99},
                 "vehicle_count": True, "profit_last_year": "0",
                 "replace_protection": 0, "wagon_removal": "false",
                 "livery": {"primary": True, "secondary": "red",
                            "primary_override": 0, "secondary_override": "true"}},
                {"group_id": handle, "parent_group_id": False},
            ],
        }, groups_spec)
        first, second, third = groups["items"]
        self.assertEqual(first["group_id"], handle)
        self.assertIsNone(first["parent_group_id"])
        self.assertEqual(first["vehicle_count"], 0)
        self.assertEqual(first["livery"], {"primary": 0, "secondary": 7,
                                          "primary_override": False, "secondary_override": True})
        self.assertEqual(second["parent_group_id"], handle)
        self.assertNotIn("vehicle_count", second)
        self.assertNotIn("profit_last_year", second)
        self.assertNotIn("replace_protection", second)
        self.assertNotIn("wagon_removal", second)
        self.assertEqual(second["livery"], {})
        self.assertNotIn("parent_group_id", third)
        self.assertNotIn("secret", groups)
        self.assertNotIn("secret", first)

        stats = projection.observation({
            "tick": 14, "company_id": 1, "scope": "group", "group_id": handle,
            "vehicle_count": 0, "profit_this_year": -5, "profit_last_year": 0,
            "engine_count": 0, "private": 1,
        }, {"name": "group.stats", "result_fields": [
            "tick", "company_id", "scope", "group_id", "vehicle_count",
            "profit_this_year", "profit_last_year", "engine_count"]})
        self.assertEqual(stats["vehicle_count"], 0)
        self.assertEqual(stats["engine_count"], 0)
        self.assertEqual(stats["profit_this_year"], -5)
        self.assertEqual(stats["group_id"], handle)
        self.assertNotIn("private", stats)

        rules = projection.observation({
            "scope": "group", "group_id": handle, "next_after_id": None,
            "items": [{"old_engine_id": 10, "new_engine_id": 12, "when_old": False,
                       "native_rule_id": 33},
                      {"old_engine_id": True, "new_engine_id": 12, "when_old": 0}],
        }, {"name": "autoreplace", "result_fields": [
            "scope", "group_id", "items", "next_after_id"]})
        self.assertEqual(rules["items"], [
            {"old_engine_id": 10, "new_engine_id": 12, "when_old": False},
            {"new_engine_id": 12}])
        self.assertIsNone(rules["next_after_id"])

        created = projection.outcome({
            "success": True, "cost": 0, "group_id": {**handle, "native_pointer": 99},
            "private": "hidden",
        }, {"name": "group.create", "result_fields": ["success", "cost", "group_id"]})
        self.assertEqual(created, {"success": True, "cost": 0, "group_id": handle})

    def test_management_observation_extensions_and_town_handle(self):
        handle = {"id": 6, "generation": 11, "engine_instance_id": "boot-a"}
        settings = {
            "autorenew_enabled": True, "autorenew_months": -2,
            "autorenew_minimum_money": 0, "renew_keep_length": False,
            "service_percent": True, "service_trains": 0,
            "service_road": 30, "service_ships": 40,
            "service_aircraft": 50, "private": "hidden",
        }
        company = projection.observation({"tick": 1, "company_id": 2,
                                          "settings": settings, "private": "hidden"},
                                         {"name": "company", "result_fields":
                                          ["tick", "company_id", "settings"]})
        self.assertEqual(company["settings"], {k: v for k, v in settings.items() if k != "private"})
        self.assertNotIn("private", company)
        bad_settings = {**settings, "autorenew_enabled": 1, "autorenew_months": True,
                        "service_road": False, "renew_keep_length": 0}
        projected = projection.observation({"settings": bad_settings},
                                           {"name": "company", "result_fields": ["settings"]})
        for key in ("autorenew_enabled", "autorenew_months", "service_road", "renew_keep_length"):
            self.assertNotIn(key, projected["settings"])

        vehicles = projection.observation({"items": [
            {"group_id": {**handle, "pointer": 19}, "service_interval": 0,
             "service_custom": False, "service_percent": True, "force_proceed": 0,
             "private": "hidden"},
            {"group_id": None, "service_interval": True,
             "service_custom": 1, "service_percent": 0, "force_proceed": False},
        ]}, {"name": "vehicles", "result_fields": ["items"]})["items"]
        self.assertEqual(vehicles[0], {"group_id": handle, "service_interval": 0,
                                       "service_custom": False, "service_percent": True,
                                       "force_proceed": 0})
        self.assertEqual(vehicles[1], {"group_id": None})

        engines = projection.observation({"items": [
            {"engine_id": 1, "hidden_for_company": False, "private": 2},
            {"engine_id": 2, "hidden_for_company": 0},
        ]}, {"name": "catalog.engines", "result_fields": ["items"]})["items"]
        self.assertEqual(engines, [{"engine_id": 1, "hidden_for_company": False},
                                   {"engine_id": 2}])

        towns = projection.observation({"items": [
            {"town_id": 3, "town": {**handle, "pointer": 4}, "private": "hidden"},
            {"town_id": True, "town": False},
        ]}, {"name": "towns", "result_fields": ["items"]})["items"]
        self.assertEqual(towns, [{"town_id": 3, "town": handle}, {}])

    def test_business_report_dtos_drop_private_and_wrong_typed_fields(self):
        handle = {"id": 7, "generation": 9, "engine_instance_id": "boot-a"}

        world = projection.observation({
            "tick": 1, "company_id": 2,
            "map": {"width": 256, "height": 256, "climate": "temperate", "seed": 123},
            "calendar": {"date_day": 4, "year": 1950, "month": 1, "day": 5,
                         "day_fraction": 0, "private": 1},
            "economy": {"date_day": 4, "year": 1950, "month": 1, "day": 5,
                        "day_fraction": 0},
            "timekeeping": {"mode": "calendar", "ticks_per_day": 74,
                            "minutes_per_calendar_year": 12,
                            "financial_period_unit": "month", "private": 1},
            "rules": {"station_spread": 12, "modified_catchment": True,
                      "vehicle_limits": {"train": 500, "road": True},
                      "cargo_distribution": {"passengers": "manual", "private": 1},
                      "private": 1},
            "network_secret": "hidden",
        }, {"name": "world", "result_fields": [
            "tick", "company_id", "map", "calendar", "economy", "timekeeping", "rules"]})
        self.assertEqual(world["map"], {"width": 256, "height": 256, "climate": "temperate"})
        self.assertEqual(world["rules"]["vehicle_limits"], {"train": 500})
        self.assertEqual(world["rules"]["cargo_distribution"], {"passengers": "manual"})
        self.assertNotIn("network_secret", world)
        self.assertNotIn("private", world["calendar"])

        land = projection.observation({
            "tile": {"tile": 5, "x": 5, "y": 0, "private": 1},
            "tile_type": 1, "height": 0, "slope": 0, "description": "Road",
            "owners": [{"slot": 0, "kind": "town", "company_id": None,
                        "town_id": {**handle, "pointer": 42}, "label": "Town", "private": 1},
                       {"slot": True, "kind": "water", "company_id": False,
                        "town_id": None, "label": "Water"}],
            "local_authority": handle, "build_date": None,
            "house": {"house_type": 1, "name": "House", "private": 1},
            "object": None, "station_spec": None,
            "accepted_cargo": [{"cargo_id": 1, "acceptance_eighths": 8,
                                "always_accepted": False, "private": 1}],
            "clear_preview": {"status": "available", "cost": 0, "error": None,
                              "advisory_tick": 1, "private": 1},
            "raw_tile": 123,
        }, {"name": "land.info", "result_fields": [
            "tile", "tile_type", "height", "slope", "description", "owners",
            "local_authority", "build_date", "house", "object", "station_spec",
            "accepted_cargo", "clear_preview"]})
        self.assertEqual(land["owners"][0]["town_id"], handle)
        self.assertEqual(land["owners"][1], {"kind": "water", "town_id": None,
                                              "label": "Water"})
        self.assertEqual(land["accepted_cargo"], [{"cargo_id": 1,
                                                    "acceptance_eighths": 8,
                                                    "always_accepted": False}])
        self.assertIsNone(land["clear_preview"]["error"])
        self.assertNotIn("raw_tile", land)

        site = projection.observation({
            "build_action": "rail.station.build",
            "selection": {"kind": "rail", "tile": {"tile": 5, "x": 5, "y": 0},
                          "spec_class": 0, "spec_index": 0,
                          "airport_type": None, "layout": None,
                          "rotation": None, "private": 1},
            "footprint": {"x": 5, "y": 0, "width": 1, "height": 2,
                          "tile_count": 2, "private": 1},
            "catchment": {"radius": 4, "coverage_model": "gui_rectangle_advisory",
                          "joined_coverage_excluded": True,
                          "irregular_airport_tiles_modeled": False, "private": 1},
            "accepted_cargo": [{"cargo_id": 1, "name": "Café mail",
                                "name_truncated": True,
                                "acceptance_eighths": 8, "preview_threshold_met": True,
                                "facility_eligible": True,
                                "always_accepted": False, "private": 1},
                               {"cargo_id": 2, "name": "Goods",
                                "name_truncated": 1,
                                "acceptance_eighths": 0, "preview_threshold_met": False,
                                "facility_eligible": False,
                                "always_accepted": False, "private": 1}],
            "supplied_cargo": [{"cargo_id": 1, "name": "Mail",
                                "name_truncated": False,
                                "source_count": 0, "preview_threshold_met": False,
                                "private": 1}],
            "authority": {"town_id": None, "station_allowed": True,
                          "airport_noise": None, "private": 1},
            "feasibility": {"status": "query_test_passed", "cost": 100,
                            "error": None, "advisory_tick": 1,
                            "cash_checked": False, "reserved": False,
                            "build_result_guaranteed": False, "private": 1},
            "guaranteed_buildable": True,
        }, {"name": "station.site", "result_fields": [
            "build_action", "selection", "footprint", "catchment",
            "accepted_cargo", "supplied_cargo", "authority", "feasibility"]})
        self.assertEqual(site["selection"]["spec_class"], 0)
        self.assertIsNone(site["selection"]["airport_type"])
        self.assertEqual(site["accepted_cargo"][0]["acceptance_eighths"], 8)
        self.assertEqual(site["accepted_cargo"][0]["name"], "Café mail")
        self.assertIs(site["accepted_cargo"][0]["name_truncated"], True)
        self.assertNotIn("private", site["accepted_cargo"][0])
        self.assertNotIn("name_truncated", site["accepted_cargo"][1])
        self.assertEqual(site["accepted_cargo"][1]["acceptance_eighths"], 0)
        self.assertEqual(site["supplied_cargo"][0]["source_count"], 0)
        self.assertIs(site["supplied_cargo"][0]["name_truncated"], False)
        self.assertNotIn("private", site["supplied_cargo"][0])
        self.assertEqual(site["feasibility"]["build_result_guaranteed"], False)
        self.assertNotIn("guaranteed_buildable", site)
        self.assertNotIn("private", site["selection"])

        authority = projection.observation({
            "town_id": {**handle, "pointer": 42}, "name": "West",
            "location": {"tile": 5, "x": 5, "y": 0}, "population": 100,
            "ratings": [{"company_id": 2, "category": "good", "exclusive": False,
                         "exact_score": 600},
                        {"company_id": True, "category": "poor", "exclusive": 0}],
            "viewer_rating": {"present": False, "category": None,
                              "exact_score": -100},
            "exclusivity": {"company_id": 2, "remaining_periods": 0,
                            "private": 1},
            "viewer_unwanted_periods": 0, "viewer_has_statue": False,
            "airport_noise": {"enabled": False, "current": 0, "limit": 0,
                              "private": 1},
            "actions": [{"name": "bribe", "enabled_by_rules": True,
                         "available_now": False, "advisory_cost": 10,
                         "private": 1}],
            "exact_rating": 600,
        }, {"name": "town.authority", "result_fields": [
            "town_id", "name", "location", "population", "ratings",
            "viewer_rating", "exclusivity", "viewer_unwanted_periods",
            "viewer_has_statue", "airport_noise", "actions"]})
        self.assertEqual(authority["town_id"], handle)
        self.assertEqual(authority["ratings"], [
            {"company_id": 2, "category": "good", "exclusive": False},
            {"category": "poor"}])
        self.assertEqual(authority["viewer_rating"], {"present": False, "category": None})
        self.assertEqual(authority["exclusivity"], {"company_id": 2,
                                                    "remaining_periods": 0})
        self.assertNotIn("exact_rating", authority)
        self.assertNotIn("private", authority["actions"][0])

    def test_company_appearance_report_and_preview_dtos(self):
        handle = {"id": 2, "generation": 5, "engine_instance_id": "boot-a"}
        component = {"component_id": 0, "name": "Hair", "type": "palette",
                     "value_count": 8, "active": True, "value": 0,
                     "native_bits": 123}
        appearance = projection.observation({
            "president_name": "Ada", "manager_face": {
                "style_id": 0, "style_label": "Classic", "components": [component],
                "raw_face": 123},
            "liveries": [{"scheme": "default", "primary": "blue",
                         "secondary": "red", "primary_inherited": False,
                         "secondary_inherited": False, "native_flag": 4},
                        {"scheme": "steam", "primary": "blue",
                         "secondary": "red", "primary_inherited": 0,
                         "secondary_inherited": True}],
            "private": 1,
        }, {"name": "company.appearance", "result_fields": [
            "president_name", "manager_face", "liveries"]})
        self.assertEqual(appearance["manager_face"], {
            "style_id": 0, "style_label": "Classic", "components": [
                {k: v for k, v in component.items() if k != "native_bits"}]})
        self.assertEqual(appearance["liveries"][1], {
            "scheme": "steam", "primary": "blue", "secondary": "red",
            "secondary_inherited": True})
        self.assertNotIn("private", appearance)

        faces = projection.observation({
            "items": [{"style_id": 1, "label": "Modern", "private": 1}],
            "next_after_id": None,
            "selected_style": {"style_id": 1, "label": "Modern",
                               "components": [{**component, "active": 1,
                                               "value": False}]},
        }, {"name": "catalog.manager_faces", "result_fields": [
            "items", "next_after_id", "selected_style"]})
        self.assertEqual(faces["items"], [{"style_id": 1, "label": "Modern"}])
        self.assertIsNone(faces["next_after_id"])
        self.assertEqual(faces["selected_style"]["components"], [{
            "component_id": 0, "name": "Hair", "type": "palette",
            "value_count": 8}])

        report = projection.observation({
            "company": {**handle, "pointer": 123}, "name": "Example",
            "balance": 0, "loan": 50, "company_value": 200,
            "vehicle_counts": {"rail": 0, "road": True, "ship": 2,
                               "aircraft": 0, "private": 1},
            "calendar_date": {"date": 100, "year": 1950, "month": 1,
                              "day": 1, "private": 1},
            "economy_date": {"date": 100, "year": 1950, "month": 1,
                             "day": 1, "quarter": 1, "wallclock": False,
                             "private": 1},
            "current_quarter": {"period": 7800, "year": 1950,
                                "quarter": 1, "income": 0, "expenses": 5,
                                "cargo_delivered": 0, "private": 1},
            "quarter_history": [{"period": 7799, "year": 1949,
                                 "quarter": 4, "income": 9, "expenses": 1,
                                 "cargo_delivered": 2, "private": 1}],
            "yearly_expenses": [{"year": 1950,
                                 "categories": {"construction": 0,
                                                "road_revenue": -10,
                                                "other": True, "private": 1},
                                 "private": 1}],
            "acquisition_offer": {"price": 100, "private": 1},
            "private_balance": 999,
        }, {"name": "company.report", "result_fields": [
            "company", "name", "balance", "loan", "company_value",
            "vehicle_counts", "calendar_date", "economy_date",
            "current_quarter", "quarter_history", "yearly_expenses",
            "acquisition_offer"]})
        self.assertEqual(report["company"], handle)
        self.assertEqual(report["vehicle_counts"], {"rail": 0, "ship": 2,
                                                      "aircraft": 0})
        self.assertEqual(report["yearly_expenses"], [{
            "year": 1950, "categories": {"construction": 0,
                                          "road_revenue": -10}}])
        self.assertEqual(report["acquisition_offer"], {"price": 100})
        self.assertNotIn("private_balance", report)

        offers = projection.observation({"items": [
            {"engine_id": 3, "name": "Prototype", "vehicle_type": "rail",
             "price": 100, "capacity": 0, "mail_capacity": 0,
             "maximum_speed": False, "private": 1}],
            "next_after_id": False,
        }, {"name": "engine.preview_offers", "result_fields": [
            "items", "next_after_id"]})
        self.assertEqual(offers, {"items": [{"engine_id": 3,
                                              "name": "Prototype",
                                              "vehicle_type": "rail", "price": 100,
                                              "capacity": 0,
                                              "mail_capacity": 0}]})

    def test_orders_and_timetable_project_only_public_rows(self):
        handle = {"id": 4, "generation": 6, "engine_instance_id": "boot-a"}
        order = {
            "position": 0, "type": 1, "implicit": False, "destination_id": 12,
            "load": 0, "unload": 0, "non_stop": 0, "refit_cargo_id": 0,
            "kind": "station", "routing": "non_stop", "load_policy": "full_load",
            "unload_policy": "if_possible", "stop_location": "far_end",
            "nearest_depot": False, "depot_action": "service",
            "destination": {"tile": 12, "x": 12, "y": 0, "pointer": 123},
            "condition": {"variable": "load", "comparator": "greater",
                          "value": 50, "target_position": 0, "private": 1},
            "refit": {"mode": "cargo", "cargo_id": 1, "private": 1},
            "wait_ticks": 0, "travel_ticks": 15,
            "wait_timetabled": False, "travel_timetabled": True,
            "speed_limit": None, "content_digest": "private",
        }
        raw = {
            "vehicle_id": {**handle, "pointer": 123},
            "order_version": "ov1:opaque", "order_count": 1,
            "manual_order_count": 1, "current_order": 0,
            "orders": [order, {**order, "position": True, "implicit": 0,
                               "wait_ticks": False, "speed_limit": True}],
            "next_after_position": None, "shared_count": 1,
            "shared_vehicles": [{**handle, "pointer": 123}],
            "next_shared_after_id": 4,
            "content_digest": "private", "autofill_digest": "private",
            "started": True, "start_tick": 0, "lateness_ticks": -1,
            "autofill": False, "preserve_wait_time": True,
            "complete": False, "duration_ticks": 15,
            "speed_unit": "kmh",
        }
        shared_fields = ["vehicle_id", "order_version", "order_count",
                         "manual_order_count", "current_order", "orders",
                         "next_after_position", "shared_count",
                         "shared_vehicles", "next_shared_after_id"]
        orders = projection.observation(raw, {"name": "orders",
                                              "result_fields": shared_fields})
        self.assertEqual(orders["vehicle_id"], handle)
        self.assertEqual(orders["shared_vehicles"], [handle])
        self.assertIsNone(orders["next_after_position"])
        self.assertEqual(orders["next_shared_after_id"], 4)
        self.assertEqual(orders["orders"][0]["destination"],
                         {"tile": 12, "x": 12, "y": 0})
        self.assertEqual(orders["orders"][0]["refit"],
                         {"mode": "cargo", "cargo_id": 1})
        self.assertIsNone(orders["orders"][0]["speed_limit"])
        for key in ("position", "implicit", "wait_ticks", "speed_limit"):
            self.assertNotIn(key, orders["orders"][1])
        self.assertNotIn("content_digest", orders)
        self.assertNotIn("content_digest", orders["orders"][0])
        self.assertNotIn("started", orders)

        timetable = projection.observation(raw, {"name": "timetable",
            "result_fields": shared_fields + ["started", "start_tick",
                "lateness_ticks", "autofill", "preserve_wait_time", "complete",
                "duration_ticks", "speed_unit"]})
        self.assertEqual(timetable["lateness_ticks"], -1)
        self.assertEqual(timetable["duration_ticks"], 15)
        self.assertIs(timetable["complete"], False)
        self.assertNotIn("autofill_digest", timetable)

    def test_optional_live_order_targets_and_execution_are_positive(self):
        handle = {"id": 12, "generation": 4, "engine_instance_id": "boot"}
        fields = ["vehicle_id", "order_version", "orders",
                  "next_after_position", "shared_vehicles",
                  "next_shared_after_id", "current_execution"]
        ordinary = projection.observation({
            "vehicle_id": handle, "order_version": "ov1:opaque",
            "orders": [{"position": 0, "destination_id": 5}],
            "next_after_position": None, "shared_vehicles": [handle],
            "next_shared_after_id": None,
        }, {"name": "orders", "result_fields": fields})
        self.assertEqual(ordinary["orders"], [{"position": 0,
                                                "destination_id": 5}])
        self.assertNotIn("current_execution", ordinary)
        self.assertNotIn("destination_entity", ordinary["orders"][0])

        raw = {
            "vehicle_id": handle, "order_version": "ov1:opaque",
            "orders": [
                {"position": 0, "destination_id": 5,
                 "destination_entity": {"kind": "station",
                                        "handle": {**handle, "pointer": 99},
                                        "private": 1}, "private": 1},
                {"position": 1, "destination_id": 6,
                 "destination_entity": {"kind": True,
                                        "handle": {"id": 6, "generation": False,
                                                   "engine_instance_id": "boot",
                                                   "pointer": 99}}},
                {"position": 2, "destination_id": 7,
                 "destination_entity": ["not an object"]},
            ],
            "next_after_position": 2, "shared_vehicles": [handle],
            "next_shared_after_id": None,
            "current_execution": {"type": 3, "kind": "depot",
                                  "real_position": None,
                                  "destination_entity": {"kind": "depot",
                                                         "handle": {**handle,
                                                                    "native_id": 123},
                                                         "private": 1},
                                  "raw_order": "private"},
            "content_digest": "private",
        }
        for name in ("orders", "timetable"):
            with self.subTest(name=name):
                spec = {"name": name, "result_fields": fields}
                result = projection.observation(raw, spec,
                                                parameters={"include_live_detail": True})
                self.assertEqual(result["orders"][0]["destination_entity"],
                                 {"kind": "station", "handle": handle})
                self.assertEqual(result["orders"][0]["destination_id"], 5)
                self.assertEqual(result["orders"][1]["destination_entity"],
                                 {"handle": {"id": 6,
                                             "engine_instance_id": "boot"}})
                self.assertNotIn("destination_entity", result["orders"][2])
                self.assertEqual(result["current_execution"], {
                    "type": 3, "kind": "depot", "real_position": None,
                    "destination_entity": {"kind": "depot", "handle": handle}})
                self.assertEqual(result["next_after_position"], 2)
                self.assertNotIn("content_digest", result)
                omitted = projection.observation(raw, spec)
                self.assertNotIn("current_execution", omitted)
                self.assertTrue(all("destination_entity" not in order
                                    for order in omitted["orders"]))
                for params in (None, {}, {"include_live_detail": False}):
                    ordinary = projection.observation(raw, spec, parameters=params)
                    self.assertNotIn("current_execution", ordinary)
                    self.assertTrue(all("destination_entity" not in order
                                        for order in ordinary["orders"]))

        malformed = projection.observation({"current_execution": {
            "type": True, "kind": "station", "real_position": False,
            "destination_entity": {"kind": False, "handle": True}}},
            {"name": "orders", "result_fields": ["current_execution"]},
            parameters={"include_live_detail": True})
        self.assertEqual(malformed, {"current_execution": {
            "kind": "station", "destination_entity": {}}})

    def test_construction_catalogs_and_handles_are_positive(self):
        handle = {"id": 3, "generation": 8, "engine_instance_id": "boot-a"}
        tile = {"tile": 12, "x": 12, "y": 0}
        rows = {
            "catalog.objects": {"object_type": 0, "name": "HQ", "width": 2,
                                "height": 2, "views": 4, "base_build_cost": 10,
                                "site_costs_extra": False, "removable": True,
                                "built_on_water": False, "water_only": False},
            "catalog.houses": {"house_type": 0, "name": "House", "width": 1,
                               "height": 1, "population": 10,
                               "introduction_year": 1950, "last_year": 2000,
                               "placement_enabled": True,
                               "protected_option": False},
            "catalog.industries": {"industry_type": 0, "name": "Coal Mine",
                                  "cost": 10, "mode": "fund", "layout_count": 1,
                                  "accepts_cargo_ids": [1, 2],
                                  "produces_cargo_ids": [3], "available": True},
            "catalog.trees": {"tree_type": 0, "name": "Oak", "variant": 0,
                              "required_tropic_zone": None},
            "catalog.station_specs": {"catalog_id": 0, "spec_class": 0,
                                      "spec_index": 0, "class_name": "Default",
                                      "name": "Default", "available": True,
                                      "disallowed_platforms": 0,
                                      "disallowed_lengths": 0},
            "catalog.roadstop_specs": {"catalog_id": 0, "spec_class": 0,
                                       "spec_index": 0, "class_name": "Default",
                                       "name": "Default", "available": True,
                                       "drive_through_only": False,
                                       "road_only": False, "tram_only": False},
            "depots": {"depot_id": handle, "name": "Depot",
                       "location": tile, "vehicle_type": "rail"},
            "waypoints": {"station_id": handle, "name": "Waypoint",
                          "location": tile, "kind": "rail",
                          "owner_company_id": None},
            "signs": {"sign_id": handle, "name": "Sign", "location": tile,
                      "owner_company_id": None, "editable": False},
        }
        for name, row in rows.items():
            with self.subTest(name=name):
                extra = {**row, "native_secret": "hidden"}
                for key in ("depot_id", "station_id", "sign_id", "location"):
                    if key in extra:
                        extra[key] = {**extra[key], "pointer": 123}
                projected = projection.observation({
                    "tick": 1, "company_id": 2, "items": [extra],
                    "next_after_id": None, "private": 1,
                }, {"name": name, "result_fields": [
                    "tick", "company_id", "items", "next_after_id"]})
                self.assertEqual(projected, {"tick": 1, "company_id": 2,
                                             "items": [row],
                                             "next_after_id": None})

        negative = projection.observation({"items": [
            {"catalog_id": True, "spec_index": False,
             "available": 1, "drive_through_only": 0,
             "road_only": True, "tram_only": False},
        ], "next_after_id": False}, {"name": "catalog.roadstop_specs",
                                   "result_fields": ["items", "next_after_id"]})
        self.assertEqual(negative, {"items": [{"road_only": True,
                                                "tram_only": False}]})
        industry = projection.observation({"items": [
            {"accepts_cargo_ids": [1, True, 3], "produces_cargo_ids": [False, 2],
             "available": 1},
        ]}, {"name": "catalog.industries", "result_fields": ["items"]})
        self.assertEqual(industry, {"items": [{"accepts_cargo_ids": [1, 3],
                                                "produces_cargo_ids": [2]}]})
        signs = projection.observation({"items": [
            {"owner_company_id": 0, "editable": 1},
            {"owner_company_id": False, "editable": False},
        ]}, {"name": "signs", "result_fields": ["items"]})
        self.assertEqual(signs, {"items": [{"owner_company_id": 0},
                                          {"editable": False}]})

        for name, key in (("sign.place", "sign_id"),
                          ("town.found", "town_id")):
            with self.subTest(action=name):
                result = projection.outcome({"success": True, "cost": 0,
                    key: {**handle, "pointer": 123}, "private": 1},
                    {"name": name, "result_fields": ["success", "cost", key]})
                self.assertEqual(result, {"success": True, "cost": 0,
                                          key: handle})

    def test_company_access_projection_excludes_private_keys(self):
        handle = {"id": 17, "generation": 8, "engine_instance_id": "boot"}
        spec = {"name": "company.access", "result_fields": [
            "tick", "company_id", "join_policy", "participant", "listed", "permitted"]}
        raw = {"tick": 4, "company_id": 1, "join_policy": "listed",
               "participant": {**handle, "public_key": "private"},
               "listed": False, "permitted": True, "allowlist": ["private"]}
        result = projection.observation(raw, spec)
        self.assertEqual(result, {"tick": 4, "company_id": 1, "join_policy": "listed",
                                  "participant": handle, "listed": False, "permitted": True})
        self.assertNotIn("listed", projection.observation({**raw, "listed": 1}, spec))
        self.assertNotIn("permitted", projection.observation({**raw, "permitted": "true"}, spec))
        policy = projection.observation({"join_policy": "any", "public_key": "private"}, spec)
        self.assertEqual(policy, {"join_policy": "any"})

    def test_town_detail_projection_preserves_display_and_nullable_growth(self):
        handle = {"id": 17, "generation": 8, "engine_instance_id": "boot"}
        tile = {"tile": 400, "x": 16, "y": 25}
        raw = {"tick": 11, "company_id": 2, "town_id": {**handle, "pointer": 1},
               "name": "T" * 2048, "name_truncated": True, "location": tile,
               "city": False, "population": 90, "house_count": 14,
               "text": "Town text", "text_truncated": False,
               "supplied_cargo": [{"cargo_id": 0, "name": "Passengers",
                                   "name_truncated": False, "produced_last_period": 3,
                                   "transported_last_period": 1, "private": 9}],
               "growth_requirements": [{"cargo_id": 1, "name": "Food",
                                        "name_truncated": True, "kind": "winter",
                                        "status": "required_in_winter",
                                        "delivered_last_period": None,
                                        "required_amount": None, "goal_internal": 42}],
               "growth": {"growing": False, "interval_days": None,
                          "funded": None, "fund_buildings_months": 9},
               "airport_noise": None, "hidden_rating": 900}
        spec = {"name": "town.detail", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["town_id"], handle)
        self.assertEqual(projected["name"], "T" * 2048)
        self.assertTrue(projected["name_truncated"])
        self.assertEqual(projected["growth"], {"growing": False,
                                               "interval_days": None, "funded": None})
        self.assertEqual(projected["airport_noise"], None)
        self.assertEqual(projected["growth_requirements"][0]["status"], "required_in_winter")
        self.assertIsNone(projected["growth_requirements"][0]["required_amount"])
        self.assertNotIn("goal_internal", projected["growth_requirements"][0])
        self.assertNotIn("private", projected["supplied_cargo"][0])
        self.assertNotIn("hidden_rating", projected)
        bad = projection.observation({**raw, "population": True,
                                      "growth": {"growing": 1, "interval_days": True,
                                                 "funded": 1},
                                      "airport_noise": {"current": True, "limit": 8}}, spec)
        self.assertNotIn("population", bad)
        self.assertEqual(bad["growth"], {})
        self.assertEqual(bad["airport_noise"], {"limit": 8})

    def test_subsidies_projection_preserves_endpoints_and_null_awardee(self):
        handle = {"id": 3, "generation": 7, "engine_instance_id": "boot"}
        endpoint = {"kind": "town", "entity": {**handle, "private": 1},
                    "name": "Town", "name_truncated": False,
                    "location": {"tile": 6, "x": 2, "y": 3}, "src_private": 2}
        item = {"subsidy_id": handle, "status": "offered", "cargo_id": 4,
                "cargo_name": "Goods", "cargo_name_truncated": True,
                "source": endpoint, "destination": endpoint,
                "awarded_company": None, "remaining_periods": 7,
                "period_unit": "month", "expires_economy_date_day": None,
                "script_state": "private"}
        raw = {"tick": 5, "company_id": 2, "items": [item],
               "next_after_id": 13, "private": 1}
        spec = {"name": "subsidies", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["items"][0]["source"]["entity"], handle)
        self.assertIsNone(projected["items"][0]["awarded_company"])
        self.assertIsNone(projected["items"][0]["expires_economy_date_day"])
        self.assertTrue(projected["items"][0]["cargo_name_truncated"])
        self.assertNotIn("script_state", projected["items"][0])
        self.assertNotIn("src_private", projected["items"][0]["source"])
        invalid = projection.observation({**raw, "next_after_id": True,
                                          "items": [{**item, "remaining_periods": True,
                                                     "cargo_name_truncated": 1}]}, spec)
        self.assertNotIn("next_after_id", invalid)
        self.assertNotIn("remaining_periods", invalid["items"][0])
        self.assertNotIn("cargo_name_truncated", invalid["items"][0])

    def test_performance_league_projection_keeps_display_rank_not_rating(self):
        handle = {"id": 1, "generation": 2, "engine_instance_id": "boot"}
        raw = {"tick": 3, "company_id": 2,
               "rows": [{"rank": 1, "company": {**handle, "private": 9},
                         "name": "Carrier", "name_truncated": False,
                         "title": "Engineer", "title_truncated": True,
                         "performance_history": 999}], "rating": 999}
        spec = {"name": "league.performance", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["rows"], [{"rank": 1, "company": handle,
                                              "name": "Carrier", "name_truncated": False,
                                              "title": "Engineer", "title_truncated": True}])
        self.assertNotIn("rating", projected)
        invalid = projection.observation({**raw, "rows": [{**raw["rows"][0], "rank": True,
                                                        "title_truncated": 1}]}, spec)
        self.assertNotIn("rank", invalid["rows"][0])
        self.assertNotIn("title_truncated", invalid["rows"][0])

    def test_league_tables_projection_preserves_fenced_handle(self):
        handle = {"id": 9, "generation": 10, "engine_instance_id": "boot"}
        raw = {"tick": 4, "company_id": 2,
               "items": [{"table_id": {**handle, "raw_title": "secret"},
                          "title": "Scores", "title_truncated": False,
                          "header": "private"}], "next_after_id": None}
        spec = {"name": "league.tables", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["items"], [{"table_id": handle,
                                               "title": "Scores", "title_truncated": False}])
        self.assertIsNone(projected["next_after_id"])
        invalid = projection.observation({**raw, "items": [{"table_id": {
            "id": True, "generation": 10, "engine_instance_id": "boot"},
            "title": "Scores", "title_truncated": 1}]}, spec)
        self.assertNotIn("id", invalid["items"][0]["table_id"])
        self.assertNotIn("title_truncated", invalid["items"][0])

    def test_league_entries_projection_keeps_visible_links_only(self):
        handle = {"id": 9, "generation": 10, "engine_instance_id": "boot"}
        entry = {"entry_id": handle, "company": None, "text": "Leader",
                 "text_truncated": False, "score": "100", "score_truncated": True,
                 "link": {"kind": "town", "tile": None,
                          "entity": {**handle, "pointer": 99}, "dst": 42},
                 "rating": 9000, "raw_encoded_string": "private"}
        raw = {"tick": 4, "company_id": 2, "table_id": handle,
               "title": "Table", "title_truncated": False,
               "header": "Header", "header_truncated": True,
               "footer": "Footer", "footer_truncated": False,
               "items": [entry, {**entry, "link": None}], "next_after_id": 18}
        spec = {"name": "league.table.entries", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["items"][0]["link"], {"kind": "town",
                                                         "tile": None, "entity": handle})
        self.assertIsNone(projected["items"][0]["company"])
        self.assertIsNone(projected["items"][1]["link"])
        self.assertTrue(projected["header_truncated"])
        self.assertNotIn("rating", projected["items"][0])
        self.assertNotIn("raw_encoded_string", projected["items"][0])
        invalid = projection.observation({**raw, "items": [{**entry, "score_truncated": 1,
                                                        "link": {"kind": "tile",
                                                                 "tile": {"tile": True,
                                                                          "x": 2, "y": 3},
                                                                 "entity": None}}]}, spec)
        self.assertNotIn("score_truncated", invalid["items"][0])
        self.assertNotIn("tile", invalid["items"][0]["link"]["tile"])

    def test_goals_projection_keeps_visible_progress_and_nullable_completion(self):
        handle = {"id": 11, "generation": 12, "engine_instance_id": "boot"}
        item = {"goal_id": handle, "text": "Deliver cargo", "text_truncated": True,
                "progress": "", "progress_truncated": False, "completed": None,
                "destination": {"kind": "none", "tile": None, "entity": None},
                "author_company": 7, "question": "private"}
        raw = {"tick": 6, "company_id": 2, "scope": "company",
               "items": [item, {**item, "destination": None}],
               "next_after_id": None}
        spec = {"name": "goals", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["scope"], "company")
        self.assertIsNone(projected["items"][0]["completed"])
        self.assertEqual(projected["items"][0]["destination"],
                         {"kind": "none", "tile": None, "entity": None})
        self.assertIsNone(projected["items"][1]["destination"])
        self.assertTrue(projected["items"][0]["text_truncated"])
        self.assertNotIn("question", projected["items"][0])
        self.assertNotIn("author_company", projected["items"][0])
        invalid = projection.observation({**raw, "items": [{**item, "completed": 1,
                                                        "text_truncated": 1,
                                                        "goal_id": {"id": 11,
                                                                    "generation": True,
                                                                    "engine_instance_id": "boot"}}]}, spec)
        self.assertNotIn("completed", invalid["items"][0])
        self.assertNotIn("text_truncated", invalid["items"][0])
        self.assertNotIn("generation", invalid["items"][0]["goal_id"])

    def test_industries_listing_keeps_numeric_id_and_fenced_handle(self):
        handle = {"id": 19, "generation": 7, "engine_instance_id": "boot"}
        row = {"industry_id": 19, "industry": {**handle, "pointer": 1},
               "name": "é" * 128, "name_truncated": True,
               "location": {"tile": 100, "x": 4, "y": 6},
               "width": 2, "height": 3, "accepted_cargo_ids": [1],
               "produced_cargo": [{"cargo_id": 2, "waiting": 0,
                                   "production_rate": 4, "private": 8}],
               "callback_text": "private"}
        raw = {"tick": 9, "company_id": 2, "items": [row],
               "next_after_id": None}
        spec = {"name": "industries", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["items"][0]["industry_id"], 19)
        self.assertEqual(projected["items"][0]["industry"], handle)
        self.assertEqual(len(projected["items"][0]["name"].encode("utf-8")), 256)
        self.assertTrue(projected["items"][0]["name_truncated"])
        self.assertEqual(projected["items"][0]["produced_cargo"], [
            {"cargo_id": 2, "waiting": 0, "production_rate": 4}])
        self.assertNotIn("callback_text", projected["items"][0])
        invalid = projection.observation({**raw, "items": [{**row,
            "industry_id": True,
            "name_truncated": 1,
            "industry": {"id": 19, "generation": True,
                         "engine_instance_id": "boot"}}]}, spec)
        self.assertNotIn("industry_id", invalid["items"][0])
        self.assertNotIn("name_truncated", invalid["items"][0])
        self.assertNotIn("generation", invalid["items"][0]["industry"])

    def test_industry_details_projection_marks_unknown_callback_and_history(self):
        handle = {"id": 19, "generation": 7, "engine_instance_id": "boot"}
        accepted = [{"slot": 0, "cargo_id": 1, "input_waiting": None,
                     "input_waiting_visibility": "unknown_callback",
                     "input_waiting_unavailable": True,
                     "delivered_last_period": None,
                     "waiting_average_last_period": None,
                     "callback_result": "private"},
                    {"slot": 1, "cargo_id": 2, "input_waiting": None,
                     "input_waiting_visibility": "hidden",
                     "input_waiting_unavailable": False,
                     "delivered_last_period": 0,
                     "waiting_average_last_period": 0},
                    {"slot": 2, "cargo_id": 3, "input_waiting": 0,
                     "input_waiting_visibility": "visible",
                     "input_waiting_unavailable": False,
                     "delivered_last_period": 5,
                     "waiting_average_last_period": 0}]
        produced = [{"slot": 0, "cargo_id": 4,
                     "produced_last_period": None,
                     "transported_last_period": None,
                     "transported_percent_last_period": None},
                    {"slot": 1, "cargo_id": 5,
                     "produced_last_period": 0,
                     "transported_last_period": 0,
                     "transported_percent_last_period": 0}]
        raw = {"tick": 9, "company_id": 2,
               "industry_id": {**handle, "pointer": 1},
               "name": "I" * 256, "name_truncated": True,
               "industry_type": 7,
               "location": {"tile": 100, "x": 4, "y": 6},
               "width": 2, "height": 3, "nearest_town": None,
               "announced_closure": False, "period_unit": "month",
               "accepted_cargo_ids": [1, 2, 3],
               "produced_cargo": [{"cargo_id": 4, "waiting": 0,
                                   "production_rate": 0}],
               "accepted": accepted, "produced": produced,
               "omitted_presentation": {"cargo_suffix_callback": True,
                                        "window_more_text_callback": False,
                                        "custom_text": True,
                                        "raw_custom_text": "secret"},
               "custom_text": "private", "callback_parameters": [1]}
        spec = {"name": "industry.details", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["industry_id"], handle)
        self.assertIsNone(projected["nearest_town"])
        self.assertEqual(projected["name"], "I" * 256)
        self.assertTrue(projected["name_truncated"])
        self.assertEqual(projected["accepted"][0]["input_waiting_visibility"],
                         "unknown_callback")
        self.assertTrue(projected["accepted"][0]["input_waiting_unavailable"])
        self.assertIsNone(projected["accepted"][0]["input_waiting"])
        self.assertEqual(projected["accepted"][1]["delivered_last_period"], 0)
        self.assertEqual(projected["accepted"][2]["input_waiting"], 0)
        self.assertIsNone(projected["produced"][0]["produced_last_period"])
        self.assertEqual(projected["produced"][1]["transported_percent_last_period"], 0)
        self.assertEqual(projected["omitted_presentation"], {
            "cargo_suffix_callback": True, "window_more_text_callback": False,
            "custom_text": True})
        self.assertNotIn("callback_result", projected["accepted"][0])
        self.assertNotIn("custom_text", projected)
        self.assertNotIn("callback_parameters", projected)
        with_town = projection.observation({**raw, "nearest_town": handle,
                                            "name_truncated": False}, spec)
        self.assertEqual(with_town["nearest_town"], handle)
        self.assertFalse(with_town["name_truncated"])
        invalid = projection.observation({**raw, "industry_type": True,
            "name_truncated": 1,
            "announced_closure": 1,
            "accepted": [{**accepted[0], "slot": True,
                          "input_waiting": True,
                          "input_waiting_unavailable": 1}],
            "omitted_presentation": {"cargo_suffix_callback": 1,
                                     "window_more_text_callback": False,
                                     "custom_text": True}}, spec)
        self.assertNotIn("industry_type", invalid)
        self.assertNotIn("name_truncated", invalid)
        self.assertNotIn("announced_closure", invalid)
        self.assertNotIn("slot", invalid["accepted"][0])
        self.assertNotIn("input_waiting", invalid["accepted"][0])
        self.assertNotIn("input_waiting_unavailable", invalid["accepted"][0])
        self.assertNotIn("cargo_suffix_callback", invalid["omitted_presentation"])

    def test_industry_monthly_stats_projection_respects_direction_and_zero(self):
        handle = {"id": 19, "generation": 7, "engine_instance_id": "boot"}
        base = {"tick": 9, "company_id": 2,
                "industry_id": {**handle, "pointer": 1},
                "cargo_id": 3, "direction": "accepted", "period_unit": "month"}
        accepted_points = [{"periods_ago": age,
                            "delivered": 0 if age == 1 else None,
                            "waiting_average": 0 if age == 1 else None,
                            "produced": 999, "private_history": 7}
                           for age in range(1, 25)]
        spec = {"name": "industry.monthly_stats",
                "result_fields": (*base, "points")}
        projected = projection.observation({**base, "points": accepted_points}, spec)
        self.assertEqual(projected["industry_id"], handle)
        self.assertEqual(len(projected["points"]), 24)
        self.assertEqual(projected["points"][0], {"periods_ago": 1,
                                                    "delivered": 0,
                                                    "waiting_average": 0})
        self.assertEqual(projected["points"][-1], {"periods_ago": 24,
                                                    "delivered": None,
                                                    "waiting_average": None})
        produced = {**base, "direction": "produced", "period_unit": "minute",
                    "points": [{"periods_ago": 1, "produced": 0,
                                "transported": None, "delivered": 900,
                                "private": 1}]}
        produced_result = projection.observation(produced, spec)
        self.assertEqual(produced_result["points"], [{"periods_ago": 1,
                                                       "produced": 0,
                                                       "transported": None}])
        invalid = projection.observation({**base, "cargo_id": True,
            "points": [{"periods_ago": True, "delivered": True,
                        "waiting_average": 0}]}, spec)
        self.assertNotIn("cargo_id", invalid)
        self.assertEqual(invalid["points"], [{"waiting_average": 0}])

    def test_display_text_projection_keeps_only_kind_specific_reference(self):
        handle = {"id": 12, "generation": 4,
                  "engine_instance_id": "opaque-boot"}
        version = "a" * 64
        target_kinds = (
            "town_text", "subsidy_cargo_name", "subsidy_source_name",
            "subsidy_destination_name", "league_table_title",
            "league_table_header", "league_table_footer",
            "league_entry_text", "league_entry_score", "goal_text",
            "goal_progress", "industry_extra_text", "industry_name",
            "industry_custom_text",
        )
        raw = {"tick": 12345, "company_id": 1, "kind": "goal_text",
               "target": {**handle, "pool_pointer": 123}, "cargo_id": 9,
               "vehicle_id": handle, "subtype": 2,
               "direction": "accepted", "slot": 1,
               "text": "Deliver 500 tonnes", "content_version": version,
               "total_bytes": 18, "next_after_byte": None,
               "encoded_string": "private", "rating": 999,
               "callback_registers": [1, 2]}
        spec = {"name": "display.text", "result_fields": tuple(raw)}
        for kind in target_kinds:
            with self.subTest(kind=kind):
                projected = projection.observation({**raw, "kind": kind}, spec)
                self.assertEqual(projected, {
                    "tick": 12345, "company_id": 1, "kind": kind,
                    "target": handle, "text": "Deliver 500 tonnes",
                    "content_version": version, "total_bytes": 18,
                    "next_after_byte": None})
                self.assertNotIn("cargo_id", projected)
        cargo = projection.observation({**raw, "kind": "cargo_name",
                                        "cargo_id": 0, "text": "",
                                        "total_bytes": 0,
                                        "next_after_byte": 0}, spec)
        self.assertEqual(cargo, {"tick": 12345, "company_id": 1,
                                 "kind": "cargo_name", "cargo_id": 0,
                                 "text": "", "content_version": version,
                                 "total_bytes": 0, "next_after_byte": 0})
        self.assertNotIn("target", cargo)

    def test_display_text_extended_kinds_keep_only_their_reference_tuple(self):
        part = {"id": 12, "generation": 4, "engine_instance_id": "boot"}
        primary = {"id": 11, "generation": 6, "engine_instance_id": "boot"}
        common = {"tick": 15, "company_id": 2,
                  "text": "Café", "content_version": "c" * 64,
                  "total_bytes": 5, "next_after_byte": None}
        raw = {**common, "kind": "vehicle_refit_label",
               "target": {**part, "pointer": 1},
               "vehicle_id": {**primary, "private": 2},
               "cargo_id": 0, "subtype": 0,
               "direction": "produced", "slot": 3,
               "callback_registers": [1], "raw_encoded_label": "private"}
        spec = {"name": "display.text", "result_fields": tuple(raw)}
        vehicle = projection.observation(raw, spec)
        self.assertEqual(vehicle, {**common, "kind": "vehicle_refit_label",
                                   "target": part, "vehicle_id": primary,
                                   "cargo_id": 0, "subtype": 0})
        self.assertNotIn("direction", vehicle)
        self.assertNotIn("slot", vehicle)
        industry = projection.observation({**raw, "kind": "industry_cargo_line",
                                           "direction": "accepted", "slot": 0}, spec)
        self.assertEqual(industry, {**common, "kind": "industry_cargo_line",
                                    "target": part, "cargo_id": 0,
                                    "direction": "accepted", "slot": 0})
        self.assertNotIn("vehicle_id", industry)
        self.assertNotIn("subtype", industry)
        for kind in ("industry_extra_text", "industry_name",
                     "industry_custom_text"):
            with self.subTest(kind=kind):
                simple = projection.observation({**raw, "kind": kind}, spec)
                self.assertEqual(simple, {**common, "kind": kind,
                                          "target": part})
        malformed = projection.observation({**raw, "cargo_id": True,
            "subtype": True,
            "target": {"id": True, "generation": 4,
                       "engine_instance_id": "boot"},
            "vehicle_id": {"id": 11, "generation": True,
                           "engine_instance_id": "boot"}}, spec)
        self.assertNotIn("cargo_id", malformed)
        self.assertNotIn("subtype", malformed)
        self.assertNotIn("id", malformed["target"])
        self.assertNotIn("generation", malformed["vehicle_id"])
        invalid_industry = projection.observation({**raw,
            "kind": "industry_cargo_line", "cargo_id": True,
            "slot": True, "direction": 1}, spec)
        for key in ("cargo_id", "slot", "direction"):
            self.assertNotIn(key, invalid_industry)

    def test_display_text_projection_drops_malformed_numbers_and_private_source(self):
        raw = {"tick": 8, "company_id": 1, "kind": "town_text",
               "target": {"id": 3, "generation": 5,
                          "engine_instance_id": "boot", "raw": "private"},
               "text": "é" * 1024, "content_version": "b" * 64,
               "total_bytes": 2048, "next_after_byte": 2048,
               "raw_encoded_bytes": [255], "command_token": "private"}
        spec = {"name": "display.text", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["text"], "é" * 1024)
        self.assertEqual(projected["total_bytes"], 2048)
        self.assertEqual(projected["next_after_byte"], 2048)
        self.assertEqual(projected["target"], {"id": 3, "generation": 5,
                                               "engine_instance_id": "boot"})
        self.assertNotIn("raw_encoded_bytes", projected)
        self.assertNotIn("command_token", projected)
        malformed = projection.observation({**raw, "tick": True,
            "total_bytes": True, "next_after_byte": True,
            "content_version": 7,
            "target": {"id": True, "generation": 5,
                       "engine_instance_id": "boot"}}, spec)
        for key in ("tick", "total_bytes", "next_after_byte", "content_version"):
            self.assertNotIn(key, malformed)
        self.assertNotIn("id", malformed["target"])
        unknown = projection.observation({**raw, "kind": ["town_text"]}, spec)
        self.assertEqual(unknown, {"tick": 8, "company_id": 1})

    def test_station_details_projection_preserves_nullable_graph_summary(self):
        station = {"id": 4, "generation": 3, "engine_instance_id": "boot"}
        cargo = {"cargo_id": 1, "accepts": True, "has_rating": False,
                 "rating_percent": None,
                 "waiting": {"available": 10, "reserved": 4, "total": 14,
                             "packet_ids": [42]},
                 "monthly_supply": None, "link_graph_id": 99}
        raw = {"tick": 7, "company_id": 2,
               "station": {**station, "pointer": 111}, "name": "Station",
               "owner": None, "sign_tile": None, "town": station,
               "facilities": ["train", "airport"], "spread": None,
               "catchment_radius": 4, "airport": None, "cargo": [cargo],
               "cached_acceptance": 999}
        spec = {"name": "station.details", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["station"], station)
        self.assertIsNone(projected["owner"])
        self.assertIsNone(projected["sign_tile"])
        self.assertIsNone(projected["spread"])
        self.assertIsNone(projected["airport"])
        self.assertEqual(projected["cargo"], [{"cargo_id": 1, "accepts": True,
                                               "has_rating": False,
                                               "rating_percent": None,
                                               "waiting": {"available": 10,
                                                           "reserved": 4,
                                                           "total": 14},
                                               "monthly_supply": None}])
        self.assertNotIn("cached_acceptance", projected)
        detailed = projection.observation({**raw, "owner": station,
                                           "spread": {"x": 2, "y": 3,
                                                      "width": 4, "height": 5,
                                                      "private": 1},
                                           "airport": {"type_id": 1, "layout": 2,
                                                       "rotation": "east",
                                                       "closed": True,
                                                       "hangars": 3,
                                                       "blocks": 99},
                                           "cargo": [{**cargo, "monthly_supply": 0,
                                                      "rating_percent": 80}]}, spec)
        self.assertEqual(detailed["owner"], station)
        self.assertEqual(detailed["spread"], {"x": 2, "y": 3,
                                               "width": 4, "height": 5})
        self.assertEqual(detailed["airport"], {"type_id": 1, "layout": 2,
                                                "rotation": "east", "closed": True,
                                                "hangars": 3})
        self.assertEqual(detailed["cargo"][0]["monthly_supply"], 0)
        invalid = projection.observation({**raw, "sign_tile": True,
                                          "catchment_radius": True,
                                          "station": {"id": 4, "generation": True,
                                                      "engine_instance_id": "boot"},
                                          "cargo": [{**cargo, "rating_percent": True,
                                                     "accepts": 1,
                                                     "waiting": {"available": True,
                                                                 "reserved": 4,
                                                                 "total": 14}}]}, spec)
        self.assertNotIn("sign_tile", invalid)
        self.assertNotIn("catchment_radius", invalid)
        self.assertNotIn("generation", invalid["station"])
        self.assertNotIn("rating_percent", invalid["cargo"][0])
        self.assertNotIn("accepts", invalid["cargo"][0])
        self.assertNotIn("available", invalid["cargo"][0]["waiting"])

    def test_station_flow_projection_keeps_cursor_and_nullable_handles(self):
        station = {"id": 8, "generation": 5, "engine_instance_id": "boot"}
        row = {"source_station_id": 6, "source": None,
               "via_station_id": None, "via": None, "amount": 45,
               "restricted": False, "local_consumption": False,
               "unrestricted_boundary": 100, "graph_node": 7}
        raw = {"tick": 9, "company_id": 2, "station": station,
               "cargo_id": 1, "unit": "planned_flow_per_economy_month",
               "has_cargo_data": True, "distribution_mode": "symmetric",
               "rows": [row, {**row, "source": {**station, "pointer": 1},
                               "via_station_id": 8, "via": station,
                               "restricted": True, "local_consumption": True}],
               "next": {"after_source_id": 6,
                        "after_share_cumulative": 120,
                        "raw_iterator": 2}, "complete": False,
               "flow_shares": "private"}
        spec = {"name": "station.cargo.flows", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertIsNone(projected["rows"][0]["source"])
        self.assertTrue(projected["has_cargo_data"])
        self.assertEqual(projected["distribution_mode"], "symmetric")
        self.assertIsNone(projected["rows"][0]["via_station_id"])
        self.assertIsNone(projected["rows"][0]["via"])
        self.assertEqual(projected["rows"][1]["source"], station)
        self.assertEqual(projected["rows"][1]["via"], station)
        self.assertTrue(projected["rows"][1]["local_consumption"])
        self.assertEqual(projected["next"], {"after_source_id": 6,
                                              "after_share_cumulative": 120})
        self.assertNotIn("flow_shares", projected)
        self.assertNotIn("graph_node", projected["rows"][0])
        terminal = projection.observation({**raw, "next": None,
                                           "complete": True}, spec)
        self.assertIsNone(terminal["next"])
        invalid = projection.observation({**raw, "cargo_id": True,
                                          "has_cargo_data": 1,
                                          "distribution_mode": True,
                                          "complete": 1,
                                          "next": {"after_source_id": True,
                                                   "after_share_cumulative": 120},
                                          "rows": [{**row, "amount": True,
                                                     "restricted": 1,
                                                     "source": {"id": 8,
                                                                "generation": 5,
                                                                "engine_instance_id": True}}]}, spec)
        self.assertNotIn("cargo_id", invalid)
        self.assertNotIn("has_cargo_data", invalid)
        self.assertNotIn("distribution_mode", invalid)
        self.assertNotIn("complete", invalid)
        self.assertEqual(invalid["next"], {"after_share_cumulative": 120})
        self.assertNotIn("amount", invalid["rows"][0])
        self.assertNotIn("restricted", invalid["rows"][0])
        self.assertNotIn("engine_instance_id", invalid["rows"][0]["source"])

    def test_station_waiting_projection_preserves_manual_histogram_and_counters(self):
        station = {"id": 8, "generation": 5, "engine_instance_id": "boot"}
        key = {"first_station_id": 8, "next_hop_id": None,
               "packet_units": 2}
        row = {**key, "first_station": {**station, "pointer": 99},
               "source_visible_in_gui": False, "next_hop": None,
               "packet_count": 3, "units": 6,
               "packet_id": 900, "source_coordinates": [1, 2],
               "feeder_share": 4}
        raw = {"tick": 9, "company_id": 2,
               "station": {**station, "pointer": 1}, "cargo_id": 1,
               "distribution_mode": "manual",
               "route_details_suppressed": True,
               "destination_semantics": "not_estimated",
               "available_units": 6,
               "native_available_cached": 6,
               "native_reserved_cached": 0,
               "native_total_cached": 6,
               "available_matches_native_cache": True,
               "native_total_addition_fits_u32": True,
               "aggregate_version": "swh1:" + "a" * 64,
               "rows": [row], "next_after_group": key,
               "complete": False, "live": True,
               "estimated_destinations": ["private"]}
        spec = {"name": "station.cargo.waiting", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["station"], station)
        self.assertEqual(projected["aggregate_version"], "swh1:" + "a" * 64)
        self.assertEqual(projected["rows"], [{**key, "first_station": station,
                                              "source_visible_in_gui": False,
                                              "next_hop": None,
                                              "packet_count": 3, "units": 6}])
        self.assertEqual(projected["next_after_group"], key)
        self.assertEqual(projected["native_reserved_cached"], 0)
        self.assertTrue(projected["available_matches_native_cache"])
        self.assertTrue(projected["native_total_addition_fits_u32"])
        self.assertTrue(projected["live"])
        self.assertNotIn("estimated_destinations", projected)
        self.assertNotIn("packet_id", projected["rows"][0])
        self.assertNotIn("source_coordinates", projected["rows"][0])
        self.assertNotIn("feeder_share", projected["rows"][0])
        terminal = projection.observation({**raw, "rows": [],
                                           "next_after_group": None,
                                           "complete": True}, spec)
        self.assertEqual(terminal["rows"], [])
        self.assertIsNone(terminal["next_after_group"])
        self.assertTrue(terminal["complete"])
        self.assertTrue(terminal["live"])

    def test_station_waiting_projection_preserves_auto_unknown_and_uint64(self):
        station = {"id": 8, "generation": 5, "engine_instance_id": "boot"}
        row = {"first_station_id": 65535, "first_station": None,
               "source_visible_in_gui": True,
               "next_hop_id": 65535, "next_hop": None,
               "packet_units": 65535, "packet_count": 65538,
               "units": 4295032830}
        raw = {"tick": 10, "company_id": 2, "station": station,
               "cargo_id": 1, "distribution_mode": "asymmetric",
               "route_details_suppressed": False,
               "destination_semantics": "not_estimated",
               "available_units": 4295032830,
               "native_available_cached": 65534,
               "native_reserved_cached": 4294967295,
               "native_total_cached": 65533,
               "available_matches_native_cache": False,
               "native_total_addition_fits_u32": False,
               "aggregate_version": "swh1:" + "b" * 64,
               "rows": [row],
               "next_after_group": {"first_station_id": 65535,
                                    "next_hop_id": 65535,
                                    "packet_units": 65535},
               "complete": False, "live": True,
               "raw_packet_ids": [1, 2]}
        spec = {"name": "station.cargo.waiting", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["available_units"], 4295032830)
        self.assertEqual(projected["rows"], [row])
        self.assertIsNone(projected["rows"][0]["first_station"])
        self.assertIsNone(projected["rows"][0]["next_hop"])
        self.assertEqual(projected["next_after_group"]["next_hop_id"], 65535)
        self.assertFalse(projected["available_matches_native_cache"])
        self.assertFalse(projected["native_total_addition_fits_u32"])
        self.assertNotIn("raw_packet_ids", projected)
        invalid = projection.observation({**raw, "available_units": True,
            "live": 1,
            "native_reserved_cached": True,
            "native_total_addition_fits_u32": 1,
            "next_after_group": {"first_station_id": True,
                                 "next_hop_id": None,
                                 "packet_units": 1},
            "rows": [{**row, "packet_units": True,
                      "packet_count": True, "units": True,
                      "source_visible_in_gui": 1,
                      "first_station": {"id": 8, "generation": True,
                                        "engine_instance_id": "boot"}}]}, spec)
        self.assertNotIn("available_units", invalid)
        self.assertNotIn("live", invalid)
        self.assertNotIn("native_reserved_cached", invalid)
        self.assertNotIn("native_total_addition_fits_u32", invalid)
        self.assertNotIn("first_station_id", invalid["next_after_group"])
        for key in ("packet_units", "packet_count", "units",
                    "source_visible_in_gui"):
            self.assertNotIn(key, invalid["rows"][0])
        self.assertNotIn("generation", invalid["rows"][0]["first_station"])

    def test_station_links_projection_keeps_only_public_edge_stats(self):
        station = {"id": 8, "generation": 5, "engine_instance_id": "boot"}
        row = {"to": {**station, "node_id": 42},
               "monthly_capacity": 50, "monthly_usage": 23,
               "travel_days": 6, "shared_with_neutral": True,
               "graph_id": 12, "edge_capacity_raw": 180}
        raw = {"tick": 9, "company_id": 2, "station": station,
               "cargo_id": 1, "rows": [row], "next_after_offset": 12,
               "complete": False, "graph_id": 12}
        spec = {"name": "station.links", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["rows"], [{"to": station,
                                              "monthly_capacity": 50,
                                              "monthly_usage": 23,
                                              "travel_days": 6,
                                              "shared_with_neutral": True}])
        self.assertEqual(projected["next_after_offset"], 12)
        self.assertNotIn("graph_id", projected)
        terminal = projection.observation({**raw, "next_after_offset": None,
                                           "complete": True}, spec)
        self.assertIsNone(terminal["next_after_offset"])
        self.assertTrue(terminal["complete"])
        invalid = projection.observation({**raw, "next_after_offset": True,
                                          "rows": [{**row, "travel_days": True,
                                                     "shared_with_neutral": 1,
                                                     "to": {"id": 8,
                                                            "generation": 5,
                                                            "engine_instance_id": True}}]}, spec)
        self.assertNotIn("next_after_offset", invalid)
        self.assertNotIn("travel_days", invalid["rows"][0])
        self.assertNotIn("shared_with_neutral", invalid["rows"][0])
        self.assertNotIn("engine_instance_id", invalid["rows"][0]["to"])

    def test_story_book_scope_schema_preserves_null_and_rejects_bool(self):
        spec = {"parameters": {"type": "object", "required": [],
                "additionalProperties": False, "properties": {
                    "book_company_id": {"anyOf": [
                        {"type": "integer", "minimum": 0, "maximum": 14},
                        {"type": "null"}]},
                    "after_id": {"type": "integer", "minimum": -1,
                                 "maximum": 63999},
                    "limit": {"type": "integer", "minimum": 1,
                              "maximum": 32}}}}
        for good in ({}, {"book_company_id": None}, {"book_company_id": 0},
                     {"book_company_id": 14, "after_id": -1, "limit": 32}):
            self.assertEqual(validate_parameters(spec, good), good)
        for bad in ({"book_company_id": False}, {"book_company_id": True},
                    {"book_company_id": ""}, {"book_company_id": []},
                    {"book_company_id": 15}, {"book_company_id": None, "private": 1}):
            with self.subTest(bad=bad), self.assertRaises(ServiceError) as raised:
                validate_parameters(spec, bad)
            self.assertEqual(raised.exception.code, "invalid_request")

    def test_goal_questions_projection_keeps_issuance_generation_cursor(self):
        handle = {"id": 7, "generation": 3,
                  "engine_instance_id": "boot"}
        item = {"question_id": {**handle, "private": 1},
                "type": "question", "buttons": ["ok"],
                "text": "Continue?", "text_truncated": False,
                "target_client": 99}
        raw = {"tick": 4, "company_id": 2, "items": [item],
               "next_after_id": 7, "next_after_generation": 0,
               "native_inbox": "private"}
        spec = {"name": "goal.questions", "result_fields": tuple(raw)}
        first = projection.observation(raw, spec)
        self.assertEqual(first["next_after_id"], 7)
        self.assertEqual(first["next_after_generation"], 0)
        self.assertEqual(first["items"], [{"question_id": handle,
                                          "type": "question", "buttons": ["ok"],
                                          "text": "Continue?",
                                          "text_truncated": False}])
        self.assertNotIn("native_inbox", first)
        self.assertNotIn("target_client", first["items"][0])
        maximum = projection.observation({**raw,
            "next_after_generation": (1 << 64) - 1}, spec)
        self.assertEqual(maximum["next_after_generation"], (1 << 64) - 1)
        terminal = projection.observation({**raw, "next_after_id": None,
            "next_after_generation": None}, spec)
        self.assertIsNone(terminal["next_after_id"])
        self.assertIsNone(terminal["next_after_generation"])
        malformed = projection.observation({**raw,
            "next_after_generation": True,
            "items": [{**item, "question_id": {"id": 7,
                "generation": True, "engine_instance_id": "boot"}}]}, spec)
        self.assertNotIn("next_after_generation", malformed)
        self.assertNotIn("generation", malformed["items"][0]["question_id"])

    def test_story_question_pages_elements_chunks_and_candidates_are_positive(self):
        handle = {"id": 5, "generation": 9, "engine_instance_id": "boot"}
        question = {"question_id": {**handle, "private": 1}, "type": "question",
                    "buttons": ["ok", "cancel", True], "text": "Continue?",
                    "text_truncated": False, "target_client": 123}
        questions = projection.observation({"tick": 1, "company_id": 2,
            "items": [question], "next_after_id": None, "private": 1},
            {"name": "goal.questions", "result_fields": [
                "tick", "company_id", "items", "next_after_id"]})
        self.assertEqual(questions["items"], [{"question_id": handle,
            "type": "question", "buttons": ["ok", "cancel"],
            "text": "Continue?", "text_truncated": False}])
        self.assertIsNone(questions["next_after_id"])
        self.assertNotIn("private", questions)

        full = projection.observation({**question, "text": "é" * 4096,
            "text_truncated": False,
            "text_offset": 0, "text_bytes": 8192, "next_text_offset": None,
            "native_recipient": "private"}, {"name": "goal.question",
            "result_fields": ["question_id", "type", "buttons", "text",
                              "text_truncated", "text_offset", "text_bytes",
                              "next_text_offset"]})
        self.assertEqual(len(full["text"].encode("utf-8")), 8192)
        self.assertEqual(full["question_id"], handle)
        self.assertIsNone(full["next_text_offset"])
        self.assertIs(full["text_truncated"], False)
        self.assertNotIn("native_recipient", full)
        question_tail = projection.observation({**question,
            "text": "last", "text_truncated": True,
            "text_offset": 1000, "text_bytes": 1004,
            "next_text_offset": None}, {"name": "goal.question",
            "result_fields": ["text", "text_truncated", "text_offset",
                              "text_bytes", "next_text_offset"]})
        self.assertEqual(question_tail["text"], "last")
        self.assertEqual(question_tail["text_offset"], 1000)
        self.assertIs(question_tail["text_truncated"], True)
        self.assertIsNone(question_tail["next_text_offset"])

        page = {"page_id": {**handle, "pointer": 1}, "book_company_id": None,
                "sort_order": 0, "date": -1, "title": "Global",
                "title_truncated": False, "interactive": True, "private": 1}
        pages = projection.observation({"items": [page, {**page,
            "book_company_id": 0, "interactive": False},
            {**page, "book_company_id": False, "interactive": 1}],
            "next_after_id": 10}, {"name": "story.pages",
            "result_fields": ["items", "next_after_id"]})
        self.assertIsNone(pages["items"][0]["book_company_id"])
        self.assertEqual(pages["items"][1]["book_company_id"], 0)
        self.assertIs(pages["items"][1]["interactive"], False)
        self.assertNotIn("book_company_id", pages["items"][2])
        self.assertNotIn("interactive", pages["items"][2])
        self.assertNotIn("private", pages["items"][0])

        element = {"element_id": {**handle, "pointer": 1},
                   "sort_order": 1, "type": "goal", "text": "Rival goal",
                   "text_truncated": False, "location": {"tile": 1,
                   "x": 1, "y": 0, "private": 1},
                   "goal_id": {**handle, "pointer": 1}, "completed": False,
                   "vehicle_type": None, "packed_button_data": 999}
        elements = projection.observation({**page, "page_version": "spv1:" + "a" * 64,
            "title_text": {"text": "Global", "text_offset": 0,
                           "text_bytes": 6, "next_text_offset": None,
                           "encoded_string": "secret"},
            "items": [element], "next_after_id": 11},
            {"name": "story.elements", "result_fields": [
                "page_id", "book_company_id", "sort_order", "date", "title",
                "title_truncated", "interactive", "page_version",
                "title_text", "items", "next_after_id"]})
        self.assertEqual(elements["page_id"], handle)
        self.assertEqual(elements["items"][0]["goal_id"], handle)
        self.assertEqual(elements["items"][0]["location"],
                         {"tile": 1, "x": 1, "y": 0})
        self.assertNotIn("packed_button_data", elements["items"][0])
        self.assertNotIn("encoded_string", elements["title_text"])

        detail = projection.observation({**element, "page_id": handle,
            "page_version": "spv1:" + "b" * 64, "text": "αβγ",
            "text_offset": 0, "text_bytes": 6, "next_text_offset": None,
            "raw_reference": 123}, {"name": "story.element",
            "result_fields": ["element_id", "sort_order", "type", "text",
                              "text_truncated", "location", "goal_id",
                              "completed", "vehicle_type", "page_id",
                              "page_version", "text_offset", "text_bytes",
                              "next_text_offset"]})
        self.assertEqual(detail["text"], "αβγ")
        self.assertEqual(detail["text_bytes"], 6)
        self.assertIsNone(detail["vehicle_type"])
        self.assertNotIn("raw_reference", detail)
        complete_chunk = projection.observation({**element,
            "text": "x" * 1000, "text_truncated": False,
            "text_offset": 0, "text_bytes": 1000,
            "next_text_offset": None}, {"name": "story.element",
            "result_fields": ["text", "text_truncated", "text_offset",
                              "text_bytes", "next_text_offset"]})
        self.assertEqual(len(complete_chunk["text"]), 1000)
        self.assertIsNone(complete_chunk["next_text_offset"])
        self.assertIs(complete_chunk["text_truncated"], False)
        element_tail = projection.observation({**element,
            "text": "last", "text_truncated": True,
            "text_offset": 1000, "text_bytes": 1004,
            "next_text_offset": None}, {"name": "story.element",
            "result_fields": ["text", "text_truncated", "text_offset",
                              "text_bytes", "next_text_offset"]})
        self.assertEqual(element_tail["text_offset"], 1000)
        self.assertIs(element_tail["text_truncated"], True)
        self.assertIsNone(element_tail["next_text_offset"])

        candidates = projection.observation({"page_version": "spv1:" + "c" * 64,
            "items": [{"vehicle_id": {**handle, "pointer": 1},
                       "owner_company_id": 3, "vehicle_type": "rail",
                       "name": "Train", "location": {"tile": 1, "x": 1,
                       "y": 0}, "private": 1}], "next_after_id": None},
            {"name": "story.vehicle_candidates", "result_fields": [
                "page_version", "items", "next_after_id"]})
        self.assertEqual(candidates["items"], [{"vehicle_id": handle,
            "owner_company_id": 3, "vehicle_type": "rail", "name": "Train",
            "location": {"tile": 1, "x": 1, "y": 0}}])
        self.assertIsNone(candidates["next_after_id"])

        outcome = projection.outcome({"success": True, "cost": 0,
            "page_version": "private", "native_command": 123},
            {"name": "story.button.press", "result_fields": [
                "success", "cost", "error"]})
        self.assertEqual(outcome, {"success": True, "cost": 0})

    def test_vehicle_details_and_company_valuation_positive_boundaries(self):
        handle = {"id": 5, "generation": 9, "engine_instance_id": "boot"}
        fields = ["tick", "company_id", "vehicle_id", "owner_company_id",
                  "owned_by_actor", "name", "vehicle_type", "engine_id",
                  "location", "stopped", "crashed", "in_depot",
                  "can_refit_now", "age_calendar_days", "maximum_age_calendar_days",
                  "build_year", "reliability_percent",
                  "engine_reliability_percent", "breakdowns_since_last_service",
                  "running_cost", "running_cost_unit", "profit_this_period",
                  "profit_last_period", "service_interval", "service_custom",
                  "service_percent", "last_service_economy_date", "order_count",
                  "manual_order_count", "current_order_position", "part_count",
                  "consist_version", "parts", "next_after_part_position"]
        raw = {"tick": 10, "company_id": 2, "vehicle_id": {**handle,
               "pointer": 1}, "owner_company_id": 2, "owned_by_actor": True,
               "name": "Train", "vehicle_type": "rail", "engine_id": 1,
               "location": None, "stopped": False, "crashed": False,
               "in_depot": True, "can_refit_now": True,
               "age_calendar_days": 0, "maximum_age_calendar_days": 100,
               "build_year": 1950, "reliability_percent": 0,
               "engine_reliability_percent": 50,
               "breakdowns_since_last_service": 0, "running_cost": 0,
               "running_cost_unit": "year", "profit_this_period": -1,
               "profit_last_period": 0, "service_interval": 0,
               "service_custom": False, "service_percent": True,
               "last_service_economy_date": 1, "order_count": 0,
               "manual_order_count": 0, "current_order_position": None,
               "part_count": 2, "consist_version": "cv1:" + "a" * 64,
               "parts": [{"position": 0, "vehicle_id": {**handle,
                          "pointer": 1}, "engine_id": 1, "cargo_id": None,
                          "cargo_subtype": None, "capacity": 0, "load": 0,
                          "can_carry_cargo": False,
                          "refittable_cargo_ids": [0, 1, True],
                          "private": 1}],
               "next_after_part_position": 0, "private": 1}
        spec = {"name": "vehicle.details", "result_fields": fields}
        owned = projection.observation(raw, spec)
        self.assertEqual(owned["vehicle_id"], handle)
        self.assertIsNone(owned["location"])
        self.assertIsNone(owned["current_order_position"])
        self.assertEqual(owned["parts"][0]["refittable_cargo_ids"], [0, 1])
        self.assertIsNone(owned["parts"][0]["cargo_id"])
        self.assertEqual(owned["next_after_part_position"], 0)
        self.assertNotIn("private", owned)
        self.assertNotIn("private", owned["parts"][0])

        foreign = projection.observation({**raw, "owner_company_id": 3,
            "owned_by_actor": False, "location": {"tile": 0,
            "x": 0, "y": 0, "pointer": 1},
            "parts": [{**raw["parts"][0], "position": True,
                       "can_carry_cargo": 0}]}, spec)
        self.assertEqual(foreign["location"], {"tile": 0, "x": 0, "y": 0})
        self.assertNotIn("can_refit_now", foreign)
        self.assertNotIn("refittable_cargo_ids", foreign["parts"][0])
        self.assertNotIn("position", foreign["parts"][0])
        self.assertNotIn("can_carry_cargo", foreign["parts"][0])
        self.assertEqual(foreign["consist_version"], "cv1:" + "a" * 64)

        valuation = projection.observation({"tick": 8, "company_id": 2,
            "company": {**handle, "private": 1}, "company_value": 0,
            "private_balance": 999}, {"name": "company.valuation",
            "result_fields": ["tick", "company_id", "company", "company_value"]})
        self.assertEqual(valuation, {"tick": 8, "company_id": 2,
                                     "company": handle, "company_value": 0})
        invalid_value = projection.observation({"company_value": True},
            {"name": "company.valuation", "result_fields": ["company_value"]})
        self.assertEqual(invalid_value, {})
        report = projection.observation({"company_value": 999, "balance": 5},
            {"name": "company.report", "result_fields": ["company_value", "balance"]})
        self.assertEqual(report, {"balance": 5})

    def test_vehicle_refit_subtypes_projection_keeps_fenced_part_and_labels(self):
        primary = {"id": 40, "generation": 7, "engine_instance_id": "boot"}
        part = {"id": 41, "generation": 9, "engine_instance_id": "boot"}
        raw = {"tick": 5, "company_id": 2,
               "vehicle_id": {**primary, "pointer": 1},
               "part_id": {**part, "raw_engine": 4},
               "cargo_id": 0, "refittable": True,
               "current_cargo": False, "keep_option": True,
               "subtypes": [{"value": 0, "label": "Café",
                             "label_truncated": False, "grf_registers": [1]},
                            {"value": 1, "label": "X" * 128,
                             "label_truncated": True,
                             "raw_encoded_string": "private"}],
               "next_after_subtype": 1, "complete": False,
               "probe_cap_reached": False,
               "callback_id": 22, "cache_state": "private"}
        spec = {"name": "vehicle.refit_subtypes", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected, {"tick": 5, "company_id": 2,
            "vehicle_id": primary, "part_id": part,
            "cargo_id": 0, "refittable": True, "current_cargo": False,
            "keep_option": True,
            "subtypes": [{"value": 0, "label": "Café",
                          "label_truncated": False},
                         {"value": 1, "label": "X" * 128,
                          "label_truncated": True}],
            "next_after_subtype": 1, "complete": False,
            "probe_cap_reached": False})
        terminal = projection.observation({**raw, "subtypes": [],
            "next_after_subtype": None, "complete": True,
            "probe_cap_reached": True}, spec)
        self.assertEqual(terminal["subtypes"], [])
        self.assertIsNone(terminal["next_after_subtype"])
        self.assertTrue(terminal["probe_cap_reached"])
        oversized = projection.observation({**raw,
            "subtypes": [{"value": index, "label": "Label",
                          "label_truncated": False} for index in range(17)]}, spec)
        self.assertEqual(len(oversized["subtypes"]), 16)

    def test_vehicle_refit_subtypes_projection_drops_bool_ints_and_private(self):
        handle = {"id": 41, "generation": 9, "engine_instance_id": "boot"}
        raw = {"tick": 5, "company_id": 2,
               "vehicle_id": {**handle, "pointer": 1},
               "part_id": {"id": True, "generation": 9,
                           "engine_instance_id": "boot"},
               "cargo_id": True, "refittable": 1,
               "current_cargo": True, "keep_option": "true",
               "subtypes": [{"value": True, "label": "Vanilla",
                             "label_truncated": 1,
                             "private_callback": "secret"}],
               "next_after_subtype": True, "complete": 1,
               "probe_cap_reached": "false", "raw_command": 12}
        spec = {"name": "vehicle.refit_subtypes", "result_fields": tuple(raw)}
        projected = projection.observation(raw, spec)
        self.assertEqual(projected["vehicle_id"], handle)
        self.assertNotIn("id", projected["part_id"])
        for key in ("cargo_id", "refittable", "keep_option",
                    "next_after_subtype", "complete", "probe_cap_reached"):
            self.assertNotIn(key, projected)
        self.assertEqual(projected["subtypes"], [{"label": "Vanilla"}])
        self.assertNotIn("raw_command", projected)

    def test_matrix_uses_only_canonical_names(self):
        names = {"road.build": {}, "vehicle.build": {}, "tool.apply": {}}
        rows = matrix(names)
        indexed = {row["action"]: row for row in rows}
        self.assertTrue(indexed["road.build"]["available"])
        self.assertTrue(indexed["vehicle.build"]["available"])
        self.assertFalse(indexed["rail.build"]["available"])
        self.assertFalse(indexed["airport.build"]["available"])
        self.assertEqual(indexed["water.dock.build"]["category"], "ship")
        self.assertEqual(indexed["bridge.build"]["category"], "bridge/tunnel")
        self.assertFalse(indexed["client display preferences and GUI configuration"]["available"])
        self.assertNotIn("tool.apply", indexed)
        self.assertNotIn("ui.apply", indexed)
        self.assertEqual(len(rows), 103 + len(CATALOG["human_only"]))


if __name__ == "__main__":
    unittest.main()
