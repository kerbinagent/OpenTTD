"""Capability matrix derived from the C++ engine's public catalog."""

from .catalog import CATALOG


def _category(name):
    prefix = name.split(".", 1)[0]
    return {
        "water": "ship",
        "airport": "aircraft",
        "train": "vehicles",
        "vehicle": "vehicles",
        "order": "orders",
        "company": "finance",
        "bridge": "bridge/tunnel",
        "tunnel": "bridge/tunnel",
        "station": "stations",
    }.get(prefix, prefix)


def matrix(supported=(), *, mode="text"):
    """Report canonical actions and explicit human-only coverage gaps."""
    if isinstance(supported, dict):
        names = set(supported)
    else:
        names = {entry if isinstance(entry, str) else entry.get("name")
                 for entry in supported if isinstance(entry, (str, dict))}
    rows = [
        {"category": _category(spec["name"]), "action": spec["name"],
         "available": spec["name"] in names}
        for spec in CATALOG["actions"] if spec["name"] != "ui.apply"
        and mode in spec.get("modes", ())
    ]
    rows.extend({"category": "human_only", "action": name, "available": False}
                for name in CATALOG["human_only"])
    return rows
