"""Persistent application settings."""

import dataclasses
import json
import logging
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "MirrorBridge"
APP_VERSION = "0.1.0"

RESOLUTIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k": (3840, 2160),
}


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME.lower()


def _default_media_dir(kind: str) -> str:
    home = Path.home()
    if kind == "video":
        sub = "Movies" if sys.platform == "darwin" else "Videos"
    else:
        sub = "Pictures"
    return str(home / sub / APP_NAME)


def _default_name() -> str:
    host = socket.gethostname().split(".")[0].strip() or "Computer"
    return "%s [%s]" % (host, APP_NAME)


@dataclass
class AppConfig:
    name: str = dataclasses.field(default_factory=_default_name)
    port: int = 7100
    resolution: str = "1080p"
    max_fps: int = 60
    h265: bool = False
    password: str = ""
    max_sessions: int = 4
    hardware_decoding: str = "auto"  # auto | on | off
    audio_enabled: bool = True
    audio_device: str = ""
    record_folder: str = dataclasses.field(default_factory=lambda: _default_media_dir("video"))
    screenshot_folder: str = dataclasses.field(default_factory=lambda: _default_media_dir("image"))
    record_fps: int = 30
    record_device_audio: bool = True
    record_microphone: bool = False
    mic_device: str = ""
    always_on_top: bool = False
    start_fullscreen: bool = False
    show_stats: bool = False
    webcast_enabled: bool = True
    webcast_port: int = 7180
    device_id: str = ""
    identity_seed: str = ""

    @property
    def display_size(self):
        return RESOLUTIONS.get(self.resolution, RESOLUTIONS["1080p"])

    @property
    def use_h265(self) -> bool:
        # 4K mirroring is only offered by senders with H.265.
        return self.h265 or self.resolution == "4k"

    def ensure_identity(self):
        """Create the persistent device id and pairing key on first run."""
        changed = False
        if len(self.device_id) != 12:
            raw = bytearray(os.urandom(6))
            raw[0] = (raw[0] | 0x02) & 0xFE  # locally administered, unicast
            self.device_id = raw.hex()
            changed = True
        if len(self.identity_seed) != 64:
            self.identity_seed = os.urandom(32).hex()
            changed = True
        return changed

    @classmethod
    def load(cls, path: Path = None) -> "AppConfig":
        path = path or config_dir() / "config.json"
        cfg = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fields = {f.name for f in dataclasses.fields(cls)}
            for key, value in data.items():
                if key in fields:
                    setattr(cfg, key, value)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read settings (%s); using defaults", exc)
        if cfg.ensure_identity():
            cfg.save(path)
        return cfg

    def save(self, path: Path = None):
        path = path or config_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)
