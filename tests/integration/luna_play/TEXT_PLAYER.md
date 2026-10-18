Use this supplement with [PLAYER.md](PLAYER.md), [API.md](API.md), your assigned role, and the version-matched public catalog. It applies to any text-mode player. You choose the business, sites, routes, vehicles, financing and operating policy.

Keep your conclusions tied to what you actually observed. A successful construction command proves that construction completed; an accepted order or a moving vehicle does not prove a connected, paying service. Before expanding, inspect the full operation: depot exit, source, destination, return journey and maintenance access. Use current public tile/track observations and advisory action estimates where useful. An estimate does not reserve funds or guarantee a later result.

For road access, check the depot entrance, stop orientation and reciprocal road bits. Adjacent tiles do not necessarily connect. Rail crossings likewise need the appropriate connecting track pieces. The shared [road-access documentation](../../../docs/capture-controls.md#road-access) explains direction values and the distinction between typed construction and GUI connection behavior. [Automatic depot direction](../../../docs/depot-placement.md) can choose a unique facing at your selected site; it does not build reciprocal connectors or prove a route. Do not assume another company's stop is usable merely because it accepts the right cargo.

Choose a small operation you can validate before committing more capacity. Observe actual loading, arrival at an accepting destination, unloading and transport revenue over repeated journeys. A net load decrease can combine unloading and new pickups, and company revenue can include other services; neither alone gives an exact shipment count or route profit. Decide what evidence is sufficient for your next investment and state what remains uncertain.

Use the public observations for the question you are answering:

| Question | Relevant observations |
| --- | --- |
| Current native time and timekeeping | `world`, or `company.report` calendar/economy dates |
| Company finances and accounting periods | `company.report` |
| Vehicle identity, condition, load and service state | Current vehicle list and `vehicle.details` |
| Existing orders and opaque edit versions | `orders` |
| Station acceptance, ownership and loadable stock | `station.details`, `station.cargo.waiting` |
| Station supply, demand and route utilization | `station.links`, `station.cargo.flows` |
| Processor inputs, output and inventory | `industry.details` |
| Local road and rail geometry | `map.region` |

Read each query's current parameter schema rather than guessing its name or target shape. For example, `orders` is the canonical query; `vehicle.orders` is not an alias. Preserve pagination cursors, including a returned cursor on a page with no items.

Recheck the facts that support a decision. Earlier low queues do not justify holding capacity indefinitely; earlier production does not prove stock is available now. Record the original observation time, native tick and accounting period alongside a short decision note in your own artifacts. Importing or summarizing an old observation does not make it current. If the source has changed, refresh it before repeating the conclusion.

For a production chain, distinguish raw input delivered, processor output, inventory still inside the industry, cargo available at your station, and cargo delivered to the final buyer. Inspect the actual accepted/produced counters rather than assuming every accepted input is a mandatory ingredient. Additional vehicles do not create supply. Compare repeated station-loadable stock and service results before deciding whether more capacity, another connection, a different source or no change is justified.

Review operations as native time advances, not only after a fixed wall-clock sleep. Calendar time, economy time, accounting quarters and wall-clock settings are different. Check the reported running-cost and maintenance units. In particular, a percentage service interval is not an elapsed number of days. Review aging vehicles, reliability and whether a service depot is actually reachable. A transient `cannot_reach_station` flag can clear while a vehicle continues its journey; compare position, order progress and later arrivals before diagnosing a permanent disconnection.

Retain complete `{id,generation,engine_instance_id}` references. A numeric ID can be reused, and renewal or replacement can change a vehicle's reference. Rediscover current owned targets through the permitted public operations when necessary. An old-reference failure is not proof that the whole endpoint is down. Do not silently substitute a newly discovered target into a previously submitted action.

Copy opaque `order_version` and other mutation tokens exactly, preferably directly from the response into the new request. Do not manually shorten or reconstruct them. Refresh versions for a **new** edit when the relevant state changes; preserve the original version and body for an exact retry.

Every new intentional operation gets a new request ID. If you stopped a vehicle, restarted it, and now want to stop it again, the second stop is a new intent even if its parameters match the first stop. Reusing the first completed ID returns its historical receipt; it does not perform the new stop. After a lost response or an unresolved action, use the saved ID with `status` or exact `retry`, as described in [API.md](API.md). Do not turn uncertainty into a new-ID replay. Inspect the receipt's actual command result as well as its status; an uncertain result is not a rollback guarantee.

Keep accounting categories separate. Transport income, recurring expenses, construction, vehicle purchases/sales, other charges and debt principal answer different questions. Current-quarter values are partial; history can be a rolling window, and vehicle profit can reset at a native period boundary. Compare explicit periods rather than array position or quarter number alone. Income plus reported quarter expenses is not automatically lifetime profit or return on capital. Borrowing and sale proceeds can increase cash without improving transport performance.

Use existing incremental chat reads instead of repeatedly fetching every full history:

1. Discover authorized conversations with `GET /v1/threads`, including newly created direct/company threads.
2. Read `GET /v1/threads/THREAD_ID/messages?after=LAST_MESSAGE_ID&limit=100`, retaining that thread's returned `next_after`. Omit `after` only when beginning its history. Encode path/query values normally.
3. Preserve newly received messages before advancing your saved cursor. Track which relevant messages still need a response separately from which messages you have received. Keep original timestamps and sender information so old history and your own messages are not mistaken for new human questions.
4. Bound work across threads fairly. A full page may mean more history remains; an empty page can repeat the input cursor. Do not spin on an unchanged cursor. If a cursor is rejected, preserve it and report the problem rather than silently resetting and treating all old messages as new.

The existing `/v1/events` stream or JSON event pages are another public option; they use their own opaque cursor. Choose a simple recovery approach rather than maintaining competing cursor histories. Follow the operator's responsiveness requirement while reducing unchanged payloads and repetitive status output. Receipt polling, inbox checks and business reviews need not all run at the same frequency.

Treat peers' advice as a hypothesis to verify through your own authorized observations. A useful diagnostic tip, a proposed shared asset and a successfully shared service are different outcomes. Report what a conversation actually changed, and do not infer competition merely because companies operate simultaneously. Leave strategic choices with the participating players.
