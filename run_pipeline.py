#!/usr/bin/env python3
"""
Process CCTV clips for one or all stores under dataset/clips/<Store folder>/.

Each store folder contains clips + store_layout.json (+ layout PNG).

Usage:
  python run_pipeline.py --store-folder "Store 1"
  python run_pipeline.py --all-stores
  python run_pipeline.py --store-folder "Store 1" --api-url http://localhost:8000
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from pipeline.layout_builder import list_store_clip_dirs, load_store_layout, normalize_store_id


CAMERA_ROLE_ORDER = {
    "entry": 0,
    "floor": 1,
    "zone": 1,
    "billing": 2,
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--store-folder", help='Clip folder name under dataset/clips, e.g. "Store 1"')
    p.add_argument("--all-stores", action="store_true", help="Process every store folder with clips")
    p.add_argument("--device", default="cpu")
    p.add_argument("--dataset", default="dataset")
    p.add_argument("--api-url", default=None)
    p.add_argument("--clip-start", default=None, help="ISO UTC anchor for timestamps (default: video mtime)")
    return p.parse_args()


def process_store(store_dir: Path, dataset: Path, device: str, api_url: str | None, clip_start: str | None):
    layout = load_store_layout(store_dir)
    store_id = layout["store_id"]
    layout_path = store_dir / "store_layout.json"
    output_dir = dataset / "events"
    output_dir.mkdir(parents=True, exist_ok=True)

    source_map = {
        cam.get("source_file", "").lower(): cam_id
        for cam_id, cam in layout.get("cameras", {}).items()
        if cam.get("source_file")
    }

    video_extensions = {".mp4", ".avi", ".mov", ".mkv"}
    videos = sorted(
        (p for p in store_dir.iterdir() if p.suffix.lower() in video_extensions),
        key=lambda p: _video_sort_key(p, source_map),
    )

    print()
    print("═══════════════════════════════════════════")
    print(f"  Store      : {store_id} ({store_dir.name})")
    print(f"  Layout     : {layout_path}")
    print(f"  Clips      : {len(videos)}")
    print("═══════════════════════════════════════════")

    total_events = 0
    cam_counts: dict[str, int] = {}

    for clip_path in videos:
        cam_id = source_map.get(clip_path.name.lower())
        if not cam_id:
            print(f"  SKIP  {clip_path.name} (not in store_layout.json)")
            continue

        cam_data = layout["cameras"][cam_id]
        if cam_data.get("exclude_from_metrics"):
            print(f"  SKIP  {clip_path.name} → {cam_id} (exclude_from_metrics)")
            continue

        cam_counts[cam_id] = cam_counts.get(cam_id, 0) + 1
        out_suffix = cam_id if cam_counts[cam_id] == 1 else f"{cam_id}_{cam_counts[cam_id]:02d}"
        out_file = output_dir / f"{store_id}_{out_suffix}_events.jsonl"

        print(f"  → {clip_path.name}  |  {cam_id}  |  {out_file.name}")

        cmd = [
            sys.executable, "-m", "pipeline.detect",
            "--video", str(clip_path),
            "--store", store_id,
            "--camera", cam_id,
            "--layout", str(layout_path),
            "--output", str(out_file),
            "--device", device,
        ]
        if clip_start:
            cmd += ["--clip-start", clip_start]
        if api_url:
            cmd += ["--api-url", api_url]

        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"     ERROR exit {result.returncode}")
            continue
        count = sum(1 for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip())
        total_events += count
        print(f"     {count} events")

    print(f"  Total events for {store_id}: {total_events}")

    if api_url:
        ingest_store_events(output_dir, store_id, api_url)

    return total_events


def ingest_store_events(output_dir: Path, store_id: str, api_url: str):
    print(f"  Ingesting {store_id} → {api_url}")
    for ef in sorted(output_dir.glob(f"{store_id}_*_events.jsonl"), key=_event_file_sort_key):
        events = []
        for line in ef.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        if not events:
            continue
        for i in range(0, len(events), 500):
            batch = events[i : i + 500]
            payload = json.dumps({"events": batch}).encode()
            req = urllib.request.Request(
                f"{api_url}/events/ingest",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
                print(f"    {ef.name}: accepted={res.get('accepted')} dup={res.get('duplicate')}")


def _video_sort_key(path: Path, source_map: dict[str, str]) -> tuple[int, str]:
    cam_id = source_map.get(path.name.lower(), "")
    text = f"{cam_id} {path.name}".lower()
    if "entry" in text:
        role = "entry"
    elif any(token in text for token in ("billing", "cashier", "checkout")):
        role = "billing"
    else:
        role = "floor"
    return CAMERA_ROLE_ORDER.get(role, 9), path.name.lower()


def _event_file_sort_key(path: Path) -> tuple[int, str]:
    text = path.name.lower()
    if "entry" in text:
        role = "entry"
    elif "billing" in text:
        role = "billing"
    else:
        role = "floor"
    return CAMERA_ROLE_ORDER.get(role, 9), path.name.lower()


def main():
    args = parse_args()
    dataset = Path(args.dataset)
    stores = list_store_clip_dirs(dataset)

    if not stores:
        print(f"ERROR: No store folders with .mp4 under {dataset / 'clips'}")
        sys.exit(1)

    if args.all_stores:
        targets = stores
    elif args.store_folder:
        targets = [dataset / "clips" / args.store_folder]
        if not targets[0].is_dir():
            print(f"ERROR: {targets[0]} not found")
            sys.exit(1)
    else:
        targets = [stores[0]]
        print(f"No --store-folder set; using {targets[0].name}")

    grand_total = 0
    for store_dir in targets:
        grand_total += process_store(
            store_dir, dataset, args.device, args.api_url, args.clip_start
        )

    print()
    print(f"Done. Total events across stores: {grand_total}")
    if targets:
        sid = load_store_layout(targets[0])["store_id"]
        print(f"  curl http://localhost:8000/stores/{sid}/metrics")
        print(f"  python dashboard/live.py --store {sid}")


if __name__ == "__main__":
    main()
