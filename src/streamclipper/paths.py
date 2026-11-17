"""Package data folders (read-only after install) and the user-writable folder."""

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
PRESET_DIR = DATA_DIR / "presets"
TEMPLATE_DIR = DATA_DIR / "templates"


def _user_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "streamclipper"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "streamclipper"


USER_DIR = _user_dir()
ENV_FILE = USER_DIR / "env_detected.json"
