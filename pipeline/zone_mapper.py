"""
Maps pixel (cx, cy) coordinates to zone names using store_layout.json polygons.
"""
import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import cv2

logger = logging.getLogger(__name__)


class ZoneMapper:
    def __init__(self, layout_path: str, store_id: str, camera_id: str):
        self.zones: list[dict] = []
        self._load(layout_path, store_id, camera_id)

    def _load(self, layout_path: str, store_id: str, camera_id: str):
        path = Path(layout_path)
        if not path.exists():
            logger.warning(f"store_layout.json not found at {path}. Zone mapping disabled.")
            return
        if path.suffix.lower() != ".json":
            logger.warning(f"Layout asset {path} is not JSON. Zone mapping disabled until polygons are calibrated.")
            return

        with open(path, encoding="utf-8") as f:
            layout = json.load(f)

        if layout.get("store_id"):
            store_id = layout["store_id"]

        # Handle multiple layout structures:
        # Format A (our generated layout): { "store_id": "ST1008", "cameras": { "CAM_ENTRY_01": { "zones": [...] } } }
        # Format B (nested stores):        { "stores": { "ST1008": { "cameras": { ... } } } }
        # Format C (flat):                 { "zones": [...] }

        raw_zones = []

        if "stores" in layout:
            # Format B
            store_data = layout["stores"].get(store_id, {})
            cameras = store_data.get("cameras", {})
            camera_data = cameras.get(camera_id, store_data)
            raw_zones = camera_data.get("zones", [])

        elif "cameras" in layout:
            # Format A — our generated layout
            cameras = layout["cameras"]
            camera_data = cameras.get(camera_id, {})
            raw_zones = camera_data.get("zones", [])
            if not raw_zones:
                # fallback: collect all zones from all cameras
                for cam in cameras.values():
                    raw_zones.extend(cam.get("zones", []))

        elif "zones" in layout:
            # Format C — flat
            raw_zones = layout["zones"]

        for z in raw_zones:
            polygon = z.get("polygon", z.get("bbox"))
            zone_name = z.get("zone_id", z.get("name", z.get("id")))
            if polygon and zone_name:
                self.zones.append({
                    "zone_id": zone_name,
                    "polygon": np.array(polygon, dtype=np.float32),
                })

        logger.info(f"Loaded {len(self.zones)} zones for camera {camera_id}")

    def get_zone(self, cx: float, cy: float) -> Optional[str]:
        """Return zone_id if point (cx, cy) falls inside a zone polygon."""
        point = (float(cx), float(cy))
        for z in self.zones:
            result = cv2.pointPolygonTest(z["polygon"], point, False)
            if result >= 0:
                return z["zone_id"]
        return None
