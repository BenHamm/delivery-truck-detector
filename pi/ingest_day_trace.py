#!/usr/bin/env python3
"""Capture a full day of production traffic as a 'trace eval' set.

Pulls every saved frame from /home/pi/detections/ for a given date and
cross-references with the systemd journal to build a per-frame ground
truth: production's Stage 1 verdict, Stage 2 verdict (if Stage 1 fired),
and end-to-end fire decision.

Output:
  /home/pi/traces/<YYYY-MM-DD>/
      trace.jsonl           one JSON object per frame, in time order
      frames/               original JPGs from /home/pi/detections/
      trace.tar.gz          single-file archive (frames + jsonl)

This is meaningfully more expensive to evaluate against than the curated
25-case eval (~1,400 frames per day vs 25), but covers the long tail of
routine production traffic that the curated set can't represent. Run
against any future architecture experiment to confirm "would this have
agreed with production on every poll?"

Usage:
    python3 ingest_day_trace.py 2026-05-01
    python3 ingest_day_trace.py today
"""
import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile


DETECTIONS_DIR = pathlib.Path("/home/pi/detections")
TRACES_DIR = pathlib.Path("/home/pi/traces")


def parse_date(arg):
    if arg in ("today", "now"):
        return datetime.date.today()
    return datetime.date.fromisoformat(arg)


def journal_for_day(d):
    """Return all detector journal lines for the given date."""
    start = f"{d.isoformat()} 00:00:00"
    end = f"{d.isoformat()} 23:59:59"
    p = subprocess.run(
        ["journalctl", "-u", "detector", "--since", start, "--until", end,
         "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True, check=False,
    )
    return p.stdout.splitlines()


def build_journal_index(lines):
    """Produce a list of {timestamp, kind, msg} dicts in chronological order."""
    out = []
    for line in lines:
        # `short-iso` format: "2026-05-01T18:23:15-0700 hostname proc[pid]: <msg>"
        m = re.match(r"^(\S+) \S+ \S+: \d{4}-\d{2}-\d{2} \S+ (.*)$", line)
        if not m:
            continue
        ts, msg = m.group(1), m.group(2)
        out.append({"ts": ts, "msg": msg})
    return out


def find_nearest_journal_event(events, ts_match, kind_filter):
    """Find the journal event closest in time to ts_match that matches kind."""
    best = None
    best_dt = None
    for e in events:
        if not kind_filter(e["msg"]):
            continue
        try:
            etime = datetime.datetime.fromisoformat(e["ts"])
        except ValueError:
            continue
        delta = abs((etime - ts_match).total_seconds())
        if best_dt is None or delta < best_dt:
            best = e
            best_dt = delta
    return best, best_dt


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    target_date = parse_date(sys.argv[1])

    out_dir = TRACES_DIR / target_date.isoformat()
    frames_dir = out_dir / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(exist_ok=True)

    print(f"[ingest] target_date={target_date.isoformat()}")
    print(f"[ingest] output={out_dir}")

    # 1. Gather frames for the date.
    date_glob = target_date.strftime("%Y%m%d")
    candidates = sorted(DETECTIONS_DIR.glob(f"*_{date_glob}_*.jpg"))
    print(f"[ingest] {len(candidates)} frames in detections/ for {date_glob}")

    # 2. Pull and structure the journal.
    journal = build_journal_index(journal_for_day(target_date))
    print(f"[ingest] {len(journal)} journal events for the day")

    is_gate = lambda m: m.startswith("Gate")
    is_carrier = lambda m: m.startswith("Carrier:")
    is_notify = lambda m: "Notification sent" in m

    # 3. Build per-frame entries.
    entries = []
    n_with_gate = 0
    n_yes = 0
    for frame in candidates:
        # filename like NONE_20260501_143025.jpg or YES_20260501_143025_tentative.jpg
        m = re.match(r"^([A-Z]+)_(\d{8})_(\d{6})(?:_(.+))?\.jpg$", frame.name)
        if not m:
            continue
        verdict_prefix, ymd, hms, suffix = m.groups()
        ts = datetime.datetime.strptime(ymd + hms, "%Y%m%d%H%M%S")

        # Find nearest Gate event within 90s of frame timestamp
        gate, gate_dt = find_nearest_journal_event(journal, ts, is_gate)
        carrier_event, _ = find_nearest_journal_event(journal, ts, is_carrier)
        notify_event, _ = find_nearest_journal_event(journal, ts, is_notify)

        gate_msg = gate["msg"] if gate and gate_dt is not None and gate_dt < 90 else None
        gate_verdict = None
        gate_backend = None
        if gate_msg:
            n_with_gate += 1
            gate_verdict = "YES" if gate_msg.rstrip().endswith("YES") else "NO"
            gate_backend = "orin" if "orin" in gate_msg.lower() else "gemini"
            if gate_verdict == "YES":
                n_yes += 1

        entry = {
            "frame": frame.name,
            "ts": ts.isoformat(),
            "filename_prefix": verdict_prefix,
            "filename_suffix": suffix,
            "gate_verdict": gate_verdict,
            "gate_backend": gate_backend,
            "gate_dt_seconds": gate_dt,
            "carrier_msg": carrier_event["msg"] if carrier_event else None,
            "fired_notification": notify_event is not None and (
                carrier_event and abs((datetime.datetime.fromisoformat(carrier_event["ts"]) -
                                       datetime.datetime.fromisoformat(notify_event["ts"])).total_seconds()) < 30
                if carrier_event else False
            ),
        }
        entries.append(entry)

        # Copy frame
        shutil.copy(frame, frames_dir / frame.name)

    # Sort by timestamp
    entries.sort(key=lambda e: e["ts"])

    # 4. Write trace.jsonl
    jsonl_path = out_dir / "trace.jsonl"
    with jsonl_path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    print(f"[ingest] wrote {jsonl_path} ({len(entries)} entries)")

    # 5. Write summary
    summary = {
        "date": target_date.isoformat(),
        "frame_count": len(entries),
        "frames_with_gate_match": n_with_gate,
        "yes_count": n_yes,
        "fire_rate_pct": (n_yes / n_with_gate * 100) if n_with_gate else 0.0,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[ingest] summary: {summary}")

    # 6. Tarball it
    tar_path = out_dir.with_suffix(".tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(out_dir, arcname=target_date.isoformat())
    print(f"[ingest] tarball: {tar_path} ({tar_path.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
