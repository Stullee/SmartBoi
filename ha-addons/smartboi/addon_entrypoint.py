"""Home Assistant add-on entrypoint. Translates the add-on's
/data/options.json into the environment variables smartboi.config.Settings
reads, then runs the exact same engine as everywhere else."""
from __future__ import annotations

import os
from pathlib import Path

from _addon_options import apply_options_as_env

if __name__ == "__main__":
    apply_options_as_env()

    # smartboi.engine keeps its persisted state (relationship graph,
    # dossiers, dedup index) under a relative "data/" directory -- same
    # convention as logs/ (see _addon_options.py). Running from within the
    # mapped /config share means both land there, visible via Samba/File
    # Editor with no docker exec ever needed.
    if os.path.isdir("/config"):
        run_dir = Path("/config/smartboi_run")
        run_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(run_dir)

    from smartboi.main import main

    main()
