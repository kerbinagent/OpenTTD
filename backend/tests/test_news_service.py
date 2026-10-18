"""The existing text observation route for bounded custom-news selectors."""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from backend.errors import ServiceError
from backend.service import Service
from backend.store import Store
from backend.tests.test_service import FakeEngine


def cursor_schema(minimum):
    return {"type": "object", "additionalProperties": False,
            "required": ["id", "generation", "engine_instance_id"],
            "properties": {
                "id": {"type": "integer", "minimum": minimum,
                       "maximum": 0x7FFFFFFFFFFFFFFF},
                "generation": {"type": "integer", "minimum": 1,
                               "maximum": 0xFFFFFFFFFFFFFFFF},
                "engine_instance_id": {"type": "string", "minLength": 1,
                                       "maxLength": 256}}}


def spec(name, parameters, fields):
    return {"name": name, "modes": ["text"], "parameters": parameters,
            "result_fields": fields}


class NewsServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "state.db"))
        self.engine = FakeEngine()
        self.service = Service(self.store, self.engine, autostart=False)
        self.token = self.store.provision("w1", "reader", 2, mode="text")
        news_params = {"type": "object", "additionalProperties": False,
                       "required": [], "properties": {
                           "after": cursor_schema(0),
                           "limit": {"type": "integer", "minimum": 1,
                                     "maximum": 32}}}
        common = {"type": "object", "additionalProperties": False,
                  "properties": {"kind": {"type": "string",
                                          "enum": ["news_custom_text"]},
                                 "target": cursor_schema(1),
                                 "limit": {"type": "integer", "minimum": 1,
                                           "maximum": 2048},
                                 "expected_content_version": {"type": "string",
                                     "minLength": 64, "maxLength": 64,
                                     "pattern": "^[0-9a-f]{64}$"}},
                  "required": ["kind", "target"]}
        first = copy.deepcopy(common)
        first["properties"]["after_byte"] = {"type": "integer", "minimum": 0,
                                              "maximum": 0}
        later = copy.deepcopy(common)
        later["required"] = ["kind", "target", "after_byte",
                             "expected_content_version"]
        later["properties"]["after_byte"] = {"type": "integer", "minimum": 1,
                                              "maximum": 0xFFFFFFFFFFFFFFFF}
        self.specs = {
            "news.custom": spec("news.custom", news_params, ["tick", "company_id",
                "history_scope", "history_start", "latest", "gap", "items",
                "next", "complete"]),
            "display.text": spec("display.text", {"anyOf": [first, later]},
                ["tick", "company_id", "kind", "target", "text",
                 "content_version", "total_bytes", "next_after_byte"]),
        }
        self.service._queries = {name: {} for name in self.specs}

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def cursor(number, *, generation=3, boot="boot"):
        return {"id": number, "generation": generation,
                "engine_instance_id": boot}

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_post_selection_is_company_bound_and_visual_gets_no_world_rpc(self):
        hello = self.engine.valid_hello_reply("w1")["result"]
        original_call = self.engine.call
        display = {"text": "Café", "version": "a" * 64}

        def call(envelope):
            if envelope["type"] != "observation.query":
                return original_call(envelope)
            self.engine.calls.append(copy.deepcopy(envelope))
            params = envelope["parameters"]
            selected = params.get("after") or params.get("target")
            if selected and (selected["generation"] != 3 or
                             selected["engine_instance_id"] != "boot"):
                return {"status": "rejected"}
            if envelope["query"] == "news.custom":
                return {"status": "completed", "result": {
                    "tick": 12, "company_id": 2,
                    "history_scope": "current_engine_instance",
                    "history_start": self.cursor(0), "latest": self.cursor(1),
                    "gap": False, "items": [{"news_id": self.cursor(1),
                        "issued_tick": 11, "calendar_date": {"year": 1950,
                            "month": 1, "day": 1}, "news_type": "general",
                        "reference": None, "encoded": "private"}],
                    "next": self.cursor(1), "complete": True}}
            if ("expected_content_version" in params and
                    params["expected_content_version"] != display["version"]):
                return {"status": "rejected"}
            return {"status": "completed", "result": {
                "tick": 12, "company_id": 2, "kind": "news_custom_text",
                "target": self.cursor(1), "text": display["text"],
                "content_version": display["version"],
                "total_bytes": len(display["text"].encode("utf-8")),
                "next_after_byte": None,
                "raw_encoded": "private"}}

        with patch("backend.service.query_spec", side_effect=self.specs.get), \
                patch.object(self.service, "_hello", return_value=hello), \
                patch.object(self.engine, "call", side_effect=call):
            for name, params in (
                ("news.custom", {}),
                ("news.custom", {"after": self.cursor(0), "limit": 32}),
                ("display.text", {"kind": "news_custom_text",
                                  "target": self.cursor(1)}),
                ("display.text", {"kind": "news_custom_text",
                                  "target": self.cursor(1), "after_byte": 1,
                                  "expected_content_version": "a" * 64,
                                  "limit": 2048}),
            ):
                with self.subTest(name=name, params=params):
                    code, result = self.service.handle("POST", "/v1/observations",
                        self.token, {"query": name, "parameters": params})
                    self.assertEqual(code, 200)
                    self.assertNotIn("encoded", result["observation"])
                    sent = [item for item in self.engine.calls if
                            item["type"] == "observation.query"][-1]
                    self.assertEqual(sent["query"], name)
                    self.assertEqual(sent["parameters"], params)
                    self.assertEqual(sent["actor"]["company_id"], 2)
                    self.assertEqual(sent["actor"]["mode"], "text")
            display.update(text="Café renommé", version="b" * 64)
            code, changed = self.service.handle("POST", "/v1/observations",
                self.token, {"query": "display.text", "parameters": {
                    "kind": "news_custom_text", "target": self.cursor(1)}})
            self.assertEqual(code, 200)
            self.assertEqual(changed["observation"]["text"], "Café renommé")
            self.assertEqual(changed["observation"]["content_version"], "b" * 64)
            self.assertEqual(len(self.engine.calls), 6)  # one bind, five queries
            before = self.engine.count("observation.query")
            invalid = (
                ("news.custom", {"after": {**self.cursor(0), "private": 1}}),
                ("news.custom", {"after": {**self.cursor(0), "id": True}}),
                ("news.custom", {"after": {**self.cursor(0), "id": -1}}),
                ("news.custom", {"after": {**self.cursor(0), "id": 1 << 63}}),
                ("news.custom", {"after": {"id": 0, "generation": 3}}),
                ("news.custom", {"limit": 0}),
                ("news.custom", {"limit": 33}),
                ("news.custom", {"company_id": 2}),
                ("display.text", {"kind": "news_custom_text",
                                  "target": self.cursor(0)}),
                ("display.text", {"kind": "news_custom_text",
                                  "target": self.cursor(1), "after_byte": 1}),
                ("display.text", {"kind": "news_custom_text",
                                  "target": self.cursor(1), "private": 1}),
            )
            for name, params in invalid:
                with self.subTest(name=name, params=params):
                    self.assert_code("invalid_request", lambda: self.service.handle(
                        "POST", "/v1/observations", self.token,
                        {"query": name, "parameters": params}))
            self.assertEqual(self.engine.count("observation.query"), before)
            for params in ({"after": self.cursor(1, generation=4)},
                           {"after": self.cursor(1, boot="old")}):
                self.assert_code("observation_unavailable", lambda: self.service.handle(
                    "POST", "/v1/observations", self.token,
                    {"query": "news.custom", "parameters": params}))
            self.assert_code("observation_unavailable", lambda: self.service.handle(
                "POST", "/v1/observations", self.token,
                {"query": "display.text", "parameters": {
                    "kind": "news_custom_text", "target": self.cursor(1),
                    "after_byte": 1, "expected_content_version": "a" * 64}}))
            visual = self.store.provision("w1", "visual_reader", 2, mode="visual")
            before_calls = len(self.engine.calls)
            for name, params in (("news.custom", {}),
                                 ("display.text", {"kind": "news_custom_text",
                                                   "target": self.cursor(1)})):
                self.assert_code("forbidden", lambda: self.service.handle(
                    "POST", "/v1/observations", visual,
                    {"query": name, "parameters": params}))
            self.assertEqual(len(self.engine.calls), before_calls)
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(self.store.max_request_sequence("w1"), 0)


if __name__ == "__main__":
    unittest.main()
