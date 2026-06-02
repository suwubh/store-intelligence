#!/usr/bin/env python3
"""
Validate dataset/clips store folders (Store 1, Store 2, …).

Usage:
  python pipeline/validate_dataset.py --dataset dataset
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.layout_builder import list_store_clip_dirs, load_store_layout, write_store_layout

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {label}")
    if detail:
        print(f"       {detail}")
    return ok


def validate_store_store_dir(store_dir: Path) -> bool:
    print()
    print(f"Store folder: {store_dir.name}")
    all_ok = True

    videos = sorted(p for p in store_dir.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS)
    all_ok = check("video clips", bool(videos), f"{len(videos)} file(s)") and all_ok

    layout_path = store_dir / "store_layout.json"
    if not layout_path.exists():
        write_store_layout(store_dir)
        print(f"       generated {layout_path.name}")
    all_ok = check("store_layout.json", layout_path.exists(), str(layout_path)) and all_ok

    try:
        layout = load_store_layout(store_dir)
        store_id = layout.get("store_id", "?")
        cameras = layout.get("cameras", {})
        check("store_id", bool(store_id), store_id)
        check("cameras mapped", bool(cameras), f"{len(cameras)} camera(s)")
        mapped = {cam.get("source_file", "").lower() for cam in cameras.values()}
        unmapped = [v.name for v in videos if v.name.lower() not in mapped]
        if unmapped:
            all_ok = check("all clips mapped in layout", False, ", ".join(unmapped)) and all_ok
        else:
            check("all clips mapped in layout", True, "every .mp4 has source_file")

        pngs = list(store_dir.glob("*layout*.png"))
        if pngs:
            check("layout image", True, pngs[0].name)
    except Exception as exc:
        all_ok = check("layout loads", False, str(exc)) and all_ok

    return all_ok


def validate_dataset(dataset_dir: str):
    root = Path(dataset_dir)
    print()
    print("Store Intelligence Dataset Validator")
    print("=" * 42)

    all_ok = check("dataset directory exists", root.exists(), str(root))
    if not root.exists():
        return False

    store_dirs = list_store_clip_dirs(root)
    if not store_dirs:
        all_ok = check("store clip folders", False, f"Expected subfolders under {root / 'clips'}") and all_ok
    else:
        check("store clip folders", True, ", ".join(d.name for d in store_dirs))
        for store_dir in store_dirs:
            all_ok = validate_store_store_dir(store_dir) and all_ok

    pos_csv = _find_pos(root)
    print()
    print("POS")
    if pos_csv:
        check("POS CSV found", True, str(pos_csv.relative_to(root)))
        _validate_pos(pos_csv)
    else:
        all_ok = check("POS CSV found", False) and all_ok

    sample_events = _find_first(root, ["events/sample_events.jsonl", "*events*.jsonl"])
    print()
    print("Sample events")
    if sample_events:
        check("sample events JSONL", True, str(sample_events.relative_to(root)))
        _validate_sample_events(sample_events)
    else:
        check("sample events JSONL", False, "optional")

    print()
    if all_ok:
        print("Dataset check completed.")
    else:
        print("Fix FAIL rows before running detection.")
    return all_ok


def _find_first(root: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    return None


def _find_pos(root: Path) -> Path | None:
    for path in sorted(root.rglob("*.csv")):
        if "pos" in path.name.lower() or "transaction" in path.name.lower():
            return path
    direct = root / "pos_transactions.csv"
    return direct if direct.exists() else None


def _validate_pos(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = set(reader.fieldnames or [])
    updated = {"order_id", "order_date", "order_time", "store_id", "total_amount"}.issubset(cols)
    original = {"transaction_id", "timestamp", "store_id"}.issubset(cols)
    check("POS schema", updated or original, ", ".join(sorted(cols)))
    stores = sorted({r.get("store_id", "").strip() for r in rows if r.get("store_id")})
    check("POS store IDs", bool(stores), ", ".join(stores[:8]))


def _validate_sample_events(path: Path):
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = Counter(str(e.get("event_type", "unknown")) for e in events)
    check("sample rows", bool(events), f"{len(events)} row(s)")
    print(f"       types: {dict(counts)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="dataset")
    args = parser.parse_args()
    sys.exit(0 if validate_dataset(args.dataset) else 1)
