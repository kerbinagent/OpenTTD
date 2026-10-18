"""Closed public requests for selected native observations.

This module describes and normalizes requests only. Renderer ownership, image
references, request retention and native capture belong to the gateway.
"""

import re
from dataclasses import dataclass

from .errors import ServiceError


GENERATION_ID = re.compile(r"^[0-9a-f]{32}$")
REQUEST_ID = re.compile(r"^obs1:([0-9]{13}):([0-9a-f]{32})$")
_AFTER_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
RETENTION_MS = 60_000
FUTURE_SKEW_MS = 5_000
MAX_REQUESTS = 64
_DEFAULT_WAIT_MS = 1_500
_MAX_WAIT_MS = 2_000
_MAX_PIXELS = 1_048_576
_MAX_NATIVE_VIRTUAL_PIXELS = 134_217_728
_ZOOM_LEVELS = ("in4x", "in2x", "normal", "out2x", "out4x", "out8x")
_LABEL_DEFAULTS = {
    "town_names": True,
    "station_names": True,
    "waypoint_names": False,
    "signs": False,
    "competitor_labels": False,
}
_IDENTITY_FIELDS = frozenset({
    "observation_generation", "observation_request_id", "after_request_id", "wait_ms",
})


@dataclass(frozen=True)
class ObservationImage:
    """A gateway-owned immutable image and its public response metadata."""

    data: bytes
    observation_id: str
    image_id: str
    generation: str
    kind: str
    width: int
    height: int
    causal_state: str
    reused: bool
    age_ms: int


def _invalid():
    raise ServiceError("invalid_request")


def _object(value, allowed, required=()):
    if (not isinstance(value, dict) or set(value) - set(allowed) or
            any(key not in value for key in required)):
        _invalid()
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid()
    return value


def _enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        _invalid()
    return value


def _matching(value, pattern):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _invalid()
    return value


def _point(value, max_x, max_y):
    value = _object(value, {"x", "y"}, {"x", "y"})
    return {"x": _integer(value["x"], 0, max_x),
            "y": _integer(value["y"], 0, max_y)}


def _world(body):
    _object(body, _IDENTITY_FIELDS | {"selection", "output", "labels"},
            {"selection", "output"})
    selection = _object(body["selection"],
                        {"kind", "tile", "observation_id", "pixel", "zoom_level"},
                        {"kind", "zoom_level"})
    kind = _enum(selection["kind"], {"map_center", "map_image_point"})
    zoom = _enum(selection["zoom_level"], _ZOOM_LEVELS)
    if kind == "map_center":
        _object(selection, {"kind", "tile", "zoom_level"}, {"tile"})
        normalized_selection = {"kind": kind, "tile": _point(selection["tile"], 65_535, 65_535),
                                "zoom_level": zoom}
    else:
        _object(selection, {"kind", "observation_id", "pixel", "zoom_level"},
                {"observation_id", "pixel"})
        normalized_selection = {
            "kind": kind,
            "observation_id": _matching(selection["observation_id"], GENERATION_ID),
            "pixel": _point(selection["pixel"], 1_279, 1_023),
            "zoom_level": zoom,
        }
    output = _object(body["output"], {"width", "height"}, {"width", "height"})
    width = _integer(output["width"], 64, 1_280)
    height = _integer(output["height"], 64, 1_024)
    pixels = width * height
    if (pixels > _MAX_PIXELS or
            pixels * 4 ** _ZOOM_LEVELS.index(zoom) > _MAX_NATIVE_VIRTUAL_PIXELS):
        _invalid()
    labels = _object(body.get("labels", {}), _LABEL_DEFAULTS)
    normalized_labels = dict(_LABEL_DEFAULTS)
    for name, value in labels.items():
        if type(value) is not bool:
            _invalid()
        normalized_labels[name] = value
    return {"selection": normalized_selection, "output": {"width": width, "height": height},
            "labels": normalized_labels}


def _native_info(body):
    _object(body, _IDENTITY_FIELDS | {"panel", "target", "presentation", "region"}, {"panel"})
    panel = _enum(body["panel"], {"company_finances", "town_directory"})
    region = _enum(body.get("region", "window"), {"window"})
    if panel == "company_finances":
        target = _object(body.get("target", {"kind": "self"}), {"kind"}, {"kind"})
        _enum(target["kind"], {"self"})
        presentation = _object(body.get("presentation", {}), {"layout"})
        return {"panel": panel, "target": {"kind": "self"}, "presentation": {
            "layout": _enum(presentation.get("layout", "expanded"), {"compact", "expanded"}),
        }, "region": region}
    if "target" in body:
        _invalid()
    presentation = _object(body.get("presentation", {}), {"sort", "scroll"})
    sort = _object(presentation.get("sort", {}), {"by", "direction"})
    scroll = _object(presentation.get("scroll", {}), {"first_row"})
    return {"panel": panel, "presentation": {
        "sort": {"by": _enum(sort.get("by", "name"), {"name", "population", "rating"}),
                 "direction": _enum(sort.get("direction", "ascending"),
                                    {"ascending", "descending"})},
        "scroll": {"first_row": _integer(scroll.get("first_row", 0), 0, 65_535)},
    }, "region": region}


def validate_request(kind, body):
    """Validate one closed request and return a fresh body with explicit defaults."""
    if kind not in ("world", "native_info"):
        _invalid()
    body = _object(body, _IDENTITY_FIELDS | (
        {"selection", "output", "labels"} if kind == "world" else
        {"panel", "target", "presentation", "region"}),
        {"observation_generation", "observation_request_id"})
    normalized = {
        "observation_generation": _matching(body["observation_generation"], GENERATION_ID),
        "observation_request_id": _matching(body["observation_request_id"], REQUEST_ID),
        "wait_ms": _integer(body.get("wait_ms", _DEFAULT_WAIT_MS), 0, _MAX_WAIT_MS),
    }
    if "after_request_id" in body:
        normalized["after_request_id"] = _matching(body["after_request_id"], _AFTER_REQUEST_ID)
    normalized.update(_world(body) if kind == "world" else _native_info(body))
    return normalized


def capability(generation):
    """Describe the finite selected-observation interface without native RPCs."""
    return {
        "version": 1,
        "image_purpose": "observation",
        "endpoint_world": "/v1/view/observe/world",
        "endpoint_native_info": "/v1/view/observe/native-info",
        "observation_generation": generation,
        "retry_retention_ms": RETENTION_MS,
        "future_clock_skew_ms": FUTURE_SKEW_MS,
        "request_identity": {"pattern": REQUEST_ID.pattern},
        "limits": {
            "max_requests": MAX_REQUESTS,
            "max_wait_ms": _MAX_WAIT_MS,
            "default_wait_ms": _DEFAULT_WAIT_MS,
            "min_width": 64,
            "max_width": 1_280,
            "min_height": 64,
            "max_height": 1_024,
            "max_pixels": _MAX_PIXELS,
            "max_native_virtual_pixels": _MAX_NATIVE_VIRTUAL_PIXELS,
        },
        "zoom_levels": list(_ZOOM_LEVELS),
        "label_defaults": dict(_LABEL_DEFAULTS),
        "panels": {
            "company_finances": {"target": {"kind": "self"},
                                 "presentation": {"layout": "expanded"}, "region": "window"},
            "town_directory": {
                "presentation": {"sort": {"by": "name", "direction": "ascending"},
                                 "scroll": {"first_row": 0}},
                "region": "window",
            },
        },
    }
