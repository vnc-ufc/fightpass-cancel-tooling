#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Virtual environment ready. Activate with: source .venv/bin/activate"
echo "Copy configs/config.example.json to configs/config.json and edit paths before running."
