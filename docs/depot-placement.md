# Typed depot placement

`road.depot.build` and `rail.depot.build` accept `direction` as either an explicit integer `0..3` or the string `"auto"`. Direction remains required. The caller chooses the tile, transport type and whether to build; automatic direction only resolves the depot's facing.

For example, these are alternative request shapes, with illustrative coordinates:

```json
{"name":"road.depot.build","parameters":{"tile":{"x":10,"y":20},"road_type":0,"direction":"auto"}}
```

```json
{"name":"rail.depot.build","parameters":{"tile":{"x":10,"y":20},"rail_type":0,"direction":2}}
```

Automatic direction examines only the four entrance-neighbor positions at the selected tile. It considers ordinary road/tram tiles with the exact selected road type and nonempty bits in that category, or plain railway with the exact selected rail type owned by the acting company. Ordinary public/town roads can qualify. Stations, crossings, other depots and bridge/tunnel tiles are not automatic-facing candidates. Type defaults retain their existing meaning; no type is substituted or upgraded.

Each candidate must also pass the native depot construction test for that facing. If exactly one candidate remains, the action posts one ordinary depot command with that concrete direction. It does not rank directions by demand, route length, traffic, operating cost or business value, and it does not choose another site.

| Result | Meaning |
| --- | --- |
| One legal matching neighbor | Build facing that neighbor, subject to normal native execution checks. |
| `depot_direction_unavailable` | No matching adjacent facing passed the construction test. Supply an explicit direction or change the proposed placement. |
| `depot_direction_ambiguous` | Multiple adjacent facings passed. Supply an explicit direction. |

Selection failures post no native construction command and charge no construction cost. They are also returned as safe failed advisory estimates. A successful estimate reserves neither money nor a facing for a later, separately submitted action. Native tests preserve both RNG streams and use the existing Shift-test lifecycle, including its documented auxiliary-allocation allowance.

Explicit integer directions keep their prior behavior: they do not require a matching adjacent road or railway, perform no automatic direction tests and acquire no additional neighbor-selection fence. Successful road/rail depot receipts now include the actual built integer `direction` alongside normal cost and depot-reference feedback, for both text and visual players. Failed builds and estimates do not claim a completed direction or newly created depot.

**Facing is not a connection or route guarantee.** Automatic direction can face a straight road without the reciprocal road bit, or a rail tile without the required entrance turn. It builds no road bits, track pieces, alternative alignment or extra connector. The GUI's separate local connection commands are not part of this typed action. Check the [road-access rules](capture-controls.md#road-access), local geometry, maintenance access and subsequent vehicle operation.

The chosen direction is frozen in the queued native command. For automatic placement, changes to its captured local tile/type/ownership/geometry dependencies reject the queued action rather than selecting a different facing. Other ordinary native conditions, such as funds or occupancy, can still change and cause execution to fail. Recover an unresolved request with its original ID and body; an exact replay returns its original receipt, not a new orientation decision.
