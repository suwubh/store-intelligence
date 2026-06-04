"""Store ID normalization shared by API ingestion and endpoint queries."""

from __future__ import annotations

import re
from typing import Any


STORE_ID_ALIASES = {
    "STORE 1": "ST1008",
    "STORE_1": "ST1008",
    "ST_STORE_1": "ST1008",
    "ST1008": "ST1008",
    "STORE 2": "ST1076",
    "STORE_2": "ST1076",
    "ST_STORE_2": "ST1076",
    "ST1076": "ST1076",
    "STORE_1076": "ST1076",
    "STORE 1076": "ST1076",
    "STORE_BLR_001": "ST1008",
    "STORE_BLR_002": "ST1076",
    "STORE_BLR_1": "ST1008",
    "STORE_BLR_2": "ST1076",
}


def normalize_store_id(value: Any) -> str | None:
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    key = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
    if key in STORE_ID_ALIASES:
        return STORE_ID_ALIASES[key]

    match = re.match(r"^STORE_BLR_(\d+)$", key)
    if match:
        num = int(match.group(1))
        if num == 1:
            return "ST1008"
        if num == 2:
            return "ST1076"

    if key.startswith("STORE_"):
        suffix = key.split("_", 1)[1]
        if suffix == "1076":
            return "ST1076"
        if suffix.isdigit():
            return f"ST{suffix}"

    return key
