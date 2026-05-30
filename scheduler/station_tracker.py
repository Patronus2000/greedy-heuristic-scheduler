"""
Station Tracker

Manages charger availability across all stations for the duration of a scenario.
When the scheduler commits a bus to a charging plan, it reserves the appropriate
charger slots here. Subsequent buses see updated availability when being evaluated.

Design: each station holds a per-charger list of (start, end) occupied intervals.
Finding the earliest available slot is a simple scan  adequate for
20–500 buses.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import ChargerEvent, Scenario, StationLog


class StationTracker:
    """
    Tracks charger occupancy across all charging stations.

    Usage:
        tracker = StationTracker(scenario)
        start, end, charger_id = tracker.earliest_slot("A", arrival_time, 25.0)
        tracker.reserve("A", charger_id, bus_id, operator, direction, arrival_time, start, end)
    """

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario
        # occupied: {station_name: {charger_id: list of (start, end) datetime pairs}}
        self._occupied: dict[str, dict[str, list[tuple[datetime, datetime]]]] = {}
        # station_logs: {station_name: StationLog}
        self._station_logs: dict[str, StationLog] = {}

        for station_name, station in scenario.charging_stations.items():
            self._occupied[station_name] = {c.id: [] for c in station.chargers}
            self._station_logs[station_name] = StationLog(station_name=station_name)

    def earliest_slot(
        self, station_name: str, arrival: datetime, duration_min: float
    ) -> tuple[datetime, datetime, str]:
        """
        Given that a bus arrives at `station_name` at `arrival`, find the
        charger that becomes available soonest and return:
          (charge_start, charge_end, charger_id)

        If the charger is free when the bus arrives, charge_start == arrival.
        Otherwise, charge_start is when the previous bus finishes.
        """
        best_start: datetime | None = None
        best_charger_id: str | None = None
        duration = timedelta(minutes=duration_min)

        chargers = self._scenario.charging_stations[station_name].chargers
        for charger in chargers:
            intervals = self._occupied[station_name][charger.id]
            # Find earliest time >= arrival when charger is free for `duration` minutes
            candidate_start = arrival
            for occ_start, occ_end in sorted(intervals):
                # If this interval overlaps our candidate window, push past it
                if occ_start < candidate_start + duration and occ_end > candidate_start:
                    candidate_start = occ_end

            if best_start is None or candidate_start < best_start:
                best_start = candidate_start
                best_charger_id = charger.id

        charge_end = best_start + duration
        return best_start, charge_end, best_charger_id

    def charge_duration_at(self, station_name: str, charger_id: str) -> float:
        """Return the configured charge duration for a specific charger."""
        chargers = self._scenario.charging_stations[station_name].chargers
        for c in chargers:
            if c.id == charger_id:
                return c.charge_duration_min
        return 25.0  # fallback

    def reserve(
        self,
        station_name: str,
        charger_id: str,
        bus_id: str,
        operator: str,
        direction: str,
        arrival: datetime,
        charge_start: datetime,
        charge_end: datetime,
    ) -> None:
        """Commit a charging slot for a bus. Also records the event in the station log."""
        self._occupied[station_name][charger_id].append((charge_start, charge_end))
        wait_min = (charge_start - arrival).total_seconds() / 60.0

        event = ChargerEvent(
            charger_id=charger_id,
            bus_id=bus_id,
            operator=operator,
            direction=direction,
            arrival_time=arrival,
            wait_duration_min=wait_min,
            charge_start_time=charge_start,
            charge_end_time=charge_end,
        )
        self._station_logs[station_name].charger_events.append(event)

    @property
    def station_logs(self) -> dict[str, StationLog]:
        return self._station_logs
