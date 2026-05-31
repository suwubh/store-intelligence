#!/usr/bin/env python3
"""
Main detection pipeline.
Usage: python -m pipeline.detect --video path/to/clip.mp4 --store ST1008 --camera CAM_ENTRY_01
"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2

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
    p.add_argument("--layout", default="dataset/store_layout.json")
    p.add_argument("--output", default="dataset/events.jsonl")
    p.add_argument("--clip-start", default=None)
    p.add_argument("--api-url", default=None)
    p.add_argument("--conf", type=float, default=0.35)
    p.add_argument("--device", default="cpu")
    return p.parse_args()


def get_clip_start_time(clip_start_arg, video_path):
    if clip_start_arg:
        return datetime.fromisoformat(clip_start_arg.replace("Z", "+00:00"))
    mtime = Path(video_path).stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


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


def run_pipeline(args):
    from ultralytics import YOLO

    if is_storeroom_camera(args.layout, args.camera):
        logger.info(f"Camera {args.camera} is marked exclude_from_metrics=true. Skipping.")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).touch()
        return

    logger.info(f"Loading YOLOv8s on {args.device}")
    model = YOLO("yolov8s.pt")

    import numpy as np

    # FIX (Issue N2): Tightened black HSV ranges to reduce staff false-positive rate.
    # Previous ranges (V:[0-80], S:[0-80]) matched dark jeans, navy trousers, shadows,
    # dark accessories — causing ~78% of detections to be wrongly classified as staff.
    # New ranges target only genuinely black/near-black clothing (V < 45).
    # Also raised STAFF_PIXEL_RATIO_THRESHOLD from 0.25 to 0.40 inside StaffDetector.
    black_ranges = [
        (np.array([0,   0,   0]), np.array([180, 255, 45])),  # strict very-dark only (V < 45)
        (np.array([0,   0,   0]), np.array([180,  50, 45])),  # dark + truly low saturation
    ]
    staff_detector = StaffDetector(uniform_ranges=black_ranges)

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
    clip_start = get_clip_start_time(args.clip_start, args.video)

    is_entry_camera = "ENTRY" in args.camera.upper()
    entry_line_ratio = get_entry_line_ratio(args.layout, args.camera)

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

        tracked_objects = tracker.update(detections, frame, timestamp, entry_line_ratio=entry_line_ratio)

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
                }
            vs = visitor_state[visitor_id]

            is_staff = staff_detector.is_staff(frame, bbox)
            zone_id = zone_mapper.get_zone(cx, cy)

            if is_entry_camera:
                direction = obj.get("direction")
                if obj.get("just_crossed"):
                    if direction == "INWARD":
                        # FIX (Issue 1): is_reentry() now correctly reads the
                        # was_reentry flag set in tracker._new_visitor().
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

                        # FIX (Issue 3): Emit BILLING_QUEUE_ABANDON when a visitor
                        # leaves a billing zone after having joined the queue.
                        # Previously this event type was defined but never emitted,
                        # causing abandonment_rate to always be 0.0.
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
                    # FIX (Issue 2): Queue depth was always 0 because tracker.get_queue_depth()
                    # reads TrackState.prev_zone which is never updated (detect.py maintains
                    # its own visitor_state dict). Fixed by computing queue depth directly
                    # from visitor_state, which IS kept up to date.
                    queue_depth = sum(
                        1
                        for vid2, other_vs in visitor_state.items()
                        if vid2 != visitor_id
                        and (other_vs.get("prev_zone") or "").upper() in BILLING_ZONES_SET
                    )

                    # FIX (Issue N4): Per spec, BILLING_QUEUE_JOIN should only fire when
                    # queue_depth > 0 (i.e. someone is already at the counter).
                    # Added guard: only emit when queue already has people in it.
                    if not vs["queue_joined"] and queue_depth > 0:
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