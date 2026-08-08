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
    # Tolerant on purpose, and it matters more than it looks.
    #
    # This runs before anything else in the container, outside any logging
    # setup and outside the engine's own error handling. An unreadable
    # options.json used to raise straight out of __main__, and the add-on
    # simply never started. That was survivable while the manifest said
    # `boot: manual` -- it stayed down until someone looked. With
    # `boot: auto` and a watchdog it becomes a restart loop instead, which
    # is louder but no more informative unless the reason is printed.
    #
    # Starting with no options is a WORKING configuration: Settings has a
    # default for every field, and the operator's real problem is the
    # unreadable file, which they can only act on if told.
    try:
        options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueError
        print(f"[smartboi] WARNING: could not read {OPTIONS_PATH} ({exc}). "
              "Starting with code defaults -- your add-on configuration is NOT being applied. "
              "Re-save the add-on options in Home Assistant to rewrite the file.", flush=True)
        return
    if not isinstance(options, dict):
        print(f"[smartboi] WARNING: {OPTIONS_PATH} holds {type(options).__name__}, expected an object. "
              "Starting with code defaults -- your add-on configuration is NOT being applied.", flush=True)
        return
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
