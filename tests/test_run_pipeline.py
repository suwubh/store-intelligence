import pytest
import argparse
import json
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

from run_pipeline import _video_sort_key, _event_file_sort_key, parse_args, get_python_executable, process_store, ingest_store_events, main

# PROMPT: Generate unit tests for run_pipeline.py sorting logic, argument parsing, and main flow.
# CHANGES MADE: Added explicit tests for the sort keys, argument parsing, and mocked main execution flow.

def test_video_sort_key():
    source_map = {"entry.mp4": "CAM_ENTRY_01", "billing.mp4": "CAM_BILL_01"}
    assert _video_sort_key(Path("entry.mp4"), source_map) == (0, "entry.mp4")
    assert _video_sort_key(Path("billing.mp4"), source_map) == (2, "billing.mp4")
    assert _video_sort_key(Path("floor.mp4"), source_map) == (1, "floor.mp4")

def test_event_file_sort_key():
    assert _event_file_sort_key(Path("store_entry_events.jsonl")) == (0, "store_entry_events.jsonl")
    assert _event_file_sort_key(Path("store_billing_events.jsonl")) == (2, "store_billing_events.jsonl")
    assert _event_file_sort_key(Path("store_floor_events.jsonl")) == (1, "store_floor_events.jsonl")

def test_parse_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--store-folder", "Store 1", "--all-stores", "--api-url", "http://localhost:8000", "--use-ocr"])
    args = parse_args()
    assert args.store_folder == "Store 1"
    assert args.all_stores is True
    assert args.api_url == "http://localhost:8000"
    assert args.use_ocr is True


def test_parse_args_default_use_ocr(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--store-folder", "Store 1"])
    args = parse_args()
    assert args.use_ocr is True


def test_parse_args_no_ocr(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--store-folder", "Store 1", "--no-ocr"])
    args = parse_args()
    assert args.use_ocr is False

def test_get_python_executable():
    exec_path = get_python_executable()
    assert isinstance(exec_path, str)
    assert len(exec_path) > 0

@patch("run_pipeline.subprocess.run")
@patch("run_pipeline.load_store_layout")
@patch("pipeline.detect.get_clip_start_time")
def test_process_store(mock_get_clip, mock_load_layout, mock_subprocess_run, tmp_path):
    store_dir = tmp_path / "Store 1"
    store_dir.mkdir()
    
    # Create dummy video file
    (store_dir / "entry.mp4").touch()
    (store_dir / "store_layout.json").touch()

    # Mock layout
    mock_load_layout.return_value = {
        "store_id": "STORE_001",
        "cameras": {
            "CAM_01": {"source_file": "entry.mp4"}
        }
    }
    
    mock_get_clip.return_value = MagicMock()
    mock_get_clip.return_value.isoformat.return_value = "2026-03-03T10:00:00Z"
    
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_subprocess_run.return_value = mock_result
    
    # Mock event file creation by subprocess
    output_dir = tmp_path / "events"
    output_dir.mkdir()
    
    def side_effect(*args, **kwargs):
        out_file = output_dir / "STORE_001_CAM_01_events.jsonl"
        out_file.write_text('{"event": "test"}', encoding="utf-8")
        return mock_result
    
    mock_subprocess_run.side_effect = side_effect
    
    total = process_store(store_dir, tmp_path, "cpu", None, None, use_ocr=True)
    assert total == 1
    assert mock_subprocess_run.call_args is not None
    called_cmd = mock_subprocess_run.call_args.args[0]
    assert "--use-ocr" in called_cmd

@patch("urllib.request.urlopen")
def test_ingest_store_events(mock_urlopen, tmp_path):
    output_dir = tmp_path / "events"
    output_dir.mkdir()
    
    out_file = output_dir / "STORE_001_entry_events.jsonl"
    out_file.write_text('{"event": "test"}\n', encoding="utf-8")
    
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"accepted": 1, "duplicate": 0}'
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response
    
    ingest_store_events(output_dir, "STORE_001", "http://localhost:8000")
    
    assert mock_urlopen.called

@patch("run_pipeline.list_store_clip_dirs")
@patch("run_pipeline.process_store")
@patch("run_pipeline.load_store_layout")
def test_main_all_stores(mock_load_layout, mock_process, mock_list, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--all-stores", "--dataset", str(tmp_path)])
    
    store_dir = tmp_path / "clips" / "Store 1"
    store_dir.mkdir(parents=True)
    
    mock_list.return_value = [store_dir]
    mock_process.return_value = 10
    mock_load_layout.return_value = {"store_id": "STORE_001"}
    
    main()
    
    assert mock_process.called
    assert mock_load_layout.called

@patch("run_pipeline.list_store_clip_dirs")
@patch("run_pipeline.process_store")
@patch("run_pipeline.load_store_layout")
def test_main_specific_store(mock_load_layout, mock_process, mock_list, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--store-folder", "Store 1", "--dataset", str(tmp_path)])
    
    store_dir = tmp_path / "clips" / "Store 1"
    store_dir.mkdir(parents=True)
    
    mock_list.return_value = [store_dir]
    mock_process.return_value = 10
    mock_load_layout.return_value = {"store_id": "STORE_001"}
    
    main()
    
    assert mock_process.called
