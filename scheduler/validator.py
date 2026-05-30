"""
Hard Constraint Validator

Post-hoc verification of a completed ScheduleResult.
Checks that all hard rules are satisfied and returns a list of violation strings.
An empty list means the schedule is valid.
"""
from __future__ import annotations

from .models import ScheduleResult


def validate(result: ScheduleResult) -> list[str]:
    """
    Validate a ScheduleResult against all hard constraints.
    Returns a list of violation descriptions. Empty = valid.
    """
    violations: list[str] = []

    # Hard Rule 1: Range constraint
    # No leg between consecutive charges (or start/end) exceeds battery range
    violations.extend(_check_range(result))

    # Hard Rule 2: No charger overlap
    # No two buses use the same charger at overlapping times
    violations.extend(_check_charger_overlap(result))

    # Hard Rule 3: Route order (no backtracking)
    violations.extend(_check_route_order(result))

    return violations


def _check_range(result: ScheduleResult) -> list[str]:
    violations = []
    for bt in result.bus_timelines:
        for event in bt.events:
            if event.event_type == "travel":
                range_start = event.range_at_start_km
                range_end = event.range_at_end_km
                distance = range_start - range_end
                if range_end < 0:
                    violations.append(
                        f"{bt.bus_id}: Range violated on leg '{event.location}'. "
                        f"Started with {range_start:.1f} km range, needed {distance:.1f} km."
                    )
    return violations


def _check_charger_overlap(result: ScheduleResult) -> list[str]:
    violations = []
    for station_name, log in result.station_logs.items():
        by_charger: dict[str, list] = {}
        for event in log.charger_events:
            by_charger.setdefault(event.charger_id, []).append(event)

        for charger_id, events in by_charger.items():
            sorted_events = sorted(events, key=lambda e: e.charge_start_time)
            for i in range(len(sorted_events) - 1):
                a = sorted_events[i]
                b = sorted_events[i + 1]
                if a.charge_end_time > b.charge_start_time:
                    violations.append(
                        f"Charger overlap at {station_name}/{charger_id}: "
                        f"{a.bus_id} ends at {a.charge_end_str}, "
                        f"{b.bus_id} starts at {b.charge_start_str}."
                    )
    return violations


def _check_route_order(result: ScheduleResult) -> list[str]:
    """Check that charge events occur in strict route order (no backtracking)."""
    violations = []
    for bt in result.bus_timelines:
        charge_events = [e for e in bt.events if e.event_type == "charge"]
        for i in range(len(charge_events) - 1):
            a = charge_events[i]
            b = charge_events[i + 1]
            if a.start_time >= b.start_time:
                violations.append(
                    f"{bt.bus_id}: Charging order violation — "
                    f"charged at {a.location} at {a.start_str} then {b.location} at {b.start_str}."
                )
    return violations
