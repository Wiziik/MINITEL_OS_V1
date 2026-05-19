#!/usr/bin/env python3
"""Boot menu for .50 — lets the user choose which Minitel app to launch."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from minitel import Minitel

PORT  = os.environ.get("MINITEL_PORT", "/dev/ttyUSB0")
HERE  = os.path.dirname(os.path.abspath(__file__))
PY    = sys.executable   # reuse the same venv Python

APPS = [
    {
        "label": "1. MinitelNet - Chat",
        "cmd": [PY, os.path.join(HERE, "client.py")],
    },
    {
        "label": "2. Twitch TV STORE",
        "cmd": [PY, "/home/pi/twitch_minitel_v2.py", "--port", PORT],
    },
    {
        "label": "3. Coeur Poetique",
        "cmd": [PY, "/home/pi/minitel_heart_search.py",
                "--port", PORT,
                "--folder", "/home/pi/texts/poetry_corpus"],
    },
]

GLYPHS = {
    'T': ["###", " # ", " # ", " # ", " # "],
    'V': ["# #", "# #", "# #", " # ", " # "],
    'S': ["###", "#  ", "###", "  #", "###"],
    'O': ["###", "# #", "# #", "# #", "###"],
    'R': ["## ", "# #", "## ", "# #", "# #"],
    'E': ["###", "#  ", "## ", "#  ", "###"],
    'C': ["###", "#  ", "#  ", "#  ", "###"],
    'H': ["# #", "# #", "###", "# #", "# #"],
    'A': [" # ", "# #", "###", "# #", "# #"],
    ' ': ["   ", "   ", "   ", "   ", "   "],
    '1': [" # ", "## ", " # ", " # ", "###"],
    '3': ["###", "  #", "###", "  #", "###"],
    '5': ["###", "#  ", "###", "  #", "###"],
    '6': ["###", "#  ", "###", "# #", "###"],
}


def _big_text(text, width=40):
    rows = [""] * 5
    chars = list(text.upper())
    for i, c in enumerate(chars):
        g = GLYPHS.get(c, ["   "] * 5)
        for j in range(5):
            rows[j] += g[j]
            if i < len(chars) - 1:
                rows[j] += " "
    pad = max(0, (width - len(rows[0])) // 2)
    return [" " * pad + r for r in rows]


def show_menu(mt):
    mt.clear_screen()
    mt.send_line("")
    for row in _big_text("3615"):
        mt.send_line(row)
    mt.send_line("")
    for row in _big_text("TV STORE"):
        mt.send_line(row)
    mt.send_line("")
    mt.send_line("----------------------------------------")
    mt.send_line("")
    for app in APPS:
        mt.send_line(f"  {app['label']}")
    mt.send_line("")
    mt.send_line("  Tapez 1, 2 ou 3 puis ENVOI:")
    mt.send_text("  > ")


def pick(mt):
    while True:
        raw = mt.read_input().strip()
        if raw in ("1", "2", "3"):
            return int(raw) - 1
        # Ignore anything else, redraw prompt
        mt.send_text("  > ")


def run_app(mt, idx):
    app = APPS[idx]
    mt.clear_screen()
    mt.send_line(f"  Lancement: {app['label']}")
    mt.send_line("")
    mt.close()       # release serial so the child app can open it
    time.sleep(0.5)
    try:
        subprocess.run(app["cmd"], env=os.environ.copy())
    except Exception as e:
        pass
    time.sleep(1)    # brief pause before reopening serial


def main():
    mt = Minitel(port=PORT)
    time.sleep(2.5)
    mt.ser.reset_input_buffer()

    while True:
        show_menu(mt)
        idx = pick(mt)
        run_app(mt, idx)
        # Reopen serial after child app closes it
        mt = Minitel(port=PORT)
        time.sleep(2.5)
        mt.ser.reset_input_buffer()


if __name__ == "__main__":
    main()
