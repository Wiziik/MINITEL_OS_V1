#!/usr/bin/env python3
"""twitch_minitel_v2.py - Two-way Twitch <-> Minitel bridge (revised).

Receives Twitch chat and prints it on the Minitel, line by line.
Reads Minitel keyboard; ENVOI sends the typed line as PRIVMSG.
Type /quit + ENVOI to return to the launcher menu.
"""
import os
import re
import serial
import socket
import ssl
import sys
import textwrap
import threading
import time
import unicodedata
import argparse
from minitel_keepalive import MinitelKeepAlive

# ── config — credentials via env vars, never hardcoded ──
SERIAL_PORT   = os.environ.get('MINITEL_PORT', '/dev/ttyUSB0')
BAUD_RATE     = 9600
TWITCH_SERVER = 'irc.chat.twitch.tv'
TWITCH_PORT   = 6697                          # TLS
TWITCH_NICK   = os.environ.get('TWITCH_NICK',    'tv_store')
TWITCH_TOKEN  = os.environ.get('TWITCH_TOKEN', '')   # set TWITCH_TOKEN env var — never hardcode
TWITCH_CHAN   = os.environ.get('TWITCH_CHANNEL', '#tv_store')

COLS      = 40
MAX_INPUT = 100

# Minitel SEP function-key codes
FN_ENVOI      = 0x41
FN_ANNULATION = 0x45
FN_CORRECTION = 0x47

# ── shared state ──
_rate_lock  = threading.Lock()   # protects _next_write only
_next_write = 0.0
ser_lock    = threading.Lock()   # protects the serial port write

conn_lock   = threading.Lock()
_socket     = None               # current live Twitch socket

input_lock  = threading.Lock()
input_buf   = ''


# ── serial write — sleep outside the lock ──────────────────────────────────

def write_ser(ser, data: bytes) -> None:
    """Rate-limited write. Reserves a send slot atomically, sleeps outside
    any lock, then writes. The ser_lock is held only for the write itself."""
    global _next_write
    with _rate_lock:
        now   = time.time()
        start = max(now, _next_write)
        _next_write = start + len(data) / 100.0

    wait = start - time.time()
    if wait > 0:
        time.sleep(wait)

    with ser_lock:
        ser.write(data)
        ser.flush()


# ── text helpers ───────────────────────────────────────────────────────────

def to_ascii(text: str) -> str:
    """Normalize accented characters to ASCII, drop the rest (emojis etc.)."""
    nfd = unicodedata.normalize('NFD', text)
    return ''.join(c for c in nfd
                   if unicodedata.category(c) != 'Mn' and ord(c) < 128)


def now_hhmm() -> str:
    return time.strftime('%H:%M')


def print_line(ser, text: str) -> None:
    line = to_ascii(text)[:COLS]
    write_ser(ser, line.encode('ascii', 'replace') + b'\r\n')


def print_message(ser, text: str) -> None:
    clean = to_ascii(text)
    lines = textwrap.wrap(clean, width=COLS) or ['']
    for ln in lines:
        print_line(ser, ln)


def print_chat(ser, user: str, msg: str) -> None:
    """Print a timestamped chat message, wrapping continuation lines."""
    ts      = now_hhmm()
    prefix  = f'{ts} {user}: '
    body    = to_ascii(msg)
    w       = COLS - len(prefix)
    parts   = textwrap.wrap(body, width=max(w, 10)) or ['']
    print_line(ser, prefix + parts[0])
    for p in parts[1:]:
        print_line(ser, ' ' * len(prefix) + p)


# ── IRC / Twitch connection ────────────────────────────────────────────────

def irc_connect(token: str, nick: str, channel: str) -> socket.socket:
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(60)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        ctx  = ssl.create_default_context()
        sock = ctx.wrap_socket(raw, server_hostname=TWITCH_SERVER)
        sock.connect((TWITCH_SERVER, TWITCH_PORT))
        sock.sendall(f'PASS {token}\r\nNICK {nick}\r\nJOIN {channel}\r\n'.encode())
        return sock
    except Exception:
        try:
            raw.close()
        except Exception:
            pass
        raise


def handle_irc_line(ser, sock, line: str, channel: str) -> None:
    if line.startswith('PING'):
        sock.sendall(b'PONG :tmi.twitch.tv\r\n')
        return
    m = re.search(r':(\w+)!\S+\s+PRIVMSG\s+#\S+\s+:(.*)', line)
    if m:
        user = m.group(1)
        msg  = m.group(2).strip()
        print_chat(ser, user, msg)


# ── listener thread (owns the socket, reconnects on drop) ─────────────────

def twitch_listener(ser, args) -> None:
    global _socket
    while True:
        sock = None
        try:
            print_message(ser, f'* connexion {args.channel}...')
            sock = irc_connect(args.token, args.nick, args.channel)
            with conn_lock:
                _socket = sock
            print_message(ser, '3615 TV STORE')
            buf = ''
            while True:
                data = sock.recv(4096).decode('utf-8', errors='ignore')
                if not data:
                    raise ConnectionError('stream closed by server')
                buf += data
                while '\r\n' in buf:
                    line, buf = buf.split('\r\n', 1)
                    if line:
                        handle_irc_line(ser, sock, line, args.channel)
        except Exception as exc:
            print(f'[twitch] {exc}')
            with conn_lock:
                _socket = None
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            print_message(ser, '* deconnecte — retry 5s...')
            time.sleep(5)


# ── keyboard reader thread ─────────────────────────────────────────────────

def send_message(ser, channel: str, nick: str) -> None:
    global input_buf
    with input_lock:
        msg       = input_buf.strip()
        input_buf = ''
    if not msg:
        return
    write_ser(ser, b'\r\n')
    with conn_lock:
        sock = _socket
    if sock:
        try:
            sock.sendall(f'PRIVMSG {channel} :{msg}\r\n'.encode('utf-8'))
            print_message(ser, f'{now_hhmm()} > {nick}: {msg}')
        except Exception as exc:
            print(f'[send] {exc}')
            print_message(ser, '* envoi echoue')
    else:
        print_message(ser, '* non connecte')


def keyboard_reader(ser, args) -> None:
    global input_buf
    expect_fn = False
    while True:
        try:
            b = ser.read(1)
            if not b:
                continue
            c = b[0] & 0x7F

            if expect_fn:
                expect_fn = False
                if c == FN_ENVOI:
                    with input_lock:
                        pending = input_buf.strip().lower()
                    if pending in ('/quit', '/q'):
                        write_ser(ser, b'\x0C')
                        time.sleep(0.2)
                        write_ser(ser, b'Au revoir.\r\n')
                        time.sleep(0.8)
                        os._exit(0)
                    send_message(ser, args.channel, args.nick)
                elif c == FN_CORRECTION:
                    with input_lock:
                        if input_buf:
                            input_buf = input_buf[:-1]
                            write_ser(ser, b'\x08 \x08')
                elif c == FN_ANNULATION:
                    with input_lock:
                        n         = len(input_buf)
                        input_buf = ''
                    if n:
                        write_ser(ser, b'\x08 \x08' * n)
                continue

            if c == 0x13:
                expect_fn = True
            elif c in (0x0D, 0x0A):
                with input_lock:
                    input_buf = ''
            elif c in (0x08, 0x7F):
                with input_lock:
                    if input_buf:
                        input_buf = input_buf[:-1]
                        write_ser(ser, b'\x08 \x08')
            elif 0x20 <= c <= 0x7E:
                with input_lock:
                    if len(input_buf) < MAX_INPUT:
                        input_buf += chr(c)
                        write_ser(ser, bytes([c]))   # echo typed char
        except Exception as exc:
            print(f'[keyboard] {exc}')
            time.sleep(0.5)


# ── main ──────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port',    default=SERIAL_PORT)
    ap.add_argument('--channel', default=TWITCH_CHAN)
    ap.add_argument('--nick',    default=TWITCH_NICK)
    ap.add_argument('--token',   default=TWITCH_TOKEN)
    args = ap.parse_args()

    if not args.token or not args.token.startswith('oauth:'):
        print('ERROR: set TWITCH_TOKEN env var (must start with oauth:)')
        sys.exit(1)

    args.channel = args.channel if args.channel.startswith('#') else f'#{args.channel}'

    print(f'Opening serial {args.port} @ {BAUD_RATE}...')
    ser = serial.Serial(args.port, BAUD_RATE, timeout=0.5)
    time.sleep(2)
    ser.reset_input_buffer()
    print('Serial open.')

    keepalive = MinitelKeepAlive(ser, interval=30)
    keepalive.start()

    write_ser(ser, b'\x0C')   # clear screen
    time.sleep(0.4)
    print_message(ser, '*** TWITCH MINITEL ***')
    print_message(ser, f'canal: {args.channel}')
    print_message(ser, 'tape + ENVOI pour envoyer')
    print_message(ser, '/quit + ENVOI pour quitter')
    print_line(ser, '-' * COLS)

    threading.Thread(target=twitch_listener, args=(ser, args), daemon=True).start()
    threading.Thread(target=keyboard_reader, args=(ser, args), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        keepalive.stop()
        with conn_lock:
            if _socket:
                try:
                    _socket.close()
                except Exception:
                    pass
        ser.close()


if __name__ == '__main__':
    main()
