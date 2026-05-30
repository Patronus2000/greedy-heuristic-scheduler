"""
Scheduler Engine

Implements the greedy constructive heuristic with 2-pass refinement.

Pass 1 — Greedy Assignment:
  Buses are processed in departure-time order (ties broken by priority desc,
  then bus_id). For each bus, all valid charging plans are simulated using the
  current state of the station timelines. The plan with the lowest weighted
  score is selected and committed.

Pass 2 — Refinement (optional, enabled by default):
  After all buses are assigned, each bus's plan is reconsidered one at a time
  while holding all others fixed. If swapping to a different plan improves the
  global score, the swap is accepted. One full sweep is performed.

Why greedy + one refinement pass?
  - The decision space is small (≤8 plans per bus, ≤20 buses per scenario).
  - Greedy is fast, deterministic, and easy to explain.
  - One refinement pass catches the most common greedy failure mode: early buses
    block chargers in a way that hurts later buses, but a different plan for the
    early bus would have been almost as good for it while helping later buses.
  - This is significantly better than pure greedy with minimal added complexity.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Callable

from .models import (
    Bus,
    BusTimeline,
    ScheduleResult,
    Scenario,
    TimelineEvent,
    Weights,
)
from .planner import enumerate_valid_plans
from .rules import SOFT_RULES, compute_weighted_score
from .station_tracker import StationTracker


def _parse_departure(bus: Bus, date: datetime) -> datetime:
    """Parse "HH:MM" departure time relative to a reference date."""
    h, m = map(int, bus.departure_time.split(":"))
    return date.replace(hour=h, minute=m, second=0, microsecond=0)


def simulate_bus(
    bus: Bus,
    charging_plan: list[str],
    scenario: Scenario,
    tracker: StationTracker,
    base_date: datetime,
) -> BusTimeline:
    """
    Simulate a bus's full journey with a given charging plan.
    Returns a BusTimeline with all events, times, and statistics.

    The tracker is READ-ONLY here — this function does not commit anything.
    Call tracker.reserve() separately to commit the plan.
    """
    route = scenario.route
    vehicle_config = scenario.vehicle_defaults
    speed = bus.effective_speed_kmh(vehicle_config)       # km/h
    max_range = bus.effective_range_km(vehicle_config)    # km

    current_time = _parse_departure(bus, base_date)
    current_range = max_range
    events: list[TimelineEvent] = []

    # Ordered stops: origin → [charging stations] → destination
    stops = [bus.origin] + charging_plan + [bus.destination]

    # Departure event
    events.append(TimelineEvent(
        event_type="depart",
        location=bus.origin,
        start_time=current_time,
        end_time=current_time,
        duration_min=0.0,
        range_at_start_km=current_range,
        range_at_end_km=current_range,
    ))

    for i in range(len(stops) - 1):
        from_stop = stops[i]
        to_stop = stops[i + 1]
        dist_km = route.segment_distance_km(from_stop, to_stop)
        travel_min = dist_km / speed * 60.0   # speed is km/h, convert to minutes
        range_after_travel = current_range - dist_km

        travel_start = current_time
        travel_end = current_time + timedelta(minutes=travel_min)

        events.append(TimelineEvent(
            event_type="travel",
            location=f"{from_stop} → {to_stop}",
            start_time=travel_start,
            end_time=travel_end,
            duration_min=travel_min,
            range_at_start_km=current_range,
            range_at_end_km=range_after_travel,
        ))

        current_time = travel_end
        current_range = range_after_travel

        is_charging_station = to_stop in scenario.charging_station_names()

        if is_charging_station and i < len(stops) - 2:
            # Arrive at charging station
            arrival = current_time
            station = scenario.charging_stations[to_stop]
            charge_duration = station.chargers[0].charge_duration_min  # will be overridden by tracker

            charge_start, charge_end, charger_id = tracker.earliest_slot(
                to_stop, arrival, charge_duration
            )
            wait_min = (charge_start - arrival).total_seconds() / 60.0

            events.append(TimelineEvent(
                event_type="arrive_station",
                location=to_stop,
                start_time=arrival,
                end_time=arrival,
                duration_min=0.0,
                range_at_start_km=current_range,
                range_at_end_km=current_range,
            ))

            if wait_min > 0:
                events.append(TimelineEvent(
                    event_type="wait",
                    location=to_stop,
                    start_time=arrival,
                    end_time=charge_start,
                    duration_min=wait_min,
                    range_at_start_km=current_range,
                    range_at_end_km=current_range,
                ))

            actual_charge_duration = (charge_end - charge_start).total_seconds() / 60.0
            events.append(TimelineEvent(
                event_type="charge",
                location=to_stop,
                start_time=charge_start,
                end_time=charge_end,
                duration_min=actual_charge_duration,
                range_at_start_km=current_range,
                range_at_end_km=max_range,
            ))

            current_time = charge_end
            current_range = max_range

        elif not is_charging_station or i == len(stops) - 2:
            # Final destination
            events.append(TimelineEvent(
                event_type="arrive_destination",
                location=to_stop,
                start_time=current_time,
                end_time=current_time,
                duration_min=0.0,
                range_at_start_km=current_range,
                range_at_end_km=current_range,
            ))

    arrival_time = current_time
    departure_time = _parse_departure(bus, base_date)
    total_trip_min = (arrival_time - departure_time).total_seconds() / 60.0
    total_wait_min = sum(e.duration_min for e in events if e.event_type == "wait")
    total_charge_min = sum(e.duration_min for e in events if e.event_type == "charge")

    return BusTimeline(
        bus_id=bus.id,
        operator=bus.operator,
        origin=bus.origin,
        destination=bus.destination,
        direction=bus.direction,
        scheduled_departure=departure_time,
        arrival_time=arrival_time,
        total_trip_min=total_trip_min,
        total_wait_min=total_wait_min,
        total_charge_min=total_charge_min,
        charging_plan=charging_plan,
        events=events,
    )


def _commit_bus(
    bus: Bus,
    timeline: BusTimeline,
    scenario: Scenario,
    tracker: StationTracker,
) -> None:
    """
    Commit a bus's charging events to the station tracker.
    Called after the best plan has been selected.
    """
    for event in timeline.events:
        if event.event_type == "charge":
            station_name = event.location
            charge_start = event.start_time
            charge_end = event.end_time

            # Find which charger was selected (re-query tracker to get charger_id)
            duration_min = event.duration_min
            arrival_events = [
                e for e in timeline.events
                if e.event_type in ("arrive_station", "wait")
                and e.location == station_name
            ]
            # The arrival is the event immediately before the first wait or charge at this station
            if arrival_events:
                arrival = arrival_events[0].start_time
            else:
                arrival = charge_start

            # Find the charger ID from the tracker's occupied data
            # (we need to match the slot we reserved — find the charger that has this interval)
            charger_id = _find_charger_for_slot(tracker, station_name, charge_start, charge_end)

            tracker.reserve(
                station_name=station_name,
                charger_id=charger_id,
                bus_id=bus.id,
                operator=bus.operator,
                direction=bus.direction,
                arrival=arrival,
                charge_start=charge_start,
                charge_end=charge_end,
            )


def _find_charger_for_slot(
    tracker: StationTracker, station_name: str, charge_start: datetime, charge_end: datetime
) -> str:
    """Find the charger ID that would have been allocated for this specific slot."""
    # Re-query which charger is available earliest at charge_start time
    chargers = tracker._scenario.charging_stations[station_name].chargers
    for charger in chargers:
        intervals = tracker._occupied[station_name][charger.id]
        # Check if this charger is free at charge_start
        free = True
        for occ_start, occ_end in intervals:
            if occ_start < charge_end and occ_end > charge_start:
                free = False
                break
        if free:
            return charger.id
    # Fallback: first charger
    return chargers[0].id


def _build_partial_result(
    scenario: Scenario,
    timelines: list[BusTimeline],
    tracker: StationTracker,
) -> ScheduleResult:
    """Build a ScheduleResult from the current state for scoring purposes."""
    return ScheduleResult(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        bus_timelines=timelines,
        station_logs=tracker.station_logs,
        total_score=0.0,
        score_breakdown={},
        hard_violations=[],
    )


# ---------------------------------------------------------------------------
# Greedy Scheduler
# ---------------------------------------------------------------------------

def _processing_order(buses: list[Bus]) -> list[Bus]:
    """
    Sort buses for greedy processing.
    Primary: departure_time (earliest first).
    Secondary: priority (highest first — higher priority buses processed before
                         same-time lower-priority buses so they get better slots).
    Tertiary: bus_id (deterministic tiebreaker).
    """
    return sorted(
        buses,
        key=lambda b: (b.departure_time, -b.priority, b.id)
    )


def run_scheduler(
    scenario: Scenario,
    hard_rule_filters: list[Callable] | None = None,
    refine: bool = True,
) -> ScheduleResult:
    """
    Main entry point. Runs the greedy scheduler with optional 2-pass refinement.

    Args:
        scenario: the loaded and validated scenario
        hard_rule_filters: optional list of (plan, bus) -> bool callables
        refine: if True, run a second pass to improve the greedy assignment
    """
    base_date = datetime(2024, 1, 1)  # arbitrary reference date for time arithmetic

    # Pass 1: Greedy assignment
    tracker = StationTracker(scenario)
    assigned_timelines: dict[str, BusTimeline] = {}  # bus_id -> BusTimeline

    ordered_buses = _processing_order(scenario.buses)

    for bus in ordered_buses:
        valid_plans = enumerate_valid_plans(scenario, bus, hard_rule_filters)

        if not valid_plans:
            # Fallback: no valid plan (shouldn't happen with correct data)
            raise ValueError(
                f"No valid charging plan found for {bus.id} "
                f"({bus.origin} → {bus.destination}). "
                f"Check route distances and battery range."
            )

        best_plan = None
        best_score = float("inf")
        best_timeline = None

        for plan in valid_plans:
            timeline = simulate_bus(bus, plan, scenario, tracker, base_date)
            # Build a partial result with current committed buses + this candidate
            current_timelines = list(assigned_timelines.values()) + [timeline]
            partial = _build_partial_result(scenario, current_timelines, tracker)
            score, _ = compute_weighted_score(partial, scenario.weights)

            if score < best_score:
                best_score = score
                best_plan = plan
                best_timeline = timeline

        # Commit the best plan
        _commit_bus(bus, best_timeline, scenario, tracker)
        assigned_timelines[bus.id] = best_timeline

    # Pass 2: Refinement sweep
    if refine:
        improved = True
        max_iterations = 3  # avoid infinite loops (convergence is usually 1 pass)
        iteration = 0
        while improved and iteration < max_iterations:
            improved = False
            iteration += 1
            for bus in ordered_buses:
                # Try all plans for this bus, holding everything else fixed
                current_timeline = assigned_timelines[bus.id]

                # Build a tracker WITHOUT this bus's committed slots
                temp_tracker = _tracker_without_bus(scenario, assigned_timelines, bus.id, base_date)

                valid_plans = enumerate_valid_plans(scenario, bus, hard_rule_filters)
                current_score, _ = compute_weighted_score(
                    _build_partial_result(scenario, list(assigned_timelines.values()), tracker),
                    scenario.weights,
                )

                for plan in valid_plans:
                    candidate_timeline = simulate_bus(bus, plan, scenario, temp_tracker, base_date)
                    test_timelines = {
                        bid: tl for bid, tl in assigned_timelines.items() if bid != bus.id
                    }
                    test_timelines[bus.id] = candidate_timeline
                    # Build a full temporary tracker for scoring
                    temp_result = _build_partial_result(
                        scenario, list(test_timelines.values()), temp_tracker
                    )
                    candidate_score, _ = compute_weighted_score(temp_result, scenario.weights)

                    if candidate_score < current_score - 0.01:  # small epsilon to avoid spurious swaps
                        # Accept the swap
                        assigned_timelines[bus.id] = candidate_timeline
                        current_score = candidate_score
                        improved = True

            if improved:
                # Rebuild the tracker from scratch with the refined assignments
                tracker = _rebuild_tracker(scenario, assigned_timelines, base_date)

    # Final scoring
    all_timelines = list(assigned_timelines.values())
    final_result = _build_partial_result(scenario, all_timelines, tracker)
    total_score, breakdown = compute_weighted_score(final_result, scenario.weights)

    return ScheduleResult(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        bus_timelines=all_timelines,
        station_logs=tracker.station_logs,
        total_score=total_score,
        score_breakdown=breakdown,
        hard_violations=[],  # populated by validator
    )


def _tracker_without_bus(
    scenario: Scenario,
    assigned_timelines: dict[str, BusTimeline],
    exclude_bus_id: str,
    base_date: datetime,
) -> StationTracker:
    """Build a fresh tracker with all buses except the excluded one committed."""
    tracker = StationTracker(scenario)
    for bus in scenario.buses:
        if bus.id == exclude_bus_id:
            continue
        if bus.id in assigned_timelines:
            _commit_bus(bus, assigned_timelines[bus.id], scenario, tracker)
    return tracker


def _rebuild_tracker(
    scenario: Scenario,
    assigned_timelines: dict[str, BusTimeline],
    base_date: datetime,
) -> StationTracker:
    """Rebuild the station tracker from scratch using the current assignments."""
    tracker = StationTracker(scenario)
    for bus in scenario.buses:
        if bus.id in assigned_timelines:
            _commit_bus(bus, assigned_timelines[bus.id], scenario, tracker)
    return tracker
