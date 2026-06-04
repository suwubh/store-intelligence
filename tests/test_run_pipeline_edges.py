import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from run_pipeline import main, process_store
import sys

# PROMPT: Generate edge case unit tests for run_pipeline.py, including missing store folders, missing layout files, and subprocess failures.
# CHANGES MADE: Implemented edge cases covering missing arguments, unrecognized files, excluded cameras, and subprocess simulation.

def test_main_no_stores(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--dataset", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 1

def test_main_missing_store_folder(monkeypatch, tmp_path):
    # Setup one valid store to pass list_store_clip_dirs, but ask for a missing one
    store_dir = tmp_path / "clips" / "Store 1"
    store_dir.mkdir(parents=True)
    (store_dir / "video.mp4").touch()
    
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--store-folder", "Store 99", "--dataset", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 1

@patch("run_pipeline.subprocess.run")
@patch("run_pipeline.load_store_layout")
@patch("pipeline.detect.get_clip_start_time")
def test_process_store_edge_cases(mock_get_clip, mock_load_layout, mock_subprocess_run, tmp_path):
    store_dir = tmp_path / "Store 1"
    store_dir.mkdir()
    
    (store_dir / "unknown.mp4").touch()
    (store_dir / "excluded.mp4").touch()
    (store_dir / "fail.mp4").touch()

    # Mock layout
    mock_load_layout.return_value = {
        "store_id": "STORE_001",
        "cameras": {
            "CAM_01": {"source_file": "excluded.mp4", "exclude_from_metrics": True},
            "CAM_02": {"source_file": "fail.mp4"}
        }
    }
    
    mock_get_clip.return_value = MagicMock()
    mock_get_clip.return_value.isoformat.return_value = "2026-03-03T10:00:00Z"
    
    mock_result = MagicMock()
    mock_result.returncode = 1  # subprocess fail
    mock_subprocess_run.return_value = mock_result
    
    # Process store should skip 'unknown.mp4' (not in layout), skip 'excluded.mp4' (exclude_from_metrics=True),
    # and fail on 'fail.mp4' because of returncode 1, thus returning total_events=0
    total = process_store(store_dir, tmp_path, "cpu", None, None)
    assert total == 0
