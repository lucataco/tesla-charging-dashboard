#!/usr/bin/env python3
"""Tesla Charging Dashboard — fetch data & render self-contained HTML."""

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import teslapy

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
CACHE_FILE = ROOT / "cache.json"


# ── Tesla API helpers ────────────────────────────────────────────────────────
def get_tesla(email: str) -> teslapy.Tesla:
    tesla = teslapy.Tesla(email, cache_file=str(CACHE_FILE))
    if not tesla.authorized:
        auth_url = tesla.authorization_url()
        print("Opening Tesla login in your browser …")
        webbrowser.open(auth_url)
        print(f"\nIf it didn't open, go to:\n{auth_url}\n")
        tesla.fetch_token(authorization_response=input("After login, paste the redirect URL here: "))
    return tesla


def fetch_v1(tesla: teslapy.Tesla) -> dict | None:
    """Last 31 days of daily aggregated charging."""
    vehicles = tesla.vehicle_list()
    if not vehicles:
        print("No vehicles found.")
        return None
    v = vehicles[0]
    try:
        data = v.get_charge_history()
    except teslapy.HTTPError as e:
        print(f"V1 charge_history failed ({e}); skipping.")
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = DATA_DIR / f"v1_charge_history_{ts}.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"Saved V1 → {path}")
    return data


def fetch_v2(tesla: teslapy.Tesla) -> dict | None:
    """Per-session Supercharger history with pagination to get all sessions."""
    vehicles = tesla.vehicle_list()
    if not vehicles:
        return None
    vin = vehicles[0]["vin"]
    url = "https://ownership.tesla.com/mobile-app/charging/history"
    all_sessions = []
    offset = 0
    page_size = 50
    while True:
        params = {
            "vin": vin,
            "deviceLanguage": "en",
            "deviceCountry": "US",
            "operationName": "getChargingHistoryV2",
            "offset": offset,
            "pageSize": page_size,
        }
        try:
            resp = tesla.get(url, params=params)
        except Exception as e:
            print(f"V2 page offset={offset} failed ({e}); stopping pagination.")
            break
        data = resp.get("data", resp) if isinstance(resp, dict) else resp
        sessions = data if isinstance(data, list) else data.get("data", [])
        if not sessions:
            break
        print(f"  V2 page offset={offset}: {len(sessions)} sessions")
        all_sessions.extend(sessions)
        if len(sessions) < page_size:
            break
        offset += page_size
    if not all_sessions:
        # Fall back to the built-in method
        try:
            v = vehicles[0]
            all_sessions = v.get_charge_history_v2()
            if isinstance(all_sessions, dict):
                all_sessions = all_sessions.get("data", [])
        except Exception as e:
            print(f"V2 fallback failed ({e}); skipping.")
            return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = DATA_DIR / f"v2_charge_history_{ts}.json"
    path.write_text(json.dumps(all_sessions, indent=2))
    print(f"Saved V2 → {path} ({len(all_sessions)} total sessions)")
    return all_sessions


def load_latest(prefix: str) -> dict | list | None:
    files = sorted(DATA_DIR.glob(f"{prefix}_*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text())




# ── DataFrame builders ───────────────────────────────────────────────────────
def build_v1_df(v1: dict) -> pd.DataFrame:
    """Parse V1 daily charging records.

    Structure: charging_history_graph.data_points[] with:
      - timestamp.timestamp.seconds (epoch)
      - values[0]=total, [1]=home, [2]=supercharger, [3]=other
      - raw_value in Wh (divide by 1000 for kWh)
    """
    rows = []
    graph = v1.get("charging_history_graph", {})
    data_points = graph.get("data_points", []) if isinstance(graph, dict) else []
    for dp in data_points:
        ts_obj = dp.get("timestamp", {})
        epoch = ts_obj.get("timestamp", {}).get("seconds")
        if not epoch:
            continue
        date = datetime.fromtimestamp(epoch, tz=timezone.utc).date()
        vals = dp.get("values", [])
        # raw_value is in Wh; fall back to 0
        total = float(vals[0].get("raw_value", 0) or 0) / 1000.0 if len(vals) > 0 else 0
        home = float(vals[1].get("raw_value", 0) or 0) / 1000.0 if len(vals) > 1 else 0
        sc = float(vals[2].get("raw_value", 0) or 0) / 1000.0 if len(vals) > 2 else 0
        other = float(vals[3].get("raw_value", 0) or 0) / 1000.0 if len(vals) > 3 else 0
        rows.append({
            "date": date,
            "total_kwh": total,
            "home_kwh": home,
            "supercharger_kwh": sc,
            "other_kwh": other,
        })
    return pd.DataFrame(rows)


def build_v2_df(v2: dict | list) -> pd.DataFrame:
    """Parse V2 per-session Supercharger records.

    Structure: flat list of session objects. Energy/cost live inside fees[].
      - siteLocationName for location
      - chargeStartDateTime / chargeStopDateTime for times
      - fees[].feeType=="CHARGING" → usageBase (kWh), totalDue ($)
    """
    rows = []
    sessions = v2 if isinstance(v2, list) else v2.get("data", [])
    for s in sessions:
        started = s.get("chargeStartDateTime")
        stopped = s.get("chargeStopDateTime")
        if not started:
            continue
        # Calculate duration from start/stop
        dur_min = 0.0
        if started and stopped:
            t0 = pd.to_datetime(started)
            t1 = pd.to_datetime(stopped)
            dur_min = (t1 - t0).total_seconds() / 60.0
        # Extract energy and cost from fees
        energy = 0.0
        cost = 0.0
        for fee in s.get("fees", []):
            if fee.get("feeType") == "CHARGING":
                energy = float(fee.get("usageBase", 0) or 0)
                cost = float(fee.get("totalDue", 0) or 0)
                break
        loc = s.get("siteLocationName") or "Unknown"
        rows.append({
            "start": pd.to_datetime(started, utc=True),
            "duration_min": dur_min,
            "kwh": energy,
            "cost": cost,
            "location": loc,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df.sort_values("start", inplace=True)
        df.reset_index(drop=True, inplace=True)
    return df


# ── Chart builders ───────────────────────────────────────────────────────────
COLORS = {
    "bg": "#111217",
    "card": "#1a1b23",
    "text": "#e0e0e0",
    "accent": "#3e6ae1",
    "green": "#22c55e",
    "red": "#ef4444",
    "orange": "#f59e0b",
    "purple": "#a855f7",
    "teal": "#14b8a6",
    "grid": "#2a2b35",
}

LAYOUT_DEFAULTS = dict(
    paper_bgcolor=COLORS["bg"],
    plot_bgcolor=COLORS["card"],
    font=dict(color=COLORS["text"], family="Inter, -apple-system, sans-serif", size=13),
    margin=dict(l=50, r=30, t=50, b=50),
    xaxis=dict(gridcolor=COLORS["grid"], zerolinecolor=COLORS["grid"]),
    yaxis=dict(gridcolor=COLORS["grid"], zerolinecolor=COLORS["grid"]),
)


def _apply_layout(fig, title: str, **kw):
    merged = {**LAYOUT_DEFAULTS, **kw}
    fig.update_layout(title=dict(text=title, font=dict(size=16)), **merged)
    return fig


def chart_daily_bar(df: pd.DataFrame) -> str:
    fig = go.Figure()
    if "home_kwh" in df.columns:
        fig.add_trace(go.Bar(x=df["date"], y=df["home_kwh"], name="Home", marker_color=COLORS["green"]))
        fig.add_trace(go.Bar(x=df["date"], y=df["supercharger_kwh"], name="Supercharger", marker_color=COLORS["accent"]))
        fig.add_trace(go.Bar(x=df["date"], y=df["other_kwh"], name="Other", marker_color=COLORS["orange"]))
        fig.update_layout(barmode="stack")
    else:
        fig.add_trace(go.Bar(x=df["date"], y=df["total_kwh"], name="Total kWh", marker_color=COLORS["accent"]))
    _apply_layout(fig, "Daily Charging (kWh)", xaxis_title="Date", yaxis_title="kWh")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_source_donut(df: pd.DataFrame) -> str:
    labels, values, colors = [], [], []
    for col, label, color in [
        ("home_kwh", "Home", COLORS["green"]),
        ("supercharger_kwh", "Supercharger", COLORS["accent"]),
        ("other_kwh", "Other", COLORS["orange"]),
    ]:
        total = df[col].sum() if col in df.columns else 0
        if total > 0:
            labels.append(label)
            values.append(round(total, 1))
            colors.append(color)
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.55, marker=dict(colors=colors),
                           textinfo="label+percent", textfont=dict(color="white")))
    _apply_layout(fig, "Charging Source Breakdown")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_energy_timeline(df: pd.DataFrame) -> str:
    fig = go.Figure(go.Scatter(x=df["start"], y=df["kwh"], mode="lines+markers",
                               marker=dict(size=7, color=COLORS["accent"]),
                               line=dict(color=COLORS["accent"], width=2),
                               hovertemplate="%{x|%b %d %H:%M}<br>%{y:.1f} kWh<extra></extra>"))
    _apply_layout(fig, "Energy per Supercharger Session", xaxis_title="Date", yaxis_title="kWh")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_cost_timeline(df: pd.DataFrame) -> str:
    fig = go.Figure(go.Scatter(x=df["start"], y=df["cost"], mode="lines+markers",
                               marker=dict(size=7, color=COLORS["green"]),
                               line=dict(color=COLORS["green"], width=2),
                               hovertemplate="%{x|%b %d %H:%M}<br>$%{y:.2f}<extra></extra>"))
    _apply_layout(fig, "Cost per Supercharger Session", xaxis_title="Date", yaxis_title="Cost ($)")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_duration_histogram(df: pd.DataFrame) -> str:
    fig = go.Figure(go.Histogram(x=df["duration_min"], nbinsx=20, marker_color=COLORS["purple"],
                                 hovertemplate="%{x:.0f} min<br>%{y} sessions<extra></extra>"))
    _apply_layout(fig, "Session Duration Distribution", xaxis_title="Minutes", yaxis_title="Sessions")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_cost_efficiency(df: pd.DataFrame) -> str:
    fig = go.Figure(go.Scatter(
        x=df["kwh"], y=df["cost"],
        mode="markers",
        marker=dict(size=df["duration_min"].clip(lower=5) / 2, color=df["duration_min"],
                    colorscale="Viridis", showscale=True,
                    colorbar=dict(title="Min"), opacity=0.8),
        text=df["location"],
        hovertemplate="%{text}<br>%{x:.1f} kWh · $%{y:.2f}<br>%{marker.size:.0f} min<extra></extra>",
    ))
    _apply_layout(fig, "Cost vs Energy (bubble = duration)", xaxis_title="kWh", yaxis_title="Cost ($)")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_top_locations(df: pd.DataFrame) -> str:
    grouped = df.groupby("location")["kwh"].sum().sort_values(ascending=True).tail(15)
    fig = go.Figure(go.Bar(x=grouped.values, y=grouped.index, orientation="h",
                           marker_color=COLORS["teal"],
                           hovertemplate="%{y}<br>%{x:.1f} kWh<extra></extra>"))
    _apply_layout(fig, "Top Supercharger Locations (by kWh)", xaxis_title="Total kWh",
                  margin=dict(l=200, r=30, t=50, b=50))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_heatmap(df: pd.DataFrame) -> str:
    df2 = df.copy()
    df2["hour"] = df2["start"].dt.hour
    df2["dow"] = df2["start"].dt.dayofweek
    pivot = df2.groupby(["dow", "hour"]).size().unstack(fill_value=0).reindex(
        index=range(7), columns=range(24), fill_value=0)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=list(range(24)), y=days,
        colorscale="Blues", hovertemplate="Hour %{x}:00 · %{y}<br>%{z} sessions<extra></extra>"))
    _apply_layout(fig, "Charging Time Heatmap", xaxis_title="Hour of Day", yaxis=dict(gridcolor=COLORS["grid"]))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_monthly_trend(df: pd.DataFrame) -> str:
    df2 = df.copy()
    df2["month"] = df2["start"].dt.tz_localize(None).dt.to_period("M").astype(str)
    monthly = df2.groupby("month").agg(kwh=("kwh", "sum"), cost=("cost", "sum")).reset_index()
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=monthly["month"], y=monthly["kwh"], name="kWh", marker_color=COLORS["accent"]),
                  secondary_y=False)
    fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["cost"], name="Cost ($)", mode="lines+markers",
                             marker=dict(size=8, color=COLORS["green"]),
                             line=dict(color=COLORS["green"], width=2)),
                  secondary_y=True)
    _apply_layout(fig, "Monthly Supercharger Spending")
    fig.update_yaxes(title_text="kWh", gridcolor=COLORS["grid"], secondary_y=False)
    fig.update_yaxes(title_text="Cost ($)", gridcolor=COLORS["grid"], secondary_y=True)
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ── V1-derived charts ────────────────────────────────────────────────────────
def chart_cumulative(df: pd.DataFrame) -> str:
    """Cumulative kWh over the 31-day period."""
    df2 = df.sort_values("date").copy()
    df2["cumulative"] = df2["total_kwh"].cumsum()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df2["date"], y=df2["cumulative"], mode="lines",
        fill="tozeroy", fillcolor="rgba(62,106,225,0.15)",
        line=dict(color=COLORS["accent"], width=2.5),
        hovertemplate="%{x|%b %d}<br>%{y:.1f} kWh cumulative<extra></extra>",
    ))
    _apply_layout(fig, "Cumulative Energy Charged", xaxis_title="Date", yaxis_title="kWh")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_dow_pattern(df: pd.DataFrame) -> str:
    """Average kWh by day of week."""
    df2 = df.copy()
    df2["dow"] = pd.to_datetime(df2["date"]).dt.dayofweek
    dow_avg = df2.groupby("dow")["total_kwh"].mean().reindex(range(7), fill_value=0)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    colors = [COLORS["accent"] if v == dow_avg.max() else COLORS["teal"] for v in dow_avg.values]
    fig = go.Figure(go.Bar(
        x=days, y=dow_avg.values, marker_color=colors,
        hovertemplate="%{x}<br>%{y:.1f} kWh avg<extra></extra>",
    ))
    _apply_layout(fig, "Average Daily Charging by Day of Week", xaxis_title="Day", yaxis_title="Avg kWh")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_rolling_avg(df: pd.DataFrame) -> str:
    """Daily kWh with 7-day rolling average overlay."""
    df2 = df.sort_values("date").copy()
    df2["rolling_7d"] = df2["total_kwh"].rolling(7, min_periods=1).mean()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df2["date"], y=df2["total_kwh"], name="Daily",
        marker_color="rgba(62,106,225,0.35)",
        hovertemplate="%{x|%b %d}<br>%{y:.1f} kWh<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df2["date"], y=df2["rolling_7d"], name="7-day avg",
        mode="lines", line=dict(color=COLORS["orange"], width=3),
        hovertemplate="%{x|%b %d}<br>%{y:.1f} kWh (7d avg)<extra></extra>",
    ))
    _apply_layout(fig, "Daily Charging with 7-Day Moving Average", xaxis_title="Date", yaxis_title="kWh")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_v1_heatmap(df: pd.DataFrame) -> str:
    """Heatmap of charging by day-of-week vs week number."""
    df2 = df.copy()
    df2["date_dt"] = pd.to_datetime(df2["date"])
    df2["dow"] = df2["date_dt"].dt.dayofweek
    df2["week"] = df2["date_dt"].dt.isocalendar().week.astype(int)
    pivot = df2.pivot_table(index="dow", columns="week", values="total_kwh", fill_value=0)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    week_labels = [f"W{w}" for w in pivot.columns]
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=week_labels, y=days,
        colorscale=[[0, COLORS["card"]], [0.5, COLORS["accent"]], [1, COLORS["green"]]],
        hovertemplate="%{y} %{x}<br>%{z:.1f} kWh<extra></extra>",
    ))
    _apply_layout(fig, "Charging Heatmap (Day vs Week)", xaxis_title="Week", yaxis=dict(gridcolor=COLORS["grid"]))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_source_stacked_area(df: pd.DataFrame) -> str:
    """Stacked area chart of charging sources over time."""
    df2 = df.sort_values("date").copy()
    fig = go.Figure()
    for col, name, color in [
        ("other_kwh", "Other", COLORS["orange"]),
        ("supercharger_kwh", "Supercharger", COLORS["accent"]),
        ("home_kwh", "Home", COLORS["green"]),
    ]:
        if col in df2.columns and df2[col].sum() > 0:
            fig.add_trace(go.Scatter(
                x=df2["date"], y=df2[col], name=name,
                mode="lines", stackgroup="one",
                line=dict(width=0.5, color=color),
                hovertemplate="%{x|%b %d}<br>%{y:.1f} kWh<extra></extra>",
            ))
    _apply_layout(fig, "Charging Sources Over Time", xaxis_title="Date", yaxis_title="kWh")
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ── HTML renderer ────────────────────────────────────────────────────────────
def render_dashboard(v1_df: pd.DataFrame | None, v2_df: pd.DataFrame | None) -> str:
    from plotly.offline import get_plotlyjs
    plotly_js = f'<script>{get_plotlyjs()}</script>'

    # Summary cards — built from V1 (31-day totals) + V2 (Supercharger detail)
    cards = []
    if v1_df is not None and not v1_df.empty:
        total_kwh = v1_df["total_kwh"].sum()
        daily_avg = v1_df["total_kwh"].mean()
        charging_days = (v1_df["total_kwh"] > 0).sum()
        peak_day = v1_df.loc[v1_df["total_kwh"].idxmax()]
        sc_kwh = v1_df["supercharger_kwh"].sum() if "supercharger_kwh" in v1_df.columns else 0
        cards.append(("Total (31d)", f"{total_kwh:,.1f} kWh"))
        cards.append(("Daily Avg", f"{daily_avg:,.1f} kWh"))
        cards.append(("Charging Days", f"{charging_days} / {len(v1_df)}"))
        cards.append(("Peak Day", f"{peak_day['total_kwh']:.1f} kWh"))
        cards.append(("Supercharger", f"{sc_kwh:,.1f} kWh"))
    if v2_df is not None and not v2_df.empty:
        total_cost = v2_df["cost"].sum()
        cards.append(("SC Cost", f"${total_cost:,.2f}"))
    cards_html = ""
    if cards:
        cards_html = '<div class="cards">' + "".join(
            f'<div class="card"><div class="card-label">{label}</div><div class="card-value">{val}</div></div>'
            for label, val in cards
        ) + "</div>"

    # Build chart sections
    sections = []

    if v1_df is not None and not v1_df.empty:
        sections.append(('<h2>Overview — Last 31 Days</h2>', "grid-2"))
        sections.append((chart_daily_bar(v1_df), None))
        sections.append((chart_source_donut(v1_df), None))

        sections.append(('<h2>Trends & Patterns</h2>', "grid-2"))
        sections.append((chart_cumulative(v1_df), None))
        sections.append((chart_rolling_avg(v1_df), None))
        sections.append((chart_dow_pattern(v1_df), None))
        sections.append((chart_source_stacked_area(v1_df), None))
        sections.append((chart_v1_heatmap(v1_df), "full"))

    if v2_df is not None and not v2_df.empty and len(v2_df) >= 2:
        sections.append(('<h2>Supercharger Analysis</h2>', "grid-2"))
        sections.append((chart_energy_timeline(v2_df), None))
        sections.append((chart_cost_timeline(v2_df), None))
        sections.append((chart_duration_histogram(v2_df), None))
        sections.append((chart_cost_efficiency(v2_df), None))
        sections.append((chart_top_locations(v2_df), "full"))
        sections.append(('<span></span>', "grid-2"))
        sections.append((chart_heatmap(v2_df), None))
        sections.append((chart_monthly_trend(v2_df), None))
    elif v2_df is not None and not v2_df.empty:
        # Single session — show a compact summary instead of sparse charts
        s = v2_df.iloc[0]
        sc_html = (f'<h2>Latest Supercharger Session</h2>'
                   f'<div class="cards" style="margin-top:12px">'
                   f'<div class="card"><div class="card-label">Location</div><div class="card-value">{s["location"]}</div></div>'
                   f'<div class="card"><div class="card-label">Date</div><div class="card-value">{s["start"].strftime("%b %d, %Y")}</div></div>'
                   f'<div class="card"><div class="card-label">Energy</div><div class="card-value">{s["kwh"]:.1f} kWh</div></div>'
                   f'<div class="card"><div class="card-label">Cost</div><div class="card-value">${s["cost"]:.2f}</div></div>'
                   f'<div class="card"><div class="card-label">Duration</div><div class="card-value">{s["duration_min"]:.0f} min</div></div>'
                   f'<div class="card"><div class="card-label">Rate</div><div class="card-value">${s["cost"]/s["kwh"]:.3f}/kWh</div></div>'
                   f'</div>')
        sections.append((sc_html, "full"))

    chart_blocks = []
    in_grid = False
    for content, flag in sections:
        if flag == "grid-2":
            if in_grid:
                chart_blocks.append("</div>")
            chart_blocks.append(content)  # heading
            chart_blocks.append('<div class="grid">')
            in_grid = True
        elif flag == "full":
            if in_grid:
                chart_blocks.append("</div>")
                in_grid = False
            chart_blocks.append(f'<div class="chart-full">{content}</div>')
        else:
            chart_blocks.append(f'<div class="chart">{content}</div>')
    if in_grid:
        chart_blocks.append("</div>")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tesla Charging Dashboard</title>
{plotly_js}
<style>
  :root {{ --bg: {COLORS['bg']}; --card: {COLORS['card']}; --text: {COLORS['text']};
           --accent: {COLORS['accent']}; --grid: {COLORS['grid']}; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text);
          font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif;
          padding: 24px; max-width: 1400px; margin: 0 auto; }}
  h1 {{ font-size: 1.8rem; margin-bottom: 4px; }}
  .subtitle {{ color: #888; font-size: 0.85rem; margin-bottom: 24px; }}
  h2 {{ font-size: 1.2rem; margin: 32px 0 12px; color: #ccc; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--card); border-radius: 10px; padding: 16px 20px;
           border: 1px solid var(--grid); }}
  .card-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em;
                 color: #888; margin-bottom: 4px; }}
  .card-value {{ font-size: 1.3rem; font-weight: 600; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .chart, .chart-full {{ background: var(--card); border-radius: 10px; padding: 12px;
                         border: 1px solid var(--grid); min-height: 340px; }}
  .chart-full {{ margin: 16px 0; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .footer {{ text-align: center; color: #555; font-size: 0.75rem; margin-top: 40px; }}
</style>
</head>
<body>
<h1>Tesla Charging Dashboard</h1>
<p class="subtitle">Generated {now}</p>
{cards_html}
{"".join(chart_blocks)}
<p class="footer">Data from Tesla API · Dashboard generated with Plotly</p>
</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Tesla Charging Dashboard")
    parser.add_argument("--email", help="Tesla account email")
    parser.add_argument("--offline", action="store_true", help="Render from cached data only")
    parser.add_argument("--discover", action="store_true", help="Dump raw API JSON and exit")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    v1_data, v2_data = None, None

    if args.offline:
        print("Offline mode — loading cached data …")
        v1_data = load_latest("v1_charge_history")
        v2_data = load_latest("v2_charge_history")
        if not v1_data and not v2_data:
            sys.exit("No cached data found in data/. Run online first.")
    else:
        if not args.email:
            sys.exit("Provide --email or use --offline.")
        tesla = get_tesla(args.email)
        v1_data = fetch_v1(tesla)
        v2_data = fetch_v2(tesla)

        if args.discover:
            print("\n─── V1 charge_history ───")
            print(json.dumps(v1_data, indent=2, default=str)[:5000] if v1_data else "(none)")
            print("\n─── V2 charge_history_v2 ───")
            print(json.dumps(v2_data, indent=2, default=str)[:5000] if v2_data else "(none)")
            print("\nFull JSON saved to data/. Inspect to map field names.")
            return

    # Build DataFrames
    v1_df = build_v1_df(v1_data) if v1_data else None
    v2_df = build_v2_df(v2_data) if v2_data else None

    if (v1_df is None or v1_df.empty) and (v2_df is None or v2_df.empty):
        sys.exit("No usable data in either V1 or V2 response.")

    print(f"V1: {len(v1_df) if v1_df is not None else 0} daily records")
    print(f"V2: {len(v2_df) if v2_df is not None else 0} Supercharger sessions")

    # Render
    html = render_dashboard(v1_df, v2_df)
    out_path = OUTPUT_DIR / "dashboard.html"
    out_path.write_text(html)
    print(f"\nDashboard → {out_path}")
    print(f"Open: file://{out_path}")


if __name__ == "__main__":
    main()
