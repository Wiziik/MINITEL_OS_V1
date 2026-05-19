#!/usr/bin/env python3
"""Snake pour Minitel.
Directions : SUITE=droite  RETOUR=gauche  SOMMAIRE=haut  REPETITION=bas
             ou Z/S/Q/D (majuscule ou minuscule)  ou 8/2/4/6 (pave num)
ENVOI = commencer / rejouer    ANNULATION = quitter
"""
import argparse
import os
import random
import serial
import sys
import time

PORT = os.environ.get('MINITEL_PORT', '/dev/ttyUSB0')
BAUD = 9600

# Screen layout (1-indexed)
SCORE_ROW  = 1
TOP_ROW    = 2
FIELD_TOP  = 3
FIELD_BOT  = 22
BOT_ROW    = 23
HELP_ROW   = 24
FIELD_W    = 40
FIELD_H    = FIELD_BOT - FIELD_TOP + 1   # 20 rows

UP    = (-1,  0)
DOWN  = ( 1,  0)
LEFT  = ( 0, -1)
RIGHT = ( 0,  1)

SEP              = 0x13
FN_ENVOI         = 0x41
FN_RETOUR        = 0x42   # ← LEFT
FN_REPETITION    = 0x43   # ↓ DOWN
FN_GUIDE         = 0x44
FN_ANNULATION    = 0x45
FN_SOMMAIRE      = 0x46   # ↑ UP
FN_CORRECTION    = 0x47
FN_SUITE         = 0x48   # → RIGHT


# ── serial ─────────────────────────────────────────────────────────────────

def cur(row, col) -> bytes:
    return bytes([0x1F, 0x40 + row, 0x40 + col])


def send(ser, buf: bytes) -> None:
    ser.write(buf)
    ser.flush()
    time.sleep(len(buf) / 100.0)


def cls(ser) -> None:
    send(ser, b'\x0C')
    time.sleep(0.4)


# ── input (single-threaded, non-blocking) ──────────────────────────────────

def poll(ser, state: dict) -> None:
    """Drain all pending bytes from the serial buffer and update state."""
    while ser.in_waiting > 0:
        b = ser.read(1)
        if not b:
            break
        c = b[0] & 0x7F

        if state.get('_fn'):
            state['_fn'] = False
            if   c == FN_ENVOI:      state['envoi'] = True
            elif c == FN_ANNULATION: state['quit']  = True
            elif c == FN_SUITE:      state['dir']   = RIGHT
            elif c == FN_RETOUR:     state['dir']   = LEFT
            elif c == FN_SOMMAIRE:   state['dir']   = UP
            elif c == FN_REPETITION: state['dir']   = DOWN
        elif c == SEP:
            state['_fn'] = True
        elif 0x20 <= c <= 0x7E:
            key = chr(c).upper()
            if   key in ('Z', '8'): state['dir'] = UP
            elif key in ('S', '2'): state['dir'] = DOWN
            elif key in ('Q', '4'): state['dir'] = LEFT
            elif key in ('D', '6'): state['dir'] = RIGHT


def wait_for_envoi(ser, state: dict) -> None:
    """Block until ENVOI or ANNULATION, polling every 50 ms."""
    state['envoi'] = False
    while not state['envoi'] and not state['quit']:
        poll(ser, state)
        time.sleep(0.05)


# ── screens ────────────────────────────────────────────────────────────────

def draw_start(ser) -> None:
    cls(ser)
    b = bytearray()
    b += cur(5,  14) + b'* SNAKE *'
    b += cur(8,  4)  + b'SUITE    = droite'
    b += cur(9,  4)  + b'RETOUR   = gauche'
    b += cur(10, 4)  + b'SOMMAIRE = haut'
    b += cur(11, 4)  + b'REPET.   = bas'
    b += cur(13, 4)  + b'ou  Z/Q/S/D  ou  8/4/2/6'
    b += cur(15, 4)  + b'ENVOI       = commencer'
    b += cur(16, 4)  + b'ANNULATION  = quitter'
    send(ser, bytes(b))


def draw_field(ser, snake, food, score) -> None:
    b = bytearray()
    b += cur(SCORE_ROW, 1)
    b += f'SNAKE            Score : {score:>4}'.encode('ascii')
    b += cur(TOP_ROW, 1) + ('+' + '-' * 38 + '+').encode('ascii')
    b += cur(BOT_ROW, 1) + ('+' + '-' * 38 + '+').encode('ascii')
    b += cur(HELP_ROW, 1) + b'SUITE/RETOUR/SOMM/REP   ANNUL=quitter'
    for i, (r, c) in enumerate(snake):
        b += cur(FIELD_TOP + r, 1 + c)
        b += bytes([ord('@') if i == 0 else ord('o')])
    b += cur(FIELD_TOP + food[0], 1 + food[1]) + b'*'
    send(ser, bytes(b))


def draw_game_over(ser, score) -> None:
    b = bytearray()
    b += cur(9,  11) + b'GAME  OVER'
    b += cur(11, 8)  + f'Score : {score}'.encode('ascii')
    b += cur(13, 4)  + b'ENVOI = rejouer   ANNUL = quitter'
    send(ser, bytes(b))


# ── game ───────────────────────────────────────────────────────────────────

def free_food(snake):
    occupied = set(snake)
    while True:
        p = (random.randint(0, FIELD_H - 1), random.randint(0, FIELD_W - 1))
        if p not in occupied:
            return p


def run_game(ser, state) -> int:
    cy, cx  = FIELD_H // 2, FIELD_W // 2
    snake   = [(cy, cx), (cy, cx - 1), (cy, cx - 2)]
    direc   = RIGHT
    state['dir'] = RIGHT
    score   = 0
    speed   = 0.40

    food = free_food(snake)
    cls(ser)
    draw_field(ser, snake, food, score)

    while not state['quit']:
        t0 = time.time()

        # Collect any pending keypresses
        poll(ser, state)
        if state['quit']:
            break

        # Apply direction (no 180° U-turn)
        nd = state['dir']
        if not (nd[0] + direc[0] == 0 and nd[1] + direc[1] == 0):
            direc = nd

        hr, hc = snake[0]
        nr, nc = hr + direc[0], hc + direc[1]

        if not (0 <= nr < FIELD_H and 0 <= nc < FIELD_W):
            return score
        if (nr, nc) in snake:
            return score

        ate = (nr, nc) == food

        buf = bytearray()
        buf += cur(FIELD_TOP + hr, 1 + hc) + b'o'
        buf += cur(FIELD_TOP + nr, 1 + nc) + b'@'

        if ate:
            snake.insert(0, (nr, nc))
            score += 10
            speed  = max(0.15, speed - 0.01)
            food   = free_food(snake)
            buf += cur(FIELD_TOP + food[0], 1 + food[1]) + b'*'
            buf += cur(SCORE_ROW, 1)
            buf += f'SNAKE            Score : {score:>4}'.encode('ascii')
        else:
            snake.insert(0, (nr, nc))
            tr, tc = snake.pop()
            buf += cur(FIELD_TOP + tr, 1 + tc) + b' '

        send(ser, bytes(buf))

        # Keep polling input during the remaining tick time
        deadline = t0 + speed
        while time.time() < deadline:
            poll(ser, state)
            if state['quit']:
                return score
            time.sleep(0.03)

    return score


# ── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default=PORT)
    args = ap.parse_args()

    ser = serial.Serial(args.port, BAUD, timeout=0.1)
    time.sleep(2)
    ser.reset_input_buffer()

    state = {'dir': RIGHT, 'quit': False, 'envoi': False, '_fn': False}

    while True:
        draw_start(ser)
        wait_for_envoi(ser, state)
        if state['quit']:
            break

        score = run_game(ser, state)
        if state['quit']:
            break

        draw_game_over(ser, score)
        wait_for_envoi(ser, state)

    cls(ser)
    send(ser, cur(12, 14) + b'Au revoir !')
    time.sleep(1)
    ser.close()
    sys.exit(0)


if __name__ == '__main__':
    main()
