#!/usr/bin/env python3
"""
Live dashboard — polls the Store Intelligence API and renders metrics in terminal.
Usage: python dashboard/live.py --store STORE_BLR_002 --api http://localhost:8000
"""
import argparse
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--store", default="ST_STORE_1", help="Store ID to monitor")
    p.add_argument("--api", default="http://localhost:8000", help="API base URL")
    p.add_argument("--interval", type=float, default=3.0, help="Refresh interval in seconds")
    return p.parse_args()



def fetch(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def make_dashboard(store_id: str, api: str) -> Table:
    metrics = fetch(f"{api}/stores/{store_id}/metrics")
    funnel  = fetch(f"{api}/stores/{store_id}/funnel")
    heatmap = fetch(f"{api}/stores/{store_id}/heatmap")
    anomalies = fetch(f"{api}/stores/{store_id}/anomalies")
    health  = fetch(f"{api}/health")

    now = datetime.now().strftime("%H:%M:%S")

    # ── Root layout grid ─────────────────────────────────────────────────────
    root = Table.grid(padding=1)
    root.add_column()
    root.add_column()

    # ── Metrics panel ────────────────────────────────────────────────────────
    m_table = Table(title=f"📊 Store Metrics — {store_id}", box=box.ROUNDED, style="cyan")
    m_table.add_column("Metric", style="bold white")
    m_table.add_column("Value", style="green")

    if metrics:
        conv = metrics.get("conversion_rate", 0)
        conv_color = "red" if conv < 0.1 else "green"
        m_table.add_row("Unique Visitors", str(metrics.get("unique_visitors", 0)))
        m_table.add_row("Conversion Rate", Text(f"{conv:.1%}", style=conv_color))
        m_table.add_row("Queue Depth", str(metrics.get("current_queue_depth", 0)))
        m_table.add_row("Abandonment Rate", f"{metrics.get('abandonment_rate', 0):.1%}")
    else:
        m_table.add_row("Status", "[yellow]Waiting for data...[/]")

    # ── Funnel panel ─────────────────────────────────────────────────────────
    f_table = Table(title="🔽 Conversion Funnel", box=box.ROUNDED, style="magenta")
    f_table.add_column("Stage")
    f_table.add_column("Count")
    f_table.add_column("Drop-off")

    if funnel and funnel.get("stages"):
        for stage in funnel["stages"]:
            drop = stage.get("drop_off_pct", 0)
            drop_style = "red bold" if drop > 50 else "yellow" if drop > 25 else "green"
            f_table.add_row(
                stage["stage"],
                str(stage["count"]),
                Text(f"{drop:.1f}%", style=drop_style),
            )
    else:
        f_table.add_row("—", "—", "—")

    root.add_row(m_table, f_table)

    # ── Heatmap panel ────────────────────────────────────────────────────────
    h_table = Table(title="🔥 Zone Heatmap", box=box.SIMPLE, style="yellow")
    h_table.add_column("Zone")
    h_table.add_column("Score", justify="right")
    h_table.add_column("Avg Dwell", justify="right")
    h_table.add_column("Visits", justify="right")

    if heatmap and heatmap.get("zones"):
        for z in heatmap["zones"][:8]:  # top 8 zones
            score = z.get("normalised_score", 0)
            bar = "█" * int(score / 10) + "░" * (10 - int(score / 10))
            dwell_s = z.get("avg_dwell_ms", 0) / 1000
            h_table.add_row(
                z["zone_id"],
                f"{bar} {score:.0f}",
                f"{dwell_s:.0f}s",
                str(z.get("visit_frequency", 0)),
            )
    else:
        h_table.add_row("—", "—", "—", "—")

    # ── Anomalies panel ──────────────────────────────────────────────────────
    a_table = Table(title="🚨 Active Anomalies", box=box.SIMPLE, style="red")
    a_table.add_column("Severity")
    a_table.add_column("Type")
    a_table.add_column("Description")

    severity_colors = {"CRITICAL": "red bold", "WARN": "yellow", "INFO": "blue"}
    if anomalies and anomalies.get("anomalies"):
        for a in anomalies["anomalies"]:
            sev = a.get("severity", "INFO")
            a_table.add_row(
                Text(sev, style=severity_colors.get(sev, "white")),
                a.get("anomaly_type", ""),
                a.get("description", "")[:60],
            )
    else:
        a_table.add_row("[green]None[/]", "—", "All systems normal")

    root.add_row(h_table, a_table)

    # ── Health bar at bottom ──────────────────────────────────────────────────
    health_str = "—"
    if health:
        status = health.get("status", "unknown")
        color = "green" if status == "ok" else "red"
        uptime = health.get("service_uptime_seconds", 0)
        health_str = f"[{color}]API: {status.upper()}[/{color}] | Uptime: {uptime:.0f}s | Refreshed: {now}"

    footer = Panel(health_str, style="dim", height=3)
    full = Table.grid()
    full.add_row(root)
    full.add_row(footer)
    return full


def main():
    args = parse_args()
    console = Console()
    console.print(f"[bold cyan]Store Intelligence Dashboard[/] — monitoring [yellow]{args.store}[/]")
    console.print(f"API: {args.api} | Refresh: {args.interval}s\n")

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                dashboard = make_dashboard(args.store, args.api)
                live.update(dashboard)
            except Exception as e:
                live.update(Panel(f"[red]Dashboard error: {e}[/red]"))
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
