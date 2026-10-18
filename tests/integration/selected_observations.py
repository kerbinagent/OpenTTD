#!/usr/bin/env python3
"""Public-HTTP acceptance for selected native observations on a disposable world.

The operator supplies an already running isolated world and one visual actor's
credential. This program never reads operator state, another credential, native
IPC, or a database. It renames its own company and opens harmless native report
windows. Images establish rendering/transport contracts, not business success.

    python tests/integration/selected_observations.py --origin http://127.0.0.1:18155 \
      --token-file /private/visual.token --artifacts /private/selected-capture

The default map centre is presentation input for a 128-square test world. Supply
--center-x and --center-y when using another disposable map.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import traceback
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.client import BackendClient, BackendError, credential_from_env_or_file
from backend.observations import ObservationImage


TERMINAL = {"completed", "rejected", "cancelled", "failed", "uncertain"}
OBSERVATION_HEADERS = {
    "x-openttd-observation-id", "x-openttd-image-id",
    "x-openttd-observation-generation", "x-openttd-capture-kind",
    "x-openttd-image-width", "x-openttd-image-height", "x-openttd-causal-state",
    "x-openttd-image-reused", "x-openttd-image-age-ms", "x-openttd-image-purpose",
}
PRIVATE_FIELDS = {
    "source_frame", "minimum_source_frame", "executed_frame", "executed_tick",
    "executed_boot_id", "authority_instance_id", "worker_instance_id",
    "renderer_lifecycle_revision", "screenshot_id", "park_ttl_ms", "path",
    "resolved_tile", "world_coordinates", "credentials", "access_proof",
    "proof_digest", "native_request_id",
}


def private_fields(value):
    if isinstance(value, dict):
        return (set(value) & PRIVATE_FIELDS) | set().union(
            *(private_fields(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(private_fields(child) for child in value))
    return set()


def inspect_png(data):
    """Check metadata-free RGB PNG chunks, CRCs and bounded decompression."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "PNG signature"
    offset, chunks, compressed = 8, [], bytearray()
    width = height = None
    while offset < len(data):
        assert offset + 12 <= len(data), "Truncated PNG chunk"
        size, name = struct.unpack(">I4s", data[offset:offset + 8])
        end = offset + 12 + size
        assert end <= len(data), "Truncated PNG payload"
        payload = data[offset + 8:offset + 8 + size]
        checksum = struct.unpack(">I", data[end - 4:end])[0]
        assert zlib.crc32(name + payload) & 0xFFFFFFFF == checksum, "PNG CRC"
        assert name in (b"IHDR", b"IDAT", b"IEND"), "Unexpected PNG metadata chunk"
        chunks.append(name.decode("ascii"))
        if name == b"IHDR":
            assert len(chunks) == 1 and size == 13, "PNG IHDR ordering"
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload)
            assert depth == 8 and colour == 2, "PNG must be RGB8"
            assert (compression, filtering, interlace) == (0, 0, 0), "PNG encoding"
            assert 0 < width <= 1280 and 0 < height <= 1024
            assert width * height <= 1_048_576
        elif name == b"IDAT":
            assert width is not None and "IEND" not in chunks, "PNG IDAT ordering"
            compressed.extend(payload)
        else:
            assert size == 0 and end == len(data), "PNG end/trailing data"
        offset = end
    assert chunks[0] == "IHDR" and chunks[-1] == "IEND" and "IDAT" in chunks
    expected = height * (width * 3 + 1)
    decoder = zlib.decompressobj()
    scanlines = decoder.decompress(compressed, expected + 1)
    assert len(scanlines) == expected and decoder.eof and not decoder.unused_data
    assert not decoder.unconsumed_tail
    stride = width * 3 + 1
    assert all(scanlines[row * stride] <= 4 for row in range(height)), "PNG row filter"
    return {"width": width, "height": height, "pixels": width * height,
            "png_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "chunks": chunks}


class HeaderClient(BackendClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.responses = []
        self.deadline = None

    def _exchange(self, request, read):
        def capture(response):
            selected = [(key.lower(), value) for key, value in response.headers.items()
                        if key.lower().startswith("x-openttd-")]
            self.responses.append({"method": request.get_method(),
                                   "path": request.selector.split("?", 1)[0],
                                   "status": response.status,
                                   "content_type": response.headers.get("Content-Type", ""),
                                   "openttd_headers": selected})
            return read(response)
        previous_timeout = self.timeout
        try:
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Public acceptance deadline expired")
                self.timeout = min(previous_timeout, remaining)
            return super()._exchange(request, capture)
        finally:
            self.timeout = previous_timeout


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.artifacts = args.artifacts.resolve()
        self.artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(self.artifacts, 0o700)
        self.requests = self.artifacts / "requests"
        self.requests.mkdir(mode=0o700)
        self.images = self.artifacts / "images"
        self.images.mkdir(mode=0o700)
        self.client = HeaderClient(args.origin, credential_from_env_or_file(str(args.token_file)), timeout=10)
        self.result = {"status": "incomplete", "origin": args.origin,
                       "scope": "Public rendering, interaction separation and causal-read acceptance; not economic or autonomous-player acceptance",
                       "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "http_attempts": [], "observations": [], "assertions": [],
                       "expected_errors": [], "actions": [], "stage": "startup"}
        self.client.on_http_attempt = self.result["http_attempts"].append
        self.sequence = 0

    def persist(self, path, value):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def saved_body(self, label, body):
        self.sequence += 1
        path = self.requests / f"{self.sequence:03d}-{label}.json"
        self.persist(path, body)
        return str(path.relative_to(self.artifacts))

    def check_public(self, value):
        found = private_fields(value)
        assert not found, f"Private fields in public response: {sorted(found)}"

    def request(self, method, path, body=None):
        if body is not None:
            self.saved_body("request", {"method": method, "path": path, "body": body})
        reply = self.client.request(method, path, body)
        self.check_public(reply)
        return reply

    def expected_error(self, label, method, path, body=None, *, status, code):
        self.result["stage"] = label
        try:
            self.request(method, path, body)
        except BackendError as error:
            assert (error.status, error.code) == (status, code), (
                f"{label}: expected {status}/{code}, got {error.status}/{error.code}")
            self.result["expected_errors"].append({"label": label, "status": error.status,
                                                     "code": error.code})
        else:
            raise AssertionError(f"{label}: request unexpectedly succeeded")

    def new_read(self, kind, parameters):
        return self.client.new_observation_request(kind, parameters)

    def observe(self, label, kind, body, *, pending_only=None, timeout=None, poll_floor=0.05):
        self.result["stage"] = label
        saved = self.saved_body(label, body)
        started = time.monotonic()
        attempts_before = len(self.result["http_attempts"])
        deadline = started + (self.args.wait_seconds if timeout is None else timeout)
        pending = []
        read = self.client.observe_world if kind == "world" else self.client.observe_native_info
        while True:
            value = read(body)
            self.check_public(value if isinstance(value, dict) else asdict(value) | {"data": None})
            if isinstance(value, ObservationImage):
                assert pending_only is None, f"{label}: expected {pending_only} pending"
                metadata = inspect_png(value.data)
                assert (metadata["width"], metadata["height"]) == (value.width, value.height)
                headers = self.client.responses[-1]["openttd_headers"]
                assert len(headers) == len(OBSERVATION_HEADERS), "Missing/repeated/extra PNG workflow headers"
                assert {key for key, _ in headers} == OBSERVATION_HEADERS, "Private or unknown PNG workflow header"
                path = self.images / (label + ".png")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(value.data)
                record = {key: child for key, child in asdict(value).items() if key != "data"}
                record.update(metadata, label=label, kind=kind,
                              image_path=str(path.relative_to(self.artifacts)), request_path=saved,
                              pending=pending, wall_ms=round((time.monotonic() - started) * 1000, 3),
                              http_attempts=len(self.result["http_attempts"]) - attempts_before,
                              workflow_headers=dict(headers))
                self.result["observations"].append(record)
                return value
            pending.append(value)
            if pending_only is not None:
                assert value["reason"] == pending_only, value
                self.result["assertions"].append({"label": label, "pending": value,
                                                    "request_path": saved})
                return value
            assert time.monotonic() < deadline, f"{label}: read did not complete; saved body {saved}; {value}"
            time.sleep(min(max(poll_floor, min(0.5, value["retry_after_ms"] / 1000)),
                           max(0, deadline - time.monotonic())))

    def action(self, name, parameters):
        body = {"request_id": uuid.uuid4().hex,
                "action": {"name": name, "parameters": parameters}}
        saved = self.saved_body("action", body)
        started = time.monotonic()
        reply = self.request("POST", "/v1/actions", body)
        while reply["status"] not in TERMINAL:
            assert time.monotonic() - started < self.args.wait_seconds, f"Action pending; retain {saved}"
            time.sleep(0.1)
            reply = self.request("GET", "/v1/actions/" + body["request_id"])
        self.result["actions"].append({"request_path": saved, "receipt": reply,
                                        "wall_ms": round((time.monotonic() - started) * 1000, 3)})
        assert reply["status"] == "completed" and reply.get("result", {}).get("success"), reply
        return body, reply

    def map_parameters(self, *, zoom="normal", labels=None):
        return {"selection": {"kind": "map_center",
                              "tile": {"x": self.args.center_x, "y": self.args.center_y},
                              "zoom_level": zoom},
                "output": {"width": 640, "height": 480},
                "labels": labels or {"town_names": True, "station_names": True,
                                     "waypoint_names": False, "signs": False,
                                     "competitor_labels": False}}

    def legacy_capture(self, label):
        self.result["stage"] = label
        deadline = time.monotonic() + self.args.wait_seconds
        while True:
            try:
                image = self.request("POST", "/v1/view/images", {})
                break
            except BackendError as error:
                if error.code != "rate_limited" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        data = self.client.image(image["image_id"])
        path = self.images / (label + ".png")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        self.result.setdefault("legacy_images", []).append({**image,
            "image_path": str(path.relative_to(self.artifacts)), "png_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()})
        return image

    def bootstrap(self):
        me = self.request("GET", "/v1/me")
        assert me["mode"] == "visual", "Supply the isolated visual actor credential"
        capabilities = self.request("GET", "/v1/capabilities")
        assert capabilities["engine_connected"] and "selected_observations" in capabilities
        self.result["actor"] = {key: me[key] for key in ("world_id", "identity_id", "company_id", "run_id", "mode") if key in me}
        self.result["selected_capability"] = capabilities["selected_observations"]

    def run(self):
        self.bootstrap()

        base_body = self.new_read("world", self.map_parameters())
        world = self.observe("map-normal", "world", base_body)
        duplicate = self.observe("map-exact-retry", "world", base_body)
        assert duplicate.reused and duplicate.data == world.data
        assert duplicate.image_id == world.image_id and duplicate.observation_id == world.observation_id
        conflict = {**base_body, "output": {"width": 641, "height": 480}}
        self.expected_error("conflicting-read-identity", "POST", "/v1/view/observe/world", conflict,
                            status=409, code="observation_id_conflict")

        zoomed = self.observe("map-out2x", "world", self.new_read("world", self.map_parameters(zoom="out2x")))
        assert zoomed.data != world.data, "Different named zoom produced identical PNG"
        no_labels = {key: False for key in self.map_parameters()["labels"]}
        self.observe("map-labels-hidden", "world", self.new_read("world", self.map_parameters(labels=no_labels)))
        point_body = self.new_read("world", {
            "selection": {"kind": "map_image_point", "observation_id": world.observation_id,
                          "pixel": {"x": 380, "y": 220}, "zoom_level": "in2x"},
            "output": {"width": 640, "height": 480}})
        self.observe("map-owned-point", "world", point_body)
        bogus_point = self.new_read("world", {
            "selection": {"kind": "map_image_point", "observation_id": uuid.uuid4().hex,
                          "pixel": {"x": 0, "y": 0}, "zoom_level": "normal"},
            "output": {"width": 640, "height": 480}})
        self.expected_error("unknown-map-image-proof", "POST", "/v1/view/observe/world", bogus_point,
                            status=410, code="observation_expired")

        finance = {}
        for layout in ("compact", "expanded"):
            finance[layout] = self.observe("finance-" + layout, "native_info", self.new_read("native_info", {
                "panel": "company_finances", "presentation": {"layout": layout}}))
        assert finance["compact"].data != finance["expanded"].data
        assert (finance["compact"].width, finance["compact"].height) != (
            finance["expanded"].width, finance["expanded"].height), "Native finance layout dimensions"
        directory = []
        for label, sort, first_row in (("towns-name", {"by": "name", "direction": "ascending"}, 0),
                                      ("towns-population", {"by": "population", "direction": "descending"}, 0),
                                      ("towns-scroll", {"by": "name", "direction": "ascending"}, 2)):
            directory.append(self.observe(label, "native_info", self.new_read("native_info", {
                "panel": "town_directory", "presentation": {
                    "sort": sort, "scroll": {"first_row": first_row}}})))
        assert directory[0].data != directory[1].data, "Directory sort presentation unchanged"
        scroll_changed = directory[0].data != directory[2].data
        self.result["assertions"].append({
            "label": "town-directory-scroll-request", "first_row": 2,
            "valid_native_png": True, "scroll_png_changed": scroll_changed,
            "semantic_scroll_covered": False,
            "visual_row_verification_required": True,
            "limitation": (
                "PNG bytes alone do not prove row movement: live values can repaint. "
                "Compare towns-name.png and towns-scroll.png to confirm changed visible "
                "rows. When every town fits, the native scrollbar clamps first_row to zero.")})
        if self.args.require_scroll_change:
            assert scroll_changed, "Scroll fixture produced identical PNGs; requires more towns than visible rows"

        action, receipt = self.action("company.rename", {"name": "Selected Capture " + uuid.uuid4().hex[:6]})
        causal = self.observe("finance-after-action", "native_info", self.new_read("native_info", {
            "panel": "company_finances", "presentation": {"layout": "expanded"},
            "after_request_id": action["request_id"]}))
        assert causal.causal_state == "satisfied"
        self.result["assertions"].append({"label": "action-causal-read", "request_id": action["request_id"],
                                            "action_status": receipt["status"], "causal_state": causal.causal_state,
                                            "public_causal_acknowledgement_verified": True,
                                            "semantic_action_visibility_verified": False,
                                            "deliberately_lagging_replica_exercised": False,
                                            "limitation": "Inspect finance-after-action.png for the renamed company; HTTP metadata alone does not prove semantic pixel visibility."})

        # Clear report windows through the ordinary image-bound presentation
        # route, then take a new lease whose unobstructed centre can be inspected.
        setup = self.legacy_capture("legacy-before-close")
        self.request("POST", "/v1/view/controls", {"image_id": setup["image_id"], "action": "close_windows"})
        legacy = self.legacy_capture("legacy-active-lease")
        # Use a fresh observation for purpose rejection. An old evicted image
        # could return the same 404 for an unrelated expiry reason.
        during = self.observe("map-during-active-lease", "world", self.new_read("world", self.map_parameters()))
        assert during.image_id != legacy["image_id"]
        for proof_kind, identity in (("observation-id", during.observation_id), ("image-id", during.image_id)):
            for control in ({"action": "pan", "dx": 0, "dy": 0},
                            {"action": "inspect", "x": 0, "y": 0}):
                self.expected_error(proof_kind + "-cannot-" + control["action"], "POST", "/v1/view/controls",
                                    {"image_id": identity, **control}, status=404, code="image_unavailable")
            self.expected_error(proof_kind + "-cannot-ui-apply", "POST", "/v1/actions", {
                "request_id": uuid.uuid4().hex,
                "action": {"name": "ui.apply", "parameters": {
                    "image_id": identity, "gesture": "move", "x": 0, "y": 0}}},
                status=404, code="image_unavailable")
        busy_body = self.new_read("native_info", {"panel": "company_finances", "wait_ms": 0})
        self.observe("panel-during-active-lease", "native_info", busy_body, pending_only="renderer_busy")
        self.request("POST", "/v1/view/controls", {"image_id": legacy["image_id"], "action": "inspect",
                     "x": legacy["width"] // 2, "y": legacy["height"] // 2})
        self.result["assertions"].append({"label": "world-observation-preserves-native-interaction-proof",
                                            "legacy_image_id": legacy["image_id"],
                                            "observation_image_id": during.image_id,
                                            "native_inspect_accepted": True})
        self.observe("panel-after-lease-released", "native_info", busy_body)

        selected_attempts = [attempt for attempt in self.result["http_attempts"]
                             if attempt["path"].startswith("/v1/view/observe/")]
        assert all(attempt["method"] == "POST" for attempt in selected_attempts)
        self.result["measurements"] = {
            "selected_http_attempts": len(selected_attempts),
            "selected_png_responses": sum(attempt["status"] == 200 for attempt in selected_attempts),
            "selected_pending_responses": sum(attempt["status"] == 202 for attempt in selected_attempts),
            "saved_selected_pngs": len(self.result["observations"]),
            "selected_png_bytes_received": sum(value["png_bytes"] for value in self.result["observations"]),
            "selected_pixels_received": sum(value["pixels"] for value in self.result["observations"]),
            "selected_new_image_ids": len({value["image_id"] for value in self.result["observations"]}),
            "legacy_png_gets": sum(attempt["method"] == "GET" and attempt["path"].startswith("/v1/images/")
                                   for attempt in self.result["http_attempts"]),
            "latency_scope": "HTTP and finite public-observation waits in this technical harness; no model thinking or native capture counter"}
        self.result["status"] = "passed"
        self.result["stage"] = "complete"

    def run_forced_lag(self):
        """Exercise the frame fence while the renderer's real lease holds it back.

        The gate never sends input to the parked renderer. In particular,
        native inspect would end the lease, so PNG retrieval below only checks
        image immutability and is not described as an interaction-proof check.
        """
        started = time.monotonic()
        overall_deadline = started + 45
        self.client.deadline = overall_deadline
        self.result["scope"] = (
            "Public forced-replica-lag causal-read acceptance; no private frame values, "
            "no lease cancellation, no action replay, no autonomous-player acceptance")
        self.bootstrap()
        legacy = self.legacy_capture("forced-lag-legacy-park")
        before_body = self.new_read("world", self.map_parameters())
        before = self.observe("forced-lag-before-action", "world", before_body)
        action, receipt = self.action("company.rename", {"name": "Forced Lag " + uuid.uuid4().hex[:6]})
        cached = self.observe("forced-lag-preaction-exact-retry", "world", before_body)
        assert cached.reused and cached.image_id == before.image_id and cached.data == before.data

        fenced_body = self.new_read("world", {**self.map_parameters(),
            "after_request_id": action["request_id"], "wait_ms": 0})
        self.observe("forced-lag-causal-pending", "world", fenced_body, pending_only="replica_pending")
        # This is a cache/download check, not proof that input would be accepted.
        old_png = self.client.image(legacy["image_id"])
        saved_png = (self.images / "forced-lag-legacy-park.png").read_bytes()
        assert old_png == saved_png, "The original legacy image bytes changed"
        remaining = overall_deadline - time.monotonic()
        assert remaining > 0, "Forced-lag setup exceeded its overall deadline"
        satisfied = self.observe("forced-lag-causal-satisfied", "world", fenced_body,
                                 timeout=remaining, poll_floor=0.5)
        assert satisfied.causal_state == "satisfied" and not satisfied.reused
        assert satisfied.image_id != before.image_id, "Causal read reused the pre-action image identity"
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        assert elapsed_ms <= 45_000, "Forced-lag causal read exceeded 45 seconds"
        pending_events = next(value["pending"] for value in self.result["observations"]
                              if value["label"] == "forced-lag-causal-satisfied")
        assert all(value["reason"] == "replica_pending" for value in pending_events), pending_events
        self.result["assertions"].append({
            "label": "forced-replica-lag-fence", "action_request_id": action["request_id"],
            "action_status": receipt["status"], "preaction_cached_read_unchanged": True,
            "first_causal_attempt": "replica_pending", "further_pending_attempts": len(pending_events),
            "final_causal_state": satisfied.causal_state, "exact_same_selected_body": True,
            "old_legacy_png_unchanged": True, "old_legacy_input_proof_exercised": False,
            "no_ui_input_or_control_sent": True, "elapsed_ms": elapsed_ms,
            "private_frames_exposed": False,
            "limitation": "The public frame fence is tested through pending/satisfied workflow states; numeric native frame values remain private."})
        # A separate real native panel lets the operator verify semantic action
        # visibility in pixels after the fence, without relying on map changes.
        remaining = overall_deadline - time.monotonic()
        assert remaining > 0, "Forced-lag gate exceeded its deadline before finance confirmation"
        self.observe("forced-lag-finance-after-action", "native_info", self.new_read("native_info", {
            "panel": "company_finances", "presentation": {"layout": "expanded"},
            "after_request_id": action["request_id"]}), timeout=remaining)
        total_elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        assert total_elapsed_ms <= 45_000, "Forced-lag gate including finance exceeded 45 seconds"
        attempts = self.result["http_attempts"]
        selected_attempts = [value for value in attempts
                             if value["path"].startswith("/v1/view/observe/")]
        self.result["measurements"] = {
            "gate_elapsed_ms": elapsed_ms,
            "total_elapsed_ms": total_elapsed_ms,
            "selected_http_attempts": len(selected_attempts),
            "selected_png_responses": sum(value["status"] == 200 for value in selected_attempts),
            "selected_pending_responses": sum(value["status"] == 202 for value in selected_attempts),
            "selected_distinct_image_ids": len({value["image_id"] for value in self.result["observations"]}),
            "selected_png_bytes_received": sum(value["png_bytes"] for value in self.result["observations"]),
            "selected_pixels_received": sum(value["pixels"] for value in self.result["observations"]),
            "model_image_deliveries": 0,
            "native_capture_count": "not measured; distinct public image IDs are reported separately"}
        self.result["status"] = "passed"
        self.result["stage"] = "complete"

    def finish(self):
        self.result["response_envelopes"] = self.client.responses
        self.persist(self.artifacts / "result.json", self.result)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path,
                        help="New private output directory; existing paths are rejected")
    parser.add_argument("--center-x", type=int, default=64)
    parser.add_argument("--center-y", type=int, default=64)
    parser.add_argument("--wait-seconds", type=float, default=25)
    parser.add_argument("--require-scroll-change", action="store_true",
                        help="Require differing directory PNGs after scrolling; operator must still verify actual row movement")
    parser.add_argument("--forced-lag", action="store_true",
                        help="Run only the <=45-second parked-renderer causal-fence gate; no UI input or lease cancellation")
    args = parser.parse_args()
    if not 0 <= args.center_x <= 65_535 or not 0 <= args.center_y <= 65_535:
        parser.error("Map presentation centre must use non-negative tile coordinates <=65535")
    if not 1 <= args.wait_seconds <= 60:
        parser.error("--wait-seconds must be between 1 and 60")
    acceptance = Acceptance(args)
    try:
        if args.forced_lag:
            acceptance.run_forced_lag()
        else:
            acceptance.run()
    except Exception as error:
        acceptance.result["status"] = "failed"
        acceptance.result["failure"] = {"type": type(error).__name__, "message": str(error),
                                          "traceback": traceback.format_exc()}
        acceptance.finish()
        print(json.dumps({"status": "failed", "stage": acceptance.result["stage"],
                          "error": str(error), "artifacts": str(acceptance.artifacts)}), flush=True)
        return 1
    acceptance.finish()
    print(json.dumps({"status": "passed", "artifacts": str(acceptance.artifacts),
                      "measurements": acceptance.result["measurements"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
