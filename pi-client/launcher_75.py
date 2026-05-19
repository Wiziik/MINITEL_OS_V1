#!/usr/bin/env python3
"""Launcher for .75 — WiFi setup if needed, then MinitelNet chat."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from minitel import Minitel

PORT = os.environ.get('MINITEL_PORT', '/dev/ttyACM0')
HERE = os.path.dirname(os.path.abspath(__file__))
PY   = sys.executable


# ── wifi helpers (same as launcher.py) ────────────────────────────────────

def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)


def _is_connected():
    ok, _ = _run(['ping', '-c', '2', '-W', '2', '8.8.8.8'], timeout=8)
    return ok


def _scan_networks():
    ok, out = _run(
        ['nmcli', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list'],
        timeout=12,
    )
    if not ok or not out.strip():
        return []
    lines = out.strip().split('\n')
    if len(lines) < 2:
        return []
    header = lines[0]
    try:
        ssid_pos     = header.index('SSID')
        signal_pos   = header.index('SIGNAL')
        security_pos = header.index('SECURITY')
    except ValueError:
        return []
    networks, seen = [], set()
    for line in lines[1:]:
        if len(line) < signal_pos:
            continue
        active   = '*' in line[:ssid_pos]
        ssid     = line[ssid_pos:signal_pos].strip()
        signal   = line[signal_pos:security_pos].strip()
        security = line[security_pos:].strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        networks.append({
            'ssid':     ssid,
            'signal':   int(signal) if signal.isdigit() else 0,
            'security': security,
            'active':   active,
        })
    return sorted(networks, key=lambda n: -n['signal'])


def _signal_bar(signal):
    if signal >= 75: return '####'
    if signal >= 50: return '### '
    if signal >= 25: return '##  '
    return '#   '


def _connect_wifi(ssid, password=None):
    cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]
    if password:
        cmd += ['password', password]
    return _run(cmd, timeout=30)


def wifi_setup(mt):
    mt.clear_screen()
    mt.send_line('')
    mt.send_line('  Verification WiFi...')
    for _ in range(5):
        time.sleep(2)
        if _is_connected():
            mt.send_line('  WiFi OK!')
            time.sleep(1)
            return

    while True:
        mt.clear_screen()
        mt.send_line('  Scan des reseaux...')
        networks = _scan_networks()
        mt.clear_screen()
        mt.send_line('WIFI')
        mt.send_line('----------------------------------------')
        mt.send_line('')
        if not networks:
            mt.send_line('  Aucun reseau trouve.')
            mt.send_line('  Verifiez l\'adaptateur WiFi.')
            mt.send_line('')
        else:
            for i, n in enumerate(networks[:7], 1):
                bar  = _signal_bar(n['signal'])
                sec  = ('WPA' if 'WPA' in n['security']
                        else '--' if not n['security'].strip()
                        else n['security'][:4])
                mark = '*' if n['active'] else ' '
                mt.send_line(f" {mark}{i}. {n['ssid'][:22]:<22} {bar} {sec}")
            mt.send_line('')
        mt.send_line('  0. Ignorer (pas de WiFi)')
        mt.send_line('')
        mt.send_line('  Numero + ENVOI:')
        mt.send_text('  > ')

        choice = mt.read_input().strip()
        if choice == '0':
            return
        if not choice.isdigit():
            continue
        idx = int(choice) - 1
        if idx < 0 or idx >= len(networks):
            continue

        network  = networks[idx]
        ssid     = network['ssid']
        needs_pw = bool(network['security'].strip()) and network['security'].strip() != '--'
        password = None
        if needs_pw:
            mt.send_line('')
            mt.send_line(f"  Reseau: {ssid[:30]}")
            mt.send_line('  Mot de passe WiFi:')
            mt.send_text('  > ')
            password = mt.read_password()
            if not password:
                continue

        mt.send_line('')
        mt.send_line(f"  Connexion a {ssid[:26]}...")
        ok, msg = _connect_wifi(ssid, password)
        if ok:
            for _ in range(5):
                time.sleep(2)
                if _is_connected():
                    mt.send_line('  Connecte!')
                    time.sleep(1.5)
                    return
            mt.send_line('  Associe, pas d\'internet.')
            mt.send_line('  Appuyez sur ENVOI.')
            mt.read_input()
        else:
            err = msg.strip().split('\n')[-1][:36]
            mt.send_line(f"  Echec: {err}")
            mt.send_line('  Appuyez sur ENVOI.')
            mt.read_input()


# ── main ───────────────────────────────────────────────────────────────────

def main():
    mt = Minitel(port=PORT)
    time.sleep(2.5)
    mt.ser.reset_input_buffer()

    wifi_setup(mt)

    mt.clear_screen()
    mt.send_line('')
    mt.send_line('  Lancement MinitelNet...')
    mt.send_line('')
    mt.close()
    time.sleep(0.5)

    subprocess.run(
        [PY, os.path.join(HERE, 'client.py')],
        env=os.environ.copy(),
    )


if __name__ == '__main__':
    main()
