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

DEFAULT_UNIFORM_RANGES = [
    # Brigade Bangalore (ST1008): staff wear all-black uniforms
    # Strict very-dark only (V < 45) — avoids dark jeans/shadows on customers
    (np.array([0,   0,   0]), np.array([180, 255, 45])),
    # Dark + truly low saturation (charcoal/near-black fabrics)
    (np.array([0,   0,   0]), np.array([180,  50, 45])),
]

# Threshold ratio of matched uniform pixels to torso pixels for classification
STAFF_PIXEL_RATIO_THRESHOLD = 0.40



class StaffDetector:
    def __init__(self, uniform_ranges: Optional[list] = None):
        self.uniform_ranges = uniform_ranges or DEFAULT_UNIFORM_RANGES

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