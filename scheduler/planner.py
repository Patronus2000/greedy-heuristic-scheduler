"""
Charging Plan Enumerator

Generates all valid subsets of charging stations for a bus's journey.
A plan is valid if no leg between consecutive charges (or start/end) exceeds
the bus's effective battery range.

The enumerator is purely data-driven — it reads distances from the Route model
and has no hardcoded station names or segment values. Adding a new station to
the route JSON automatically expands the plans considered here.
"""
from __future__ import annotations

from itertools import combinations
from typing import Callable

from .models import Bus, Route, Scenario, VehicleConfig


def _legs_for_plan(
    stations_in_order: list[str],
    charging_plan: list[str],
    route: Route,
    origin: str,
    destination: str,
) -> list[float]:
    """
    Compute the distance of each leg for a given charging plan.
    Legs are: origin → first_charge, then between charges, then last_charge → destination.
    """
    stops = [origin] + list(charging_plan) + [destination]
    legs = []
    for i in range(len(stops) - 1):
        legs.append(route.segment_distance_km(stops[i], stops[i + 1]))
    return legs


def enumerate_valid_plans(
    scenario: Scenario,
    bus: Bus,
    hard_rule_filters: list[Callable[[list[str], Bus], bool]] | None = None,
) -> list[list[str]]:
    """
    Return all valid charging plans for a bus, given the scenario's route and
    vehicle config. A plan is a list of station names the bus will charge at,
    in route order.

    hard_rule_filters: optional list of callables (plan, bus) -> bool.
    A plan is kept only if ALL filters return True.
    """
    route = scenario.route
    vehicle_config = scenario.vehicle_defaults
    max_range = bus.effective_range_km(vehicle_config)

    # Charging stations available on this route (as a set for quick lookup)
    station_names = scenario.charging_station_names()

    # Ordered list of intermediate charging stations between origin and destination
    ordered_stations = route.charging_waypoints_between(
        bus.origin, bus.destination, station_names
    )

    valid_plans: list[list[str]] = []

    # A bus needs at least 2 charges for a 540 km trip with 240 km range.
    # We enumerate from 1 upward (letting the range check eliminate impossible plans)
    # so that this function generalises to any route length / battery size.
    for r in range(1, len(ordered_stations) + 1):
        for combo in combinations(ordered_stations, r):
            legs = _legs_for_plan(ordered_stations, list(combo), route, bus.origin, bus.destination)
            if all(leg <= max_range for leg in legs):
                plan = list(combo)
                # Apply any hard rule filters
                if hard_rule_filters:
                    if not all(f(plan, bus) for f in hard_rule_filters):
                        continue
                valid_plans.append(plan)

    return valid_plans
