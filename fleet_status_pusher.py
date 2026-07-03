"""
Fleet Status Pusher
--------------------
Mirrors phone stats onto the StreakNet GitHub Pages site — no server involved.

This is the read-only data layer from monitor.py (uptime, battery %,
charging state, virtual temperature, watched-process presence), with
everything else removed on purpose:
    - no interactive SSH terminal
    - no AutoTouch tap / app-launch / screenshot control
    - no "Discover Apps" scanner
    - no web server listening for requests at all

Instead, every POLL_INTERVAL_SECONDS this script:
    1. SSHes into each phone and runs three read-only commands
       (`uptime`, `ioreg -c AppleSmartBattery -r`, `ps -axo etime,comm`)
    2. Writes the results to <REPO_DIR>/fleet-status.json
    3. If that file actually changed, commits and pushes it

GitHub Pages then serves fleet-status.json as a normal static file at
the same URL as your site, e.g.:
    https://<you>.github.io/<repo>/fleet-status.json
and index.html just fetches that file periodically. Nothing is listening
for incoming connections anywhere in this setup — this script only makes
outbound connections (to the phones, and to GitHub), so there's no new
attack surface to defend.

Requirements:
    pip install paramiko
    git must already be installed and this machine must already be able to
    `git push` to the repo without a prompt (SSH key or a cached/stored
    credential — however you normally push to this repo from this machine).

Setup:
    1. Fill in DEVICES below (same values as monitor.py).
    2. Set KEY_PATH (recommended) or a per-device password, same as monitor.py.
    3. Set REPO_DIR to the local path of your cloned StreakNet GitHub Pages
       repo (the folder that contains index.html).
    4. Run: python fleet_status_pusher.py
    5. Leave it running (e.g. in a terminal, or as a background/startup task).

Notes:
    - Every push triggers a small GitHub Pages rebuild (usually live again
      within well under a minute). POLL_INTERVAL_SECONDS defaults to 60 to
      keep that reasonable and avoid spamming your commit history — phone
      battery/temp doesn't change fast enough to need it more often than that.
    - The script skips the commit entirely if nothing changed, so idle
      phones won't generate empty commits.
"""

import re
import json
import time
import subprocess
from pathlib import Path

import paramiko

# ----------------------------------------------------------------------------
# CONFIG — edit this section for your phones and repo
# ----------------------------------------------------------------------------

KEY_PATH = None  # e.g. r"C:\Users\you\.ssh\iphone_rig" — set this once key auth works

DEVICES = [
    {
        "name": "iPhone 8 (Global)",
        "host": "192.168.0.108",
        "user": "mobile",
        "password": "your-custom-password",
        "port": 22,
    },
    {
        "name": "iPhone 8 (GSM)",
        "host": "192.168.0.218",
        "user": "mobile",
        "password": "your-custom-password",
        "port": 22,
    },
]

WATCHED_PROCESSES = [
    "Immortalizer",
    "AdvancedBrightnessSlider",
    "NewTerm3",
    "sshd",
    "SpringBoard",
    "backboardd",
]

# Local path to your cloned GitHub Pages repo (the folder with index.html in it).
REPO_DIR = Path(r"C:\Users\Anthony Barnett\StreakNET")

OUTPUT_FILENAME = "fleet-status.json"
POLL_INTERVAL_SECONDS = 60
GIT_COMMIT_MESSAGE = "Update fleet status"

# ----------------------------------------------------------------------------
# DATA LAYER (read-only) — ported from monitor.py, control features removed
# ----------------------------------------------------------------------------


def ssh_connect(device_cfg):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=device_cfg["host"],
        port=device_cfg.get("port", 22),
        username=device_cfg["user"],
        timeout=4,
        banner_timeout=4,
        auth_timeout=4,
    )
    if KEY_PATH:
        connect_kwargs["key_filename"] = KEY_PATH
    else:
        connect_kwargs["password"] = device_cfg.get("password")
    client.connect(**connect_kwargs)
    return client


def run_cmd(client, cmd, timeout=5):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="ignore")
    err = stderr.read().decode(errors="ignore")
    return out, err


def parse_uptime(raw):
    m = re.search(r"up\s+(.*?),\s+\d+ user", raw)
    if m:
        return m.group(1).strip()
    return raw.strip() or "—"


def parse_ioreg_battery(raw):
    def find_int(key):
        m = re.search(rf'"{key}"\s*=\s*(-?\d+)', raw)
        return int(m.group(1)) if m else None

    def find_bool(key):
        m = re.search(rf'"{key}"\s*=\s*(Yes|No|true|false)', raw, re.IGNORECASE)
        if not m:
            return None
        return m.group(1).lower() in ("yes", "true")

    capacity = find_int("CurrentCapacity")
    charging = find_bool("IsCharging")
    virtual_temp_centideg = find_int("VirtualTemperature")

    celsius = None
    fahrenheit = None
    if virtual_temp_centideg is not None:
        celsius = virtual_temp_centideg / 100.0
        fahrenheit = celsius * 9 / 5 + 32

    return capacity, charging, celsius, fahrenheit


def parse_processes(raw):
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    found = []
    for line in lines[1:]:  # skip header
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        etime, comm = parts
        base = comm.rsplit("/", 1)[-1]
        for watched in WATCHED_PROCESSES:
            if watched.lower() in base.lower():
                found.append(watched)
                break
    return found, len(lines) - 1


def fetch_status(device_cfg):
    """Returns a plain dict — safe to serialize straight to JSON. Contains no
    host/user/password, only the display name and the stats."""
    result = {
        "name": device_cfg["name"],
        "online": False,
        "error": "",
        "uptime": "—",
        "battery_pct": None,
        "is_charging": False,
        "temp_f": None,
        "temp_c": None,
        "watched_processes": [],
        "process_count": None,
    }
    try:
        client = ssh_connect(device_cfg)
    except Exception:
        result["error"] = "offline or unreachable"
        return result

    try:
        result["online"] = True

        out, _ = run_cmd(client, "uptime")
        result["uptime"] = parse_uptime(out)

        out, err = run_cmd(client, "ioreg -c AppleSmartBattery -r")
        if "not found" in err.lower() or not out.strip():
            result["error"] = "battery/temp unavailable on device"
        else:
            capacity, charging, c, f = parse_ioreg_battery(out)
            result["battery_pct"] = capacity
            result["is_charging"] = bool(charging)
            result["temp_c"] = round(c, 1) if c is not None else None
            result["temp_f"] = round(f, 1) if f is not None else None

        out, _ = run_cmd(client, "ps -axo etime,comm")
        procs, total = parse_processes(out)
        result["watched_processes"] = procs
        result["process_count"] = total

    except Exception:
        result["error"] = "stat read failed"
    finally:
        try:
            client.close()
        except Exception:
            pass

    return result


# ----------------------------------------------------------------------------
# WRITE + PUSH TO THE REPO
# ----------------------------------------------------------------------------


def build_snapshot():
    return {
        "generated_at": time.time(),
        "devices": [fetch_status(d) for d in DEVICES],
    }


def write_snapshot(snapshot, output_path):
    output_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )


def push_if_changed(repo_dir, output_filename):
    """Commits + pushes OUTPUT_FILENAME only if it actually changed."""
    status = git(["status", "--porcelain", output_filename], cwd=repo_dir)
    if not status.stdout.strip():
        return False, "no changes"

    git(["add", output_filename], cwd=repo_dir)
    commit = git(["commit", "-m", GIT_COMMIT_MESSAGE], cwd=repo_dir)
    if commit.returncode != 0:
        return False, f"commit failed: {commit.stderr.strip()[:200]}"

    push = git(["push"], cwd=repo_dir)
    if push.returncode != 0:
        return False, f"push failed: {push.stderr.strip()[:200]}"

    return True, "pushed"


def main():
    output_path = REPO_DIR / OUTPUT_FILENAME
    if not REPO_DIR.exists():
        raise SystemExit(f"REPO_DIR does not exist: {REPO_DIR}")

    print(f"Fleet status pusher running — writing to {output_path}")
    print(f"Polling every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.\n")

    while True:
        try:
            snapshot = build_snapshot()
            write_snapshot(snapshot, output_path)
            ok, info = push_if_changed(REPO_DIR, OUTPUT_FILENAME)
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] wrote snapshot — {info}")
        except Exception as e:
            print(f"[error] {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
