# PROMPT: Add focused component tests for the retail CCTV pipeline utilities: store ID mapping, layout generation, zone mapping, event emission, and dataset validation.
# CHANGES MADE: Used temporary files and monkeypatching instead of real videos so tests stay fast and deterministic.

import json
from datetime import datetime, timezone

from app.models import StoreEvent
from app.store_ids import normalize_store_id
from pipeline.emit import EventEmitter
from pipeline.layout_builder import build_layout_for_store_dir, infer_camera_role, normalize_store_id as pipeline_store_id
from pipeline.validate_dataset import validate_dataset
from pipeline.zone_mapper import ZoneMapper


def test_store_id_normalization_contract():
    assert normalize_store_id("Store 1") == "ST1008"
    assert normalize_store_id("ST_STORE_1") == "ST1008"
    assert normalize_store_id("store_1076") == "ST1076"
    assert normalize_store_id("") is None
    assert normalize_store_id("custom store") == "CUSTOM_STORE"
    assert pipeline_store_id("Store 2") == "ST1076"


def test_supported_sample_event_normalizes_to_canonical_schema():
    event = StoreEvent(
        event_type="zone_entered",
        track_id=42,
        store_id="ST_STORE_2",
        camera_id="CAM_FLOOR_01",
        zone_id="LIPSTICK",
        event_time="2026-03-08T18:10:45.280000",
    )

    assert event.store_id == "ST1076"
    assert event.visitor_id == "VIS_T42"
    assert event.event_type.value == "ZONE_ENTER"
    assert event.confidence == 0.80


def test_layout_builder_maps_real_store_names_and_roles(tmp_path, monkeypatch):
    store_dir = tmp_path / "Store 2"
    store_dir.mkdir()
    for name in ["entry 1.mp4", "entry 2.mp4", "zone.mp4", "billing_area.mp4"]:
        (store_dir / name).write_bytes(b"")

    monkeypatch.setattr("pipeline.layout_builder.probe_video", lambda path: (960, 1080, 25.0))
    layout = build_layout_for_store_dir(store_dir)

    assert layout["store_id"] == "ST1076"
    assert layout["staff_info"]["detector_profile"] == "store2_pink_black"
    assert infer_camera_role("billing_area.mp4") == "BILLING"
    assert layout["cameras"]["CAM_ENTRY_02"]["exclude_from_metrics"] is True
    assert layout["cameras"]["CAM_BILLING_01"]["zones"][0]["zone_id"] == "BILLING_COUNTER"


def test_zone_mapper_reads_camera_specific_polygons(tmp_path):
    layout = {
        "store_id": "ST1008",
        "cameras": {
            "CAM_FLOOR_01": {
                "zones": [
                    {"zone_id": "LEFT", "polygon": [[0, 0], [50, 0], [50, 100], [0, 100]]},
                    {"zone_id": "RIGHT", "polygon": [[50, 0], [100, 0], [100, 100], [50, 100]]},
                ]
            }
        },
    }
    path = tmp_path / "store_layout.json"
    path.write_text(json.dumps(layout), encoding="utf-8")

    mapper = ZoneMapper(str(path), "ST1008", "CAM_FLOOR_01")
    assert mapper.get_zone(10, 10) == "LEFT"
    assert mapper.get_zone(75, 10) == "RIGHT"
    assert mapper.get_zone(120, 10) is None


def test_event_emitter_writes_canonical_jsonl(tmp_path):
    out = tmp_path / "events.jsonl"
    emitter = EventEmitter("ST1008", "CAM_ENTRY_01", str(out))
    emitter.emit(
        visitor_id="VIS_1",
        event_type="ENTRY",
        timestamp=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
        zone_id=None,
        dwell_ms=0,
        is_staff=False,
        confidence=0.23456,
    )
    emitter.flush()

    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["store_id"] == "ST1008"
    assert row["event_type"] == "ENTRY"
    assert row["confidence"] == 0.2346
    assert row["metadata"]["session_seq"] == 1


def test_event_emitter_posts_batches_to_api(tmp_path, monkeypatch):
    calls = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, timeout))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    emitter = EventEmitter("ST1008", "CAM_ENTRY_01", str(tmp_path / "events.jsonl"), api_url="http://api", batch_size=1)
    emitter.emit(
        visitor_id="VIS_1",
        event_type="ENTRY",
        timestamp=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
        zone_id=None,
        dwell_ms=0,
        is_staff=False,
        confidence=0.9,
    )
    emitter.flush()

    assert calls == [("http://api/events/ingest", 5)]


def test_dataset_validator_generates_layout_for_temp_dataset(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    store_dir = dataset / "clips" / "Store 1"
    store_dir.mkdir(parents=True)
    (store_dir / "entry.mp4").write_bytes(b"")
    (dataset / "pos_transactions.csv").write_text(
        "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
        "1,10-04-2026,12:15:05,ST1008,399945,Faces Canada,302.33\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("pipeline.layout_builder.probe_video", lambda path: (640, 480, 15.0))
    assert validate_dataset(str(dataset)) is True
    generated = json.loads((store_dir / "store_layout.json").read_text(encoding="utf-8"))
    assert generated["store_id"] == "ST1008"


def test_resolve_device():
    from pipeline.detect import resolve_device
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") in ["cpu", "cuda"]

def test_frame_to_timestamp():
    from pipeline.detect import frame_to_timestamp
    start = datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)
    ts = frame_to_timestamp(start, 30, 15.0)
    import datetime as dt
    assert ts == start + dt.timedelta(seconds=2)

def test_is_storeroom_camera(tmp_path):
    from pipeline.detect import is_storeroom_camera
    layout = {"store_id": "ST1008", "cameras": {"CAM_1": {"exclude_from_metrics": True}}}
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(layout), encoding="utf-8")
    assert is_storeroom_camera(str(path), "CAM_1") is True
    assert is_storeroom_camera(str(path), "CAM_2") is False

def test_get_entry_line_ratio(tmp_path):
    from pipeline.detect import get_entry_line_ratio
    layout = {"store_id": "ST1008", "cameras": {"CAM_1": {"entry_line_y_ratio": 0.5}}}
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(layout), encoding="utf-8")
    assert get_entry_line_ratio(str(path), "CAM_1") == 0.5
    assert get_entry_line_ratio(str(path), "CAM_2") == 0.40

def test_get_entry_inward_direction(tmp_path):
    from pipeline.detect import get_entry_inward_direction
    layout = {"store_id": "ST1008", "cameras": {"CAM_1": {"entry_inward_direction": "up"}}}
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(layout), encoding="utf-8")
    assert get_entry_inward_direction(str(path), "CAM_1") == "up"
    assert get_entry_inward_direction(str(path), "CAM_2") == "down"

def test_get_clip_start_time_with_arg():
    from pipeline.detect import get_clip_start_time
    ts = get_clip_start_time("2026-03-08T12:00:00Z", "dummy.mp4", use_ocr=False)
    assert ts == datetime(2026, 3, 8, 12, 0, 0, tzinfo=timezone.utc)

def test_get_clip_start_time_no_ocr(monkeypatch):
    from pipeline.detect import get_clip_start_time
    import cv2
    import pytest
    class DummyCap:
        def isOpened(self): return True
        def read(self): return False, None
        def release(self): pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda x: DummyCap())
    with pytest.raises(ValueError):
        get_clip_start_time(None, "dummy.mp4", use_ocr=True)

def test_extract_timestamp_from_frame(monkeypatch):
    import pipeline.detect
    from unittest.mock import MagicMock
    import numpy as np
    
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = ["10/04/2026", "20:00:00"]
    pipeline.detect.ocr_reader = mock_reader
    
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    dt = pipeline.detect._extract_timestamp_from_frame(frame)
    assert dt == datetime(2026, 4, 10, 20, 0, 0, tzinfo=timezone.utc)
