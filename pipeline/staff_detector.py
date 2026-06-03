"""
Staff detector — uses HSV color range to detect store uniforms.
Color ranges are calibrated once you see the actual footage.
Falls back to a conservative False (not staff) if frame crop is too small.
"""
import logging
import numpy as np
import cv2
from typing import Optional

logger = logging.getLogger(__name__)

BLACK_UNIFORM_RANGES = [
    # Brigade Bangalore (ST1008): staff wear all-black uniforms
    # Strict very-dark only (V < 45) — avoids dark jeans/shadows on customers
    (np.array([0,   0,   0]), np.array([180, 255, 45])),
    # Dark + truly low saturation (charcoal/near-black fabrics)
    (np.array([0,   0,   0]), np.array([180,  50, 45])),
]
DEFAULT_UNIFORM_RANGES = BLACK_UNIFORM_RANGES

PINK_TOP_RANGES = [
    (np.array([135, 35, 80]), np.array([175, 255, 255])),
    (np.array([0, 35, 80]), np.array([12, 255, 255])),
]

STAFF_PIXEL_RATIO_THRESHOLD = 0.40
STORE2_PINK_RATIO_THRESHOLD = 0.12
STORE2_BLACK_RATIO_THRESHOLD = 0.22



class StaffDetector:
    def __init__(self, uniform_ranges: Optional[list] = None, store_id: str | None = None):
        self.store_id = (store_id or "").upper()
        self.uniform_ranges = uniform_ranges or BLACK_UNIFORM_RANGES

    def is_staff(self, frame: np.ndarray, bbox: list) -> bool:
        """
        Returns True if the person in bbox is likely staff based on uniform color.
        Only examines the torso region (middle 40% of bounding box height).
        """
        try:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(frame.shape[1], x2)
            y2 = min(frame.shape[0], y2)

            if (x2 - x1) < 10 or (y2 - y1) < 20:
                return False

            if self.store_id == "ST1076":
                return self._is_store2_staff(frame, x1, y1, x2, y2)

            # Torso = middle 40% of height
            h = y2 - y1
            torso_y1 = y1 + int(h * 0.30)
            torso_y2 = y1 + int(h * 0.70)
            torso = frame[torso_y1:torso_y2, x1:x2]

            if torso.size == 0:
                return False

            hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
            total_pixels = hsv.shape[0] * hsv.shape[1]

            for lower, upper in self.uniform_ranges:
                mask = cv2.inRange(hsv, lower, upper)
                match_ratio = np.count_nonzero(mask) / total_pixels
                if match_ratio > STAFF_PIXEL_RATIO_THRESHOLD:
                    return True

            return False

        except Exception as e:
            logger.debug(f"Staff detection error: {e}")
            return False

    def update_ranges(self, new_ranges: list):
        """Hot-update uniform color ranges without restarting pipeline."""
        self.uniform_ranges = new_ranges
        logger.info(f"Staff detector updated with {len(new_ranges)} color ranges")

    def _is_store2_staff(self, frame: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> bool:
        h = y2 - y1
        upper = frame[y1 + int(h * 0.20):y1 + int(h * 0.58), x1:x2]
        lower = frame[y1 + int(h * 0.55):y1 + int(h * 0.92), x1:x2]
        if upper.size == 0 or lower.size == 0:
            return False

        pink_ratio = _mask_ratio(upper, PINK_TOP_RANGES)
        black_ratio = _mask_ratio(lower, BLACK_UNIFORM_RANGES)
        return (
            pink_ratio >= STORE2_PINK_RATIO_THRESHOLD
            and black_ratio >= STORE2_BLACK_RATIO_THRESHOLD
        ) or pink_ratio >= (STORE2_PINK_RATIO_THRESHOLD * 2)


def _mask_ratio(crop: np.ndarray, ranges: list[tuple[np.ndarray, np.ndarray]]) -> float:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    total_pixels = hsv.shape[0] * hsv.shape[1]
    if total_pixels == 0:
        return 0.0
    mask_total = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lower, upper in ranges:
        mask_total = cv2.bitwise_or(mask_total, cv2.inRange(hsv, lower, upper))
    return np.count_nonzero(mask_total) / total_pixels
