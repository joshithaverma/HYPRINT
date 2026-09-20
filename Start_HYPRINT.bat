@echo off
title HYPRINT Backend & Cloudflare Tunnel Manager
cd /d "%~dp0"
echo Starting HYPRINT Backend and Cloudflare Tunnel Manager...

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" auto_tunnel.py
) else (
    python auto_tunnel.py
)

pause
