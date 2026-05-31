#!/usr/bin/env python3
"""
run_pipeline.py — cross-platform pipeline runner (Windows/Mac/Linux)
Usage: python run_pipeline.py [--api-url http://localhost:8000] [--store ST1008] [--device cpu]
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--store",      default="ST1008")
    p.add_argument("--device",     default="cpu")
    p.add_argument("--dataset",    default="dataset")
    p.add_argument("--api-url",    default=None)
    p.add_argument("--clip-start", default="2026-04-10T10:00:00Z")
    return p.parse_args()


def main():
    args = parse_args()

    layout_path  = Path(args.dataset) / "store_layout.json"
    clips_dir    = Path(args.dataset) / "clips" / args.store
    output_dir   = Path(args.dataset) / "events"
    output_dir.mkdir(parents=True, exist_ok=True)

    print()
    print("╔══════════════════════════════════════════╗")
    print("║  Store Intelligence Detection Pipeline   ║")
    print("╚══════════════════════════════════════════╝")
    print(f"  Store     : {args.store}")
    print(f"  Clips     : {clips_dir}")
    print(f"  Output    : {output_dir}")
    print(f"  Layout    : {layout_path}")
    print(f"  Device    : {args.device}")
    print(f"  ClipStart : {args.clip_start}")
    if args.api_url:
        print(f"  API       : {args.api_url}")
    print()

    if not clips_dir.exists():
        print(f"ERROR: Clips directory not found: {clips_dir}")
        print(f"       Make sure your clips are at:  {clips_dir}\\CAM 1.mp4  etc.")
        sys.exit(1)

    if not layout_path.exists():
        print(f"ERROR: store_layout.json not found at {layout_path}")
        sys.exit(1)

    with open(layout_path) as f:
        layout = json.load(f)

    # Build filename → camera_id map from source_file fields
    source_file_map = {}
    for cam_id, cam_data in layout.get("cameras", {}).items():
        sf = cam_data.get("source_file", "")
        if sf:
            source_file_map[sf.lower()] = cam_id

    print(f"Camera map from store_layout.json:")
    for sf, cam in source_file_map.items():
        exclude = layout["cameras"][cam].get("exclude_from_metrics", False)
        print(f"  {sf!r:25s} → {cam}  {'(SKIP — storeroom)' if exclude else ''}")
    print()

    # Find all video files
    video_extensions = {".mp4", ".avi", ".mov", ".mkv"}
    video_files = sorted([
        f for f in clips_dir.iterdir()
        if f.suffix.lower() in video_extensions
    ])

    if not video_files:
        print(f"ERROR: No video files found in {clips_dir}")
        sys.exit(1)

    print(f"Found {len(video_files)} video file(s)")

    total_events = 0

    for clip_path in video_files:
        fname = clip_path.name
        cam_id = source_file_map.get(fname.lower())

        if cam_id is None:
            print(f"  SKIP  {fname!r} — not in store_layout.json source_file map")
            continue

        cam_data = layout["cameras"][cam_id]
        if cam_data.get("exclude_from_metrics", False):
            print(f"  SKIP  {fname!r} → {cam_id} (storeroom — excluded from metrics)")
            continue

        out_file = output_dir / f"{args.store}_{cam_id}_events.jsonl"
        print(f"  Processing  {fname!r}  →  {cam_id}")
        print(f"  Output   →  {out_file.name}")

        cmd = [
            sys.executable, "-m", "pipeline.detect",
            "--video",       str(clip_path),
            "--store",       args.store,
            "--camera",      cam_id,
            "--layout",      str(layout_path),
            "--output",      str(out_file),
            "--clip-start",  args.clip_start,
            "--device",      args.device,
        ]
        if args.api_url:
            cmd += ["--api-url", args.api_url]

        result = subprocess.run(cmd)

        if result.returncode == 0:
            count = 0
            try:
                count = sum(1 for line in open(out_file) if line.strip())
            except Exception:
                pass
            total_events += count
            print(f"  ✅  {count} events  →  {out_file.name}")
        else:
            print(f"  ❌  Detection failed for {fname!r} (exit code {result.returncode})")

        print()

    print("═══════════════════════════════════════════")
    print(f"  Total events emitted: {total_events}")
    print("═══════════════════════════════════════════")

    # Ingest into API if requested
    if args.api_url:
        print()
        print(f"📡  Ingesting all events into {args.api_url} ...")
        event_files = sorted(output_dir.glob("*.jsonl"))
        for ef in event_files:
            events = []
            with open(ef) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            pass
            if not events:
                print(f"  (empty) {ef.name}")
                continue

            acc = dupes = errs = 0
            for i in range(0, len(events), 500):
                batch = events[i:i + 500]
                payload = json.dumps({"events": batch}).encode()
                req = urllib.request.Request(
                    f"{args.api_url}/events/ingest",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        res = json.loads(r.read())
                        acc   += res.get("accepted",  0)
                        dupes += res.get("duplicate", 0)
                        errs  += res.get("rejected",  0)
                except Exception as e:
                    print(f"  ⚠️   Ingest error ({ef.name}): {e}")

            print(f"  ✅  {ef.name}: {acc} accepted, {dupes} dupes, {errs} rejected")

    print()
    print("Next steps:")
    print("  1. Start API:      docker compose up -d")
    print(f"  2. Check metrics:  curl http://localhost:8000/stores/{args.store}/metrics")
    print(f"  3. Dashboard:      python dashboard/live.py --store {args.store}")
    print()


if __name__ == "__main__":
    main()