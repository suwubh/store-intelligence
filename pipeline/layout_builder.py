"""
Build and load per-store layout JSON from dataset/clips/<store folder>/.

Each store folder may contain:
  - *.mp4 clips
  - *layout*.png (reference image; polygons live in store_layout.json)
  - store_layout.json (generated or hand-calibrated)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

import cv2

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}


def normalize_store_id(folder_name: str) -> str:
    """Map clip folder name to a stable store_id for events and API paths."""
    name = folder_name.strip()
    # FIX (minor): removed "store 1" / "store 2" keys — re.sub() normalises
    # spaces to underscores before this lookup, so space-keyed entries were
    # unreachable dead code.
    explicit = {
        "store_1": "ST1008",
        "store_2": "ST1076",
    }
    key = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    if key in explicit:
        return explicit[key]
    m = re.match(r"store\s*(\d+)", name, re.I)
    if m:
        number = m.group(1)
        if number == "1":
            return "ST1008"
        if number == "2":
            return "ST1076"
        return f"ST{number}"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return slug if slug else "ST_UNKNOWN"


def infer_camera_role(filename: str) -> str:
    """Return ENTRY | FLOOR | BILLING | UNKNOWN from clip filename."""
    target = filename.lower()
    if "entry" in target:
        return "ENTRY"
    if any(token in target for token in ("billing", "cashier", "checkout")):
        return "BILLING"
    if any(token in target for token in ("zone", "floor", "cam")):
        return "FLOOR"
    return "UNKNOWN"


def _floor_zones(w: int, h: int, columns: int = 4, rows: int = 2, prefix: str = "ZONE") -> list[dict]:
    cw, rh = w // columns, h // rows
    zones = []
    idx = 0
    for row in range(rows):
        for col in range(columns):
            idx += 1
            zones.append({
                "zone_id": f"{prefix}_{idx:02d}",
                "polygon": [
                    [col * cw,       row * rh],
                    [(col + 1) * cw, row * rh],
                    [(col + 1) * cw, (row + 1) * rh],
                    [col * cw,       (row + 1) * rh],
                ],
            })
    return zones


def _camera_zones(
    role: str,
    w: int,
    h: int,
    floor_index: int = 1,
    entry_inward_direction: str = "up",
) -> list[dict]:
    """
    Generate zone polygons for a camera.

    For ENTRY cameras, ``entry_inward_direction`` controls which half of the
    frame is labelled INSIDE vs EXTERIOR:

      - ``"up"``   → top half  = INSIDE  (person walks toward the top of the
                                           frame to enter the store)
      - ``"down"`` → bottom half = INSIDE (person walks toward the bottom of
                                           the frame to enter the store)

    The default is ``"up"``; operators **must** verify and correct this value
    in the generated store_layout.json whenever the physical camera orientation
    differs.  A WARNING is logged at generation time as a reminder.
    """
    # FIX (critical): previously always assigned top=INSIDE / bottom=EXTERIOR,
    # ignoring entry_inward_direction entirely.  Now the assignment respects
    # which direction the camera considers "inward".
    if role == "ENTRY":
        line_y = int(h * 0.55)
        top_poly = [[0, 0],      [w, 0],      [w, line_y], [0, line_y]]
        bot_poly = [[0, line_y], [w, line_y], [w, h],      [0, h]]
        if entry_inward_direction == "up":
            inside_poly, exterior_poly = top_poly, bot_poly
        else:  # "down"
            inside_poly, exterior_poly = bot_poly, top_poly
        return [
            {"zone_id": "ENTRY_INSIDE",   "polygon": inside_poly},
            {"zone_id": "ENTRY_EXTERIOR", "polygon": exterior_poly},
        ]

    if role == "BILLING":
        return [
            {
                "zone_id": "BILLING_COUNTER",
                "polygon": [[0, 0], [w, 0], [w, int(h * 0.55)], [0, int(h * 0.55)]],
            },
            {
                "zone_id": "BILLING_QUEUE",
                "polygon": [[0, int(h * 0.55)], [w, int(h * 0.55)], [w, h], [0, h]],
            },
        ]

    if role == "FLOOR":
        return _floor_zones(w, h, columns=4, rows=2, prefix=f"FLOOR_{floor_index}")

    return []


def _assign_camera_id(
    role: str,
    floor_index: int,
    entry_index: int,
    billing_index: int = 1,
) -> str:
    # FIX (minor): billing_index parameter added so multiple billing cameras
    # get unique IDs instead of silently overwriting each other in the dict.
    if role == "ENTRY":
        return f"CAM_ENTRY_{entry_index:02d}"
    if role == "BILLING":
        return f"CAM_BILLING_{billing_index:02d}"
    if role == "FLOOR":
        return f"CAM_FLOOR_{floor_index:02d}"
    return "CAM_UNKNOWN"


def probe_video(path: Path) -> tuple[int, int, float]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return 1920, 1080, 15.0
    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))  or 1920
    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 15.0)
    cap.release()
    return w, h, fps


def build_layout_for_store_dir(store_dir: Path, store_id: Optional[str] = None) -> dict:
    """Create layout dict from videos in a store clip folder."""
    store_dir = Path(store_dir)
    store_id  = store_id or normalize_store_id(store_dir.name)

    videos = sorted(
        p for p in store_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not videos:
        raise FileNotFoundError(f"No video clips in {store_dir}")

    cameras: dict[str, dict] = {}
    floor_count   = 0
    entry_count   = 0
    billing_count = 0  # FIX (minor): track billing cameras so IDs are unique

    for video in videos:
        role = infer_camera_role(video.name)
        w, h, fps = probe_video(video)

        if role == "FLOOR":
            floor_count += 1
        elif role == "ENTRY":
            entry_count += 1
        elif role == "BILLING":
            billing_count += 1

        cam_id = _assign_camera_id(role, floor_count, entry_count, billing_count)

        # FIX (minor): never auto-exclude a second entry clip — it may be the
        # same physical camera on a different date, which must be processed.
        # Instead, log a warning so the operator can decide; they must set
        # exclude_from_metrics=True manually for genuine duplicate cameras.
        if role == "ENTRY" and entry_count > 1:
            logger.warning(
                "Multiple ENTRY clips detected (%d total). '%s' → %s: verify "
                "whether this is the same camera on a different date or a true "
                "duplicate.  Set exclude_from_metrics=True manually if it "
                "should be skipped.",
                entry_count,
                video.name,
                cam_id,
            )

        entry_line  = 0.55 if role == "ENTRY" else None
        camera_dict = {
            "description":          f"{role} — {video.name}",
            "source_file":          video.name,
            "coverage":             role.lower(),
            "resolution":           {"width": w, "height": h, "fps": round(fps, 2)},
            "exclude_from_metrics": False,   # operator sets True for genuine duplicates
            "entry_line_y_ratio":   entry_line,
            "zones": _camera_zones(
                role,
                w,
                h,
                floor_index=max(floor_count, 1),
                # Default "up"; corrected per-camera in store_layout.json by the operator.
                entry_inward_direction="up",
            ),
        }

        # FIX (critical): entry_inward_direction was never written to the output
        # dict, causing KeyError / silent wrong-default in downstream code.
        if role == "ENTRY":
            camera_dict["entry_inward_direction"] = "up"
            logger.warning(
                "Auto-generated entry_inward_direction='up' for %s (%s). "
                "Verify the physical camera orientation and correct the value "
                "in store_layout.json if the inward direction is 'down'.",
                cam_id,
                video.name,
            )

        cameras[cam_id] = camera_dict

    layout_pngs  = sorted(store_dir.glob("*layout*.png")) + sorted(store_dir.glob("*layout*.jpg"))
    layout_image = layout_pngs[0].name if layout_pngs else None

    return {
        "store_id":     store_id,
        "store_name":   store_dir.name,
        "clip_folder":  store_dir.name,
        "layout_image": layout_image,
        "open_hours":   {"open": "10:00", "close": "22:00"},
        "billing_zones": ["BILLING_COUNTER", "BILLING_QUEUE"],
        "staff_info":   _staff_info_for_store(store_id),
        "cameras":      cameras,
    }


def _staff_info_for_store(store_id: str) -> dict:
    if store_id == "ST1076":
        return {
            "uniform_color":     "pink top / black bottom",
            "detector_profile":  "store2_pink_black",
            "note": "Store 2 staff wear a pink top and black bottom; tune HSV ranges if uniforms differ.",
        }
    return {
        "uniform_color":    "black",
        "detector_profile": "store1_black",
        "note": "Store 1 staff wear all black; calibrate with pipeline/calibrate_staff.py if needed.",
    }


def write_store_layout(store_dir: Path, store_id: Optional[str] = None) -> Path:
    store_dir = Path(store_dir)
    layout    = build_layout_for_store_dir(store_dir, store_id)
    out       = store_dir / "store_layout.json"
    out.write_text(json.dumps(layout, indent=2), encoding="utf-8")
    logger.info("Wrote %s (%d cameras)", out, len(layout["cameras"]))
    return out


def load_store_layout(store_dir: Path) -> dict:
    store_dir = Path(store_dir)
    path      = store_dir / "store_layout.json"
    if not path.exists():
        write_store_layout(store_dir)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_layout_path(
    dataset_dir: Path,
    store_folder: Optional[str] = None,
    explicit_layout: Optional[str] = None,
) -> tuple[Path, str, dict]:
    """Returns (layout_json_path, store_id, layout_dict)."""
    dataset_dir = Path(dataset_dir)
    if explicit_layout:
        layout_path = Path(explicit_layout)
        with open(layout_path, encoding="utf-8") as f:
            layout = json.load(f)
        store_id = layout.get("store_id") or "ST_UNKNOWN"
        return layout_path, store_id, layout

    if store_folder:
        store_dir = dataset_dir / "clips" / store_folder
        if not store_dir.is_dir():
            store_dir = Path(store_folder)
    else:
        clips_root = dataset_dir / "clips"
        candidates = sorted(
            p for p in clips_root.iterdir()
            if p.is_dir() and any(p.glob("*.mp4"))
        ) if clips_root.is_dir() else []
        if not candidates:
            raise FileNotFoundError(f"No store folders with clips under {clips_root}")
        store_dir = candidates[0]

    layout = load_store_layout(store_dir)
    return store_dir / "store_layout.json", layout["store_id"], layout


def list_store_clip_dirs(dataset_dir: Path) -> list[Path]:
    clips_root = Path(dataset_dir) / "clips"
    if not clips_root.is_dir():
        return []
    return sorted(
        p for p in clips_root.iterdir()
        if p.is_dir() and any(p.glob("*.mp4"))
    )