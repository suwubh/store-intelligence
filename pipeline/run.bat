@echo off
REM Process Store 1 / Store 2 clips (layout JSON lives in each store folder).
REM Usage: pipeline\run.bat --store-folder "Store 1" [--api-url http://localhost:8000]

set STORE_FOLDER=
set API_URL=
set DEVICE=auto
set DATASET=dataset
set CLIP_START=

:parse
if "%~1"=="" goto run
if "%~1"=="--store-folder" ( set STORE_FOLDER=%~2 & shift & shift & goto parse )
if "%~1"=="--api-url"      ( set API_URL=%~2      & shift & shift & goto parse )
if "%~1"=="--device"       ( set DEVICE=%~2       & shift & shift & goto parse )
if "%~1"=="--dataset"      ( set DATASET=%~2      & shift & shift & goto parse )
if "%~1"=="--clip-start"   ( set CLIP_START=%~2   & shift & shift & goto parse )
if "%~1"=="--all-stores"   ( set ALL_STORES=1     & shift & goto parse )
shift & goto parse

:run
set CMD=python run_pipeline.py --dataset %DATASET% --device %DEVICE%
if defined STORE_FOLDER set CMD=%CMD% --store-folder "%STORE_FOLDER%"
if defined ALL_STORES set CMD=%CMD% --all-stores
if defined API_URL set CMD=%CMD% --api-url %API_URL%
if defined CLIP_START set CMD=%CMD% --clip-start %CLIP_START%
%CMD%
