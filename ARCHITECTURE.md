# Architecture

## Why a greedy scheduler (and not something fancier)

I spent some time at the start just working out how big this problem actually is. Each bus does 540 km on a 240 km battery, so it needs at least two charging stops. When you enumerate which stations are reachable from which points given range constraints, there are only 8 valid charging plans per direction. 20 buses × 8 plans = 160 options total. That's... not a lot.

So I didn't reach for heavy machinery. The scheduler is a greedy constructive heuristic — it processes buses one at a time (earliest departure first), scores every valid plan for that bus against the current state of the world, and commits the best one. After all buses are assigned, a local search pass re-evaluates each bus while holding the others fixed, to catch cases where an early assignment was locally fine but globally unlucky.

I did consider two alternatives:

**Discrete event simulation** - makes sense when you have randomness (variable travel times, random breakdowns, etc.). We don't. Everything here is deterministic. A DES would just be a more complicated way to compute the same thing.

**ILP / constraint programming** - would give optimal solutions, but the spec says "adding a new rule must not require rewriting the engine." With an ILP, adding a rule means reformulating the objective function and possibly the constraints. That's not trivial and it breaks the pluggability requirement. I'd rather have a near-optimal solution I can extend in 10 minutes than a provably optimal one that takes a day to modify.

The greedy + scoring approach gets me both: rules are just functions that take a schedule and return a number. Want a new rule? Write a function, register it, add a weight to the config. The search strategy (greedy vs beam search vs whatever) is completely independent of the scoring -# Architecture: Bus Charging Scheduler

---

## Framework Choice: Greedy Constructive Heuristic with Local Search Refinement

### What I chose and why

The scheduler is a **greedy constructive heuristic with local search refinement**, driven by a **pluggable rule registry**.

Before choosing an approach, I analysed the actual decision space:

- Each bus travels 540 km and starts with 240 km range → must charge **at least twice**.
- There are exactly **8 valid 2-or-more-stop charging plans** per direction (range constraints eliminate most combinations).
- With 20 buses × 8 plans each, the search space is manageable without complex optimisation machinery.

**Why not a Discrete Event Simulator (DES)?**
A DES is the right tool when you need to model stochastic events (random arrivals, failures, variable travel times). Here, everything is deterministic — travel times are fixed, charging durations are fixed, there's no randomness. A DES adds complexity without benefit.

**Why not an ILP / constraint solver?**
An integer linear program would solve this optimally, but adding a new rule requires reformulating the objective function — a non-trivial change. The spec explicitly says *"Adding a new rule must not require rewriting the engine."* A rule-based scoring function satisfies this; an ILP reformulation doesn't.

**Why greedy + scoring works here:**
1. The decision space is small. Greedy with look-ahead is effective.
2. Rules are composable. Each rule is `(ScheduleResult) → float`. Adding a rule means appending a function.
3. Defensible. "I analysed the problem, found the search space is small, chose the simplest correct approach" is a stronger engineering answer than over-engineering.
4. The scoring framework is independent of the search strategy. You can swap greedy for beam search or branch-and-bound later without touching a single rule function.

### Local search refinement

After the initial greedy pass, each bus's plan is reconsidered one at a time while holding all others fixed. If an alternative plan improves the global score, the swap is accepted. Up to 3 sweeps are performed (convergence typically happens in 1). This catches the most common greedy failure mode: an early bus claims a charger in a way that's slightly sub-optimal for itself but significantly hurts later buses.

---

## Data Structure Design

### Input: Scenario JSON

The scenario JSON is the single source of truth for everything the scheduler needs. The five human-readable departure tables in the spec are just the starting point — the actual data structure I designed captures the full world model.

**Key design decisions:**

#### 1. Waypoints with `distance_from_start_km` (not parallel arrays)

```json
"route": {
  "waypoints": [
    { "name": "Bengaluru", "distance_from_start_km": 0 },
    { "name": "A",         "distance_from_start_km": 100 },
    { "name": "B",         "distance_from_start_km": 220 },
    ...
  ]
}
```

Segment distances are *computed* (`waypoints[i+1].distance - waypoints[i].distance`), never stored separately. This avoids the classic parallel-array sync bug. Adding a station = adding one object.

#### 2. Chargers as a list of objects

```json
"A": { "chargers": [{ "id": "A-1", "charge_duration_min": 25 }] }
```

Not `"charger_count": 1`. When station A gets a second charger — or a faster charger — you add/modify objects in the list. The scheduler iterates the list dynamically.

#### 3. Per-bus vehicle override

```json
{ "id": "bus-BK-02", "operator": "freshbus", ..., "vehicle_override": { "battery_range_km": 300 } }
```

All buses default to scenario-level `vehicle_defaults`. Any bus can override individual fields. The plan enumerator reads the effective values with fallback.

#### 4. Weights as a flat dict keyed by rule name

```json
"weights": { "individual_wait": 1.0, "operator_fairness": 1.0, "overall_time": 1.0 }
```

Adding a new rule = add a key. Changing a weight = change one value. One place, one line. The Pydantic model uses `extra="allow"`, so new weights added to the JSON are automatically preserved and used by the scoring engine — no code change needed.

#### 5. Operator as a plain string

New operators require no code change — just use a new string value in the bus entry.

---

## Anticipated Changes and How the Design Handles Them

I designed the data structure by asking: *"If someone tells me tomorrow that something in this world is different, what's the chance my code breaks?"* Here is the full list of changes I anticipated, and how each is handled.

### Tier 1 — Zero code changes required (data only)

| Change | How handled |
|--------|-------------|
| More buses (50, 100, 500) | Add entries to `buses[]`. Greedy scales linearly — no architectural change. |
| More chargers per station (2, 3, 5) | Add charger objects to the `chargers[]` list. The tracker picks the earliest-available charger. |
| New station added to the route (e.g., E between D and Kochi) | Add one waypoint with its `distance_from_start_km`. The plan enumerator recomputes valid plans from distances — no hardcoded station names. |
| New operator | Use a new string in the `operator` field. No enum, no registration. |
| Weight tuning | Change a number in `weights`. One place. |
| New departure schedule | Change `departure_time` values. Each scenario is independent. |
| Segment distances change (road construction, new bypass) | Change `distance_from_start_km` values. Segments are computed, not stored. |
| Different charging durations per station | Each charger has its own `charge_duration_min`. Already per-charger. |
| Different battery range per bus type (express vs standard) | Add `vehicle_override.battery_range_km` to the bus entry. Enumerator reads effective range with fallback to defaults. |
| Different speeds (express vs local) | Add `vehicle_override.speed_kmh` to the bus entry. Simulation reads effective speed with fallback. |
| Scenario-specific default vehicle config | Change `vehicle_defaults` at scenario level. |

### Tier 2 — New rule function + optional new data field (no engine rewrite)

| Change | Data change | Code change |
|--------|-------------|-------------|
| Priority buses (SLA, contractual) | Add `priority: int` field to bus (already present, defaults to 0) | New soft rule: `score_priority_delay()`. Processing order already uses `priority` as secondary sort key. |
| Time-of-day electricity costs | Add `cost_schedule` array to station config | New soft rule: `score_electricity_cost()`. Evaluates charge start times against cost schedule. |
| Driver shift limits (max trip hours) | Add `max_shift_hours` to bus | New hard rule filter passed to plan enumerator: filters out plans where total trip exceeds shift limit. |
| Operator SLA (max acceptable delay) | Add `sla_max_delay_min` to bus | New hard rule filter in enumerator. |
| Station maintenance windows (offline periods) | Add `availability_schedule` to station | New hard rule filter: "is station available at time T?" |
| Charger cooldown between uses | Add `cooldown_min` to charger config | Station tracker adds a post-charge cooldown gap when computing the next available slot. |
| Minimum rest time between charges per bus | Add `min_rest_between_charges_min` to vehicle config | Hard rule filter on plans. |

### Tier 3 — Moderate changes (architecture intact, some refactoring needed)

| Change | Impact |
|--------|--------|
| Partial charging (e.g., 80% in 15 min vs 100% in 25 min) | Charging becomes a decision variable. Extends the plan space but the scoring framework is unchanged. |
| Battery degradation (older bus = reduced effective range) | Per-bus `effective_range_km` override. Plan enumerator already uses this. |
| Weather/AC impact on range | Scenario-level or per-bus `range_modifier`. Multiplied into effective range. |
| Multiple routes sharing stations | Route model stays the same. Station tracker is keyed by station name (not route), so shared stations work automatically. Enumerator needs to enumerate plans per route independently. Minor refactor. |
| Non-linear route topology (branches, hubs) | Route becomes a graph rather than an ordered list of waypoints. Significant refactor of the enumerator and route model, but all rule functions remain unchanged. |

---

## Soft Rule Definitions

### 1. Individual Wait (`individual_wait`)

```python
score = sum(bus.total_wait_min² for bus in all_buses)
```

Uses squared wait time (L2 penalty). One bus waiting 60 min is penalised more than two buses each waiting 30 min (3600 vs 1800). This is intentional — it prioritises eliminating outliers over reducing average wait.

### 2. Operator Fairness (`operator_fairness`)

```python
# Within-operator: buses of the same operator should have similar waits
intra = sum(variance(waits) for each operator's fleet)

# Cross-operator: no operator should be systematically worse off
inter = variance(mean_wait per operator)

score = intra + inter
```

Two components: within-operator fairness (KPN's buses should have similar experiences to each other) and cross-operator fairness (KPN as a group shouldn't be systematically worse than Freshbus).

### 3. Overall Time (`overall_time`)

```python
score = sum(total_trip_duration for each bus)
```

Minimises the total network time. Pushes toward less waiting and more direct routing.

---

## Adding a New Rule: Code Example

```python
# scheduler/rules.py

# Step 1: Write the function
def score_electricity_cost(result: ScheduleResult) -> float:
    """Prefer charging during off-peak hours (lower electricity cost)."""
    total_cost = 0.0
    for bus_timeline in result.bus_timelines:
        for event in bus_timeline.events:
            if event.event_type == "charge":
                hour = event.start_time.hour
                cost_multiplier = 2.0 if 18 <= hour <= 22 else 1.0  # peak hours
                total_cost += event.duration_min * cost_multiplier
    return total_cost

# Step 2: Register it
SOFT_RULES.append(Rule("electricity_cost", score_electricity_cost, "soft"))
```

```json
// Step 3: Add to scenario JSON
"weights": { ..., "electricity_cost": 0.5 }
```

---

## Assumptions

1. **Constant speed**: 60 km/h for all buses. Configurable per-bus via `vehicle_override.speed_kmh`.

2. **Buses always depart on time**: No origin delays. The scheduler only controls charging — departure is given.

3. **Charging always to full**: Each charge fills to 240 km range (or per-bus effective range) regardless of remaining battery. Duration is fixed per charger config.

4. **No range consumed while waiting or charging**: A bus sitting in a queue doesn't use battery. Standard assumption for this type of problem.

5. **FIFO queue with global scoring**: When multiple buses want to charge at the same station around the same time, the scheduler determines which gets there first by the order it commits plans. The globally-scored greedy assignment naturally distributes buses across stations.

6. **Operator fairness = within + cross-operator variance**: "Each operator's fleet should run smoothly as a group" is interpreted as both intra-operator consistency and inter-operator equity.

7. **Greedy processing order**: Buses processed in departure-time order (earliest first), with `priority` as a secondary key. This is deterministic and reproducible.

8. **2-pass refinement terminates**: At most 3 refinement sweeps are run. In practice, convergence happens in 1 sweep for all tested scenarios.
 you could swap it out later without touching any rule code.

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

- **Partial charging** (charge to 80% in 15 min instead of 100% in 25 min) - charging duration becomes a decision variable, which blows up the plan space. Scoring framework still works though, you'd just need a smarter enumerator.
- **Multiple routes sharing stations** - the station tracker is already keyed by name not route, so the sharing part actually works. But the enumerator currently assumes one route, so that needs a small refactor.
- **Non-linear routes (branches, hubs)** - the route model would need to become a graph instead of a list. Significant change to the enumerator, but none of the rule functions care about route topology, so they'd survive untouched.

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

1. **Constant speed** - 60 km/h everywhere. Overridable per-bus via `vehicle_override.speed_kmh` if needed.

2. **Buses depart on time** - the scheduler controls charging, not departures. Departure time is an input.

3. **Full charges only** - every charge session fills to 240 km (or the bus's effective range). No partial charging. Duration is fixed per charger.

4. **No battery drain while idle** - a bus waiting in queue or being charged doesn't consume range. This is standard for this kind of problem.

5. **FIFO at stations** - when buses arrive at the same station around the same time, the order they get chargers is determined by when the scheduler commits their plans. The scored greedy assignment naturally spreads buses across stations.

6. **Fairness = intra + inter variance** - I interpreted "each operator's fleet should run smoothly" as both within-operator consistency and across-operator equity.

7. **Deterministic processing order** — buses processed by departure time, then by priority. Same input always gives same output.

8. **Refinement converges** - 3 sweeps max, but in practice 1 is enough.
