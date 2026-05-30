"""
Bus Charging Scheduler — Streamlit App

Pick a scenario → see the input → see what the scheduler decided.

Layout:
  1. Sidebar: scenario selector
  2. Main area:
     Tab 1 — Scenario Input (route, buses, weights)
     Tab 2 — Per-Bus Timetable
     Tab 3 — Per-Station View
     Tab 4 — Schedule Score & Validation
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from scheduler import load_all_scenarios, run_scheduler, validate

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Custom CSS
# ---------------------------------------------------------------------------

st.markdown("""
<style>
    .main { background-color: #0f1117; }
    .block-container { padding-top: 4rem; }
    .stTabs [data-baseweb="tab"] {
        font-size: 0.95rem;
        font-weight: 600;
        padding: 0.5rem 1.2rem;
    }
    .metric-card {
        background: #1e2130;
        border-radius: 10px;
        padding: 1rem 1.2rem;
        border-left: 4px solid #4CAF50;
        margin-bottom: 0.5rem;
    }
    .metric-card.warn {
        border-left-color: #FF5252;
    }
    .event-travel  { color: #64B5F6; }
    .event-charge  { color: #81C784; }
    .event-wait    { color: #FFB74D; }
    .event-depart  { color: #CE93D8; }
    .event-arrive  { color: #CE93D8; }
    .tag-kpn       { background: #1565C0; color: white; border-radius:4px; padding:2px 7px; font-size:0.8rem; }
    .tag-freshbus  { background: #2E7D32; color: white; border-radius:4px; padding:2px 7px; font-size:0.8rem; }
    .tag-flixbus   { background: #6A1B9A; color: white; border-radius:4px; padding:2px 7px; font-size:0.8rem; }
    .tag-other     { background: #424242; color: white; border-radius:4px; padding:2px 7px; font-size:0.8rem; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Load scenarios (cached)
# ---------------------------------------------------------------------------

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

@st.cache_resource
def load_scenarios():
    return load_all_scenarios(SCENARIOS_DIR)

scenarios = load_scenarios()
scenario_map = {s.name: s for s in scenarios}

# ---------------------------------------------------------------------------
# Sidebar — scenario selector
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚡ Bus Charging Scheduler")
    st.markdown("---")
    selected_name = st.selectbox(
        "Select Scenario",
        options=list(scenario_map.keys()),
        index=0,
    )
    scenario = scenario_map[selected_name]
    st.markdown("---")
    st.markdown(f"**{scenario.description}**")
    st.markdown("---")
    st.markdown(f"🚌 **{len(scenario.buses)} buses**")
    st.markdown(f"⚡ **{len(scenario.charging_stations)} stations**")
    operators = sorted(set(b.operator for b in scenario.buses))
    st.markdown(f"🏢 **{', '.join(operators)}**")

# ---------------------------------------------------------------------------
# Run scheduler (cached per scenario id)
# ---------------------------------------------------------------------------

@st.cache_data
def run_and_validate(scenario_id: str):
    s = next(sc for sc in scenarios if sc.id == scenario_id)
    result = run_scheduler(s, refine=True)
    violations = validate(result)
    result.hard_violations = violations
    return result

with st.spinner("Running scheduler..."):
    result = run_and_validate(scenario.id)

# ---------------------------------------------------------------------------
# Helper: operator colour tag
# ---------------------------------------------------------------------------

def op_tag(operator: str) -> str:
    cls = f"tag-{operator.lower()}" if operator.lower() in ("kpn", "freshbus", "flixbus") else "tag-other"
    return f'<span class="{cls}">{operator}</span>'

# ---------------------------------------------------------------------------
# Main tabs
# ---------------------------------------------------------------------------

tab1, tab2, tab3, tab4 = st.tabs([
    "📋 Scenario Input",
    "🚌 Per-Bus Timetable",
    "⚡ Per-Station View",
    "📊 Score & Validation",
])

# ============================================================
# Tab 1 — Scenario Input
# ============================================================
with tab1:
    st.header(f"{scenario.name}")
    st.caption(scenario.description)

    col_route, col_weights = st.columns([2, 1])

    with col_route:
        st.subheader("Route")
        waypoints = scenario.route.waypoints
        route_rows = []
        for i in range(len(waypoints) - 1):
            a = waypoints[i]
            b = waypoints[i + 1]
            dist = b.distance_from_start_km - a.distance_from_start_km
            travel_min = dist / scenario.vehicle_defaults.speed_kmh * 60
            is_charging = b.name in scenario.charging_station_names()
            charger_info = ""
            if is_charging:
                station = scenario.charging_stations[b.name]
                n_chargers = len(station.chargers)
                charge_dur = station.chargers[0].charge_duration_min
                charger_info = f"{n_chargers} charger(s), {charge_dur:.0f} min"
            route_rows.append({
                "Segment": f"{a.name} → {b.name}",
                "Distance (km)": dist,
                "Travel Time (min)": f"{travel_min:.0f}",
                "Charging Station?": "✅ " + charger_info if is_charging else "—",
            })
        st.dataframe(pd.DataFrame(route_rows), use_container_width=True, hide_index=True)

    with col_weights:
        st.subheader("Optimization Weights")
        w = scenario.weights
        weight_rows = [
            {"Rule": "Individual Wait", "Weight": w.individual_wait},
            {"Rule": "Operator Fairness", "Weight": w.operator_fairness},
            {"Rule": "Overall Time", "Weight": w.overall_time},
        ]
        st.dataframe(pd.DataFrame(weight_rows), use_container_width=True, hide_index=True)
        st.caption(
            f"**Vehicle defaults:** {scenario.vehicle_defaults.battery_range_km:.0f} km range, "
            f"{scenario.vehicle_defaults.speed_kmh:.0f} km/h"
        )

    st.subheader("Buses")
    bus_rows = []
    for bus in sorted(scenario.buses, key=lambda b: (b.departure_time, b.id)):
        bus_rows.append({
            "Bus ID": bus.id,
            "Operator": bus.operator,
            "Direction": bus.direction,
            "Departure": bus.departure_time,
            "Battery (km)": bus.effective_range_km(scenario.vehicle_defaults),
            "Speed (km/h)": bus.effective_speed_kmh(scenario.vehicle_defaults),
        })
    st.dataframe(pd.DataFrame(bus_rows), use_container_width=True, hide_index=True)


# ============================================================
# Tab 2 — Per-Bus Timetable
# ============================================================
with tab2:
    st.header("Per-Bus Timetable")
    st.caption("For each bus, the complete journey: departure → charging stops → arrival.")

    # Filter controls
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        direction_filter = st.selectbox(
            "Direction",
            options=["All"] + sorted(set(bt.direction for bt in result.bus_timelines)),
        )
    with col_f2:
        operator_filter = st.selectbox(
            "Operator",
            options=["All"] + sorted(set(bt.operator for bt in result.bus_timelines)),
        )

    filtered = result.bus_timelines
    if direction_filter != "All":
        filtered = [bt for bt in filtered if bt.direction == direction_filter]
    if operator_filter != "All":
        filtered = [bt for bt in filtered if bt.operator == operator_filter]

    filtered = sorted(filtered, key=lambda bt: bt.scheduled_departure)

    for bt in filtered:
        plan_str = " → ".join([bt.origin] + bt.charging_plan + [bt.destination])
        with st.expander(
            f"**{bt.bus_id}** &nbsp;|&nbsp; {bt.operator} &nbsp;|&nbsp; "
            f"{bt.direction} &nbsp;|&nbsp; Departs {bt.departure_str} &nbsp;|&nbsp; "
            f"Arrives {bt.arrival_str} &nbsp;|&nbsp; Wait: {bt.total_wait_min:.0f} min",
            expanded=False,
        ):
            col_a, col_b, col_c, col_d = st.columns(4)
            col_a.metric("Total Trip", f"{bt.total_trip_min:.0f} min")
            col_b.metric("Total Wait", f"{bt.total_wait_min:.0f} min")
            col_c.metric("Total Charging", f"{bt.total_charge_min:.0f} min")
            col_d.metric("Charging Plan", ", ".join(bt.charging_plan))

            event_rows = []
            for e in bt.events:
                if e.event_type in ("depart", "arrive_destination"):
                    icon = "🟣"
                elif e.event_type == "travel":
                    icon = "🔵"
                elif e.event_type == "charge":
                    icon = "🟢"
                elif e.event_type == "wait":
                    icon = "🟡"
                else:
                    icon = "⚪"

                event_rows.append({
                    "": icon,
                    "Event": e.event_type.replace("_", " ").title(),
                    "Location": e.location,
                    "Start": e.start_str,
                    "End": e.end_str if e.duration_min > 0 else "—",
                    "Duration (min)": f"{e.duration_min:.0f}" if e.duration_min > 0 else "—",
                    "Range at End (km)": f"{e.range_at_end_km:.0f}",
                })

            st.dataframe(
                pd.DataFrame(event_rows),
                use_container_width=True,
                hide_index=True,
            )

    # Summary table
    st.markdown("---")
    st.subheader("Summary Table")
    summary_rows = []
    for bt in sorted(result.bus_timelines, key=lambda x: x.scheduled_departure):
        summary_rows.append({
            "Bus": bt.bus_id,
            "Operator": bt.operator,
            "Direction": bt.direction,
            "Departure": bt.departure_str,
            "Arrival": bt.arrival_str,
            "Charging Plan": " → ".join(bt.charging_plan),
            "Wait (min)": f"{bt.total_wait_min:.0f}",
            "Trip (min)": f"{bt.total_trip_min:.0f}",
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)


# ============================================================
# Tab 3 — Per-Station View
# ============================================================
with tab3:
    st.header("Per-Station Charging Queue")
    st.caption("For each station, the order in which buses charged — including wait times.")

    station_cols = st.columns(len(result.station_logs))

    for col, (station_name, log) in zip(
        station_cols, sorted(result.station_logs.items())
    ):
        events = log.sorted_events()
        with col:
            st.subheader(f"Station {station_name}")
            if not events:
                st.caption("No buses charged here.")
                continue

            for i, ev in enumerate(events):
                wait_color = "🟢" if ev.wait_duration_min == 0 else ("🟡" if ev.wait_duration_min < 30 else "🔴")
                st.markdown(
                    f"**{i+1}. {ev.bus_id}** ({ev.operator})<br>"
                    f"Direction: {ev.direction}<br>"
                    f"Arrived: {ev.arrival_str}<br>"
                    f"Charge: {ev.charge_start_str} → {ev.charge_end_str}<br>"
                    f"Wait: {wait_color} {ev.wait_duration_min:.0f} min",
                    unsafe_allow_html=True,
                )
                st.markdown("---")

    # Detailed table view
    st.subheader("All Charging Events")
    all_events = []
    for station_name, log in sorted(result.station_logs.items()):
        for ev in log.sorted_events():
            all_events.append({
                "Station": station_name,
                "Charger": ev.charger_id,
                "Order": "",
                "Bus": ev.bus_id,
                "Operator": ev.operator,
                "Direction": ev.direction,
                "Arrived": ev.arrival_str,
                "Wait (min)": f"{ev.wait_duration_min:.0f}",
                "Charge Start": ev.charge_start_str,
                "Charge End": ev.charge_end_str,
            })
    if all_events:
        df = pd.DataFrame(all_events)
        # Add order within station
        df["Order"] = df.groupby("Station").cumcount() + 1
        st.dataframe(df, use_container_width=True, hide_index=True)


# ============================================================
# Tab 4 — Score & Validation
# ============================================================
with tab4:
    st.header("Schedule Score & Validation")

    # Validation status
    if result.is_valid:
        st.success("✅ All hard constraints satisfied. Schedule is valid.")
    else:
        st.error(f"❌ {len(result.hard_violations)} hard constraint violation(s) found:")
        for v in result.hard_violations:
            st.markdown(f"- {v}")

    st.markdown("---")

    # Score breakdown
    st.subheader("Weighted Score Breakdown")
    w = scenario.weights
    score_rows = []
    rule_labels = {
        "individual_wait": "Individual Wait (Σ wait² per bus)",
        "operator_fairness": "Operator Fairness (intra + inter variance)",
        "overall_time": "Overall Trip Time (Σ trip duration)",
    }
    for rule_name, raw_score in result.score_breakdown.items():
        weight = w.get(rule_name)
        score_rows.append({
            "Rule": rule_labels.get(rule_name, rule_name),
            "Raw Score": f"{raw_score:,.1f}",
            "Weight": weight,
            "Weighted Score": f"{raw_score * weight:,.1f}",
        })
    st.dataframe(pd.DataFrame(score_rows), use_container_width=True, hide_index=True)

    col_total, _ = st.columns([1, 2])
    with col_total:
        st.metric("Total Weighted Score", f"{result.total_score:,.1f}", help="Lower is better")

    st.markdown("---")

    # Per-operator stats
    st.subheader("Per-Operator Statistics")
    op_stats: dict[str, list] = {}
    for bt in result.bus_timelines:
        op_stats.setdefault(bt.operator, []).append(bt)

    op_rows = []
    for op, timelines in sorted(op_stats.items()):
        waits = [bt.total_wait_min for bt in timelines]
        trips = [bt.total_trip_min for bt in timelines]
        op_rows.append({
            "Operator": op,
            "Buses": len(timelines),
            "Avg Wait (min)": f"{sum(waits)/len(waits):.1f}",
            "Max Wait (min)": f"{max(waits):.1f}",
            "Min Wait (min)": f"{min(waits):.1f}",
            "Avg Trip (min)": f"{sum(trips)/len(trips):.1f}",
        })
    st.dataframe(pd.DataFrame(op_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # Per-bus wait chart
    st.subheader("Wait Time per Bus")
    chart_data = pd.DataFrame([
        {"Bus": bt.bus_id, "Operator": bt.operator, "Wait (min)": bt.total_wait_min}
        for bt in sorted(result.bus_timelines, key=lambda x: x.scheduled_departure)
    ])
    st.bar_chart(chart_data.set_index("Bus")["Wait (min)"])
