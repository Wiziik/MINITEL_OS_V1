"""Per-device configuration for a Minitel Pi.

Single source of truth for: device token, display name, server URL, serial port,
and which apps/features this box offers. The git tree is identical on every Pi;
only the local config file differs. That file is written once, at enroll time,
to a path OUTSIDE the repo so `git pull` never touches it.

Precedence (highest first): environment variable > config file > built-in default.
Config file: /etc/minitelnet/device.json  (override path with MINITEL_CONFIG).
"""
import glob
import json
import os

CONFIG_PATH = os.environ.get("MINITEL_CONFIG", "/etc/minitelnet/device.json")

DEFAULTS = {
    "server_url":    "ws://localhost:8000/ws",
    "display_name":  "minitel",
    "token":         "",
    "serial_port":   "",            # "" = autodetect
    "service":       "minitelnet-client.service",
    "apps":          ["chat", "coeur", "snake"],
    "show_menu":     True,
    "show_settings": True,
    "keepalive":     True,
}

# env var name -> config key (string-valued overrides)
_ENV_MAP = {
    "MINITELNET_URL":       "server_url",
    "MINITEL_DISPLAY_NAME": "display_name",
    "MINITEL_DEVICE_TOKEN": "token",
    "MINITEL_PORT":         "serial_port",
    "MINITELNET_SERVICE":   "service",
}


def _load_file(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def autodetect_port():
    """First present serial device. USB-serial Arduinos show as ttyUSB*,
    native-USB ones (Mega/Leonardo) as ttyACM*; we don't assume which."""
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return "/dev/ttyUSB0"          # fallback; serial open() raises if truly absent


def load():
    cfg = dict(DEFAULTS)
    cfg.update(_load_file(CONFIG_PATH))
    for env, key in _ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    if os.environ.get("MINITEL_KEEPALIVE") is not None:
        cfg["keepalive"] = os.environ["MINITEL_KEEPALIVE"] == "1"
    if not cfg.get("serial_port"):
        cfg["serial_port"] = autodetect_port()
    return cfg


def http_base(server_url):
    """Derive the HTTP(S) base from the WS(S) URL by dropping the /ws suffix."""
    return (server_url.replace("wss://", "https://")
                      .replace("ws://", "http://")
                      .rsplit("/ws", 1)[0])
