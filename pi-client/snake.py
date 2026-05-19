#!/usr/bin/env python3
"""Snake pour Minitel — Z=haut S=bas Q=gauche D=droite  ENVOI=pause  ANNUL=quitter"""
import argparse
import os
import random
import serial
import sys
import threading
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

SEP           = 0x13
FN_ENVOI      = 0x41
FN_ANNULATION = 0x45


# ── serial helpers ─────────────────────────────────────────────────────────

def cur(row, col) -> bytes:
    return bytes([0x1F, 0x40 + row, 0x40 + col])


def send(ser, buf: bytes) -> None:
    ser.write(buf)
    ser.flush()
    time.sleep(len(buf) / 100.0)


def cls(ser) -> None:
    send(ser, b'\x0C')
    time.sleep(0.4)


# ── screens ────────────────────────────────────────────────────────────────

def draw_start(ser) -> None:
    cls(ser)
    b = bytearray()
    b += cur(6,  14) + b'* SNAKE *'
    b += cur(9,  8)  + b'Z = haut    S = bas'
    b += cur(10, 8)  + b'Q = gauche  D = droite'
    b += cur(12, 6)  + b'ENVOI pour commencer'
    b += cur(13, 6)  + b'ANNULATION pour quitter'
    send(ser, bytes(b))


def draw_field(ser, snake, food, score) -> None:
    b = bytearray()
    # score bar
    b += cur(SCORE_ROW, 1)
    b += f'SNAKE         Score : {score:>4}'.encode('ascii')
    # borders
    b += cur(TOP_ROW, 1) + ('+' + '-' * 38 + '+').encode('ascii')
    b += cur(BOT_ROW, 1) + ('+' + '-' * 38 + '+').encode('ascii')
    # help
    b += cur(HELP_ROW, 1) + b'Z S Q D = direction    ANNUL = quitter'
    # snake
    for i, (r, c) in enumerate(snake):
        b += cur(FIELD_TOP + r, 1 + c)
        b += bytes([ord('@') if i == 0 else ord('o')])
    # food
    b += cur(FIELD_TOP + food[0], 1 + food[1]) + b'*'
    send(ser, bytes(b))


def draw_game_over(ser, score) -> None:
    b = bytearray()
    b += cur(9,  11) + b'GAME  OVER'
    b += cur(11, 8)  + f'Score : {score}'.encode('ascii')
    b += cur(13, 4)  + b'ENVOI = rejouer   ANNUL = quitter'
    send(ser, bytes(b))


# ── game logic ─────────────────────────────────────────────────────────────

def free_food(snake):
    occupied = set(snake)
    while True:
        pos = (random.randint(0, FIELD_H - 1), random.randint(0, FIELD_W - 1))
        if pos not in occupied:
            return pos


def opposite(a, b):
    return a[0] + b[0] == 0 and a[1] + b[1] == 0


def run_game(ser, state):
    cy, cx = FIELD_H // 2, FIELD_W // 2
    snake   = [(cy, cx), (cy, cx - 1), (cy, cx - 2)]
    direc   = RIGHT
    state['dir'] = RIGHT
    score   = 0
    speed   = 0.40      # seconds per tick (decreases as score grows)

    food = free_food(snake)
    cls(ser)
    draw_field(ser, snake, food, score)

    while not state['quit']:
        t0 = time.time()

        # Direction (no U-turn)
        nd = state['dir']
        if not opposite(nd, direc):
            direc = nd

        hr, hc = snake[0]
        nr, nc = hr + direc[0], hc + direc[1]

        # Collision checks
        if not (0 <= nr < FIELD_H and 0 <= nc < FIELD_W):
            return score
        if (nr, nc) in snake:
            return score

        ate = (nr, nc) == food

        # Build frame — all updates in one write
        buf = bytearray()
        buf += cur(FIELD_TOP + hr, 1 + hc) + b'o'       # old head → body
        buf += cur(FIELD_TOP + nr, 1 + nc) + b'@'       # new head

        if ate:
            snake.insert(0, (nr, nc))
            score += 10
            speed  = max(0.15, speed - 0.01)
            food   = free_food(snake)
            buf += cur(FIELD_TOP + food[0], 1 + food[1]) + b'*'
            buf += cur(SCORE_ROW, 1)
            buf += f'SNAKE         Score : {score:>4}'.encode('ascii')
        else:
            snake.insert(0, (nr, nc))
            tr, tc = snake.pop()
            buf += cur(FIELD_TOP + tr, 1 + tc) + b' '  # erase tail

        send(ser, bytes(buf))

        wait = speed - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)

    return score


# ── keyboard thread ────────────────────────────────────────────────────────

def kb_thread(ser, state):
    expect_fn = False
    while not state['quit']:
        try:
            b = ser.read(1)
            if not b:
                continue
            c = b[0] & 0x7F
            if expect_fn:
                expect_fn = False
                if   c == FN_ENVOI:      state['envoi'] = True
                elif c == FN_ANNULATION: state['quit']  = True
                continue
            if   c == 0x13:     expect_fn    = True
            elif c == ord('z'): state['dir'] = UP
            elif c == ord('s'): state['dir'] = DOWN
            elif c == ord('q'): state['dir'] = LEFT
            elif c == ord('d'): state['dir'] = RIGHT
        except Exception:
            time.sleep(0.1)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default=PORT)
    args = ap.parse_args()

    ser = serial.Serial(args.port, BAUD, timeout=0.3)
    time.sleep(2)
    ser.reset_input_buffer()

    state = {'dir': RIGHT, 'quit': False, 'envoi': False}
    threading.Thread(target=kb_thread, args=(ser, state), daemon=True).start()

    while True:
        draw_start(ser)
        state['envoi'] = False
        while not state['envoi'] and not state['quit']:
            time.sleep(0.05)
        if state['quit']:
            break

        score = run_game(ser, state)
        if state['quit']:
            break

        draw_game_over(ser, score)
        state['envoi'] = False
        while not state['envoi'] and not state['quit']:
            time.sleep(0.05)

    cls(ser)
    send(ser, cur(12, 14) + b'Au revoir !')
    time.sleep(1)
    ser.close()
    sys.exit(0)


if __name__ == '__main__':
    main()
