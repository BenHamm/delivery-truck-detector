#!/usr/bin/env python3
"""Threshold watchdog for the new Stage 1 architecture.

Reads the detector journal for the last hour, computes:
  - Stage 1 fire rate (YES per total gates)
  - Fallback rate (orin-failed -> gemini)
  - Shadow disagreements (orin verdict != gemini verdict)

Emits ALERT lines (grep-friendly) when thresholds are exceeded.
Designed to be run every 5 min via systemd timer.

Thresholds derived from May 1 production data (4 YES across 660 gates over
7.5h = 0.5/hr mean fire rate, max 1/hr observed):
  - HIGH_FIRE: >15 fires in any rolling 1h. ~30x baseline; even a busy
    delivery day with 5 trucks * 5 fires each across 12h hits ~2/hr peak.
  - FALLBACK: >10% of orin calls falling back to gemini. Healthy = 0%.
  - SHADOW_DISAGREE: any single occurrence (Orin=NO + Gemini=YES is the
    smoking gun for "we missed a real catch"; Orin=YES + Gemini=NO means
    permissive Stage 1 fired on something restrictive Stage 1 wouldn't,
    which is expected by design but still worth knowing).

Future: ping Pushover or Healthchecks.io on ALERT lines. For now they're
journal-only -- grep with `journalctl -u watchdog | grep ALERT`.
"""
import re
import subprocess
import sys
from collections import Counter

# Thresholds (see module docstring for derivation).
# HIGH_TRACKED is the post-Stage-2 fire rate -- Stage 1 YES followed by
# carrier classification of UPS/FEDEX/AMAZON. This is the rate at which
# we'd actually notify Greg. May 5 incident: a USPS truck idled for ~3h
# producing 35 raw Stage 1 YES with zero tracked-carrier verdicts; old
# rule (>15 raw YES/hr) spammed alerts even though the system handled
# the case correctly. The right signal is *what we notified on*.
HIGH_TRACKED_PER_HOUR = 8
FALLBACK_RATE_PCT = 10.0
TRACKED_CARRIERS = {"UPS", "FEDEX", "AMAZON"}


def journal_last_hour():
    """Return all detector log lines from the last 60 minutes."""
    p = subprocess.run(
        ["journalctl", "-u", "detector", "--since", "60 minutes ago",
         "--no-pager", "-o", "short"],
        capture_output=True, text=True, check=False,
    )
    return p.stdout.splitlines()


def main():
    lines = journal_last_hour()
    counts = Counter()
    disagreements = []
    for line in lines:
        # Match the message portion after the "datetime hostname proc[pid]: " preamble.
        m = re.match(r"^\S+\s+\d+\s+\S+\s+\S+\s+\S+:\s+\S+\s+\S+\s+(.*)$", line)
        if not m:
            continue
        msg = m.group(1)
        if msg.startswith("Gate"):
            counts["gates"] += 1
            if "orin" in msg.lower():
                counts["orin_gates"] += 1
            if msg.rstrip().endswith("YES"):
                counts["yes"] += 1
        elif msg.startswith("Carrier:"):
            # "Carrier: 'UPS' -> UPS"  -- pull verdict after the arrow
            verdict = msg.rsplit("->", 1)[-1].strip()
            if verdict in TRACKED_CARRIERS:
                counts["tracked"] += 1
        elif "falling back to Gemini" in msg:
            counts["fallbacks"] += 1
        elif msg.startswith("Shadow:"):
            counts["shadow_agree"] += 1
        elif "SHADOW DISAGREE" in msg:
            counts["shadow_disagree"] += 1
            disagreements.append(msg[:160])

    gates = counts["gates"]
    yes = counts["yes"]
    orin_gates = counts["orin_gates"]
    fallbacks = counts["fallbacks"]
    # Fraction of *intended* Orin calls that actually fell back. When Orin
    # is fully down, orin_gates=0 and ALL traffic is fallbacks -- we need
    # this to register as 100%, not "no data".
    intended_orin_calls = orin_gates + fallbacks
    fallback_rate = (fallbacks / intended_orin_calls * 100
                     if intended_orin_calls else 0.0)

    tracked = counts["tracked"]
    print(f"WATCHDOG: 1h window  gates={gates} yes={yes} tracked={tracked} "
          f"orin={orin_gates} fallbacks={fallbacks} ({fallback_rate:.1f}%) "
          f"shadow_agree={counts['shadow_agree']} shadow_disagree={counts['shadow_disagree']}")

    alerts = []
    if tracked > HIGH_TRACKED_PER_HOUR:
        alerts.append(f"HIGH_TRACKED: {tracked} tracked-carrier verdicts in last 1h "
                      f"(threshold >{HIGH_TRACKED_PER_HOUR}). Likely a real delivery surge or "
                      f"a Stage 2 hallucination -- worth eyeballing.")
    if intended_orin_calls > 0 and fallback_rate > FALLBACK_RATE_PCT:
        alerts.append(f"HIGH_FALLBACK: {fallback_rate:.1f}% of intended orin calls fell "
                      f"back (threshold >{FALLBACK_RATE_PCT:.0f}%, {fallbacks}/{intended_orin_calls}). "
                      f"{'Orin endpoint DOWN' if orin_gates == 0 else 'Orin endpoint flaky'}.")
    if disagreements:
        alerts.append(f"SHADOW_DISAGREE: {len(disagreements)} disagreements in last 1h. "
                      f"First: {disagreements[0]}")

    for a in alerts:
        print(f"ALERT: {a}", file=sys.stderr)

    sys.exit(1 if alerts else 0)


if __name__ == "__main__":
    main()
