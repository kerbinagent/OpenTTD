"""Positive DTO boundary for metadata-only custom news and selected text."""

import unittest

from backend import projection


FIELDS = ["tick", "company_id", "history_scope", "history_start", "latest",
          "gap", "items", "next", "complete"]


def cursor(number):
    return {"id": number, "generation": 3, "engine_instance_id": "boot"}


class NewsProjectionTest(unittest.TestCase):
    def test_full_page_retains_only_metadata_and_kind_specific_reference(self):
        handle = {"id": 42, "generation": 7, "engine_instance_id": "boot"}
        references = [
            None,
            {"kind": "tile", "location": {"x": 1, "y": 2}},
            {"kind": "town", "target": handle, "location": {"x": 3, "y": 4}},
            {"kind": "industry", "target": handle, "location": None},
            {"kind": "station", "target": handle, "location": {"x": 5, "y": 6}},
            {"kind": "vehicle", "target": handle, "location": None},
            {"kind": "engine", "engine_id": 15},
        ]
        rows = []
        for index in range(32):
            reference = references[index % len(references)]
            if reference is not None:
                reference = {**reference, "private": 99, "engine_id": 27,
                             "target": {**handle, "private": 88},
                             "location": {"x": 1, "y": 2, "private": 77}}
                # Preserve each variant's meaningful source fields alongside
                # injected cross-kind fields, which the projection must remove.
                if index % len(references) in (2, 4):
                    reference["location"] = {"x": 3, "y": 4, "private": 77}
                if index % len(references) in (3, 5):
                    reference["location"] = None
            rows.append({"news_id": {**cursor(index + 1), "private": 1},
                         "issued_tick": 100 + index,
                         "calendar_date": {"year": 1950, "month": 1,
                                           "day": index % 28 + 1,
                                           "private": 1},
                         "news_type": "general", "reference": reference,
                         "text": "secret body", "text_truncated": False,
                         "text_bytes": 11, "preview": "secret",
                         "encoded": "raw", "private": 1})
        raw = {"tick": 300, "company_id": 2,
               "history_scope": "current_engine_instance",
               "history_start": {**cursor(0), "private": 1},
               "latest": cursor(32), "gap": False, "items": rows,
               "next": cursor(32), "complete": True,
               "encoded_body": "private", "decoded_length": 4000}
        result = projection.observation(raw, {"name": "news.custom",
                                               "result_fields": FIELDS})
        self.assertEqual(len(result["items"]), 32)
        self.assertEqual(result["history_start"], cursor(0))
        self.assertEqual(result["latest"], cursor(32))
        self.assertEqual(result["next"], cursor(32))
        self.assertIs(result["gap"], False)
        self.assertIs(result["complete"], True)
        self.assertEqual(result["items"][0]["reference"], None)
        self.assertEqual(result["items"][1]["reference"],
                         {"kind": "tile", "location": {"x": 1, "y": 2}})
        self.assertEqual(result["items"][2]["reference"],
                         {"kind": "town", "target": handle,
                          "location": {"x": 3, "y": 4}})
        self.assertEqual(result["items"][3]["reference"],
                         {"kind": "industry", "target": handle,
                          "location": None})
        self.assertEqual(result["items"][4]["reference"],
                         {"kind": "station", "target": handle,
                          "location": {"x": 3, "y": 4}})
        self.assertEqual(result["items"][5]["reference"],
                         {"kind": "vehicle", "target": handle,
                          "location": None})
        self.assertEqual(result["items"][6]["reference"],
                         {"kind": "engine", "engine_id": 27})
        self.assertEqual(result["items"][31]["news_id"], cursor(32))
        for row in result["items"]:
            self.assertEqual(set(row), {"news_id", "issued_tick",
                                        "calendar_date", "news_type", "reference"})
            self.assertNotIn("private", row["calendar_date"])
        self.assertNotIn("encoded_body", result)
        self.assertNotIn("decoded_length", result)

    def test_malformed_reference_variants_drop_the_reference_only(self):
        valid = {"id": 2, "generation": 3, "engine_instance_id": "boot"}
        malformed = [
            {"kind": "unknown", "target": valid},
            {"kind": "tile", "location": None},
            {"kind": "tile", "location": {"x": True, "y": 1}},
            {"kind": "town", "target": {"id": True, "generation": 3,
                                        "engine_instance_id": "boot"},
             "location": None},
            {"kind": "station", "target": valid},
            {"kind": "engine", "engine_id": True},
            {"kind": True, "engine_id": 2},
        ]
        rows = [{"news_id": cursor(i + 1), "issued_tick": i,
                 "calendar_date": {"year": 1950, "month": 1, "day": 1},
                 "news_type": "general", "reference": reference}
                for i, reference in enumerate(malformed)]
        result = projection.observation({"tick": 8, "company_id": 1,
            "history_scope": "current_engine_instance", "items": rows,
            "history_start": cursor(0), "latest": cursor(7),
            "gap": True, "next": cursor(7), "complete": True},
            {"name": "news.custom", "result_fields": FIELDS})
        self.assertEqual(len(result["items"]), 7)
        self.assertTrue(all("reference" not in row for row in result["items"]))
        self.assertTrue(all("news_id" in row for row in result["items"]))

    def test_news_display_text_uses_existing_selected_text_projection(self):
        target = cursor(9)
        raw = {"tick": 12, "company_id": 2, "kind": "news_custom_text",
               "target": {**target, "private": 1},
               "text": "Café 🚂", "content_version": "a" * 64,
               "total_bytes": 10, "next_after_byte": None,
               "cargo_id": 4, "vehicle_id": target, "subtype": 2,
               "direction": "accepted", "slot": 3,
               "encoded": "private", "decoded_length": 10}
        spec = {"name": "display.text", "result_fields": tuple(raw)}
        result = projection.observation(raw, spec)
        self.assertEqual(result, {"tick": 12, "company_id": 2,
            "kind": "news_custom_text", "target": target,
            "text": "Café 🚂", "content_version": "a" * 64,
            "total_bytes": 10, "next_after_byte": None})
        malformed = projection.observation({**raw, "target": {
            "id": True, "generation": 3, "engine_instance_id": "boot"},
            "total_bytes": True}, spec)
        self.assertNotIn("id", malformed["target"])
        self.assertNotIn("total_bytes", malformed)
        unknown = projection.observation({**raw, "kind": "private_kind"}, spec)
        self.assertEqual(unknown, {"tick": 12, "company_id": 2})


if __name__ == "__main__":
    unittest.main()
