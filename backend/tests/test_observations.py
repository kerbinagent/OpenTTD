"""Closed observation requests normalize into independent, bounded values."""

import copy
from dataclasses import FrozenInstanceError
import unittest

from backend.errors import ServiceError
from backend.observations import ObservationImage, capability, validate_request


GENERATION = "0123456789abcdef0123456789abcdef"
REQUEST_ID = "obs1:1712345678901:abcdef0123456789abcdef0123456789"
OBSERVATION_ID = "fedcba9876543210fedcba9876543210"


def world_request():
    return {
        "observation_generation": GENERATION,
        "observation_request_id": REQUEST_ID,
        "selection": {
            "kind": "map_center", "tile": {"x": 12, "y": 34},
            "zoom_level": "normal",
        },
        "output": {"width": 640, "height": 480},
    }


def native_request(panel="company_finances"):
    return {
        "observation_generation": GENERATION,
        "observation_request_id": REQUEST_ID,
        "panel": panel,
    }


def assign_path(body, path, value):
    for component in path[:-1]:
        body = body[component]
    body[path[-1]] = value


class ObservationRequestTests(unittest.TestCase):
    def assert_invalid(self, kind, body):
        with self.assertRaises(ServiceError) as raised:
            validate_request(kind, body)
        self.assertEqual(raised.exception.code, "invalid_request")

    def test_world_defaults_are_complete_and_do_not_mutate_input(self):
        body = world_request()
        before = copy.deepcopy(body)
        result = validate_request("world", body)
        self.assertEqual(body, before)
        self.assertEqual(result["observation_generation"], GENERATION)
        self.assertEqual(result["observation_request_id"], REQUEST_ID)
        self.assertEqual(result["wait_ms"], 1500)
        self.assertEqual(result["selection"], body["selection"])
        self.assertEqual(result["output"], body["output"])
        self.assertEqual(result["labels"], {
            "town_names": True, "station_names": True, "waypoint_names": False,
            "signs": False, "competitor_labels": False,
        })
        result["selection"]["tile"]["x"] = 99
        result["output"]["width"] = 64
        result["labels"]["town_names"] = False
        self.assertEqual(body, before)
        self.assertTrue(validate_request("world", body)["labels"]["town_names"])

    def test_world_explicit_options_and_partial_labels(self):
        body = world_request()
        body.update({"after_request_id": "request_1.x:y-z", "wait_ms": 0,
                     "labels": {"town_names": False, "signs": True}})
        result = validate_request("world", body)
        self.assertEqual(result["after_request_id"], "request_1.x:y-z")
        self.assertEqual(result["wait_ms"], 0)
        self.assertEqual(result["labels"], {
            "town_names": False, "station_names": True, "waypoint_names": False,
            "signs": True, "competitor_labels": False,
        })

    def test_map_center_and_image_point_accept_coordinate_boundaries(self):
        for value in (0, 65535):
            body = world_request()
            body["selection"]["tile"] = {"x": value, "y": value}
            self.assertEqual(validate_request("world", body)["selection"], body["selection"])
        for pixel in ({"x": 0, "y": 0}, {"x": 1279, "y": 1023}):
            body = world_request()
            body["selection"] = {
                "kind": "map_image_point", "observation_id": OBSERVATION_ID,
                "pixel": pixel, "zoom_level": "normal",
            }
            before = copy.deepcopy(body)
            result = validate_request("world", body)
            self.assertEqual(result["selection"], body["selection"])
            result["selection"]["pixel"]["x"] = 100
            self.assertEqual(body, before)

    def test_output_limits_and_every_zoom_level(self):
        for width, height in ((64, 64), (1280, 64), (64, 1024), (1024, 1024)):
            body = world_request()
            body["output"] = {"width": width, "height": height}
            self.assertEqual(validate_request("world", body)["output"], body["output"])
        for zoom in ("in4x", "in2x", "normal", "out2x", "out4x", "out8x"):
            body = world_request()
            body["selection"]["zoom_level"] = zoom
            body["output"] = {"width": 64, "height": 64}
            self.assertEqual(validate_request("world", body)["selection"]["zoom_level"], zoom)

    def test_pixel_and_native_virtual_area_caps(self):
        for zoom, valid_output, invalid_output in (
            ("in4x", {"width": 1280, "height": 819}, {"width": 1280, "height": 820}),
            ("out4x", {"width": 1024, "height": 512}, {"width": 1025, "height": 512}),
            ("out8x", {"width": 512, "height": 256}, {"width": 513, "height": 256}),
        ):
            with self.subTest(zoom=zoom):
                body = world_request()
                body["selection"]["zoom_level"] = zoom
                body["output"] = valid_output
                self.assertEqual(validate_request("world", body)["output"], valid_output)
                body["output"] = invalid_output
                self.assert_invalid("world", body)

    def test_required_fields_and_unknown_request_kinds(self):
        for kind, factory, fields in (
            ("world", world_request, ("observation_generation", "observation_request_id", "selection", "output")),
            ("native_info", native_request, ("observation_generation", "observation_request_id", "panel")),
        ):
            for field in fields:
                with self.subTest(kind=kind, field=field):
                    body = factory()
                    del body[field]
                    self.assert_invalid(kind, body)
        for kind in ("", "native-info", "other", None, True):
            with self.subTest(kind=kind):
                self.assert_invalid(kind, world_request())

    def test_common_fields_have_strict_types_and_formats(self):
        invalid = {
            "observation_generation": (None, False, 1, [], {}, "0" * 31, "0" * 33,
                                       "A" * 32, "g" * 32, GENERATION + "\n"),
            "observation_request_id": (None, False, 1, [], {}, "", "obs1:171234567890:" + "a" * 32,
                                       "obs1:17123456789012:" + "a" * 32,
                                       "obs2:1712345678901:" + "a" * 32,
                                       "obs1:1712345678901:" + "A" * 32,
                                       REQUEST_ID + "\n"),
            "after_request_id": (None, False, 1, [], {}, "", "x" * 129,
                                 "a/b", "white space", "line\nbreak", "a\n"),
            "wait_ms": (None, False, True, -1, 2001, 1.0, "1", [], {}),
        }
        for kind, factory in (("world", world_request), ("native_info", native_request)):
            for field, values in invalid.items():
                for value in values:
                    with self.subTest(kind=kind, field=field, value=value):
                        body = factory()
                        body[field] = value
                        self.assert_invalid(kind, body)
            for wait_ms in (0, 2000):
                body = factory()
                body["wait_ms"] = wait_ms
                body["after_request_id"] = "a" * 128
                result = validate_request(kind, body)
                self.assertEqual(result["wait_ms"], wait_ms)
                self.assertEqual(result["after_request_id"], "a" * 128)

    def test_numeric_fields_reject_bool_float_and_out_of_range(self):
        fields = (
            (("selection", "tile", "x"), 0, 65535),
            (("selection", "tile", "y"), 0, 65535),
            (("output", "width"), 64, 1280),
            (("output", "height"), 64, 1024),
        )
        for path, minimum, maximum in fields:
            for value in (False, True, float(minimum), str(minimum), None, minimum - 1, maximum + 1):
                with self.subTest(path=path, value=value):
                    body = world_request()
                    assign_path(body, path, value)
                    self.assert_invalid("world", body)
        for key, maximum in (("x", 1279), ("y", 1023)):
            for value in (False, True, 0.0, "0", None, -1, maximum + 1):
                with self.subTest(pixel=key, value=value):
                    body = world_request()
                    body["selection"] = {
                        "kind": "map_image_point", "observation_id": OBSERVATION_ID,
                        "pixel": {"x": 0, "y": 0}, "zoom_level": "normal",
                    }
                    body["selection"]["pixel"][key] = value
                    self.assert_invalid("world", body)

    def test_selection_discriminators_ids_and_required_fields(self):
        for value in (None, [], "map_center", {}, {"kind": "unknown"}):
            with self.subTest(selection=value):
                body = world_request()
                body["selection"] = value
                self.assert_invalid("world", body)
        for field in ("kind", "tile", "zoom_level"):
            body = world_request()
            del body["selection"][field]
            self.assert_invalid("world", body)
        for zoom in (None, True, 0, "", "normal\n", "out16x"):
            body = world_request()
            body["selection"]["zoom_level"] = zoom
            self.assert_invalid("world", body)
        selection = {"kind": "map_image_point", "observation_id": OBSERVATION_ID,
                     "pixel": {"x": 0, "y": 0}, "zoom_level": "normal"}
        for field in selection:
            body = world_request()
            body["selection"] = copy.deepcopy(selection)
            del body["selection"][field]
            self.assert_invalid("world", body)
        for value in (None, 1, True, "a" * 31, "a" * 33, "A" * 32, OBSERVATION_ID + "\n"):
            body = world_request()
            body["selection"] = {**selection, "observation_id": value}
            self.assert_invalid("world", body)

    def test_world_objects_are_closed_and_require_objects(self):
        for path in ((), ("selection",), ("selection", "tile"), ("output",), ("labels",)):
            body = world_request()
            body["labels"] = {}
            target = body
            for component in path:
                target = target[component]
            target["unknown"] = True
            with self.subTest(path=path):
                self.assert_invalid("world", body)
        for path in (("selection", "tile"), ("output",), ("labels",)):
            for value in (None, [], "", True, 1):
                with self.subTest(path=path, value=value):
                    body = world_request()
                    assign_path(body, path, value)
                    self.assert_invalid("world", body)
        for field in ("width", "height"):
            body = world_request()
            del body["output"][field]
            self.assert_invalid("world", body)
        for field in ("x", "y"):
            body = world_request()
            del body["selection"]["tile"][field]
            self.assert_invalid("world", body)
        for value in (None, [], "", True, 1):
            self.assert_invalid("world", value)

    def test_image_point_objects_are_closed(self):
        for path in (("selection",), ("selection", "pixel")):
            body = world_request()
            body["selection"] = {
                "kind": "map_image_point", "observation_id": OBSERVATION_ID,
                "pixel": {"x": 0, "y": 0}, "zoom_level": "normal",
            }
            target = body
            for component in path:
                target = target[component]
            target["unknown"] = True
            self.assert_invalid("world", body)
        body = world_request()
        body["selection"]["observation_id"] = OBSERVATION_ID
        self.assert_invalid("world", body)

    def test_labels_are_strict_booleans(self):
        names = ("town_names", "station_names", "waypoint_names", "signs", "competitor_labels")
        for name in names:
            for value in (True, False):
                body = world_request()
                body["labels"] = {name: value}
                self.assertIs(validate_request("world", body)["labels"][name], value)
            for value in (None, 0, 1, "true", [], {}):
                with self.subTest(name=name, value=value):
                    body = world_request()
                    body["labels"] = {name: value}
                    self.assert_invalid("world", body)

    def test_finance_defaults_and_explicit_compact_layout(self):
        body = native_request()
        before = copy.deepcopy(body)
        result = validate_request("native_info", body)
        self.assertEqual(body, before)
        self.assertEqual(result["target"], {"kind": "self"})
        self.assertEqual(result["presentation"], {"layout": "expanded"})
        self.assertEqual(result["region"], "window")
        self.assertEqual(result["wait_ms"], 1500)
        body["presentation"] = {}
        self.assertEqual(validate_request("native_info", body)["presentation"], {"layout": "expanded"})
        body.update({"target": {"kind": "self"}, "presentation": {"layout": "compact"},
                     "region": "window"})
        result = validate_request("native_info", body)
        self.assertEqual(result["presentation"], {"layout": "compact"})
        result["target"]["kind"] = "changed"
        result["presentation"]["layout"] = "changed"
        self.assertEqual(body["target"], {"kind": "self"})
        self.assertEqual(body["presentation"], {"layout": "compact"})

    def test_directory_defaults_partial_presentation_and_sort_options(self):
        body = native_request("town_directory")
        result = validate_request("native_info", body)
        self.assertEqual(result["presentation"], {
            "sort": {"by": "name", "direction": "ascending"}, "scroll": {"first_row": 0},
        })
        self.assertEqual(result["region"], "window")
        for by in ("name", "population", "rating"):
            for direction in ("ascending", "descending"):
                for first_row in (0, 65535):
                    body["presentation"] = {"sort": {"by": by, "direction": direction},
                                            "scroll": {"first_row": first_row}}
                    self.assertEqual(validate_request("native_info", body)["presentation"], body["presentation"])
        for presentation, expected in (
            ({}, {"sort": {"by": "name", "direction": "ascending"}, "scroll": {"first_row": 0}}),
            ({"sort": {"by": "rating"}}, {"sort": {"by": "rating", "direction": "ascending"}, "scroll": {"first_row": 0}}),
            ({"scroll": {}}, {"sort": {"by": "name", "direction": "ascending"}, "scroll": {"first_row": 0}}),
        ):
            body["presentation"] = presentation
            before = copy.deepcopy(body)
            self.assertEqual(validate_request("native_info", body)["presentation"], expected)
            self.assertEqual(body, before)

    def test_native_panels_and_regions_are_closed(self):
        for panel in (None, True, 1, "", "company", "town_directory\n"):
            self.assert_invalid("native_info", native_request(panel))
        for panel in ("company_finances", "town_directory"):
            for region in (None, True, 1, "", "viewport", "window\n", {}):
                body = native_request(panel)
                body["region"] = region
                self.assert_invalid("native_info", body)
            for field in ("selection", "output", "labels", "unknown"):
                body = native_request(panel)
                body[field] = {}
                self.assert_invalid("native_info", body)
        body = native_request("town_directory")
        body["target"] = {"kind": "self"}
        self.assert_invalid("native_info", body)

    def test_native_nested_objects_are_closed_and_panel_specific(self):
        cases = (
            ("company_finances", "target", None),
            ("company_finances", "target", {}),
            ("company_finances", "target", {"kind": "company", "company_id": 0}),
            ("company_finances", "target", {"kind": "self", "company_id": 0}),
            ("company_finances", "presentation", None),
            ("company_finances", "presentation", {"layout": "other"}),
            ("company_finances", "presentation", {"layout": True}),
            ("company_finances", "presentation", {"layout": "compact", "unknown": True}),
            ("company_finances", "presentation", {"sort": {"by": "name"}}),
            ("town_directory", "presentation", None),
            ("town_directory", "presentation", {"layout": "compact"}),
            ("town_directory", "presentation", {"unknown": True}),
            ("town_directory", "presentation", {"sort": None}),
            ("town_directory", "presentation", {"sort": {"by": "other"}}),
            ("town_directory", "presentation", {"sort": {"by": True}}),
            ("town_directory", "presentation", {"sort": {"direction": "other"}}),
            ("town_directory", "presentation", {"sort": {"unknown": True}}),
            ("town_directory", "presentation", {"scroll": None}),
            ("town_directory", "presentation", {"scroll": {"unknown": True}}),
        )
        for panel, field, value in cases:
            with self.subTest(panel=panel, field=field, value=value):
                body = native_request(panel)
                body[field] = value
                self.assert_invalid("native_info", body)
        for value in (False, True, -1, 65536, 0.0, "0", None):
            body = native_request("town_directory")
            body["presentation"] = {"scroll": {"first_row": value}}
            self.assert_invalid("native_info", body)

    def test_capability_reports_generation_and_observation_routes(self):
        result = capability(GENERATION)
        self.assertEqual(result["observation_generation"], GENERATION)
        self.assertEqual(result["endpoint_world"], "/v1/view/observe/world")
        self.assertEqual(result["endpoint_native_info"], "/v1/view/observe/native-info")
        result["label_defaults"]["town_names"] = False
        result["panels"]["town_directory"]["presentation"]["scroll"]["first_row"] = 42
        fresh = capability(GENERATION)
        self.assertTrue(fresh["label_defaults"]["town_names"])
        self.assertEqual(fresh["panels"]["town_directory"]["presentation"]["scroll"]["first_row"], 0)

    def test_observation_image_is_frozen(self):
        image = ObservationImage(
            data=b"png", observation_id=OBSERVATION_ID, image_id="1" * 32,
            generation=GENERATION, kind="world", width=64, height=64,
            causal_state="current", reused=False, age_ms=0,
        )
        self.assertEqual(image.data, b"png")
        self.assertEqual(image.observation_id, OBSERVATION_ID)
        self.assertFalse(image.reused)
        with self.assertRaises(FrozenInstanceError):
            image.age_ms = 1


if __name__ == "__main__":
    unittest.main()
