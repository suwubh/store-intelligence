"""
Main detection pipeline.
Usage: python -m pipeline.detect --video path/to/clip.mp4 --store ST1008 --camera CAM_ENTRY_01
"""
import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from pipeline.tracker import MultiObjectTracker
from pipeline.emit import EventEmitter
from pipeline.zone_mapper import ZoneMapper
from pipeline.staff_detector import StaffDetector

logger = logging.getLogger(__name__)

BILLING_ZONES_SET = {"BILLING_COUNTER", "BILLING_QUEUE", "BILLING"}


def parse_args():
    p = argparse.ArgumentParser(description="CCTV detection pipeline")
    p.add_argument("--video", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--camera", required=True)
    p.add_argument("--layout", default=None, help="Path to store_layout.json (default: beside the video)")
    p.add_argument("--output", default="dataset/events.jsonl")
    p.add_argument("--clip-start", default=None, help="ISO UTC timestamp override, e.g. 2026-04-10T20:00:00Z")
    p.add_argument("--api-url", default=None)
    p.add_argument("--conf", type=float, default=0.20)
    p.add_argument("--device", default="cpu", help="Execution device: cpu or cuda")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Timestamp extraction
# ---------------------------------------------------------------------------

def _extract_timestamp_from_frame(frame, ocr_reader) -> datetime | None:
    """
    Read the wall-clock timestamp burned into the top-right corner of a camera
    frame by the CP IP Cam firmware (format: DD/MM/YYYY HH:MM:SS).

    Uses EasyOCR passed in from the caller so the model is only loaded once per
    pipeline run, not once per frame.
    """
    if frame is None or ocr_reader is None:
        return None

    h, w = frame.shape[:2]

    # Timestamp sits in the top-right corner.
    # Crop: y from 4%-10% of height, x from 74%-97% of width.
    crop = frame[int(h * 0.04):int(h * 0.10), int(w * 0.74):int(w * 0.97)]

    try:
        results = ocr_reader.readtext(crop, detail=0)
        raw_text = " ".join(str(r) for r in results)
    except Exception:
        return None

    if not raw_text.strip():
        return None

    # Strategy A: clean separator match — DD/MM/YYYY HH:MM:SS
    m = re.search(
        r"(\d{1,2})[/\-](\d{2})[/\-](\d{4})\s+(\d{2}):(\d{2}):(\d{2})",
        raw_text,
    )

    # Strategy B: separators garbled by OCR — strip to digits and match positionally
    if not m:
        digits = re.sub(r"[^\d]", "", raw_text)
        m = re.search(r"(\d{1,2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})", digits)

    if not m:
        return None

    try:
        day, month, year, hour, minute, second = (int(x) for x in m.groups())
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def _init_ocr(use_gpu: bool):
    """
    Initialise EasyOCR. GPU is used when available and requested; falls back to
    CPU silently so the pipeline never crashes on a CPU-only machine.
    """
    try:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=use_gpu, verbose=False)
        logger.info(f"EasyOCR initialised (gpu={use_gpu})")
        return reader
    except Exception as exc:
        logger.warning(f"EasyOCR init failed ({exc}); timestamp auto-detection disabled")
        return None


def get_clip_start_time(clip_start_arg: str | None, video_path: str, ocr_reader) -> datetime:
    """
    Determine the wall-clock start time for a video clip.

    Priority order:
      1. Explicit --clip-start argument (ISO 8601 string)
      2. Timestamp burned into the first few frames, read via EasyOCR
      3. Hardcoded fallback keyed on filename pattern (last resort)
    """
    if clip_start_arg:
        return datetime.fromisoformat(clip_start_arg.replace("Z", "+00:00"))

    # Try auto-detection from the first five frames
    detected = None
    cap = cv2.VideoCapture(video_path)
    if cap.isOpened():
        for _ in range(5):
            ret, frame = cap.read()
            if not ret:
                break
            detected = _extract_timestamp_from_frame(frame, ocr_reader)
            if detected:
                logger.info(f"Auto-detected clip start: {detected.isoformat()} ({Path(video_path).name})")
                break
        cap.release()

    if detected:
        return detected

    # Hardcoded fallback — only reached if OCR fails or EasyOCR is not installed
    logger.warning(f"Could not auto-detect timestamp from {Path(video_path).name}, using hardcoded fallback")
    video_name = Path(video_path).name.lower()
    if "cam" in video_name:
        # Store 1 clips — footage recorded ~20:00 on 10 Apr 2026
        return datetime(2026, 4, 10, 20, 0, 0, tzinfo=timezone.utc)
    # Store 2 clips — footage recorded ~13:39 on 08 Mar 2026
    return datetime(2026, 3, 8, 13, 39, 0, tzinfo=timezone.utc)


def frame_to_timestamp(clip_start: datetime, frame_idx: int, fps: float) -> datetime:
    return clip_start + timedelta(seconds=frame_idx / fps)


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _load_camera(layout_path: str, camera_id: str) -> dict:
    try:
        with open(layout_path) as f:
            layout = json.load(f)
        return layout.get("cameras", {}).get(camera_id, {})
    except Exception:
        return {}


def is_storeroom_camera(layout_path: str, camera_id: str) -> bool:
    return _load_camera(layout_path, camera_id).get("exclude_from_metrics", False)


def get_entry_line_ratio(layout_path: str, camera_id: str) -> float | None:
    return _load_camera(layout_path, camera_id).get("entry_line_y_ratio", 0.40)


def get_entry_inward_direction(layout_path: str, camera_id: str) -> str:
    """
    Returns 'down' (y increases = entering) or 'up' (y decreases = entering).

    Default is 'down': camera mounted inside store facing outward, mall at top.
    Store 1 CAM_ENTRY_01 is 'up': camera angled from inside-right, person
    entering moves from bottom-right toward top-left (y decreases).
    Set per-camera in store_layout.json via "entry_inward_direction".
    """
    return _load_camera(layout_path, camera_id).get("entry_inward_direction", "down")


def _resolve_layout_path(args) -> str:
    if args.layout:
        return args.layout
    return str(Path(args.video).resolve().parent / "store_layout.json")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(args):
    from ultralytics import YOLO

    args.layout = _resolve_layout_path(args)

    if is_storeroom_camera(args.layout, args.camera):
        logger.info(f"Camera {args.camera} is marked exclude_from_metrics=true — skipping.")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).touch()
        return

    use_gpu = "cuda" in args.device.lower()
    ocr_reader = _init_ocr(use_gpu)

    logger.info(f"Loading YOLOv8s on {args.device}")
    model = YOLO("yolov8s.pt")

    black_ranges = [
        (np.array([0,   0,   0]), np.array([180, 255, 45])),
        (np.array([0,   0,   0]), np.array([180,  50, 45])),
    ]
    staff_detector = StaffDetector(uniform_ranges=black_ranges, store_id=args.store)
    zone_mapper = ZoneMapper(args.layout, args.store, args.camera)
    tracker = MultiObjectTracker(reid_enabled=True)
    emitter = EventEmitter(
        store_id=args.store,
        camera_id=args.camera,
        output_path=args.output,
        api_url=args.api_url,
    )

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {args.video}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    clip_start = get_clip_start_time(args.clip_start, args.video, ocr_reader)

    is_entry_camera = "ENTRY" in args.camera.upper()
    entry_line_ratio = get_entry_line_ratio(args.layout, args.camera)
    entry_inward_direction = get_entry_inward_direction(args.layout, args.camera)

    logger.info(
        f"Processing {total_frames} frames @ {fps:.1f}fps | "
        f"store={args.store} cam={args.camera} | "
        f"entry_cam={is_entry_camera} entry_line_ratio={entry_line_ratio} "
        f"entry_inward={entry_inward_direction} | clip_start={clip_start.isoformat()}"
    )

    PROCESS_EVERY_N = 2
    frame_idx = 0
    visitor_state: dict = {}

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % PROCESS_EVERY_N != 0:
            continue

        timestamp = frame_to_timestamp(clip_start, frame_idx, fps)

        results = model(
            frame,
            classes=[0],
            conf=args.conf,
            device=args.device,
            verbose=False,
        )[0]

        detections = []
        for box in results.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            detections.append({"bbox": [x1, y1, x2, y2], "confidence": float(box.conf[0])})

        tracked_objects = tracker.update(
            detections,
            frame,
            timestamp,
            entry_line_ratio=entry_line_ratio,
            entry_inward_direction=entry_inward_direction,
        )

        # Emit synthetic ZONE_EXIT for tracks pruned this frame.
        # Without this, dwell time for the last zone of every session is lost.
        for exited_track in tracker.exited_tracks:
            if getattr(exited_track, "_needs_zone_exit", False):
                exited_track._needs_zone_exit = False
                vid = exited_track.visitor_id
                vs = visitor_state.get(vid)
                if vs and vs.get("prev_zone"):
                    _sd = vs.get("staff_decisions", [])
                    _is_staff = (sum(_sd) / len(_sd)) >= 0.45 if _sd else False
                    emitter.emit(
                        visitor_id=vid,
                        event_type="ZONE_EXIT",
                        timestamp=timestamp,
                        zone_id=vs["prev_zone"],
                        dwell_ms=int(vs["zone_dwell_ms"]),
                        is_staff=_is_staff,
                        confidence=exited_track.confidence,
                    )
                    vs["prev_zone"] = None
                    vs["zone_dwell_ms"] = 0.0

        for obj in tracked_objects:
            visitor_id = obj["visitor_id"]
            bbox = obj["bbox"]
            confidence = obj["confidence"]
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2

            if visitor_id not in visitor_state:
                visitor_state[visitor_id] = {
                    "prev_zone": None,
                    "zone_dwell_ms": 0.0,
                    "dwell_emits": 0,
                    "queue_joined": False,
                    "staff_decisions": [],
                }
            vs = visitor_state[visitor_id]

            # Rolling staff vote — capped at last 30 frames to prevent unbounded growth
            is_staff_frame = staff_detector.is_staff(frame, bbox)
            vs["staff_decisions"].append(is_staff_frame)
            if len(vs["staff_decisions"]) > 30:
                vs["staff_decisions"] = vs["staff_decisions"][-30:]
            is_staff = (sum(vs["staff_decisions"]) / len(vs["staff_decisions"])) >= 0.45

            zone_id = zone_mapper.get_zone(cx, cy)

            # Entry / exit line crossing
            if is_entry_camera and obj.get("just_crossed"):
                direction = obj.get("direction")
                if direction == "INWARD":
                    is_reentry = tracker.is_reentry(visitor_id)
                    emitter.emit(
                        visitor_id=visitor_id,
                        event_type="REENTRY" if is_reentry else "ENTRY",
                        timestamp=timestamp,
                        zone_id=None,
                        dwell_ms=0,
                        is_staff=is_staff,
                        confidence=confidence,
                    )
                elif direction == "OUTWARD":
                    emitter.emit(
                        visitor_id=visitor_id,
                        event_type="EXIT",
                        timestamp=timestamp,
                        zone_id=None,
                        dwell_ms=0,
                        is_staff=is_staff,
                        confidence=confidence,
                    )

            # Zone tracking
            if zone_id and zone_id not in ("ENTRY_EXTERIOR",):
                prev_zone = vs["prev_zone"]

                if prev_zone != zone_id:
                    if prev_zone:
                        emitter.emit(
                            visitor_id=visitor_id,
                            event_type="ZONE_EXIT",
                            timestamp=timestamp,
                            zone_id=prev_zone,
                            dwell_ms=int(vs["zone_dwell_ms"]),
                            is_staff=is_staff,
                            confidence=confidence,
                        )
                        # NOTE: BILLING_QUEUE_ABANDON is not emitted here.
                        # The pipeline cannot distinguish a genuine abandonment from a
                        # completed purchase — it has no POS data. The ingestion layer
                        # handles this retroactively after the 5-minute POS window.
                        if prev_zone.upper() in BILLING_ZONES_SET and vs.get("queue_joined") and not is_staff:
                            vs["queue_joined"] = False

                    emitter.emit(
                        visitor_id=visitor_id,
                        event_type="ZONE_ENTER",
                        timestamp=timestamp,
                        zone_id=zone_id,
                        dwell_ms=0,
                        is_staff=is_staff,
                        confidence=confidence,
                    )
                    vs["prev_zone"] = zone_id
                    vs["zone_dwell_ms"] = 0.0
                    vs["dwell_emits"] = 0

                vs["zone_dwell_ms"] += (PROCESS_EVERY_N / fps) * 1000

                dwell_intervals = int(vs["zone_dwell_ms"] / 30000)
                if dwell_intervals > vs["dwell_emits"]:
                    emitter.emit(
                        visitor_id=visitor_id,
                        event_type="ZONE_DWELL",
                        timestamp=timestamp,
                        zone_id=zone_id,
                        dwell_ms=int(vs["zone_dwell_ms"]),
                        is_staff=is_staff,
                        confidence=confidence,
                    )
                    vs["dwell_emits"] = dwell_intervals

                if zone_id.upper() in BILLING_ZONES_SET:
                    others_in_billing = sum(
                        1
                        for vid2, other_vs in visitor_state.items()
                        if vid2 != visitor_id
                        and (other_vs.get("prev_zone") or "").upper() in BILLING_ZONES_SET
                    )
                    queue_depth = others_in_billing + 1

                    if not vs["queue_joined"]:
                        emitter.emit(
                            visitor_id=visitor_id,
                            event_type="BILLING_QUEUE_JOIN",
                            timestamp=timestamp,
                            zone_id=zone_id,
                            dwell_ms=0,
                            is_staff=is_staff,
                            confidence=confidence,
                            queue_depth=queue_depth,
                        )
                        vs["queue_joined"] = True
                else:
                    vs["queue_joined"] = False

        if frame_idx % 450 == 0:
            pct = (frame_idx / total_frames) * 100 if total_frames else 0
            logger.info(f"Progress: {pct:.1f}% | frame {frame_idx}/{total_frames} | tracked={len(tracker.tracks)}")

    cap.release()
    emitter.flush()
    logger.info(f"Pipeline complete -> {args.output}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    run_pipeline(args)

