# PROMPT: Write unit tests for a retail object tracking pipeline that uses MultiObjectTracker. Focus on verifying that numeric visitor track IDs from ByteTrack are formatted with the 'VIS_' prefix, and test re-entry logic using appearance cosine similarity matching.
# CHANGES MADE: Integrated dummy frame inputs using numpy zeros to mock visual crops. Added state assertions to verify tracker registers reentry flags and resets them correctly on retrieval.

import pytest
from datetime import datetime, timezone

import numpy as np
from pipeline.tracker import MultiObjectTracker, TrackState
from pipeline.staff_detector import StaffDetector

def test_tracker_visitor_id_prefixing():
    # Verify that numeric track IDs are correctly prefixed with VIS_
    tracker = MultiObjectTracker(reid_enabled=False)
    dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    now = datetime.now(timezone.utc)
    
    # Ingest a numeric track ID
    vid1 = tracker._get_or_create_visitor("42", [10, 10, 20, 20], dummy_frame, now, 0.95)
    assert vid1 == "VIS_42"
    
    # Ingest a non-numeric track ID
    vid2 = tracker._get_or_create_visitor("VIS_abc123", [10, 10, 20, 20], dummy_frame, now, 0.95)
    assert vid2 == "VIS_abc123"

def test_reentry_logic():
    tracker = MultiObjectTracker(reid_enabled=True)
    dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    now = datetime.now(timezone.utc)
    
    # New visitor
    vid1 = tracker._get_or_create_visitor("1", [10, 10, 20, 20], dummy_frame, now, 0.9)
    assert not tracker.is_reentry(vid1)
    
    # Simulate exit by pruning
    tracker._lost_counters[vid1] = tracker.max_lost_frames + 1
    tracker._prune_lost(now)
    assert vid1 not in tracker.tracks
    assert len(tracker.exited_tracks) == 1
    
    # Re-enter the same visitor with similar appearance (exact match here due to same dummy crop)
    vid2 = tracker._new_visitor([12, 12, 22, 22], tracker.exited_tracks[0].appearance, now, 0.9)
    assert vid2 == vid1
    assert tracker.is_reentry(vid2)
    # The reentry flag should be cleared after reading
    assert not tracker.is_reentry(vid2)


def test_store2_staff_profile_detects_pink_top_black_bottom():
    detector = StaffDetector(store_id="ST1076")
    frame = np.full((120, 80, 3), 255, dtype=np.uint8)
    # BGR pink upper body and black lower body inside the person box.
    frame[20:65, 20:60] = np.array([180, 80, 220], dtype=np.uint8)
    frame[65:110, 20:60] = np.array([15, 15, 15], dtype=np.uint8)

    assert detector.is_staff(frame, [20, 10, 60, 115])


def test_store1_staff_profile_detects_black_uniform():
    detector = StaffDetector(store_id="ST1008")
    frame = np.full((100, 80, 3), 255, dtype=np.uint8)
    frame[30:70, 20:60] = np.array([10, 10, 10], dtype=np.uint8)

    assert detector.is_staff(frame, [20, 10, 60, 90])
