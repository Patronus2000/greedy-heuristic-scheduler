"""
Smoke test — verifies all 5 scenarios load, schedule, and pass hard validation.
Run with: python test_smoke.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scheduler import load_all_scenarios, run_scheduler, validate
from scheduler.planner import enumerate_valid_plans


def test_plan_enumeration():
    """Verify Bengaluru→Kochi produces exactly 8 valid plans."""
    from scheduler import load_scenario
    scenario = load_scenario("scenarios/scenario_1.json")
    bus = next(b for b in scenario.buses if b.origin == "Bengaluru")
    plans = enumerate_valid_plans(scenario, bus)
    print(f"Valid plans for {bus.origin}->{bus.destination}: {len(plans)}")
    for p in plans:
        print(f"  {p}")
    assert len(plans) == 8, f"Expected 8 valid plans, got {len(plans)}"
    print("✅ Plan enumeration: PASS\n")


def test_all_scenarios():
    """Run all 5 scenarios and verify zero hard violations."""
    scenarios = load_all_scenarios("scenarios")
    print(f"Loaded {len(scenarios)} scenarios.\n")

    for scenario in scenarios:
        print(f"Running {scenario.name}...")
        result = run_scheduler(scenario, refine=True)
        violations = validate(result)
        result.hard_violations = violations

        n_buses = len(result.bus_timelines)
        avg_wait = sum(bt.total_wait_min for bt in result.bus_timelines) / n_buses
        max_wait = max(bt.total_wait_min for bt in result.bus_timelines)

        print(f"  Buses scheduled: {n_buses}")
        print(f"  Total score: {result.total_score:,.1f}")
        print(f"  Avg wait: {avg_wait:.1f} min | Max wait: {max_wait:.1f} min")
        print(f"  Hard violations: {len(violations)}")
        for v in violations:
            print(f"    ❌ {v}")

        if not violations:
            print(f"  ✅ Valid schedule\n")
        else:
            print(f"  ❌ INVALID SCHEDULE — {len(violations)} violation(s)\n")

        assert len(violations) == 0, f"Hard violations in {scenario.name}: {violations}"

    print("✅ All scenarios: PASS")


if __name__ == "__main__":
    test_plan_enumeration()
    test_all_scenarios()
