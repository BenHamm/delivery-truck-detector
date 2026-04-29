#!/usr/bin/env python3
"""Pull recent interesting frames from the Pi into eval/frames/_unlabeled/
so they can be reviewed and added to dataset.json by hand.

"Interesting" = anything that's not a routine NONE background sample:
  - YES_*_tentative.jpg  (Stage 1 fired)
  - <CARRIER>_*_confirm.jpg for any carrier (Stage 2 verdict)
  - Optionally: NONE_*.jpg samples that occurred within ~2min of a tentative
    (lead-up frames, useful for false-positive/false-negative debugging)

Usage:
    python ingest_from_pi.py --since "2026-04-29 00:00:00"
    python ingest_from_pi.py --hours 24
"""
import argparse
import datetime
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
STAGING = HERE / "frames" / "_unlabeled"
PI_HOST = "pi@100.73.243.128"
PI_PASS = "raspberry"
PI_DETECTIONS = "/home/pi/detections"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def ssh(remote_cmd):
    return run(["sshpass", "-p", PI_PASS, "ssh",
                "-o", "StrictHostKeyChecking=no", PI_HOST, remote_cmd])


def scp(remote_path, local_path):
    return run(["sshpass", "-p", PI_PASS, "scp",
                "-o", "StrictHostKeyChecking=no",
                f"{PI_HOST}:{remote_path}", str(local_path)])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=None,
                   help="Only pull frames with mtime newer than this (YYYY-MM-DD HH:MM:SS)")
    p.add_argument("--hours", type=int, default=24,
                   help="If --since not given, pull frames from the last N hours")
    p.add_argument("--include-none", action="store_true",
                   help="Also pull NONE_* frames (default: skip routine NONE samples)")
    args = p.parse_args()

    if args.since:
        cutoff = datetime.datetime.fromisoformat(args.since)
    else:
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=args.hours)

    STAGING.mkdir(parents=True, exist_ok=True)
    print(f"Cutoff: {cutoff.isoformat()}")

    # List interesting files from the Pi.
    pattern = "YES_*_tentative.jpg|UPS_*_confirm.jpg|FEDEX_*_confirm.jpg|AMAZON_*_confirm.jpg|USPS_*_confirm.jpg|OTHER_*_confirm.jpg|NONE_*_confirm.jpg"
    listing = ssh(f"ls -l {PI_DETECTIONS}/")
    if listing.returncode != 0:
        print(f"FAILED to list Pi detections: {listing.stderr}", file=sys.stderr)
        sys.exit(1)

    pulled = []
    skipped_old = 0
    skipped_routine_none = 0

    for line in listing.stdout.splitlines():
        m = re.match(r"\S+\s+\d+\s+\S+\s+\S+\s+\d+\s+(\S+)\s+(\S+)\s+(\S+\.jpg)$", line)
        if not m:
            continue
        month, day_or_time, name = m.group(1), m.group(2), m.group(3)

        # Filter: confirm/tentative pairs always interesting; NONE_*_confirm
        # is interesting (Stage 2 dismissed a tentative). Plain NONE_*.jpg
        # background samples are routine; skip unless --include-none.
        is_routine_none = name.startswith("NONE_") and "_confirm" not in name
        if is_routine_none and not args.include_none:
            skipped_routine_none += 1
            continue

        # Parse the timestamp from the filename: e.g. YES_20260429_123056_tentative.jpg
        ts_match = re.search(r"_(\d{8})_(\d{6})", name)
        if not ts_match:
            continue
        date_s, time_s = ts_match.group(1), ts_match.group(2)
        ts = datetime.datetime.strptime(date_s + time_s, "%Y%m%d%H%M%S")
        if ts < cutoff:
            skipped_old += 1
            continue

        local = STAGING / name
        if local.exists():
            continue
        r = scp(f"{PI_DETECTIONS}/{name}", local)
        if r.returncode == 0:
            pulled.append(name)
            print(f"  pulled: {name}")
        else:
            print(f"  FAILED: {name} ({r.stderr.strip()})", file=sys.stderr)

    print()
    print(f"Pulled {len(pulled)} new frames -> {STAGING}")
    print(f"Skipped {skipped_old} old (before cutoff) and {skipped_routine_none} routine NONE samples")
    if pulled:
        print()
        print("Next: review the frames, move them under frames/<date>/ with descriptive names,")
        print("and add labeled entries to dataset.json.")


if __name__ == "__main__":
    main()
