"""
Bus Charging Scheduler — Data Models

Input and output Pydantic models for the scheduler.
All input comes from scenario JSON files.
All output is computed by the engine and passed to the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


# ---------------------------------------------------------------------------
# Input Models (parsed from scenario JSON)
# ---------------------------------------------------------------------------

class Waypoint(BaseModel):
    """A named point along the route, defined by its distance from the start."""
    name: str
    distance_from_start_km: float


class Route(BaseModel):
    """
    An ordered sequence of waypoints defining a corridor.
    Segment distances are computed from waypoint distances — never stored
    separately — so adding or reordering a waypoint is a single data change.
    """
    id: str
    name: str
    waypoints: list[Waypoint]

    def segment_distance_km(self, from_name: str, to_name: str) -> float:
        """Return the distance between two consecutive waypoints by name."""
        names = [w.name for w in self.waypoints]
        i = names.index(from_name)
        j = names.index(to_name)
        return abs(self.waypoints[j].distance_from_start_km - self.waypoints[i].distance_from_start_km)

    def waypoint_names(self) -> list[str]:
        return [w.name for w in self.waypoints]

    def charging_waypoints_between(self, origin: str, destination: str, charging_station_names: set[str]) -> list[str]:
        """
        Return the ordered list of charging-capable waypoints between origin and
        destination (exclusive of endpoints), respecting direction of travel.
        """
        names = [w.name for w in self.waypoints]
        i = names.index(origin)
        j = names.index(destination)
        if i < j:
            segment = names[i+1:j]
        else:
            segment = list(reversed(names[j+1:i]))
        return [n for n in segment if n in charging_station_names]

    def distance_from_start(self, name: str) -> float:
        for w in self.waypoints:
            if w.name == name:
                return w.distance_from_start_km
        raise ValueError(f"Waypoint '{name}' not found in route.")


class Charger(BaseModel):
    """
    A single charging unit at a station.
    Defined as a list so stations can have multiple chargers with different speeds.
    """
    id: str
    charge_duration_min: float = 25.0
    # Future fields (already modelled, ignored if not present):
    # cooldown_min: float = 0.0
    # charger_type: str = "standard"   # "standard" | "fast"


class ChargingStation(BaseModel):
    """
    A physical charging station co-located at a route waypoint.
    Chargers are a list of objects so mixed fast/slow chargers are supported.
    """
    chargers: list[Charger]
    # Future fields:
    # availability_schedule: list[dict] = []   # time windows when station is open
    # cost_per_kwh: float | None = None        # for electricity cost rules


class VehicleConfig(BaseModel):
    """Default vehicle properties. Per-bus overrides are supported."""
    battery_range_km: float = 240.0
    speed_kmh: float = 60.0


class Weights(BaseModel):
    """
    Soft rule weights. Keys must match rule names in the rule registry.
    Adding a new rule = add a key here + add the rule function to rules.py.
    Changing a weight = change one number here.

    Uses extra="allow" so that new rule weights added to the JSON are
    preserved without requiring a code change to this class.
    """
    model_config = ConfigDict(extra="allow")

    individual_wait: float = 1.0
    operator_fairness: float = 1.0
    overall_time: float = 1.0

    def get(self, name: str, default: float = 0.0) -> float:
        """Look up a weight by rule name, checking both declared and extra fields."""
        # Check declared fields first
        val = getattr(self, name, None)
        if val is not None:
            return val
        # Check dynamically-added fields from JSON (e.g., "priority_delay": 2.0)
        if self.__pydantic_extra__ and name in self.__pydantic_extra__:
            return self.__pydantic_extra__[name]
        return default


class Bus(BaseModel):
    """
    A bus with its scheduled departure.
    vehicle_override lets individual buses deviate from scenario defaults
    (e.g., express buses with larger battery, different speed).
    """
    id: str
    operator: str                              # plain string — new operators need no code change
    origin: str
    destination: str
    departure_time: str                        # "HH:MM" format
    vehicle_override: Optional[VehicleConfig] = None
    # Future extensibility fields (safe to add to JSON, ignored until rules use them):
    priority: int = 0                          # 0 = normal, higher = more priority
    tags: list[str] = []                       # arbitrary categorisation ("express", "night", ...)
    # sla_max_delay_min: float | None = None   # hard deadline for arrival

    @property
    def direction(self) -> str:
        return f"{self.origin}→{self.destination}"

    def effective_range_km(self, defaults: VehicleConfig) -> float:
        if self.vehicle_override and self.vehicle_override.battery_range_km:
            return self.vehicle_override.battery_range_km
        return defaults.battery_range_km

    def effective_speed_kmh(self, defaults: VehicleConfig) -> float:
        if self.vehicle_override and self.vehicle_override.speed_kmh:
            return self.vehicle_override.speed_kmh
        return defaults.speed_kmh


class Scenario(BaseModel):
    """
    A fully self-contained scenario. Everything the scheduler needs is here.
    The 5 scenario JSON files each produce one of these.
    """
    id: str
    name: str
    description: str = ""
    route: Route
    charging_stations: dict[str, ChargingStation]   # keyed by waypoint name
    vehicle_defaults: VehicleConfig = VehicleConfig()
    weights: Weights = Weights()
    buses: list[Bus]

    def charging_station_names(self) -> set[str]:
        return set(self.charging_stations.keys())


# ---------------------------------------------------------------------------
# Output Models (produced by the engine, consumed by the UI)
# ---------------------------------------------------------------------------

@dataclass
class TimelineEvent:
    """
    A single step in a bus's journey.
    event_type: "depart" | "travel" | "arrive_station" | "wait" | "charge" | "arrive_destination"
    """
    event_type: str
    location: str
    start_time: datetime
    end_time: datetime
    duration_min: float
    range_at_start_km: float
    range_at_end_km: float

    @property
    def start_str(self) -> str:
        return self.start_time.strftime("%H:%M")

    @property
    def end_str(self) -> str:
        return self.end_time.strftime("%H:%M")


@dataclass
class BusTimeline:
    """Complete journey record for one bus."""
    bus_id: str
    operator: str
    origin: str
    destination: str
    direction: str
    scheduled_departure: datetime
    arrival_time: datetime
    total_trip_min: float
    total_wait_min: float
    total_charge_min: float
    charging_plan: list[str]            # ordered list of stations used, e.g. ["A", "C"]
    events: list[TimelineEvent] = field(default_factory=list)

    @property
    def arrival_str(self) -> str:
        return self.arrival_time.strftime("%H:%M")

    @property
    def departure_str(self) -> str:
        return self.scheduled_departure.strftime("%H:%M")


@dataclass
class ChargerEvent:
    """One bus's charging slot at one charger."""
    charger_id: str
    bus_id: str
    operator: str
    direction: str
    arrival_time: datetime
    wait_duration_min: float
    charge_start_time: datetime
    charge_end_time: datetime

    @property
    def arrival_str(self) -> str:
        return self.arrival_time.strftime("%H:%M")

    @property
    def charge_start_str(self) -> str:
        return self.charge_start_time.strftime("%H:%M")

    @property
    def charge_end_str(self) -> str:
        return self.charge_end_time.strftime("%H:%M")


@dataclass
class StationLog:
    """All charging events at one station, ordered by charge start time."""
    station_name: str
    charger_events: list[ChargerEvent] = field(default_factory=list)

    def sorted_events(self) -> list[ChargerEvent]:
        return sorted(self.charger_events, key=lambda e: e.charge_start_time)


@dataclass
class ScheduleResult:
    """The complete output of the scheduler for one scenario run."""
    scenario_id: str
    scenario_name: str
    bus_timelines: list[BusTimeline]
    station_logs: dict[str, StationLog]         # keyed by station name
    total_score: float
    score_breakdown: dict[str, float]           # per-rule scores before weighting
    hard_violations: list[str]                  # list of violation descriptions (empty = valid)

    @property
    def is_valid(self) -> bool:
        return len(self.hard_violations) == 0
