"""Shared helper: translates the add-on's /data/options.json (filled in via
the HA UI configuration form) into the environment variables
smartboi.config.Settings reads."""
from __future__ import annotations

import json
import os
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")


def apply_options_as_env() -> None:
    if not OPTIONS_PATH.exists():
        return
    options = json.loads(OPTIONS_PATH.read_text())
    for key, value in options.items():
        if value is None:
            continue
        env_key = key.upper()
        # An explicitly provided environment variable wins over the stored
        # option -- lets a one-off `docker exec` override a single setting
        # without touching (and restarting) the live add-on config.
        if env_key in os.environ:
            continue
        os.environ[env_key] = str(value)

    # /config is the add-on's mapped share of the Home Assistant config
    # directory (see config.yaml's `map: config:rw`) -- the same folder
    # Samba and the File Editor/Studio Code Server add-ons already expose.
    # Writing dossiers/graph/logs there means they just show up for
    # download, with no docker cp / host-terminal access ever needed.
    os.environ.setdefault("LOG_DIR", "/config/smartboi_logs")
