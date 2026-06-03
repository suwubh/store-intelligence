"""
Tracker module — wraps ByteTrack-style tracking with appearance Re-ID.
Falls back to centroid + IoU tracking if ByteTrack is unavailable or the
installed Ultralytics tracker API does not match the expected signature.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 10-minute window for Re-ID matching after exit
REID_MAX_ABSENCE_SECONDS = 600
REID_APPEARANCE_THRESHOLD = 0.75
ENTRY_LINE_RATIO = 0.40


class TrackState:
    def __init__(self, visitor_id: str, bbox: list, timestamp: datetime, appearance: np.ndarray | None):
        self.visitor_id = visitor_id
        self.bbox = bbox
        self.last_seen = timestamp
        self.appearance = appearance
        self.centroid_history: list[tuple] = []
        self.prev_zone: str | None = None
        self.zone_dwell_ms: float = 0
        self.dwell_emits: int = 0
        self.queue_joined: bool = False
        self.just_crossed: bool = False
        self.direction: str | None = None
        self.last_y: float | None = None
        self.is_active: bool = True
        self.confidence: float = 1.0
        # Track state helper to handle re-entries correctly
        self.was_reentry: bool = False


class MultiObjectTracker:
    def __init__(self, reid_enabled: bool = True, max_lost_frames: int = 30):
        self.tracks: dict[str, TrackState] = {}
        self.exited_tracks: list[TrackState] = []
        self.reid_enabled = reid_enabled
        self.max_lost_frames = max_lost_frames
        self._lost_counters: dict[str, int] = defaultdict(int)

        self._use_bytetrack = False
        self._bytetracker = None
        self._bt_args = None

        try:
            from ultralytics.trackers.byte_tracker import BYTETracker

            self._bt_args = _ByteTrackArgs()
            self._bytetracker = BYTETracker(self._bt_args)
            self._use_bytetrack = True
            logger.info("ByteTrack initialised")
        except Exception as e:
            logger.warning(f"ByteTrack unavailable ({e}), using IoU tracker")

    def update(
        self,
        detections: list[dict],
        frame: np.ndarray,
        timestamp: datetime,
        entry_line_ratio: float = ENTRY_LINE_RATIO,
        entry_inward_direction: str = "down",
    ) -> list[dict]:
        frame_h, frame_w = frame.shape[:2]

        entry_line_y = None
        if entry_line_ratio is not None:
            entry_line_y = int(frame_h * entry_line_ratio)

        if self._use_bytetrack:
            tracked = self._bytetrack_update(detections, frame, timestamp, frame_h, frame_w)
            if tracked is None:
                tracked = self._iou_update(detections, frame, timestamp)
        else:
            tracked = self._iou_update(detections, frame, timestamp)

        results = []
        for track in tracked:
            visitor_id = track["visitor_id"]
            state = self.tracks.get(visitor_id)
            if state is None:
                continue

            cy = (track["bbox"][1] + track["bbox"][3]) / 2
            state.just_crossed = False

            # Only entry cameras perform line-crossing logic
            if entry_line_y is not None:
                if state.last_y is not None:
                    if entry_inward_direction == "down":
                        # Camera faces outward, exterior at top — entering moves y downward
                        if state.last_y < entry_line_y <= cy:
                            state.direction = "INWARD"
                            state.just_crossed = True
                        elif state.last_y > entry_line_y >= cy:
                            state.direction = "OUTWARD"
                            state.just_crossed = True
                    else:  # "up"
                        # Camera angled inside-right — entering moves y upward
                        if state.last_y > entry_line_y >= cy:
                            state.direction = "INWARD"
                            state.just_crossed = True
                        elif state.last_y < entry_line_y <= cy:
                            state.direction = "OUTWARD"
                            state.just_crossed = True

            state.last_y = cy

            results.append(
                {
                    "visitor_id": visitor_id,
                    "bbox": track["bbox"],
                    "confidence": track["confidence"],
                    "direction": state.direction,
                    "just_crossed": state.just_crossed,
                    "prev_zone": state.prev_zone,
                    "zone_dwell_ms": state.zone_dwell_ms,
                    "dwell_emits": state.dwell_emits,
                    "queue_joined": state.queue_joined,
                }
            )

        self._prune_lost(timestamp)
        return results

    def _bytetrack_update(self, detections, frame, timestamp, frame_h, frame_w):
        if not detections:
            return []

        payloads = self._build_bytetrack_payloads(detections, frame_h, frame_w)
        if not payloads:
            return None

        call_variants = []
        for payload_name, payload in payloads:
            call_variants.extend([
                (payload_name, payload, (frame_h, frame_w), (frame_h, frame_w)),
                (payload_name, payload, [frame_h, frame_w], [frame_h, frame_w]),
                (payload_name, payload, frame, None),
                (payload_name, payload, None, None),
            ])

        last_error = None
        for _, payload, a, b in call_variants:
            try:
                if a is None and b is None:
                    online_targets = self._bytetracker.update(payload)
                elif b is None:
                    online_targets = self._bytetracker.update(payload, a)
                else:
                    online_targets = self._bytetracker.update(payload, a, b)

                parsed = self._parse_bytetrack_targets(online_targets, frame, timestamp)
                if parsed is not None:
                    return parsed
            except TypeError as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                continue

        logger.warning(f"ByteTrack update failed ({last_error}); using IoU tracker")
        return None

    def _build_bytetrack_payloads(self, detections, frame_h, frame_w):
        dets = np.array([[*d["bbox"], d["confidence"], 0.0] for d in detections], dtype=np.float32)
        payloads = []

        try:
            from ultralytics.engine.results import Boxes
            payloads.append(("boxes", Boxes(np.ascontiguousarray(dets), orig_shape=(frame_h, frame_w))))
        except Exception:
            pass

        payloads.append(("ndarray", np.ascontiguousarray(dets)))

        try:
            import torch
            payloads.append(("tensor", torch.as_tensor(dets)))
        except Exception:
            pass

        return payloads

    def _parse_bytetrack_targets(self, online_targets, frame, timestamp):
        if online_targets is None:
            return None

        results = []
        for t in online_targets:
            if hasattr(t, "tlwh"):
                tlwh = t.tlwh
                x1, y1 = float(tlwh[0]), float(tlwh[1])
                x2, y2 = x1 + float(tlwh[2]), y1 + float(tlwh[3])
                track_id = str(getattr(t, "track_id", "0"))
                score = float(getattr(t, "score", getattr(t, "conf", 1.0)))
            else:
                row = np.asarray(t).astype(float).ravel()
                if row.size < 4:
                    continue
                x1, y1, x2, y2 = row[:4]
                if row.size >= 5:
                    track_id = str(int(row[4]))
                else:
                    track_id = f"T{len(results)}"
                score = float(row[5]) if row.size > 5 else 1.0

            visitor_id = self._get_or_create_visitor(track_id, [x1, y1, x2, y2], frame, timestamp, score)
            results.append(
                {
                    "visitor_id": visitor_id,
                    "bbox": [x1, y1, x2, y2],
                    "confidence": score,
                }
            )

        return results

    def _iou_update(self, detections, frame, timestamp) -> list[dict]:
        if not detections:
            for vid in list(self.tracks.keys()):
                self._lost_counters[vid] += 1
            return []

        track_ids = list(self.tracks.keys())
        matched_tracks = set()
        matched_dets = set()
        results = []

        if track_ids:
            iou_matrix = np.zeros((len(track_ids), len(detections)))
            for i, tid in enumerate(track_ids):
                for j, det in enumerate(detections):
                    iou_matrix[i, j] = _iou(self.tracks[tid].bbox, det["bbox"])

            while True:
                if iou_matrix.size == 0 or iou_matrix.max() < 0.3:
                    break
                i, j = np.unravel_index(iou_matrix.argmax(), iou_matrix.shape)
                tid = track_ids[i]
                if tid not in matched_tracks and j not in matched_dets:
                    matched_tracks.add(tid)
                    matched_dets.add(j)
                    det = detections[j]
                    self.tracks[tid].bbox = det["bbox"]
                    self.tracks[tid].last_seen = timestamp
                    self.tracks[tid].confidence = det["confidence"]
                    self._lost_counters[tid] = 0
                    results.append(
                        {
                            "visitor_id": tid,
                            "bbox": det["bbox"],
                            "confidence": det["confidence"],
                        }
                    )
                iou_matrix[i, :] = -1
                iou_matrix[:, j] = -1

        for tid in track_ids:
            if tid not in matched_tracks:
                self._lost_counters[tid] += 1

        for j, det in enumerate(detections):
            if j not in matched_dets:
                appearance = _extract_appearance(frame, det["bbox"])
                visitor_id = self._new_visitor(det["bbox"], appearance, timestamp, det["confidence"])
                results.append(
                    {
                        "visitor_id": visitor_id,
                        "bbox": det["bbox"],
                        "confidence": det["confidence"],
                    }
                )

        return results

    def _get_or_create_visitor(self, track_id, bbox, frame, timestamp, score) -> str:
        track_id_str = str(track_id)
        if track_id_str.isdigit():
            track_id_str = f"VIS_{track_id_str}"

        if track_id_str in self.tracks:
            state = self.tracks[track_id_str]
            state.bbox = bbox
            state.last_seen = timestamp
            state.confidence = score
            self._lost_counters[track_id_str] = 0
            return track_id_str

        appearance = _extract_appearance(frame, bbox)
        return self._new_visitor(bbox, appearance, timestamp, score, preferred_id=track_id_str)


    def _new_visitor(self, bbox, appearance, timestamp, confidence, preferred_id=None) -> str:
        # Re-entry detection: search exited tracks for appearance matching
        if self.reid_enabled and appearance is not None:
            for exited in list(self.exited_tracks):
                if exited.appearance is None:
                    continue
                sim = _cosine_similarity(appearance, exited.appearance)
                time_since_exit = (timestamp - exited.last_seen).total_seconds()
                if sim > REID_APPEARANCE_THRESHOLD and time_since_exit < REID_MAX_ABSENCE_SECONDS:
                    vid = exited.visitor_id
                    self.exited_tracks.remove(exited)

                    new_state = TrackState(vid, bbox, timestamp, appearance)
                    new_state.confidence = confidence
                    new_state.was_reentry = True   # signal for is_reentry() to consume
                    self.tracks[vid] = new_state
                    return vid

        vid = preferred_id or f"VIS_{uuid.uuid4().hex[:6]}"
        state = TrackState(vid, bbox, timestamp, appearance)
        state.confidence = confidence
        self.tracks[vid] = state
        return vid

    def is_reentry(self, visitor_id: str) -> bool:
        # Consume and clear the was_reentry flag from the active track state
        state = self.tracks.get(visitor_id)
        if state and state.was_reentry:
            state.was_reentry = False
            return True
        return False

    def get_queue_depth(self) -> int:
        billing_tracks = sum(
            1
            for t in self.tracks.values()
            if t.prev_zone and t.prev_zone.upper() in ("BILLING", "BILLING_COUNTER", "CHECKOUT")
        )
        return max(0, billing_tracks - 1)

    def _prune_lost(self, timestamp: datetime):
        to_remove = []
        for vid, count in self._lost_counters.items():
            if count > self.max_lost_frames:
                if vid in self.tracks:
                    track = self.tracks[vid]
                    # Signal that this track needs a synthetic ZONE_EXIT before pruning
                    # Callers can check exited_tracks[-1].prev_zone after calling _prune_lost
                    track._needs_zone_exit = True
                    self.exited_tracks.append(track)
                    self.exited_tracks = self.exited_tracks[-50:]
                to_remove.append(vid)

        for vid in to_remove:
            self.tracks.pop(vid, None)
            self._lost_counters.pop(vid, None)


def _iou(boxA: list, boxB: list) -> float:
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    if inter == 0:
        return 0.0
    areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    return inter / (areaA + areaB - inter)


def _extract_appearance(frame: np.ndarray, bbox: list) -> np.ndarray | None:
    try:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            return None
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        return hist.flatten()
    except Exception:
        return None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class _ByteTrackArgs:
    tracker_type = "bytetrack"
    track_high_thresh = 0.25
    track_low_thresh = 0.10
    new_track_thresh = 0.25
    track_buffer = 30
    match_thresh = 0.8
    fuse_score = True
    mot20 = False