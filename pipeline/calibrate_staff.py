#!/usr/bin/env python3
"""
calibrate_staff.py — Extract sample frames from clips and help identify staff uniform colors.
Run this BEFORE the main pipeline. It saves sample frames so you can visually identify
the uniform color, then outputs the HSV range to paste into staff_detector.py.

Usage:
  python pipeline/calibrate_staff.py --video dataset/clips/ST1008/entry.mp4
  python pipeline/calibrate_staff.py --video dataset/clips/ST1008/entry.mp4 --interactive
"""
import argparse
import os
import cv2
import numpy as np
from pathlib import Path


def extract_sample_frames(video_path: str, output_dir: str, n_frames: int = 10):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Cannot open: {video_path}")
        return []

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    duration_s = total / fps if fps else 0

    print(f"Video: {Path(video_path).name}")
    print(f"Frames: {total} | FPS: {fps:.1f} | Duration: {duration_s:.0f}s")

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    step = max(1, total // n_frames)
    saved = []

    for i in range(n_frames):
        frame_idx = i * step
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            break
        ts = frame_idx / fps if fps else 0
        out_path = os.path.join(output_dir, f"frame_{i:02d}_ts{ts:.0f}s.jpg")
        cv2.imwrite(out_path, frame)
        saved.append(out_path)

    cap.release()
    print(f"Saved {len(saved)} frames to {output_dir}/")
    return saved


def analyze_roi_color(image_path: str, x: int, y: int, w: int, h: int):
    """Analyze the HSV color distribution of a region of interest."""
    img = cv2.imread(image_path)
    if img is None:
        print(f"Cannot read: {image_path}")
        return

    roi = img[y:y+h, x:x+w]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    h_vals = hsv[:, :, 0].flatten()
    s_vals = hsv[:, :, 1].flatten()
    v_vals = hsv[:, :, 2].flatten()

    print(f"\n=== ROI Color Analysis ({x},{y}) {w}x{h} ===")
    print(f"H: mean={h_vals.mean():.0f}  std={h_vals.std():.0f}  range=[{h_vals.min():.0f}, {h_vals.max():.0f}]")
    print(f"S: mean={s_vals.mean():.0f}  std={s_vals.std():.0f}  range=[{s_vals.min():.0f}, {s_vals.max():.0f}]")
    print(f"V: mean={v_vals.mean():.0f}  std={v_vals.std():.0f}  range=[{v_vals.min():.0f}, {v_vals.max():.0f}]")

    # Suggest HSV range with some tolerance
    h_lo = max(0, h_vals.mean() - 2*h_vals.std())
    h_hi = min(179, h_vals.mean() + 2*h_vals.std())
    s_lo = max(0, s_vals.mean() - 2*s_vals.std())
    s_hi = min(255, s_vals.mean() + 2*s_vals.std())
    v_lo = max(0, v_vals.mean() - 2*v_vals.std())
    v_hi = min(255, v_vals.mean() + 2*v_vals.std())

    print(f"\n  Paste this into staff_detector.py DEFAULT_UNIFORM_RANGES:")
    print(f"  (np.array([{h_lo:.0f}, {s_lo:.0f}, {v_lo:.0f}]), np.array([{h_hi:.0f}, {s_hi:.0f}, {v_hi:.0f}])),")


def print_purplle_staff():
    """Print known staff info from the Brigade Bangalore POS data."""
    print("\n════════════════════════════════════════════")
    print("  Known Staff — Brigade Road, Bangalore (ST1008)")
    print("════════════════════════════════════════════")
    staff = [
        ("CL2063", "kasthuri v"),
        ("CL2727", "Zufishan Khazra"),
        ("CL1997", "Shashikala"),
        ("CL2541", "Naziya Begum"),
        ("CL2680", "Priya v"),
    ]
    print("  Employee Code | Name")
    for code, name in staff:
        print(f"  {code}            | {name}")
    print()
    print("  ↑ These 5 people appear in POS data as salespersons.")
    print("  Their uniform color should be identified from the clips.")
    print("  Look for consistent clothing across all 5 people in the footage.")
    print("════════════════════════════════════════════\n")


def main():
    p = argparse.ArgumentParser(description="Staff uniform color calibration")
    p.add_argument("--video", help="Path to video clip for frame extraction")
    p.add_argument("--output", default="dataset/calibration_frames", help="Output dir for frames")
    p.add_argument("--frames", type=int, default=15, help="Number of frames to sample")
    p.add_argument("--analyze", help="Path to a saved frame to analyze ROI colors")
    p.add_argument("--roi", nargs=4, type=int, metavar=("X", "Y", "W", "H"),
                   help="ROI to analyze: x y width height")
    args = p.parse_args()

    print_purplle_staff()

    if args.video:
        frames = extract_sample_frames(args.video, args.output, args.frames)
        print(f"\nNext step:")
        print(f"  1. Open the frames in {args.output}/ and find one with a staff member")
        print(f"  2. Note the pixel coordinates of their torso region")
        print(f"  3. Run: python pipeline/calibrate_staff.py \\")
        print(f"           --analyze {args.output}/frame_XX_tsYYs.jpg \\")
        print(f"           --roi X Y W H")

    if args.analyze and args.roi:
        analyze_roi_color(args.analyze, *args.roi)

    if not args.video and not args.analyze:
        print("Usage:")
        print("  # Step 1: Extract frames from a clip")
        print("  python pipeline/calibrate_staff.py --video dataset/clips/ST1008/entry.mp4")
        print()
        print("  # Step 2: Analyze a staff person's torso region in a frame")
        print("  python pipeline/calibrate_staff.py --analyze dataset/calibration_frames/frame_03_ts60s.jpg --roi 150 80 60 120")


if __name__ == "__main__":
    main()
