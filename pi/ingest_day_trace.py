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

# How many days of trace tarballs to keep. Traces exist for forensics and
# eval mining, both of which work on recent data. Unbounded retention is
# what filled the 59G card on Sep 4 2026 -- 124 days of traces (53G) took
# the root filesystem to 100%, and every detector archival write started
# failing with ENOSPC. 30 days is ~7G at current volume.
RETAIN_DAYS = 30

# Frames worth keeping forever. A day-trace is ~1,000 frames / ~200 MB, but
# ~97% is unsuffixed NONE_* -- the camera watching an empty street. The eval
# set keys on tentative/confirm pairs, and carrier-prefixed frames are the
# actual catches. Dropping only unsuffixed NONE_* shrinks a day to ~1-3% while
# preserving every frame anything has ever read, plus trace.jsonl, which still
# describes ALL frames (so fire-rate/verdict analysis survives intact).
# The "-distilled" name deliberately does NOT match the RETAIN_DAYS glob
# below, so distilled archives are immune to retention pruning.
DROP_FRAME = re.compile(r"(^|/)NONE_\d{8}_\d{6}\.jpg$")


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
    """Find the journal event closest in time to ts_match that matches kind.
    Frame filename timestamps are tz-naive local time; journal short-iso
    timestamps are tz-aware. Strip the tz to compare."""
    best = None
    best_dt = None
    for e in events:
        if not kind_filter(e["msg"]):
            continue
        try:
            etime = datetime.datetime.fromisoformat(e["ts"]).replace(tzinfo=None)
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
            "fired_notification": (
                notify_event is not None and carrier_event is not None and
                abs((datetime.datetime.fromisoformat(carrier_event["ts"]).replace(tzinfo=None) -
                     datetime.datetime.fromisoformat(notify_event["ts"]).replace(tzinfo=None)
                     ).total_seconds()) < 30
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

    # 7. Drop the uncompressed source dir now that it's archived.
    #    Before Sep 4 2026 this step didn't exist, so every run left BOTH
    #    the dir and its tarball on disk -- traces/ grew at 2x the needed
    #    rate and eventually filled the card. Verify by file count first:
    #    a truncated tarball must never cost us the only copy.
    n_src = sum(1 for p in out_dir.rglob("*") if p.is_file())
    with tarfile.open(tar_path, "r:gz") as tar:
        n_tar = sum(1 for m in tar.getmembers() if m.isfile())
    if n_src > 0 and n_tar == n_src:
        shutil.rmtree(out_dir)
        print(f"[ingest] verified {n_tar} files in tarball; removed source dir")
    else:
        print(f"[ingest] WARNING: tarball/dir mismatch (dir={n_src} tar={n_tar}) "
              f"-- keeping {out_dir}")

    # 8. Distill: write the permanent small archive alongside the full one.
    #    Retention (step 9) eventually deletes the 200 MB original; this is
    #    what survives.
    dist_path = TRACES_DIR / (tar_path.name[: -len(".tar.gz")] + "-distilled.tar.gz")
    if not dist_path.exists():
        kept = dropped = 0
        try:
            with tarfile.open(tar_path, "r|gz") as tin, \
                 tarfile.open(dist_path, "w:gz") as tout:
                for m in tin:
                    if not m.isfile():
                        continue
                    if DROP_FRAME.search(m.name):
                        dropped += 1
                    else:
                        tout.addfile(m, tin.extractfile(m))
                        kept += 1
            print(f"[ingest] distilled: {dist_path.name} kept={kept} dropped={dropped} "
                  f"({dist_path.stat().st_size / 1024 / 1024:.1f} MB)")
        except Exception as e:
            print(f"[ingest] WARNING: distill failed: {e}")
            dist_path.unlink(missing_ok=True)

    # 9. Retention: prune tarballs older than RETAIN_DAYS.
    cutoff = target_date - datetime.timedelta(days=RETAIN_DAYS)
    pruned = freed = 0
    for old in sorted(TRACES_DIR.glob("????-??-??.tar.gz")):
        # NB: .stem only strips the LAST suffix, so "2026-05-01.tar.gz"
        # gives "2026-05-01.tar" -- which fromisoformat rejects, silently
        # turning this whole loop into a no-op. Strip the full extension.
        try:
            d = datetime.date.fromisoformat(old.name[: -len(".tar.gz")])
        except ValueError:
            continue
        if d < cutoff:
            freed += old.stat().st_size
            old.unlink()
            pruned += 1
            print(f"[ingest] retention: removed {old.name}")
    if pruned:
        print(f"[ingest] retention: pruned {pruned} tarball(s) older than "
              f"{RETAIN_DAYS}d, freed {freed / 1024 / 1024:.0f} MB")

    # 9. Disk report -- cheap signal in the ingest log if we're trending full.
    st = shutil.disk_usage("/")
    print(f"[ingest] disk /: {st.free / 1024**3:.1f} GB free "
          f"({100.0 * st.free / st.total:.1f}%)")


if __name__ == "__main__":
    main()
