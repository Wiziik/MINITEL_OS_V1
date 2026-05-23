#!/usr/bin/env python3
"""Enroll one Minitel Pi: mint a device server-side and write its local config.

Run this once per box at provisioning time (at your bench, or over SSH/Tailscale).
It calls the server's admin endpoint with your admin secret, then writes the
returned token to the config file the launcher/client read. The git tree stays
identical across all Pis; only this file differs.

Example:
  sudo python3 tools/enroll.py \
      --server https://minitel.example.com \
      --admin-secret "$MINITELNET_ADMIN_SECRET" \
      --name Camille \
      --apps chat,coeur,snake

The admin secret may also come from $MINITELNET_ADMIN_SECRET instead of --admin-secret.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_CONFIG = "/etc/minitelnet/device.json"


def _post(url, body, admin_secret):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Secret": admin_secret},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser(description="Enroll a Minitel device.")
    ap.add_argument("--server", required=True,
                    help="HTTPS base of the server, e.g. https://minitel.example.com")
    ap.add_argument("--name", required=True, help="Display name (the chat handle).")
    ap.add_argument("--admin-secret", default=os.environ.get("MINITELNET_ADMIN_SECRET", ""),
                    help="Admin secret (or set $MINITELNET_ADMIN_SECRET).")
    ap.add_argument("--apps", default="chat,coeur,snake",
                    help="Comma-separated apps from {chat,twitch,coeur,snake}.")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="Where to write the config.")
    ap.add_argument("--chat-only", action="store_true",
                    help="Kiosk mode: no menu, no settings, no keepalive.")
    ap.add_argument("--no-keepalive", action="store_true", help="Disable the XON keepalive.")
    args = ap.parse_args()

    if not args.admin_secret:
        sys.exit("error: no admin secret (pass --admin-secret or set MINITELNET_ADMIN_SECRET)")

    base = args.server.rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

    try:
        res = _post(base + "/admin/enroll", {"display_name": args.name}, args.admin_secret)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", str(e))
        except Exception:
            detail = str(e)
        sys.exit(f"enroll failed ({e.code}): {detail}")
    except Exception as e:
        sys.exit(f"enroll failed: {e}")

    cfg = {
        "server_url":    ws_url,
        "display_name":  res["display_name"],
        "device_id":     res["device_id"],
        "token":         res["token"],
        "apps":          [a.strip() for a in args.apps.split(",") if a.strip()],
        "show_menu":     not args.chat_only,
        "show_settings": not args.chat_only,
        "keepalive":     not (args.chat_only or args.no_keepalive),
    }

    os.makedirs(os.path.dirname(args.config) or ".", exist_ok=True)
    with open(args.config, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(args.config, 0o600)

    print(f"enrolled {res['device_id']} as '{res['display_name']}'")
    print(f"config written to {args.config}")


if __name__ == "__main__":
    main()
