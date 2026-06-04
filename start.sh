#!/bin/sh
# Docker entrypoint: run startup tests in DEV_MODE, then start the server.
set -e

if [ "$DEV_MODE" = "true" ] || [ "$DEV_MODE" = "1" ]; then
  echo "[start.sh] DEV_MODE enabled — running startup tests..."
  python -c "from tests import run_startup_tests; run_startup_tests()" 2>&1
  echo "[start.sh] Tests passed. Starting server..."
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000
