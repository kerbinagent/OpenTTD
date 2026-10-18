"""Known successful stock-regression construction sites, without prebuilt depots.

See regression/regression/main.nut and result.txt. All coordinates are native
tile indices on that fixture's 256-wide map, dated November 1954. These plans
still execute normal construction, purchase, ownership and financing commands.
"""

MODES = (
    {"mode": "road", "build": "road.depot.build", "parameters": {"tile": 33417, "direction": 2}, "depot": 33417, "engine": 153},
    {"mode": "rail", "build": "rail.depot.build", "parameters": {"tile": 10008, "direction": 0}, "depot": 10008, "engine": 9},
    {"mode": "ship", "build": "water.depot.build", "parameters": {"tile": 28479, "axis": "x"}, "depot": 28479, "engine": 204},
    {"mode": "aircraft", "build": "airport.build", "parameters": {"tile": 32116, "airport_type": 0}, "depot": 32119, "engine": 219},
)

# These sites have native passenger production and acceptance, verified with
# AITestMode on a fresh fixture. The acceptance test must still prove actual
# construction, loading, movement and passenger revenue through the live bridge.
PASSENGER_ROUTES = (
    {
        "mode": "road", "engine": 116, "depot": 40791, "destinations": (40529, 40541),
        "construction": (
            ("road.build", {"start": 40530, "end": 40540}),
            ("road.build", {"start": 40535, "end": 40791}),
            ("terrain.clear", {"tile": 40791}),
            ("road.depot.build", {"tile": 40791, "direction": 3}),
            ("road.stop.build", {"tile": 40529, "direction": 2, "stop_type": "bus"}),
            ("road.stop.build", {"tile": 40541, "direction": 0, "stop_type": "bus"}),
        ),
    },
    {
        "mode": "rail", "engine": 9, "wagon_engine": 27, "depot": 23870, "destinations": (23860, 23868),
        "construction": (
            ("rail.depot.build", {"tile": 23870, "direction": 0}),
            ("rail.station.build", {"tile": 23860, "axis": "x", "tracks": 1, "length": 2}),
            ("rail.station.build", {"tile": 23868, "axis": "x", "tracks": 1, "length": 2}),
            ("rail.build_line", {"start": 23862, "end": 23867, "track": "x"}),
        ),
    },
    {
        "mode": "ship", "engine": 206, "depot": 4283, "destinations": (4542, 4037),
        "construction": (
            ("water.depot.build", {"tile": 4283, "axis": "x"}),
            ("water.dock.build", {"tile": 4542}),
            ("water.dock.build", {"tile": 4037}),
        ),
    },
    {
        "mode": "aircraft", "engine": 219, "depot": 13462, "destinations": (13459, 14779),
        "construction": (
            ("airport.build", {"tile": 13459, "airport_type": 0}),
            ("airport.build", {"tile": 14779, "airport_type": 0}),
        ),
    },
)
