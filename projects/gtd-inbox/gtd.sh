#!/usr/bin/env sh
# macOS / Linux launcher. Usage: ./gtd.sh doctor   or   ./gtd.sh run
# Set GTD_AGENT_PYTHON to use a specific interpreter.
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "${GTD_AGENT_PYTHON:-python3}" "$DIR/run.py" "$@"
