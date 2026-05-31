#!/usr/bin/env python3
"""
validate_dataset.py — Run this FIRST after receiving the dataset.
Checks all files, prints a go/no-go report, and calibrates the pipeline config.

Usage: python pipeline/validate_dataset.py --dataset dataset/
"""
import argparse
import json
import csv
import os
import sys
from pathlib import Path
from datetime import datetime


def check(label, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {status}  {label}")
    if detail:
        print(f"       → {detail}")
    return ok


def validate_dataset(dataset_dir: str):
    d = Path(dataset_dir)
    all_ok = True
    store_id = None

    print("\n══════════════════════════════════════════")
    print("  Store Intelligence — Dataset Validator")
    print("══════════════════════════════════════════\n")

    # ── 1. Directory structure ─────────────────────────────────────────────────
    print("📁 Directory structure")
    clips_dir = d / "clips"
    ok = check("dataset/ exists", d.exists())
    all_ok &= ok

    clips_ok = check("dataset/clips/ exists", clips_dir.exists(),
                     "Create: mkdir -p dataset/clips/ST1008" if not clips_dir.exists() else "")
    all_ok &= clips_ok

    # ── 2. Video clips ────────────────────────────────────────────────────────
    print("\n🎥 Video clips")
    video_files = list(d.rglob("*.mp4")) + list(d.rglob("*.MP4")) + \
                  list(d.rglob("*.avi")) + list(d.rglob("*.mov"))

    check(f"Video files found: {len(video_files)}", len(video_files) > 0,
          "No video files found — check clips/ subdirectory" if not video_files else "")

    for v in sorted(video_files):
        size_mb = v.stat().st_size / (1024 * 1024)
        check(f"{v.relative_to(d)}", size_mb > 1, f"{size_mb:.1f} MB")

    # Detect store folder name → store_id
    if clips_dir.exists():
        store_dirs = [x for x in clips_dir.iterdir() if x.is_dir()]
        if store_dirs:
            store_id = store_dirs[0].name
            check(f"Store ID detected: {store_id}", True)
        else:
            check("Store subdirectory inside clips/", False,
                  "Expected: dataset/clips/ST1008/*.mp4")

    # ── 3. POS CSV ────────────────────────────────────────────────────────────
    print("\n💳 POS Transactions CSV")
    csv_files = list(d.glob("*.csv")) + list(d.glob("**/*.csv"))
    pos_file = None

    for f in csv_files:
        if "pos" in f.name.lower() or "transaction" in f.name.lower() or \
           "brigade" in f.name.lower() or "april" in f.name.lower():
            pos_file = f
            break
    if not pos_file and csv_files:
        pos_file = csv_files[0]

    if check("POS CSV found", pos_file is not None,
             f"Looked in {d} — found: {[f.name for f in csv_files]}"):
        with open(pos_file, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            rows = list(reader)

        is_purplle = "invoice_number" in cols
        check("Format detected",
              True,
              f"Purplle 39-col format" if is_purplle else "Simple 4-col format")
        check(f"Row count: {len(rows)}", len(rows) > 0)

        if is_purplle and rows:
            store_ids = set(r.get("store_id", "").strip() for r in rows)
            check(f"Store IDs in POS: {store_ids}", True)
            if store_id is None and store_ids:
                store_id = list(store_ids)[0]

            # Check date format
            sample_date = rows[0].get("order_date", "")
            try:
                datetime.strptime(sample_date, "%d-%m-%Y")
                check(f"Date format OK: '{sample_date}'", True)
            except ValueError:
                try:
                    datetime.strptime(sample_date, "%Y-%m-%d")
                    check(f"Date format ISO: '{sample_date}'", True)
                except ValueError:
                    check(f"Date format unknown: '{sample_date}'", False,
                          "Update load_pos_transactions() strptime format")

            invoices = set(r.get("invoice_number") for r in rows if r.get("invoice_number"))
            times = sorted(r.get("order_time","") for r in rows if r.get("order_time"))
            check(f"Unique invoices: {len(invoices)}", True)
            check(f"Time range: {times[0]} → {times[-1]}", True)

    # ── 4. Store Layout ────────────────────────────────────────────────────────
    print("\n🗺️  Store Layout")
    layout_candidates = list(d.glob("*.json")) + list(d.glob("**/*.json"))
    layout_file = None
    for f in layout_candidates:
        if "layout" in f.name.lower() or "store" in f.name.lower():
            layout_file = f
            break
    if not layout_file and layout_candidates:
        layout_file = layout_candidates[0]

    # Also check for Excel
    xl_files = list(d.glob("*.xlsx")) + list(d.glob("**/*.xlsx"))

    if layout_file:
        with open(layout_file) as f:
            layout = json.load(f)
        check(f"store_layout.json found: {layout_file.name}", True)
        has_cameras = "cameras" in layout
        has_zones_in_cameras = has_cameras and any(
            "zones" in v for v in layout["cameras"].values()
        )
        check("Has cameras key", has_cameras)
        check("Has zones in cameras", has_zones_in_cameras)
        if has_cameras:
            for cam, data in layout["cameras"].items():
                n = len(data.get("zones", []))
                check(f"  {cam}: {n} zones", n > 0)
    elif xl_files:
        check("Excel layout file found (will extract zones manually)", True,
              f"{xl_files[0].name} — zones must be defined in store_layout.json")
    else:
        check("store_layout.json", False, "No layout file found — zone mapping disabled")

    # ── 5. Sample events / assertions ─────────────────────────────────────────
    print("\n📋 Reference files")
    sample_events = list(d.glob("*events*.jsonl")) + list(d.glob("**/*.jsonl"))
    check(f"sample_events.jsonl", len(sample_events) > 0,
          f"Found: {[f.name for f in sample_events]}" if sample_events else "Not found — optional")

    assertions = list(d.glob("*assertions*.py")) + list(d.glob("**/*.py"))
    check(f"assertions.py", len(assertions) > 0,
          f"Found: {[f.name for f in assertions]}" if assertions else "Not found — optional")

    # ── 6. Config recommendations ──────────────────────────────────────────────
    print("\n⚙️  Recommended pipeline config")
    print(f"""
  Store ID    : {store_id or 'UNKNOWN — check clips/ folder name'}
  POS CSV     : {pos_file or 'NOT FOUND'}
  Layout JSON : {layout_file or 'dataset/store_layout.json (use our generated one)'}
  Video dir   : {clips_dir / store_id if store_id else clips_dir}

  Run command :
    bash pipeline/run.sh \\
      --store {store_id or 'ST1008'} \\
      --layout {layout_file or 'dataset/store_layout.json'} \\
      --api-url http://localhost:8000
""")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("══════════════════════════════════════════")
    if all_ok:
        print("  ✅  Dataset looks good. Ready to run.")
    else:
        print("  ⚠️   Some checks failed — fix before running pipeline.")
    print("══════════════════════════════════════════\n")

    return store_id


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="dataset", help="Path to dataset directory")
    args = p.parse_args()
    validate_dataset(args.dataset)
