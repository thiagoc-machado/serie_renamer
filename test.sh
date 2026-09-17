#!/usr/bin/env sh
set -eu

python3 -m pytest -q
python3 -m py_compile app/main.py app/services.py
