"""Thin wrapper so the screener can be run straight from a checkout:

    python scripts/screen_candidates.py INTT ASYS CVU

The implementation lives in smartboi.screen (inside the package) so it also
ships with the Home Assistant add-on, which installs the package from git and
never copies scripts/ -- see that module's docstring. Inside a deployment,
run it as:

    python -m smartboi.screen INTT ASYS CVU
"""
from __future__ import annotations

import sys
from pathlib import Path

# Lets this run without a prior `pip install -e .` -- same src-layout the
# package itself uses, harmless if already installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from smartboi.screen import main  # noqa: E402

if __name__ == "__main__":
    main()
