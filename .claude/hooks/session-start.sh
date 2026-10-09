#!/bin/bash
# Installs TARS's dependencies in Claude Code cloud sessions so tests and the linter run.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# PyAudio (from the voice extras) builds against PortAudio on Linux.
if ! dpkg -s portaudio19-dev >/dev/null 2>&1; then
  (apt-get install -y -q portaudio19-dev >/dev/null 2>&1) \
    || (apt-get update -q >/dev/null 2>&1 && apt-get install -y -q portaudio19-dev >/dev/null)
fi

if ! command -v uv >/dev/null 2>&1; then
  pip install -q uv
fi

uv sync --locked --extra voice --extra desktop --extra dev
