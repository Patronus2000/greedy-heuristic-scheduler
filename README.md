# Bus Charging Scheduler


A scheduling system for electric buses charging along the Bengaluru–Kochi corridor. Given a scenario (buses, route, weights), it decides which charging stations each bus uses and in what order buses use each charger — minimising wait times, ensuring operator fairness, and respecting hard constraints.

**[Live App →](https://your-app.streamlit.app)** *(https://greedy-heuristic-scheduler-akhilp.streamlit.app/)*

---

## Running Locally

```bash
# Clone and install
git clone https://github.com/Patronus2000/Exponent-Energy.git
cd Exponent-Energy
pip install -r requirements.txt

# Run
streamlit run app.py
```

Then open `http://localhost:8501` in your browser. Use the dropdown in the sidebar to select any of the 5 scenarios.

---

## Project Structure

```
.
├── app.py                    # Streamlit entry point
├── requirements.txt
├── scenarios/                # 5 scenario JSON files (the input data)
│   ├── scenario_1.json       # Even spacing — baseline
│   ├── scenario_2.json       # Bunched start — departure clustering
│   ├── scenario_3.json       # Asymmetric load — directional imbalance
│   ├── scenario_4.json       # Operator-heavy — fairness stress test
│   └── scenario_5.json       # Worst case — maximum charger contention
├── scheduler/
│   ├── models.py             # Pydantic input + output dataclasses
│   ├── loader.py             # JSON → Scenario model
│   ├── planner.py            # Valid charging plan enumerator
│   ├── engine.py             # Greedy scheduler with local search refinement
│   ├── rules.py              # Rule registry (soft + hard rules)
│   ├── station_tracker.py    # Charger availability tracker
│   └── validator.py          # Post-hoc hard constraint checker
├── test_smoke.py             # Smoke tests
├── README.md
└── ARCHITECTURE.md
```

---

## Scenario Design

Each scenario is designed to stress a different aspect of the scheduler. All use the same route (Bengaluru–Kochi, 540 km) and the same 4 charging stations with 1 charger each.

| Scenario | Intent | What it tests |
|---|---|---|
| **1 — Even Spacing** | Buses depart every 15 min, operators rotate evenly (kpn, freshbus, flixbus). | Baseline: does the scheduler produce a reasonable schedule under low contention? Most buses should have zero or minimal wait. |
| **2 — Bunched Start** | All 20 buses depart within 60 min (19:00–19:45 in each direction). | Charger contention: with only 4 chargers and 20 buses arriving in waves, the scheduler must distribute buses across stations to avoid long queues. |
| **3 — Asymmetric Load** | 10 Bengaluru→Kochi buses but only 4 Kochi→Bengaluru buses (14 total). | Directional imbalance: one direction has much more traffic. Tests whether the scheduler handles unequal demand without giving the lighter direction an unfair advantage. |
| **4 — Operator-Heavy** | KPN operates 8 of 10 BK buses (80%), weights set to `operator_fairness: 2.0`. | Fairness under dominance: when one operator has most of the fleet, does doubling the fairness weight cause the scheduler to redistribute wait times more equitably across operators? |
| **5 — Worst Case Convergence** | All 20 buses depart within 72 min, tightly clustered. | Maximum contention: every charger will be queued. Tests whether the 2-pass refinement converges and whether the scheduler gracefully handles scenarios where wait times are unavoidable. |

---

## How to Change a Weight

Weights live in the scenario JSON file. No code changes required.

**Example:** Make operator fairness 5× more important in Scenario 4:

```json
// scenarios/scenario_4.json
"weights": {
  "individual_wait": 1.0,
  "operator_fairness": 5.0,   // ← change this number
  "overall_time": 1.0
}
```

Reload the app. The scheduler will re-run with the new weights automatically.

---

## How to Add a New Rule

**Example:** Add a rule penalising long-waiting high-priority buses.

**Step 1:** Write the rule function in `scheduler/rules.py`:

```python
def score_priority_delay(result: ScheduleResult) -> float:
    """High-priority buses should wait less."""
    return sum(
        bt.total_wait_min * (1 + bt.priority)
        for bt in result.bus_timelines
    )
```

**Step 2:** Register it in the `SOFT_RULES` list (same file):

```python
SOFT_RULES: list[Rule] = [
    Rule("individual_wait",   score_individual_wait,   "soft"),
    Rule("operator_fairness", score_operator_fairness, "soft"),
    Rule("overall_time",      score_overall_time,      "soft"),
    Rule("priority_delay",    score_priority_delay,    "soft"),  # ← add this
]
```

**Step 3:** Add the weight to any scenario JSON:

```json
"weights": {
  "individual_wait": 1.0,
  "operator_fairness": 1.0,
  "overall_time": 1.0,
  "priority_delay": 2.0   // ← add this
}
```

Done. No engine changes, no model changes.

---

## Running Smoke Tests

```bash
python test_smoke.py
```

> **Windows note:** If you see a `UnicodeEncodeError`, use `python -X utf8 test_smoke.py` to enable UTF-8 output.

Verifies:
- Plan enumeration produces exactly 8 valid plans per direction
- All 5 scenarios produce zero hard constraint violations

---
