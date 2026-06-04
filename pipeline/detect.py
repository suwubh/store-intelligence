#!/usr/bin/env python3
"""
Main detection pipeline.
Usage: python -m pipeline.detect --video path/to/clip.mp4 --store ST1008 --camera CAM_ENTRY_01 --device cuda
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reconfigure stdout/stderr to use UTF-8 to prevent UnicodeEncodeError on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import cv2
import torch

from pipeline.tracker import MultiObjectTracker
from pipeline.emit import EventEmitter
from pipeline.zone_mapper import ZoneMapper
from pipeline.staff_detector import StaffDetector

logger = logging.getLogger(__name__)

BILLING_ZONES_SET = {"BILLING_COUNTER", "BILLING_QUEUE", "BILLING"}

# Global EasyOCR hook initialized dynamically inside run_pipeline()
ocr_reader = None


def parse_args():
    p = argparse.ArgumentParser(description="CCTV detection pipeline")
    p.add_argument("--video", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--camera", required=True)
    p.add_argument("--layout", default=None, help="Path to store_layout.json (default: beside the video)")
    p.add_argument("--output", default="dataset/events.jsonl")
    p.add_argument("--clip-start", default=None)
    p.add_argument("--use-ocr", action="store_true", help="Attempt to extract timestamp using OCR")
    p.add_argument("--api-url", default=None)
    p.add_argument("--conf", type=float, default=0.20)
    p.add_argument("--device", default="auto", help="Execution device: auto/cpu/cuda")
    return p.parse_args()


def _extract_timestamp_from_frame(frame) -> datetime | None:
    """
    Extracts a timezone-aware timestamp from the top-right corner of a video frame
    using a deep learning scene-text parsing engine and dual fallback structural regex anchors.
    """
    global ocr_reader
    import re

    if frame is None:
        return None

    # Lazy-loaded default fallback configuration if accessed outside run_pipeline sequence
    if ocr_reader is None:
        import easyocr
        ocr_reader = easyocr.Reader(['en'], gpu=False)

    h, w = frame.shape[:2]
    
    # 1. Take a generous crop of the upper-right corner to safeguard against font-shift variations
    crop = frame[0:int(h * 0.15), int(w * 0.65):w]

    # 2. Extract spatial text blocks (detail=0 returns raw text string blocks, minimizing execution overhead)
    try:
        results = ocr_reader.readtext(crop, detail=0)
        raw_text = " ".join(results)
    except Exception:
        return None

    if not raw_text:
        return None

    # 3. Match Strategy A: Continuous structural parsing pattern DD/MM/YYYY HH:MM:SS
    match = re.search(
        r"(\d{2})[\/\-\s](\d{2})[\/\-\s](\d{4})\s+(\d{2}):(\d{2}):(\d{2})", 
        raw_text
    )
    
    # 4. Match Strategy B (Fallback): Heal compressed or segmented digital digit streams
    if not match:
        digits_only = re.sub(r"[^\d]", "", raw_text)
        match = re.search(r"(\d{2})(\d{2})(2026)(\d{2})(\d{2})(\d{2})", digits_only)
        if not match:
            return None

    try:
        day, month, year, hour, minute, second = (int(x) for x in match.groups())
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def get_clip_start_time(clip_start_arg, video_path, use_ocr):
    """
    Determine the wall-clock start time of a video clip.
    Priority:
      1. Explicit --clip-start argument
      2. Timestamp burned into the first frame by the camera (auto-detected if --use-ocr is passed)
    """
    if clip_start_arg:
        return datetime.fromisoformat(clip_start_arg.replace("Z", "+00:00"))

    if use_ocr:
        # Try reading timestamp from the first frame of the video
        cap = cv2.VideoCapture(video_path)
        detected = None
        if cap.isOpened():
            # Try first 5 frames in case frame 0 is dark/blank
            for _ in range(5):
                ret, frame = cap.read()
                if not ret:
                    break
                detected = _extract_timestamp_from_frame(frame)
                if detected:
                    logger.info(f"Auto-detected clip start from frame: {detected.isoformat()}")
                    break
            cap.release()

        if detected:
            return detected
            
        logger.warning(f"Could not auto-detect timestamp from {Path(video_path).name} using OCR.")

    raise ValueError(f"Could not determine timestamp from {Path(video_path).name}. Please provide --clip-start.")


def frame_to_timestamp(clip_start, frame_idx, fps):
    return clip_start + timedelta(seconds=frame_idx / fps)


def is_storeroom_camera(layout_path, camera_id):
    """Check if this camera should be excluded from metrics (storeroom etc)."""
    try:
        with open(layout_path) as f:
            layout = json.load(f)
        cam = layout.get("cameras", {}).get(camera_id, {})
        return cam.get("exclude_from_metrics", False)
    except Exception:
        return False


def get_entry_line_ratio(layout_path, camera_id):
    """Get entry line Y ratio from layout, default 0.40."""
    try:
        with open(layout_path) as f:
            layout = json.load(f)
        cam = layout.get("cameras", {}).get(camera_id, {})
        return cam.get("entry_line_y_ratio", 0.40)
    except Exception:
        return 0.40


def get_entry_inward_direction(layout_path, camera_id):
    """
    Returns 'down' (y increases when entering) or 'up' (y decreases when entering).
    """
    try:
        with open(layout_path) as f:
            layout = json.load(f)
        cam = layout.get("cameras", {}).get(camera_id, {})
        return cam.get("entry_inward_direction", "down")
    except Exception:
        return "down"


def _resolve_layout_path(args) -> str:
    if args.layout:
        return args.layout
    video_dir = Path(args.video).resolve().parent
    return str(video_dir / "store_layout.json")


def resolve_device(requested: str | None) -> str:
    requested = (requested or "auto").lower()

    if requested in ("cpu",):
        return "cpu"

    # auto / cuda / gpu => try GPU first, otherwise CPU
    try:
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass

    return "cpu"


def infer_frame(frame, model, conf, device):
    try:
        return model(frame, classes=[0], conf=conf, device=device, verbose=False)[0]
    except Exception as e:
        if device == "cuda":
            logger.warning(f"CUDA inference failed ({e}); retrying on CPU.")
            return model(frame, classes=[0], conf=conf, device="cpu", verbose=False)[0]
        raise


def run_pipeline(args):
    global ocr_reader
    from ultralytics import YOLO
    import easyocr
    import numpy as np

    args.layout = _resolve_layout_path(args)

    if is_storeroom_camera(args.layout, args.camera):
        logger.info(f"Camera {args.camera} is marked exclude_from_metrics=true. Skipping.")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).touch()
        return

    # -----------------------------------------------------------------
    # DYNAMIC INITIALIZATION & MODEL WARMUP SEQUENCE
    # -----------------------------------------------------------------
    runtime_device = resolve_device(args.device)
    use_gpu = runtime_device == "cuda"
    if args.use_ocr:
        logger.info(f"Initializing EasyOCR Engine (GPU Support = {use_gpu})...")
        try:
            ocr_reader = easyocr.Reader(['en'], gpu=use_gpu)
            dev_str = ocr_reader.device if isinstance(ocr_reader.device, str) else getattr(ocr_reader.device, "type", str(ocr_reader.device))
            logger.info(f"EasyOCR successfully mapped to execution target: [{dev_str.upper()}]")
        except Exception as e:
            logger.warning(f"GPU hardware initialization failure for OCR: {e}. Falling back to CPU mode.")
            ocr_reader = easyocr.Reader(['en'], gpu=False)
            runtime_device = "cpu"

    logger.info(f"Loading YOLOv8s on {runtime_device}")
    model = YOLO("yolov8s.pt")
    if runtime_device == "cuda":
        model.to("cuda")

    staff_detector = StaffDetector(store_id=args.store)

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
    clip_start = get_clip_start_time(args.clip_start, args.video, args.use_ocr)

    is_entry_camera = "ENTRY" in args.camera.upper()
    entry_line_ratio = get_entry_line_ratio(args.layout, args.camera)
    entry_inward_direction = get_entry_inward_direction(args.layout, args.camera)

    logger.info(
        f"Processing {total_frames} frames @ {fps:.1f}fps | "
        f"store={args.store} cam={args.camera} | "
        f"entry_cam={is_entry_camera} entry_line_ratio={entry_line_ratio}"
    )

    frame_idx = 0
    PROCESS_EVERY_N = 2
    visitor_state = {}

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % PROCESS_EVERY_N != 0:
            continue

        timestamp = frame_to_timestamp(clip_start, frame_idx, fps)

        results = infer_frame(frame, model, args.conf, runtime_device)

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
                    if vs["prev_zone"].upper() in BILLING_ZONES_SET and vs.get("queue_joined") and not _is_staff:
                        emitter.emit(
                            visitor_id=vid,
                            event_type="BILLING_QUEUE_ABANDON",
                            timestamp=timestamp,
                            zone_id=vs["prev_zone"],
                            dwell_ms=int(vs["zone_dwell_ms"]),
                            is_staff=_is_staff,
                            confidence=exited_track.confidence,
                        )
                        vs["queue_joined"] = False
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

            is_staff_frame = staff_detector.is_staff(frame, bbox)
            vs["staff_decisions"].append(is_staff_frame)
            if len(vs["staff_decisions"]) > 30:
                vs["staff_decisions"] = vs["staff_decisions"][-30:]
            is_staff = (sum(vs["staff_decisions"]) / len(vs["staff_decisions"])) >= 0.45

            zone_id = zone_mapper.get_zone(cx, cy)

            if is_entry_camera:
                direction = obj.get("direction")
                if obj.get("just_crossed"):
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

                        if prev_zone.upper() in BILLING_ZONES_SET and vs.get("queue_joined") and not is_staff:
                            emitter.emit(
                                visitor_id=visitor_id,
                                event_type="BILLING_QUEUE_ABANDON",
                                timestamp=timestamp,
                                zone_id=prev_zone,
                                dwell_ms=int(vs["zone_dwell_ms"]),
                                is_staff=is_staff,
                                confidence=confidence,
                            )
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
                    others_waiting = sum(
                        1
                        for vid2, other_vs in visitor_state.items()
                        if vid2 != visitor_id
                        and (other_vs.get("prev_zone") or "").upper() in BILLING_ZONES_SET
                    )
                    queue_depth = others_waiting + 1

                    if not vs["queue_joined"] and others_waiting > 0:
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
    logger.info(f"Pipeline complete → {args.output}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    run_pipeline(args)