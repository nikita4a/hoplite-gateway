@echo off
title Hoplite Gateway
cd /d "%~dp0"

:: deps
python -c "import fastapi, httpx, uvicorn" 2>nul || pip install fastapi uvicorn httpx

:: config migration
if not exist config.json if exist cookies.json (
    python -c "import json; c=json.load(open('cookies.json')); json.dump({'api_key':c.get('api_key',''),'api_base':c.get('api_base','https://api.hoplite.sh'),'project_id':'','ngrok_token':''}, open('config.json','w'), indent=2)"
)

if not exist config.json (
    echo [!] No config.json — the menu will ask for your API key.
)

:: launch menu (GUI) — it starts the gateway for you
start "" pythonw menu.pyw 2>nul || start "" python menu.pyw
echo Menu launched. Close this window freely.
timeout /t 3 >nul