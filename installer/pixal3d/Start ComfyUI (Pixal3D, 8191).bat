@echo off
cd /D "%~dp0"
title ComfyUI-Pixal3D (127.0.0.1:8191)
rem Self-contained portable ComfyUI for AstraForge Pixal3D / TRELLIS.2 experiments.
rem Port 8191, loopback only. The studio Hunyuan ComfyUI stays on 8190.
set CUDA_PATH=
set CUDA_HOME=
set CUDA_BIN_PATH=
set CUDNN_PATH=
set PYTHONPATH=
set PYTHONHOME=
set VIRTUAL_ENV=
set CONDA_PREFIX=
set HF_HOME=%~dp0cache\huggingface
set TORCH_HOME=%~dp0cache\torch
set HF_HUB_DISABLE_TELEMETRY=1
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set PYTHONUTF8=1
set "PATH=%windir%\System32;%windir%\System32\WindowsPowerShell\v1.0;%PATH%"
.\python_embeded\python.exe -I -W ignore::FutureWarning ComfyUI\main.py --windows-standalone-build --listen 127.0.0.1 --port 8191 --disable-auto-launch %*
pause
