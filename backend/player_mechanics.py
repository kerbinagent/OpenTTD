"""Small explicit mechanical conversions for public text and capture players.

These helpers never choose targets, inspect a map, submit gameplay or refresh a
saved retry. Native command checks remain authoritative.
"""

from __future__ import annotations

from copy import deepcopy
import re
import time

from .json_tools import dumps


ORDER_ACTIONS = frozenset({
    "order.insert", "order.delete", "order.skip", "order.move", "order.copy",
    "order.unshare", "order.conditional.insert", "order.modify", "order.refit",
    "timetable.set", "timetable.start", "timetable.autofill", "timetable.reset_lateness",
})
TOKEN_FIELDS = {"vehicle_id", "order_version", "source_vehicle_id", "source_order_version"}
EDGE_BITS = {"NW": 1, "SW": 2, "SE": 4, "NE": 8}
RAIL_TRACKS = {
    frozenset(("NE", "SW")): "x", frozenset(("NW", "SE")): "y",
    frozenset(("NE", "NW")): "upper", frozenset(("SE", "SW")): "lower",
    frozenset(("NW", "SW")): "left", frozenset(("NE", "SE")): "right",
}


def rail_track(edge_a, edge_b):
    """Translate two physical tile boundary edges; no route or map lookup."""
    if not isinstance(edge_a, str) or not isinstance(edge_b, str) or edge_a == edge_b:
        raise ValueError("Choose two different physical edges: NE, SE, SW, NW")
    try:
        return RAIL_TRACKS[frozenset((edge_a, edge_b))]
    except KeyError:
        raise ValueError("Choose two different physical edges: NE, SE, SW, NW") from None


def road_bits(*edges):
    """Translate chosen physical tile boundary edges into the native road mask."""
    if not edges or any(not isinstance(edge, str) or edge not in EDGE_BITS for edge in edges):
        raise ValueError("Choose physical edges from NE, SE, SW, NW")
    if len(set(edges)) != len(edges):
        raise ValueError("Physical edges must be distinct")
    return sum(EDGE_BITS[edge] for edge in edges)



def _reference(value):
    if (not isinstance(value, dict) or set(value) != {"id", "generation", "engine_instance_id"}
            or type(value["id"]) is not int or not 0 <= value["id"] <= 1_044_479
            or type(value["generation"]) is not int or not 1 <= value["generation"] <= 2**64 - 1
            or not isinstance(value["engine_instance_id"], str) or not 1 <= len(value["engine_instance_id"]) <= 256):
        raise ValueError("Provide the complete selected vehicle reference")
    return deepcopy(value)


def prepare_order(client, actions, name, vehicle_ref, parameters, *, source_ref=None):
    """Save a NEW chosen order intent with machine-carried version tokens.

    This does not submit it. Call retry(saved['request_id']) to submit that
    exact saved body; subsequent retries do not resolve any reference.
    """
    if not isinstance(name, str) or name not in ORDER_ACTIONS or not isinstance(parameters, dict) or TOKEN_FIELDS & parameters.keys():
        raise ValueError("Choose a supported order action and omit mechanically filled vehicle/version fields")
    if (source_ref is not None) != (name == "order.copy"):
        raise ValueError("A full source reference is required only for order.copy")

    def resolve(selected):
        selected = _reference(selected)
        reply = client.request("POST", "/v1/control/reference", {"kind": "vehicle", "id": selected["id"]})
        if (not isinstance(reply, dict) or reply.get("kind") != "vehicle"
                or dumps(reply.get("reference")) != dumps(selected)
                or not isinstance(reply.get("order_version"), str)
                or not re.fullmatch(r"ov1:[0-9a-fA-F]{64}", reply["order_version"])):
            raise ValueError("Selected vehicle incarnation changed or its order version is unavailable")
        return selected, reply["order_version"]

    destination, version = resolve(vehicle_ref)
    prepared = {**deepcopy(parameters), "vehicle_id": destination, "order_version": version}
    if source_ref is not None:
        # Native selected-reference reads share a one-second admission gate.
        # Wait after the successful response, so request transit latency cannot
        # shorten the server's interval. Another player may still occupy the
        # gate: fail normally without saving or guessing newer tokens.
        time.sleep(1.05)
        source, source_version = resolve(source_ref)
        prepared.update(source_vehicle_id=source, source_order_version=source_version)
    return actions.new_intent(name, prepared)
