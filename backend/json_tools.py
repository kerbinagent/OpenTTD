"""Bounded JSON helpers shared by the public and private protocol boundaries."""

import json


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite JSON number")


def loads(data):
    """Reject ambiguous duplicate keys and non-standard numeric constants."""
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)


def dumps(value):
    """Use deterministic UTF-8 JSON for wire messages and request digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
