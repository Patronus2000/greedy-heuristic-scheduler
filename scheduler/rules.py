"""
Rule Engine

All scheduling rules live here. The rule registry is the ONLY place you need to
touch when adding a new rule — the engine reads the registry dynamically.

How to add a new soft rule:
  1. Write a function: def score_my_rule(result: ScheduleResult) -> float
  2. Append to SOFT_RULES: Rule("my_rule", score_my_rule, "soft")
  3. Add weight to the scenario JSON: "weights": { ..., "my_rule": 1.0 }
  Done. No engine changes.

How to add a new hard rule (plan-level filter):
  1. Write a function: def filter_my_rule(plan: list[str], bus: Bus) -> bool
  2. Append to HARD_RULE_FILTERS list in engine.py (or pass directly to enumerate_valid_plans)
  Done.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Callable

from .models import ScheduleResult


@dataclass
class Rule:
    """
    A named, typed rule.

    name:      must match a key in scenario weights (for soft rules).
    evaluate:  callable (ScheduleResult) -> float. Lower is better.
    rule_type: "soft" or "hard".
    """
    name: str
    evaluate: Callable[[ScheduleResult], float]
    rule_type: str   # "soft" | "hard"


# ---------------------------------------------------------------------------
# Soft Rule Functions
# (lower score = better schedule)
# ---------------------------------------------------------------------------

def score_individual_wait(result: ScheduleResult) -> float:
    """
    No single bus should wait too long.

    Uses squared wait times (L2 penalty) so that one bus waiting 60 min is
    penalised more than two buses each waiting 30 min (3600 vs 1800).
    """
    return sum(bt.total_wait_min ** 2 for bt in result.bus_timelines)


def score_operator_fairness(result: ScheduleResult) -> float:
    """
    Each operator's fleet should run smoothly as a group.

    Two components:
      1. Intra-operator variance: within each operator, waits should be similar.
      2. Inter-operator variance: no operator should be systematically worse off.

    Both use variance (average squared deviation from the mean).
    If an operator has only one bus, intra-variance is 0.
    """
    # Group wait times by operator
    by_operator: dict[str, list[float]] = {}
    for bt in result.bus_timelines:
        by_operator.setdefault(bt.operator, []).append(bt.total_wait_min)

    # Intra-operator variance: average within-group variance
    intra = 0.0
    means = []
    for waits in by_operator.values():
        if len(waits) > 1:
            intra += statistics.variance(waits)
        means.append(sum(waits) / len(waits))

    # Inter-operator variance: variance of per-operator mean waits
    inter = statistics.variance(means) if len(means) > 1 else 0.0

    return intra + inter


def score_overall_time(result: ScheduleResult) -> float:
    """
    Total trip duration across the whole network should be low.
    Minimising this pushes toward less total waiting and more direct routes.
    """
    return sum(bt.total_trip_min for bt in result.bus_timelines)


# ---------------------------------------------------------------------------
# Rule Registry
# ---------------------------------------------------------------------------

SOFT_RULES: list[Rule] = [
    Rule("individual_wait",   score_individual_wait,   "soft"),
    Rule("operator_fairness", score_operator_fairness, "soft"),
    Rule("overall_time",      score_overall_time,      "soft"),
    # ── Add new soft rules here ──────────────────────────────────────────
    # Example:
    # Rule("priority_delay", score_priority_delay, "soft"),
    # Rule("electricity_cost", score_electricity_cost, "soft"),
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_weighted_score(
    result: ScheduleResult,
    weights_obj,
    rules: list[Rule] | None = None,
) -> tuple[float, dict[str, float]]:
    """
    Compute the weighted sum score for a ScheduleResult.

    Returns:
        (total_score, breakdown)
        breakdown: {rule_name: raw_score_before_weighting}
    """
    if rules is None:
        rules = SOFT_RULES

    breakdown: dict[str, float] = {}
    total = 0.0

    for rule in rules:
        if rule.rule_type != "soft":
            continue
        raw = rule.evaluate(result)
        weight = weights_obj.get(rule.name, 0.0)
        breakdown[rule.name] = raw
        total += weight * raw

    return total, breakdown
