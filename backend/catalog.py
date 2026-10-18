"""Engine-owned public action and query catalog.

The checked-in JSON is generated from the C++ integration catalog. This module
validates request *shape* only; live ownership, targets, costs and game rules
remain the engine's responsibility.
"""

import hashlib
import json
import re
from pathlib import Path

from .errors import ServiceError


CATALOG = json.loads((Path(__file__).parent / "schemas" / "engine-catalog-v1.json").read_text(encoding="utf-8"))
_ACTIONS = {entry["name"]: entry for entry in CATALOG["actions"]}
_QUERIES = {entry["name"]: entry for entry in CATALOG["queries"]}


def _canonical_json_bytes(value):
    """The native catalog hash's restricted, compact UTF-8 JSON encoding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


CATALOG_HASH_ALGORITHM = "blake2b-256"
CATALOG_HASH = hashlib.blake2b(_canonical_json_bytes(CATALOG), digest_size=32).hexdigest()
CATALOG_ACTION_NAMES = [entry["name"] for entry in CATALOG["actions"]]
CATALOG_QUERY_NAMES = [entry["name"] for entry in CATALOG["queries"]]


def verify_engine_capabilities(capabilities):
    """Fence gameplay if compact hello differs from the native catalog artifact."""
    expected_keys = set(CATALOG) | {"catalog_hash_algorithm", "catalog_hash"}
    if not isinstance(capabilities, dict) or set(capabilities) != expected_keys:
        raise ServiceError("catalog_mismatch", status=503)
    if (type(capabilities["version"]) is not int or
            capabilities["version"] != CATALOG["version"] or
            capabilities["catalog_hash_algorithm"] != CATALOG_HASH_ALGORITHM or
            capabilities["catalog_hash"] != CATALOG_HASH):
        raise ServiceError("catalog_mismatch", status=503)
    for kind, expected in (("actions", CATALOG_ACTION_NAMES), ("queries", CATALOG_QUERY_NAMES)):
        names = capabilities[kind]
        if (not isinstance(names, list) or any(type(name) is not str for name in names) or
                len(set(names)) != len(names) or names != expected):
            raise ServiceError("catalog_mismatch", status=503)
    try:
        for key in set(CATALOG) - {"version", "actions", "queries"}:
            if _canonical_json_bytes(capabilities[key]) != _canonical_json_bytes(CATALOG[key]):
                raise ServiceError("catalog_mismatch", status=503)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ServiceError("catalog_mismatch", status=503) from None


def action_spec(name):
    return _ACTIONS.get(name) if isinstance(name, str) else None


def query_spec(name):
    return _QUERIES.get(name) if isinstance(name, str) else None


def _matches(value, schema, depth=0):
    if depth > 16 or not isinstance(schema, dict):
        return False
    if "anyOf" in schema:
        return any(_matches(value, branch, depth + 1) for branch in schema["anyOf"])
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            return False
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", ())):
            return False
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            return False
        return all(_matches(item, properties[key], depth + 1) for key, item in value.items() if key in properties)
    if kind == "integer":
        if type(value) is not int:
            return False
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    elif kind == "boolean":
        if type(value) is not bool:
            return False
    elif kind == "null":
        if value is not None:
            return False
    elif kind == "string":
        if not isinstance(value, str):
            return False
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
        if "pattern" in schema:
            try:
                if re.search(schema["pattern"], value) is None:
                    return False
            except (re.error, TypeError):
                return False
    else:
        return False
    return "enum" not in schema or any(type(value) is type(choice) and value == choice for choice in schema["enum"])


def validate_parameters(spec, params):
    """Reject malformed input with a stable, non-revealing public error."""
    if not isinstance(spec, dict) or not _matches(params, spec.get("parameters")):
        raise ServiceError("invalid_request")
    return params
