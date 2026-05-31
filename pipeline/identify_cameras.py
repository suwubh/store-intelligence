#!/usr/bin/env python3
"""
identify_cameras.py — Extract a sample frame from each CAM X.mp4 to identify
which camera covers which area (entry, floor, billing).

Usage: python pipeline/identify_cameras.py --clips dataset/clips/ST1008/
"""
import argparse
import os
import json
import sys
from pathlib import Path


def extract_frame(video_path: str, output_path: str, second: int = 30) -> bool:
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"  Cannot open: {video_path}")
            return False

        fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = total / fps

        # Extract at 30s, 60s, and midpoint
        sample_points = [
            min(second, duration * 0.1),
            duration * 0.3,
            duration * 0.6,
        ]

        frames_saved = []
        for i, ts in enumerate(sample_points):
            cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000)
            ret, frame = cap.read()
            if not ret:
                continue
            out = output_path.replace(".jpg", f"_t{int(ts)}s.jpg")
            cv2.imwrite(out, frame)
            frames_saved.append(out)

        cap.release()
        print(f"  ✅ {Path(video_path).name} → {len(frames_saved)} frames | {w}x{h} | {duration:.0f}s")
        return True, w, h, duration

    except ImportError:
        print("  OpenCV not installed. Run: pip install opencv-python-headless")
        return False, 0, 0, 0
    except Exception as e:
        print(f"  Error: {e}")
        return False, 0, 0, 0


def update_layout_for_resolution(layout_path: str, camera_mapping: dict, resolutions: dict):
    """Update store_layout.json with correct camera IDs and actual video resolutions."""
    with open(layout_path) as f:
        layout = json.load(f)

    # Rebuild cameras section with actual resolution-based polygons
    new_cameras = {}
    for cam_file, cam_type in camera_mapping.items():
        res = resolutions.get(cam_file, (1920, 1080))
        w, h = res

        if cam_type == "ENTRY":
            cam_id = "CAM_ENTRY_01"
            zones = [
                {"zone_id": "ENTRY_ZONE",   "polygon": [[0,0],[w,0],[w,int(h*0.35)],[0,int(h*0.35)]]},
                {"zone_id": "EXIT_ZONE",    "polygon": [[0,int(h*0.35)],[w,int(h*0.35)],[w,int(h*0.65)],[0,int(h*0.65)]]},
                {"zone_id": "ACCESSORIES",  "polygon": [[int(w*0.7),int(h*0.65)],[w,int(h*0.65)],[w,h],[int(w*0.7),h]]},
            ]
        elif cam_type == "FLOOR_01":
            cam_id = "CAM_FLOOR_01"
            # Divide into 4 columns x 3 rows = 12 zones
            cw, rh = w // 4, h // 3
            zone_names = [
                "MAYBELLINE", "LAKME", "FACES_CANADA", "LOREAL",
                "NYBAE", "MARS_PLUS", "ALPS_GOODNESS", "BEAUTY_ESSENTIALS",
                "MINIMALIST", "AQUALOGICA", "DERMDOC", "GOOD_VIBES",
            ]
            zones = []
            for idx, name in enumerate(zone_names):
                col, row = idx % 4, idx // 4
                zones.append({
                    "zone_id": name,
                    "polygon": [
                        [col*cw, row*rh], [(col+1)*cw, row*rh],
                        [(col+1)*cw, (row+1)*rh], [col*cw, (row+1)*rh]
                    ]
                })
        elif cam_type == "FLOOR_02":
            cam_id = "CAM_FLOOR_02"
            cw, rh = w // 4, h // 2
            zone_names = [
                "FOXTALE", "PILGRIM", "TFS", "SWISS_RENEE",
                "SALM", "MENS_CARE", "DK", "EB",
            ]
            zones = []
            for idx, name in enumerate(zone_names):
                col, row = idx % 4, idx // 4
                zones.append({
                    "zone_id": name,
                    "polygon": [
                        [col*cw, row*rh], [(col+1)*cw, row*rh],
                        [(col+1)*cw, (row+1)*rh], [col*cw, (row+1)*rh]
                    ]
                })
        elif cam_type == "FLOOR_03":
            cam_id = "CAM_FLOOR_03"
            cw, rh = w // 3, h // 2
            zone_names = ["FRAGRANCE", "HAIRCARE", "PERSONAL_CARE", "FOOT_CARE", "LIP_ZONE", "MISC"]
            zones = []
            for idx, name in enumerate(zone_names):
                col, row = idx % 3, idx // 3
                zones.append({
                    "zone_id": name,
                    "polygon": [
                        [col*cw, row*rh], [(col+1)*cw, row*rh],
                        [(col+1)*cw, (row+1)*rh], [col*cw, (row+1)*rh]
                    ]
                })
        elif cam_type == "BILLING":
            cam_id = "CAM_BILLING_01"
            zones = [
                {"zone_id": "BILLING_COUNTER", "polygon": [[0,0],[w,0],[w,int(h*0.5)],[0,int(h*0.5)]]},
                {"zone_id": "BILLING_QUEUE",   "polygon": [[0,int(h*0.5)],[w,int(h*0.5)],[w,h],[0,h]]},
            ]
        else:
            continue

        new_cameras[cam_id] = {
            "description": f"Camera covering {cam_type.lower().replace('_', ' ')} area",
            "source_file": cam_file,
            "resolution": {"width": w, "height": h},
            "zones": zones,
        }

    layout["cameras"] = new_cameras
    with open(layout_path, "w") as f:
        json.dump(layout, f, indent=2)
    print(f"\n✅ store_layout.json updated with {len(new_cameras)} cameras and actual resolutions.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--clips", default="dataset/clips/ST1008", help="Path to folder with CAM X.mp4 files")
    p.add_argument("--output", default="dataset/camera_samples", help="Output folder for sample frames")
    p.add_argument("--layout", default="dataset/store_layout.json", help="Layout JSON to update")
    args = p.parse_args()

    clips_dir = Path(args.clips)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    videos = sorted(clips_dir.glob("*.mp4")) + sorted(clips_dir.glob("*.MP4"))
    if not videos:
        print(f"No .mp4 files found in {clips_dir}")
        sys.exit(1)

    print(f"\n📷 Extracting sample frames from {len(videos)} cameras...")
    print(f"   Output: {out_dir}/\n")

    resolutions = {}
    for v in videos:
        safe_name = v.stem.replace(" ", "_")
        out_path = str(out_dir / f"{safe_name}.jpg")
        result = extract_frame(str(v), out_path)
        if isinstance(result, tuple) and result[0]:
            _, w, h, dur = result
            resolutions[v.name] = (w, h)

    print(f"""
══════════════════════════════════════════════
  NEXT STEP — Open these frames and identify:
══════════════════════════════════════════════
  Folder: {out_dir}/

  For each camera, decide:
    ENTRY    → shows the store entrance/door
    FLOOR_01 → shows makeup/skin product shelves (front section)
    FLOOR_02 → shows skin/hair product shelves (back section)
    FLOOR_03 → shows 5th camera area (if applicable)
    BILLING  → shows billing counter and queue

  Then run with your mapping (example):
    python pipeline/identify_cameras.py \\
      --clips {clips_dir} \\
      --map "CAM 1.mp4=ENTRY" \\
      --map "CAM 2.mp4=FLOOR_01" \\
      --map "CAM 3.mp4=FLOOR_02" \\
      --map "CAM 4.mp4=BILLING" \\
      --map "CAM 5.mp4=FLOOR_03" \\
      --update-layout
══════════════════════════════════════════════
""")

    # If --map and --update-layout args provided, update layout
    if hasattr(args, 'map') and args.map and hasattr(args, 'update_layout') and args.update_layout:
        mapping = {}
        for m in args.map:
            file, cam_type = m.split("=", 1)
            mapping[file.strip()] = cam_type.strip()
        update_layout_for_resolution(args.layout, mapping, resolutions)


# Re-parse with map args support
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--clips", default="dataset/clips/ST1008")
    p.add_argument("--output", default="dataset/camera_samples")
    p.add_argument("--layout", default="dataset/store_layout.json")
    p.add_argument("--map", action="append", metavar="FILE=TYPE",
                   help="e.g. 'CAM 1.mp4=ENTRY'. Repeat for each camera.")
    p.add_argument("--update-layout", action="store_true",
                   help="Update store_layout.json with the mapping and real resolutions")
    args = p.parse_args()

    clips_dir = Path(args.clips)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    videos = sorted(clips_dir.glob("*.mp4")) + sorted(clips_dir.glob("*.MP4"))
    if not videos:
        print(f"No .mp4 files found in {clips_dir}")
        sys.exit(1)

    resolutions = {}
    print(f"\n📷 Sampling {len(videos)} cameras → {out_dir}/\n")
    for v in videos:
        safe_name = v.stem.replace(" ", "_")
        out_path = str(out_dir / f"{safe_name}.jpg")
        result = extract_frame(str(v), out_path)
        if isinstance(result, tuple) and result[0]:
            _, w, h, dur = result
            resolutions[v.name] = (w, h)

    if args.map and args.update_layout:
        mapping = {}
        for m in args.map:
            file, cam_type = m.split("=", 1)
            mapping[file.strip()] = cam_type.strip()
        update_layout_for_resolution(args.layout, mapping, resolutions)
        print("\n✅ Layout updated. Run the pipeline next:")
        print("   bash pipeline/run.sh --api-url http://localhost:8000")
    elif not args.map:
        print(f"""
Open the sample frames in: {out_dir}/
Then identify each camera and run:

  python pipeline/identify_cameras.py \\
    --clips {clips_dir} \\
    --map "CAM 1.mp4=ENTRY" \\
    --map "CAM 2.mp4=FLOOR_01" \\
    --map "CAM 3.mp4=FLOOR_02" \\
    --map "CAM 4.mp4=BILLING" \\
    --map "CAM 5.mp4=FLOOR_03" \\
    --update-layout
""")
