@echo off
REM run.bat — Windows equivalent of run.sh
REM Usage: pipeline\run.bat [--api-url http://localhost:8000] [--device cpu]

set STORE_ID=ST1008
set DEVICE=cpu
set DATASET_DIR=dataset
set API_URL=
set CLIP_START=2026-04-10T10:00:00Z

:parse
if "%~1"=="" goto start
if "%~1"=="--api-url"    ( set API_URL=%~2   & shift & shift & goto parse )
if "%~1"=="--store"      ( set STORE_ID=%~2  & shift & shift & goto parse )
if "%~1"=="--device"     ( set DEVICE=%~2    & shift & shift & goto parse )
if "%~1"=="--clip-start" ( set CLIP_START=%~2 & shift & shift & goto parse )
shift & goto parse

:start
set OUTPUT_DIR=%DATASET_DIR%\events
if not exist %OUTPUT_DIR% mkdir %OUTPUT_DIR%
set LAYOUT=%DATASET_DIR%\store_layout.json

echo.
echo Store Intelligence Detection Pipeline
echo Store   : %STORE_ID%
echo Dataset : %DATASET_DIR%
echo Device  : %DEVICE%
echo ClipStart: %CLIP_START%
echo.

REM Process each camera using Python to read source_file mapping
python -c "
import json, os, subprocess, sys

layout = json.load(open('%LAYOUT%'))
store_dir = os.path.join('%DATASET_DIR%', 'clips', '%STORE_ID%')
output_dir = '%OUTPUT_DIR%'

if not os.path.isdir(store_dir):
    print(f'ERROR: {store_dir} not found')
    sys.exit(1)

# Build source_file -> camera_id map
sf_map = {}
for cam_id, cam in layout.get('cameras', {}).items():
    sf = cam.get('source_file', '')
    if sf:
        sf_map[sf.lower()] = cam_id

total = 0
for fname in sorted(os.listdir(store_dir)):
    if not fname.lower().endswith(('.mp4', '.avi', '.mov')):
        continue
    cam_id = sf_map.get(fname.lower())
    if not cam_id:
        print(f'  WARNING: {fname} not mapped in store_layout.json — skipping')
        continue
    clip_path = os.path.join(store_dir, fname)
    out_file = os.path.join(output_dir, f'%STORE_ID%_{cam_id}_events.jsonl')
    print(f'  Processing {fname} -> {cam_id}')
    cmd = [
        sys.executable, '-m', 'pipeline.detect',
        '--video', clip_path,
        '--store', '%STORE_ID%',
        '--camera', cam_id,
        '--layout', '%LAYOUT%',
        '--output', out_file,
        '--clip-start', '%CLIP_START%',
        '--device', '%DEVICE%',
    ]
    if '%API_URL%':
        cmd += ['--api-url', '%API_URL%']
    result = subprocess.run(cmd)
    if result.returncode == 0:
        try:
            count = sum(1 for _ in open(out_file))
        except Exception:
            count = 0
        total += count
        print(f'  OK: {count} events -> {os.path.basename(out_file)}')
    else:
        print(f'  ERROR processing {fname}')

print()
print(f'Total events: {total}')
"

echo.
echo Done. Start the API:   docker compose up
echo Then run dashboard:    python dashboard\live.py --store %STORE_ID%
