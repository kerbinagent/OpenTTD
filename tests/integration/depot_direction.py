#!/usr/bin/env python3
"""Opt-in public-HTTP depot-facing regression for an operator's disposable world.

This program constructs small road/rail fixtures using one text actor. It reads
only that actor's credential and public API. It does not launch an engine, read
operator state, access IPC/database files, or borrow another actor's identity.
The supplied world MUST be disposable: fixtures are intentionally left in place.

    python tests/integration/depot_direction.py --origin http://127.0.0.1:18155 \\
      --token-file /private/text.token --artifacts /private/depot-regression

Every gameplay intent is saved before submission. An interrupted or uncertain
run stops, leaving its original request body for exact recovery; it never mints
a replacement ID for an unresolved action. The artifact directory must be new.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration" / "luna_play"))
from client import PlayerClient
from backend.client import BackendError


# These public catalog conventions describe fixture geometry, not auto selection.
DIRECTION_OFFSETS = ((-1, 0), (0, 1), (1, 0), (0, -1))
RECIPROCAL_BITS = (2, 1, 8, 4)
CONSTRUCTION_FIELDS = (
    "height", "slope", "owner", "road_type", "tram_type", "road_bits", "tram_bits",
    "rail_type", "track_bits", "depot", "depot_direction", "disallowed_road_directions",
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def construction_facts(tiles):
    """Ignore natural clear/tree growth, retaining public construction facts."""
    return [{"x": tile["x"], "y": tile["y"],
             "type": "natural_land" if tile["type"] in (0, 4) else tile["type"],
             **{key: tile[key] for key in CONSTRUCTION_FIELDS if key in tile}}
            for tile in tiles]


class BoundedPlayer(PlayerClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = time.monotonic()
        self.requests_seen = 0

    def request(self, method, path, body=None):
        require(time.monotonic() - self.started < 180, "180-second fixture deadline exceeded")
        self.requests_seen += 1
        require(self.requests_seen <= 400, "400-request fixture budget exceeded")
        return super().request(method, path, body)


class Regression:
    def __init__(self, args):
        self.artifacts = args.artifacts.resolve()
        self.artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(self.artifacts, 0o700)
        self.player = BoundedPlayer(args.origin, str(args.token_file), self.artifacts)
        self.result = {"status": "incomplete", "origin": args.origin,
                       "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "scope": "Depot facing, native result feedback and exact replay; not connection or route success",
                       "limitations": [
                           "Natural clear/tree type transitions are normalized in construction comparisons; raw tile facts are retained.",
                           "An unpaused public fixture does not prove unchanged funds, RNG streams, internal command counts or auxiliary allocations.",
                           "Foreign-rail exclusion and queue-time dependency changes require separate native fixtures.",
                       ],
                       "stage": "startup", "actions": [], "quotes": [], "cases": [],
                       "limits": {"seconds": 180, "http_requests": 400, "actions": 24,
                                  "fixture_map_pages": 96}}
        self.action_count = 0
        self.last_quote = None

    def observe(self, query, parameters=None):
        return self.player.request("POST", "/v1/observations", {
            "query": query, "parameters": parameters or {}})["observation"]

    def region(self, center):
        page = self.observe("map.region", {"x": center["x"] - 1, "y": center["y"] - 1,
                                           "width": 3, "height": 3, "limit": 9})
        require(page["complete"] is True and len(page["tiles"]) == 9, "Incomplete local fixture read")
        return page["tiles"]

    def at(self, tiles, point):
        return next(tile for tile in tiles if tile["x"] == point["x"] and tile["y"] == point["y"])

    def action(self, label, name, parameters, *, expected_error=None, saved_body=None):
        self.result["stage"] = label
        self.action_count += 1
        require(self.action_count <= 24, "24-action fixture budget exceeded")
        body = saved_body or self.player.actions.new_intent(name, parameters)
        # PlayerClient also saves/verifies canonical bytes before POST. There is
        # deliberately no recovery path that changes this body's ID or targets.
        entry = {"label": label, "request": body, "terminal": None}
        self.result["actions"].append(entry)
        receipt = self.player.issue(body)
        entry["terminal"] = receipt
        outcome = receipt.get("result", {})
        if expected_error is None:
            require(receipt["status"] == "completed" and outcome.get("success") is True,
                    f"{label}: expected completed construction: {receipt}")
        else:
            require(receipt["status"] == "rejected" and outcome.get("success") is False,
                    f"{label}: expected rejected selection: {receipt}")
            require(outcome.get("cost") == 0 and outcome.get("error", {}).get("code") == expected_error,
                    f"{label}: wrong safe selection failure: {receipt}")
            require("direction" not in outcome and "depot_id" not in outcome,
                    f"{label}: failed construction claims an actual depot")
        return body, receipt

    def quote(self, label, name, parameters):
        self.result["stage"] = label
        if self.last_quote is not None:
            time.sleep(max(0, 1.05 - (time.monotonic() - self.last_quote)))
        deadline = time.monotonic() + 4
        body = {"action": {"name": name, "parameters": parameters}}
        while True:
            self.last_quote = time.monotonic()
            try:
                reply = self.player.request("POST", "/v1/actions/estimate", body)
                break
            except BackendError as error:
                if error.code != "estimate_rate_limited" or time.monotonic() >= deadline:
                    raise
                time.sleep(1.05)
        estimate = reply["estimate"]
        require(estimate.get("advisory") is True, f"{label}: quote must be advisory")
        require("direction" not in estimate and "depot_id" not in estimate,
                f"{label}: quote claims a completed depot")
        self.result["quotes"].append({"label": label, "request": body, "response": reply})
        return estimate

    def fixtures(self, count):
        dimensions = self.observe("map.region", {"x": 1, "y": 1, "width": 1, "height": 1})
        width, height = dimensions["map_width"], dimensions["map_height"]
        require(width >= 32 and height >= 32, "Fixture map must be at least 32 square")
        self.result["map"] = {"width": width, "height": height}
        selected, pages = [], 0
        # At most 96 public pages. Row-major 16-square blocks keep pagination
        # bounded and explicit; no hidden map dump or placement endpoint is used.
        for y in range(1, height - 1, 16):
            for x in range(1, width - 1, 16):
                block_width, block_height = min(16, width - 1 - x), min(16, height - 1 - y)
                known, after = {}, -1
                while True:
                    require(pages < 96, "No sufficient flat disposable fixtures within 96 public map pages")
                    page = self.observe("map.region", {"x": x, "y": y,
                        "width": block_width, "height": block_height,
                        "after_offset": after, "limit": 100})
                    pages += 1
                    known.update({(tile["x"], tile["y"]): tile for tile in page["tiles"]})
                    if page["complete"]:
                        break
                    following = page["next_after_offset"]
                    require(type(following) is int and following > after, "Map pagination did not advance")
                    after = following
                for cy in range(y + 1, y + block_height - 1):
                    for cx in range(x + 1, x + block_width - 1):
                        if any(abs(cx - old["x"]) <= 4 and abs(cy - old["y"]) <= 4 for old in selected):
                            continue
                        square = [known[(cx + dx, cy + dy)] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
                        if not all(tile["type"] in (0, 4) and tile.get("slope") == 0 for tile in square):
                            continue
                        if len({tile["height"] for tile in square}) != 1:
                            continue
                        selected.append({"x": cx, "y": cy})
                        if len(selected) == count:
                            self.result["fixture_map_pages"] = pages
                            self.result["fixture_centers"] = selected
                            return selected
        raise AssertionError("Not enough disjoint flat natural-land fixtures")

    def road(self, label, tile, road_type, direction):
        # Use a straight road along the perpendicular axis, intentionally
        # omitting the entrance's reciprocal bit to detect hidden connection.
        road_bits = 5 if direction in (0, 2) else 10
        require(not road_bits & RECIPROCAL_BITS[direction], "Fixture must omit reciprocal road bit")
        self.action(label, "road.pieces.build", {"tile": tile, "road_type": road_type, "road_bits": road_bits})
        return road_bits

    def run(self):
        me = self.player.request("GET", "/v1/me")
        require(me.get("mode") == "text" and type(me.get("company_id")) is int,
                "Use one active text company credential for this disposable fixture")
        self.result["actor"] = me
        self.result["world_before"] = self.player.request("GET", "/v1/world/status")
        road_types = self.observe("catalog.roadtypes")["items"]
        ordinary = [item for item in road_types if item.get("tram") is False]
        require(ordinary, "No available ordinary public road type")
        road_type = ordinary[0]["road_type"]
        rail_types = self.observe("catalog.railtypes")["items"]
        require(rail_types, "No available stock rail type for owned-rail fixture")
        rail_type = rail_types[0]["rail_type"]
        self.result["selected_types"] = {"road_type": road_type, "rail_type": rail_type}
        self.result["company_before"] = self.observe("company")
        require(self.result["company_before"]["balance"] >= 25000, "Disposable actor needs at least 25000 cash")
        centers = self.fixtures(8)

        original = None
        for direction, center in enumerate(centers[:4]):
            dx, dy = DIRECTION_OFFSETS[direction]
            neighbor = {"x": center["x"] + dx, "y": center["y"] + dy}
            expected_bits = self.road(f"road-{direction}", neighbor, road_type, direction)
            before = self.region(center)
            body, receipt = self.action(f"auto-road-{direction}", "road.depot.build", {
                "tile": center, "road_type": road_type, "direction": "auto"})
            after = self.region(center)
            depot = self.at(after, center)
            require(type(receipt["result"].get("direction")) is int
                    and receipt["result"]["direction"] == direction,
                    "Receipt did not report actual expected integer facing")
            require(depot.get("depot") is True and depot.get("depot_direction") == direction,
                    "Public built depot disagrees with actual receipt facing")
            require(self.at(after, neighbor).get("road_bits") == expected_bits,
                    "Auto facing added an undeclared reciprocal road connector")
            require(construction_facts([self.at(before, neighbor)]) == construction_facts([self.at(after, neighbor)]),
                    "Auto facing changed neighboring road construction facts")
            self.result["cases"].append({"case": f"road-direction-{direction}", "center": center,
                "neighbor": neighbor, "before": before, "after": after,
                "request_id": body["request_id"], "receipt": receipt, "neighbor_unchanged": True})
            if direction == 0:
                original = body, receipt, center

        for index, code in ((4, "depot_direction_unavailable"), (5, "depot_direction_ambiguous")):
            center = centers[index]
            if index == 5:
                for direction in (0, 2):
                    dx, dy = DIRECTION_OFFSETS[direction]
                    self.road(f"ambiguous-road-{direction}", {"x": center["x"] + dx, "y": center["y"] + dy},
                              road_type, direction)
            parameters = {"tile": center, "road_type": road_type, "direction": "auto"}
            before = self.region(center)
            quote = self.quote(code + "-quote", "road.depot.build", parameters)
            after_quote = self.region(center)
            require(quote.get("success") is False and quote.get("cost") == 0
                    and quote.get("error", {}).get("code") == code, f"Incorrect safe failed quote: {quote}")
            require(construction_facts(before) == construction_facts(after_quote), "Failed quote changed local construction")
            body, receipt = self.action(code, "road.depot.build", parameters, expected_error=code)
            after = self.region(center)
            require(construction_facts(before) == construction_facts(after), "Failed auto selection changed local construction")
            self.result["cases"].append({"case": code, "before": before, "after_quote": after_quote,
                "after": after, "request_id": body["request_id"], "receipt": receipt, "quote": quote,
                "local_construction_unchanged": True})

        center = centers[6]
        before = self.region(center)
        _, receipt = self.action("explicit-isolated-road", "road.depot.build", {
            "tile": center, "road_type": road_type, "direction": 1})
        after = self.region(center)
        require(type(receipt["result"].get("direction")) is int
                and receipt["result"]["direction"] == 1
                and self.at(after, center).get("depot_direction") == 1,
                "Explicit integer direction acquired an automatic-neighbor requirement")
        self.result["cases"].append({"case": "explicit-without-neighbor", "before": before,
                                     "after": after, "receipt": receipt})

        body, receipt, center = original
        second_neighbor = {"x": center["x"] + 1, "y": center["y"]}
        self.road("new-second-neighbor-before-replay", second_neighbor, road_type, 2)
        before = self.region(center)
        _, replay = self.action("exact-completed-replay", "road.depot.build", body["action"]["parameters"], saved_body=body)
        after = self.region(center)
        # Checkpoint state may legitimately advance after the original receipt.
        for key in ("request_id", "status", "result", "tick"):
            require(replay.get(key) == receipt.get(key), f"Exact replay changed original {key}")
        require(construction_facts(before) == construction_facts(after), "Exact completed replay mutated construction")
        self.result["cases"].append({"case": "exact-replay-after-new-neighbor", "request": body,
            "original_receipt": receipt, "replayed_receipt": replay,
            "before": before, "after": after, "local_construction_unchanged": True})

        center = centers[7]
        neighbor = {"x": center["x"] - 1, "y": center["y"]}
        self.action("owned-rail-neighbor", "rail.build", {"tile": neighbor, "rail_type": rail_type, "track": "y"})
        before = self.region(center)
        require(self.at(before, neighbor).get("owner") == me["company_id"], "Rail fixture is not actor owned")
        _, receipt = self.action("auto-owned-rail", "rail.depot.build", {
            "tile": center, "rail_type": rail_type, "direction": "auto"})
        after = self.region(center)
        require(type(receipt["result"].get("direction")) is int
                and receipt["result"]["direction"] == 0
                and self.at(after, center).get("depot_direction") == 0,
                "Owned-rail automatic facing disagrees with expected fixture")
        require(construction_facts([self.at(before, neighbor)]) == construction_facts([self.at(after, neighbor)]),
                "Auto rail facing changed neighboring track construction")
        self.result["cases"].append({"case": "owned-rail-facing-only", "before": before,
                                     "after": after, "receipt": receipt, "neighbor_unchanged": True})
        self.result["company_after"] = self.observe("company")
        self.result["world_after"] = self.player.request("GET", "/v1/world/status")
        for key in ("world_id", "world_epoch", "engine_instance_id"):
            require(self.result["world_before"][key] == self.result["world_after"][key],
                    f"Fixture world identity changed during test: {key}")
        self.result["status"] = "passed"
        self.result["stage"] = "complete"

    def finish(self):
        self.result["elapsed_seconds"] = round(time.monotonic() - self.player.started, 3)
        self.result["http_requests"] = self.player.requests_seen
        self.result["action_submissions"] = self.action_count
        path = self.artifacts / "depot-direction-result.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(self.result, stream, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
        return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    regression = Regression(args)
    try:
        regression.run()
    except Exception as error:
        regression.result["status"] = "failed"
        regression.result["failure"] = {"type": type(error).__name__, "message": str(error)}
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        print(str(regression.finish()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
