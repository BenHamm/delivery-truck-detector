#!/usr/bin/env python3
"""Periodic forensic sample of the capture frame. Detection-agnostic.

WHY: on Sep 22 2026 the long-lived ffmpeg began writing rotated,
black-cornered frames ~28h into its run. The camera was fine (a fresh RTSP
grab was upright) and detector.py has no rotation code, so the fault was
inside that ffmpeg process -- but nothing recorded WHEN it started or what
the frames looked like over time. Detection kept working well enough that
fire rate and alert volume stayed in-band, so ~27h passed before a human
noticed by eye.

This logs one line per run so a future incident can be bracketed to the
sample interval and correlated against ffmpeg-capture.log by timestamp and
by the ffmpeg pid/start time recorded here.

Black corners => rotated/letterboxed. All-dark => dead or covered camera.
Changed WxH => stream renegotiated. Stale mtime => capture stalled.
"""
import os
import subprocess
import sys
import time

CAPTURE_FILE = "/tmp/truck_capture.jpg"
LOG = "/home/pi/logs/frame-health.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
CORNER = 40


def ffprobe_val(args):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    except Exception:
        return ""


def luma(crop=None):
    chain = f"movie={CAPTURE_FILE}"
    if crop:
        chain += f",crop={crop}"
    chain += ",signalstats"
    v = ffprobe_val(["ffprobe", "-v", "error", "-f", "lavfi", "-i", chain,
                     "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
                     "-of", "csv=p=0"])
    return v or "NA"


def capture_proc():
    """pid and start time of the ffmpeg writing CAPTURE_FILE."""
    try:
        out = subprocess.run(["ps", "-eo", "pid,lstart,args"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if "ffmpeg" in line and CAPTURE_FILE in line:
                parts = line.split()
                return parts[0], " ".join(parts[1:6])
    except Exception:
        pass
    return "NA", "NA"


def main():
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass

    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        st = os.stat(CAPTURE_FILE)
        age = f"{time.time() - st.st_mtime:.1f}"
        size = st.st_size
    except OSError:
        age, size = "NA", 0

    wh = ffprobe_val(["ffprobe", "-v", "error", "-select_streams", "v",
                      "-show_entries", "stream=width,height", "-of", "csv=p=0",
                      CAPTURE_FILE]) or "NA"
    w = h = None
    if "," in wh:
        try:
            w, h = (int(x) for x in wh.split(",")[:2])
        except ValueError:
            pass

    if w and h:
        c = CORNER
        corners = {
            "tl": luma(f"{c}:{c}:0:0"),
            "tr": luma(f"{c}:{c}:{w-c}:0"),
            "bl": luma(f"{c}:{c}:0:{h-c}"),
            "br": luma(f"{c}:{c}:{w-c}:{h-c}"),
        }
    else:
        corners = {k: "NA" for k in ("tl", "tr", "bl", "br")}

    pid, started = capture_proc()
    line = (f"{ts} pid={pid} started=\"{started}\" size={size} age_s={age} "
            f"wh={wh} mean_y={luma()} "
            + " ".join(f"{k}={v}" for k, v in corners.items()) + "\n")
    try:
        with open(LOG, "a") as f:
            f.write(line)
    except OSError as e:
        print(f"could not write {LOG}: {e}", file=sys.stderr)
    sys.stdout.write(line)


if __name__ == "__main__":
    main()
