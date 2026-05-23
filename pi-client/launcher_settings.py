"""Settings menu shared by launcher.py / launcher_58.py / launcher_78.py.

Entries:
  1. WiFi               re-run the launcher's own wifi_setup callback
  2. Mise a jour        git pull on /home/pi/minitelnet + restart systemd unit
  3. Addons             enable/disable .py files from /home/pi/addons/
  4. A propos           host / ip / commit / python
  0. Retour
"""
import glob
import json
import os
import socket
import subprocess
import sys
import time

REPO_DIR    = "/home/pi/minitelnet"
ADDONS_DIR  = "/home/pi/addons"
ADDONS_JSON = "/home/pi/addons.json"


# ── small helpers ──────────────────────────────────────────────────────────

def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr)
    except Exception as e:
        return False, str(e)


def _git_commit():
    ok, out = _run(["git", "-C", REPO_DIR, "rev-parse", "--short", "HEAD"], timeout=5)
    return out.strip() if ok else "?"


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?"


# ── addons config ──────────────────────────────────────────────────────────

def load_addons_config():
    if not os.path.exists(ADDONS_JSON):
        return {"enabled": []}
    try:
        with open(ADDONS_JSON) as f:
            return json.load(f)
    except Exception:
        return {"enabled": []}


def save_addons_config(cfg):
    os.makedirs(os.path.dirname(ADDONS_JSON) or ".", exist_ok=True)
    with open(ADDONS_JSON, "w") as f:
        json.dump(cfg, f, indent=2)


def list_addon_files():
    os.makedirs(ADDONS_DIR, exist_ok=True)
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(ADDONS_DIR, "*.py")))


def enabled_addon_apps(python_bin, port):
    """Return APP dicts for enabled addons that still exist on disk."""
    cfg = load_addons_config()
    apps = []
    for fname in cfg.get("enabled", []):
        path = os.path.join(ADDONS_DIR, fname)
        if not os.path.exists(path):
            continue
        label = fname[:-3].replace("_", " ").title()
        apps.append({
            "label": label[:30],
            "cmd":   [python_bin, path, "--port", port],
            "_addon": True,
        })
    return apps


# ── sub-screens ────────────────────────────────────────────────────────────

def _do_update(mt, service):
    mt.clear_screen()
    mt.send_line("")
    mt.send_line("  MISE A JOUR")
    mt.send_line("  ----------------------------------------")
    mt.send_line("")
    before = _git_commit()
    mt.send_line(f"  Avant : {before}")
    mt.send_line("  git pull...")

    ok, out = _run(["git", "-C", REPO_DIR, "pull", "--rebase"], timeout=60)
    after = _git_commit()

    if not ok:
        mt.send_line("  Echec git pull")
        for line in (out or "").splitlines()[:4]:
            mt.send_line(f"  {line[:38]}")
        mt.send_line("")
        mt.send_line("  ENVOI pour retour.")
        mt.read_input()
        return

    mt.send_line(f"  Apres : {after}")
    if before == after:
        mt.send_line("  Deja a jour.")
        mt.send_line("")
        mt.send_line("  ENVOI pour retour.")
        mt.read_input()
        return

    mt.send_line("  Redemarrage du service...")
    time.sleep(1.0)
    try:
        mt.close()
    except Exception:
        pass
    subprocess.Popen(["sudo", "systemctl", "restart", service])
    # systemd will kill us; if it doesn't, exit ourselves so the new code runs.
    time.sleep(3)
    sys.exit(0)


def _addon_manager(mt):
    while True:
        cfg     = load_addons_config()
        enabled = set(cfg.get("enabled", []))
        files   = list_addon_files()

        mt.clear_screen()
        mt.send_line("")
        mt.send_line("  ADDONS")
        mt.send_line("  ----------------------------------------")
        mt.send_line("")

        if not files:
            mt.send_line("  Aucun addon trouve.")
            mt.send_line(f"  Dossier: {ADDONS_DIR}")
            mt.send_line("")
            mt.send_line("  Copiez vos .py la (scp)")
            mt.send_line("  puis revenez ici.")
            mt.send_line("")
            mt.send_line("  ENVOI pour retour.")
            mt.read_input()
            return

        for i, f in enumerate(files[:9], 1):
            mark = "[X]" if f in enabled else "[ ]"
            mt.send_line(f"  {i}. {mark} {f[:30]}")
        mt.send_line("")
        mt.send_line("  Numero = activer/desactiver")
        mt.send_line("  0. Retour")
        mt.send_line("")
        mt.send_text("  > ")

        choice = mt.read_input().strip()
        if choice == "0":
            return
        if not choice.isdigit():
            continue
        idx = int(choice) - 1
        if not (0 <= idx < len(files)):
            continue

        f = files[idx]
        if f in enabled:
            enabled.discard(f)
        else:
            enabled.add(f)
        cfg["enabled"] = sorted(enabled)
        save_addons_config(cfg)


def _show_about(mt, service):
    mt.clear_screen()
    mt.send_line("")
    mt.send_line("  A PROPOS")
    mt.send_line("  ----------------------------------------")
    mt.send_line("")
    mt.send_line(f"  Host    : {socket.gethostname()[:30]}")
    mt.send_line(f"  IP      : {_local_ip()}")
    mt.send_line(f"  Commit  : {_git_commit()}")
    mt.send_line(f"  Python  : {sys.version.split()[0]}")
    mt.send_line(f"  Service : {service[:30]}")
    mt.send_line("")
    mt.send_line("  ENVOI pour retour.")
    mt.read_input()


# ── main entry point ───────────────────────────────────────────────────────

def settings_menu(mt, service, wifi_callback):
    """Show the settings menu.

    service        — systemd unit name used by 'Mise a jour'
    wifi_callback  — callable(mt) that re-runs the launcher's WiFi picker
    """
    while True:
        mt.clear_screen()
        mt.send_line("")
        mt.send_line("  REGLAGES")
        mt.send_line("  ----------------------------------------")
        mt.send_line("")
        mt.send_line("  1. WiFi")
        mt.send_line("  2. Mise a jour")
        mt.send_line("  3. Addons")
        mt.send_line("  4. A propos")
        mt.send_line("")
        mt.send_line("  0. Retour")
        mt.send_line("")
        mt.send_text("  > ")

        choice = mt.read_input().strip()
        if   choice == "1":
            wifi_callback(mt)
        elif choice == "2":
            _do_update(mt, service)
        elif choice == "3":
            _addon_manager(mt)
        elif choice == "4":
            _show_about(mt, service)
        elif choice == "0":
            return
