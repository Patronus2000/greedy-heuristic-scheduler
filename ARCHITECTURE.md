# Architecture

## Why a greedy scheduler (and not something fancier)

I spent some time at the start just working out how big this problem actually is. Each bus does 540 km on a 240 km battery, so it needs at least two charging stops. When you enumerate which stations are reachable from which points given range constraints, there are only 8 valid charging plans per direction. 20 buses × 8 plans = 160 options total. That's... not a lot.

So I didn't reach for heavy machinery. The scheduler is a greedy constructive heuristic — it processes buses one at a time (earliest departure first), scores every valid plan for that bus against the current state of the world, and commits the best one. After all buses are assigned, a local search pass re-evaluates each bus while holding the others fixed, to catch cases where an early assignment was locally fine but globally unlucky.

I did consider two alternatives:

**Discrete event simulation** — makes sense when you have randomness (variable travel times, random breakdowns, etc.). We don't. Everything here is deterministic. A DES would just be a more complicated way to compute the same thing.

**ILP / constraint programming** — would give optimal solutions, but the spec says "adding a new rule must not require rewriting the engine." With an ILP, adding a rule means reformulating the objective function and possibly the constraints. That's not trivial and it breaks the pluggability requirement. I'd rather have a near-optimal solution I can extend in 10 minutes than a provably optimal one that takes a day to modify.

The greedy + scoring approach gets me both: rules are just functions that take a schedule and return a number. Want a new rule? Write a function, register it, add a weight to the config. The search strategy (greedy vs beam search vs whatever) is completely independent of the scoring — you could swap it out later without touching any rule code.

### The local search refinement

The greedy pass has an obvious failure mode: bus #1 grabs charger A at 08:00, which is fine for bus #1, but bus #3 really needed that slot and now has to wait 25 extra minutes. The local search pass fixes this by reconsidering each bus's plan (one at a time, everyone else stays fixed) and swapping if it improves the total score.

I run up to 3 sweeps but it usually converges in 1. Haven't seen a case where it needs more than 2.

---

## Data model

The scenario JSON is the single source of truth. I'll walk through the non-obvious choices.

### Waypoints, not segment arrays

```json
"route": {
  "waypoints": [
    { "name": "Bengaluru", "distance_from_start_km": 0 },
    { "name": "A",         "distance_from_start_km": 100 },
    { "name": "B",         "distance_from_start_km": 220 }
  ]
}
```

Segment distances (A→B = 120 km) are computed on the fly from the difference. I've been bitten before by parallel arrays that drift out of sync — storing distances from start and computing segments avoids that entirely. Adding a new station is one new object in the list.

### Chargers as objects, not counts

```json
"A": { "chargers": [{ "id": "A-1", "charge_duration_min": 25 }] }
```

I didn't do `"charger_count": 1` because chargers aren't fungible — station A might get a faster charger next quarter, or a second slower one. Each charger is its own object with its own duration. The scheduler just iterates the list.

### Per-bus overrides

```json
{ "id": "bus-BK-02", "operator": "freshbus", "vehicle_override": { "battery_range_km": 300 } }
```

There's a scenario-level `vehicle_defaults` block, and any bus can override individual fields. So if one bus has a bigger battery, you just set it on that bus. The plan enumerator reads the effective value with fallback.

### Weights are just a dict

```json
"weights": { "individual_wait": 1.0, "operator_fairness": 1.0, "overall_time": 1.0 }
```

One key per rule, one float per key. The Pydantic model uses `extra="allow"` so new keys don't need a schema change. Add a rule, add a weight — done.

### Operators are plain strings

No enum. No registration. Just a string on the bus. If tomorrow there's a third operator called "Volvo Transit", nothing in the code cares.

---

## What changes without breaking things

I thought about this pretty carefully — "if someone tells me tomorrow that X is different, does my code break?"

### Things that are purely data changes (zero code)

- More buses (50, 500, whatever). Just add entries. Greedy scales linearly.
- More chargers at a station. Add objects to `chargers[]`. The tracker picks the earliest-available one.
- New station on the route. One new waypoint object. The plan enumerator recomputes valid plans from distances, no station names are hardcoded.
- New operator. New string. That's it.
- Different weights. Change a number in the JSON.
- Different departure times, segment distances, charging durations, battery sizes, speeds — all just config. `vehicle_override` and per-charger durations handle the heterogeneous cases.

### Things that need a new rule function (but no engine changes)

| What changes | Data side | Code side |
|---|---|---|
| Priority/SLA buses | `priority` field on bus (already there, defaults to 0) | New scoring function. Processing order already sorts by priority. |
| Electricity pricing by time of day | `cost_schedule` on station | New scoring function that checks charge times against the schedule. |
| Driver shift limits | `max_shift_hours` on bus | Hard constraint filter in the plan enumerator. |
| Station maintenance windows | `availability_schedule` on station | Hard constraint: "is station online at time T?" |
| Charger cooldown | `cooldown_min` on charger | Station tracker adds a gap after each charge session. |

### Things that need real refactoring

- **Partial charging** (charge to 80% in 15 min instead of 100% in 25 min) — charging duration becomes a decision variable, which blows up the plan space. Scoring framework still works though, you'd just need a smarter enumerator.
- **Multiple routes sharing stations** — the station tracker is already keyed by name not route, so the sharing part actually works. But the enumerator currently assumes one route, so that needs a small refactor.
- **Non-linear routes (branches, hubs)** — the route model would need to become a graph instead of a list. Significant change to the enumerator, but none of the rule functions care about route topology, so they'd survive untouched.

---

## How the scoring rules work

### Individual wait

```
score = Σ (bus.total_wait_min²)
```

Squared, not linear. The reasoning: one bus waiting 60 minutes is worse than two buses each waiting 30, even though the total is the same (3600 vs 1800). The L2 penalty naturally pushes toward eliminating outliers rather than just reducing the average.

### Operator fairness

Two components:

```
intra = Σ variance(wait times within each operator's fleet)
inter = variance(mean wait time across operators)
```

The first part says "KPN's buses should have roughly similar wait times to each other." The second says "KPN as a whole shouldn't be systematically worse off than Freshbus." Both matter — you can have low inter-operator variance but one operator's buses are all over the place, or vice versa.

### Overall time

```
score = Σ (total trip duration per bus)
```

Straightforward. Just minimises time spent on the network.

---

## Adding a new rule

This is the part I wanted to be really easy. Here's what it looks like end to end:

```python
# scheduler/rules.py

def score_electricity_cost(result: ScheduleResult) -> float:
    """Prefer charging during off-peak hours."""
    total_cost = 0.0
    for bus_timeline in result.bus_timelines:
        for event in bus_timeline.events:
            if event.event_type == "charge":
                hour = event.start_time.hour
                rate = 2.0 if 18 <= hour <= 22 else 1.0
                total_cost += event.duration_min * rate
    return total_cost

SOFT_RULES.append(Rule("electricity_cost", score_electricity_cost, "soft"))
```

Then in the scenario JSON:

```json
"weights": { "individual_wait": 1.0, "operator_fairness": 1.0, "overall_time": 1.0, "electricity_cost": 0.5 }
```

Three steps: write function, register it, add weight. No engine changes.

---

## Assumptions

1. **Constant speed** — 60 km/h everywhere. Overridable per-bus via `vehicle_override.speed_kmh` if needed.

2. **Buses depart on time** — the scheduler controls charging, not departures. Departure time is an input.

3. **Full charges only** — every charge session fills to 240 km (or the bus's effective range). No partial charging. Duration is fixed per charger.

4. **No battery drain while idle** — a bus waiting in queue or being charged doesn't consume range. This is standard for this kind of problem.

5. **FIFO at stations** — when buses arrive at the same station around the same time, the order they get chargers is determined by when the scheduler commits their plans. The scored greedy assignment naturally spreads buses across stations.

6. **Fairness = intra + inter variance** — I interpreted "each operator's fleet should run smoothly" as both within-operator consistency and across-operator equity.

7. **Deterministic processing order** — buses processed by departure time, then by priority. Same input always gives same output.

8. **Refinement converges** — 3 sweeps max, but in practice 1 is enough.
