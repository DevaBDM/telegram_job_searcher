#!/usr/bin/env bash
# Keep the job saver running. Any arguments are forwarded, e.g.  ./run.sh --once
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python main.py "$@"
else
    exec python3 main.py "$@"
fi
