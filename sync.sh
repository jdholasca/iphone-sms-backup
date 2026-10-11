#!/usr/bin/env bash
set -euo pipefail
exec "$(dirname "$0")/venv/bin/python" "$(dirname "$0")/iphone_backup_app.py" auto-backup
