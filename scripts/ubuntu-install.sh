#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
trader_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$trader_project"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates tzdata
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Требуется Python 3.12+; используйте Ubuntu 24.04+"'
if [[ ! -x .venv-termux/bin/python ]]; then
    python3 -m venv .venv-termux
fi
.venv-termux/bin/python -m pip install --upgrade pip setuptools wheel
# Ubuntu provides glibc; require wheels for heavy packages instead of attempting an Android source build.
.venv-termux/bin/python -m pip install --only-binary=:all: 'numpy>=2,<3' 'pandas>=2.2,<3' pyarrow
.venv-termux/bin/python -m pip install -e .
.venv-termux/bin/python -m pip check
.venv-termux/bin/python -c 'import sqlite3, ssl, numpy, pandas, streamlit, httpx, websockets; print("Python, SQLite, TLS и зависимости готовы")'
if [[ ! -f .env ]]; then
    install -m 600 .env.example .env
fi
chmod 600 .env
mkdir -p data/logs
chmod 700 data data/logs
# Keep API checks explicit; installation never starts a trader or requires keys.
printf '%s\n' 'Окружение готово. .env сохранён; существующие ключи, настройки и БД не перезаписаны.'
