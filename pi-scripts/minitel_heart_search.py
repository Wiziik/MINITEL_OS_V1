#!/usr/bin/env python3
"""
minitel_heart_search.py - Calligramme heart with word search.
Type a word on row 24, press ENVOI to find a poem containing it.
SUITE cycles through multiple matches. CORRECTION = backspace.
"""
import serial, time, threading, unicodedata, re, glob, os, sys, argparse
from pathlib import Path

BAUD_RATE  = 9600
COLS       = 40
ROWS       = 24
HEART_ROWS = 21   # rows 2-22 (row 1=title, row 23=status, row 24=search)
PAGE_DELAY = 20   # seconds between heart pages

KEY_ENVOI      = 0x41   # after SEP 0x13
KEY_ANNULATION = 0x45   # after SEP 0x13
KEY_CORRECTION = 0x47   # after SEP 0x13
KEY_SUITE      = 0x48   # after SEP 0x13
DEFAULT_ZOOM   = 1.05


def normalize(text):
    nfd = unicodedata.normalize('NFD', text)
    return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').lower()

def to_minitel(text):
    nfd = unicodedata.normalize('NFD', text)
    return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').encode('ascii', 'ignore').decode('ascii')

def heart_row_specs(cols=40, rows=21, zoom=1.05):
    cx, cy = (cols - 1) / 2.0, (rows - 1) / 2.0
    rows_map = {}
    for r in range(rows):
        for c in range(cols):
            x = (c - cx) / (cols / 4.2) * 0.82 * zoom
            y = -(r - cy) / (rows / 2.6) * zoom
            if (x**2 + y**2 - 1)**3 - x**2 * y**3 <= 0.01:
                rows_map.setdefault(r, []).append(c)
    return [(r, sorted(cl)) for r, cl in sorted(rows_map.items())]

def fill_heart(words, cols=40, rows=21, zoom=1.05):
    specs = heart_row_specs(cols, rows, zoom)
    grid  = [[' '] * cols for _ in range(rows)]
    words = list(words)
    wi    = 0
    for row, row_cols in specs:
        width, line = len(row_cols), ''
        while wi < len(words):
            w = words[wi]
            cand = (line + ' ' + w).lstrip() if line else w
            if len(cand) <= width:
                line = cand; wi += 1
            elif not line:
                line = w[:width]; words[wi] = w[width:]
            else:
                break
        for i, ch in enumerate(line):
            grid[row][row_cols[i]] = ch
    return [''.join(r) for r in grid], wi

def find_optimal_zoom(words, cols=40, rows=21):
    if not words:
        return DEFAULT_ZOOM
    lo, hi, best = DEFAULT_ZOOM, 3.5, DEFAULT_ZOOM
    for _ in range(22):
        mid = (lo + hi) / 2
        _, used = fill_heart(words, cols, rows, mid)
        if used >= len(words):
            best = mid; lo = mid
        else:
            hi = mid
    return best

def paginate(text, cols=40, rows=21):
    clean = to_minitel(re.sub(r'\s+', ' ', text).strip())
    words = clean.split()
    pages, i = [], 0
    while i < len(words):
        _, used = fill_heart(words[i:], cols, rows, DEFAULT_ZOOM)
        if used == 0:
            break
        pages.append(words[i:i + used])
        i += used
    return pages or [[]]


class MinitelSearch:
    def __init__(self, ser, folder):
        self.ser       = ser
        self.folder    = folder
        self._lock     = threading.Lock()
        self._stop     = threading.Event()
        self._cancel   = threading.Event()
        self._trigger  = threading.Event()
        self._input    = ''
        self._matches  = []
        self._midx     = 0

    # ── serial primitives ───────────────────────────────────────────────────

    def _write_row(self, row, text):
        line = to_minitel(text)[:COLS].ljust(COLS)
        with self._lock:
            self.ser.write(bytes([0x1F, 0x40 + row, 0x40 + 1]))
            self.ser.write(line.encode('ascii'))
        time.sleep(len(line) / 120 + 0.02)

    def _clear(self):
        with self._lock:
            self.ser.write(b'\x0C')
        time.sleep(0.6)

    # ── UI helpers ──────────────────────────────────────────────────────────

    def _search_bar(self):
        prompt = f"MOT: {self._input}"
        if len(prompt) < COLS - 1:
            prompt += '_'
        self._write_row(24, prompt)

    def _status(self, msg):
        self._write_row(23, msg.center(COLS)[:COLS])

    # ── keepalive ───────────────────────────────────────────────────────────

    def _keepalive_loop(self):
        while not self._stop.wait(30):
            with self._lock:
                self.ser.write(b'\x11')

    # ── display loop ────────────────────────────────────────────────────────

    def _display_poem(self, path):
        self._cancel.clear()
        title = Path(path).stem.upper()
        try:
            text = Path(path).read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            print(f"[heart] read error: {e}")
            return

        pages = paginate(text, cols=COLS, rows=HEART_ROWS)
        total = len(pages)

        for i, chunk in enumerate(pages):
            if self._cancel.is_set() or self._stop.is_set():
                return
            zoom = find_optimal_zoom(chunk, cols=COLS, rows=HEART_ROWS)
            grid, _ = fill_heart(chunk, cols=COLS, rows=HEART_ROWS, zoom=zoom)

            tag    = f" {i+1}/{total}"
            header = title[:COLS - len(tag)] + tag
            self._write_row(1, header)
            for r, line in enumerate(grid):
                if self._cancel.is_set():
                    return
                self._write_row(r + 2, line)
            self._write_row(23, '')
            self._search_bar()
            print(f"[heart] {title} page {i+1}/{total} zoom={zoom:.2f}")

            if i < total - 1:
                self._cancel.wait(PAGE_DELAY)

    def _display_loop(self):
        while not self._stop.is_set():
            self._trigger.wait()
            self._trigger.clear()
            if self._matches and not self._stop.is_set():
                path = self._matches[self._midx]
                self._display_poem(path)

    # ── search ──────────────────────────────────────────────────────────────

    def _search(self, word):
        needle = normalize(word)
        matches = []
        for f in sorted(glob.glob(os.path.join(self.folder, '*.txt'))):
            try:
                if needle in normalize(Path(f).read_text(encoding='utf-8', errors='ignore')):
                    matches.append(f)
            except Exception:
                pass
        return matches

    def _launch(self):
        self._cancel.set()
        self._trigger.set()

    # ── keyboard handlers ────────────────────────────────────────────────────

    def _on_envoi(self):
        word = self._input.strip()
        self._input = ''
        if not word:
            return
        if word.lower() in ('/quit', '/q'):
            self._clear()
            self._write_row(12, 'Au revoir.'.center(COLS))
            time.sleep(1.0)
            sys.exit(0)
        matches = self._search(word)
        self._matches = matches
        self._midx    = 0
        if not matches:
            self._status(f"'{to_minitel(word)}' introuvable")
            self._search_bar()
            print(f"[search] '{word}' → no match")
        else:
            n = len(matches)
            label = "SUITE=suivant" if n > 1 else ""
            self._status(f"{n} poeme(s)  {label}")
            print(f"[search] '{word}' → {n} match(es), showing {Path(matches[0]).stem}")
            self._launch()

    def _on_suite(self):
        if not self._matches:
            return
        self._midx = (self._midx + 1) % len(self._matches)
        name = Path(self._matches[self._midx]).stem
        self._status(f"{self._midx+1}/{len(self._matches)}: {name[:30]}")
        self._launch()

    # ── main ────────────────────────────────────────────────────────────────

    def run(self):
        threading.Thread(target=self._keepalive_loop, daemon=True).start()
        threading.Thread(target=self._display_loop,   daemon=True).start()

        self._clear()
        self._write_row(1, "CALLIGRAMMES".center(COLS))
        self._status("TAPEZ UN MOT  ENVOI POUR CHERCHER")
        self._search_bar()

        while not self._stop.is_set():
            data = self.ser.read(1)
            if not data:
                continue
            b = data[0] & 0x7F  # strip high bit (7E1 parity bleed-through)

            if b in (0x08, 0x7F):              # standalone backspace
                if self._input:
                    self._input = self._input[:-1]
                    self._search_bar()

            elif b == 0x13:                    # SEP — function key
                b2 = self.ser.read(1)
                if not b2:
                    continue
                code = b2[0] & 0x7F
                if code == KEY_ENVOI:
                    self._on_envoi()
                    self._search_bar()
                elif code == KEY_CORRECTION:
                    if self._input:
                        self._input = self._input[:-1]
                        self._search_bar()
                elif code == KEY_SUITE:
                    self._on_suite()
                elif code == KEY_ANNULATION:
                    self._input = ''
                    self._search_bar()

            elif 0x20 <= b <= 0x7E:
                if len(self._input) < 20:
                    self._input += chr(b)
                    self._search_bar()

    def stop(self):
        self._stop.set()
        self._cancel.set()
        self._trigger.set()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--folder', default='/home/pi/texts/poetry_corpus')
    p.add_argument('--port',   default='/dev/ttyUSB0')
    p.add_argument('--baud',   type=int, default=BAUD_RATE)
    args = p.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
        time.sleep(2)
        print(f"[search] connected on {args.port}")
    except Exception as e:
        print(f"Serial error: {e}"); sys.exit(1)

    app = MinitelSearch(ser, args.folder)
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n[search] stopped.")
    finally:
        app.stop()
        ser.close()

if __name__ == '__main__':
    main()
