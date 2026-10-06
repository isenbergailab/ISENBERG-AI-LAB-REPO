#!/usr/bin/env python3
"""Cross-platform launcher for the GTD agent (Windows, macOS, Linux).

    python run.py doctor
    python run.py run

Uses config.toml next to this file unless you pass --config first.
"""
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit(f"The GTD agent needs Python 3.10 or newer; this is {sys.version.split()[0]}.")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gtd_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not arguments or arguments[0] != "--config":
        arguments = ["--config", str(HERE / "config.toml"), *arguments]
    sys.exit(main(arguments))
