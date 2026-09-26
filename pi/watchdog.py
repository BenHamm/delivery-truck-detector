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

ALERT lines page Ben's phone via Pushover (see `page()`), rate-limited to
one page per condition per 12h. They also stay greppable in the journal:
`journalctl -u watchdog | grep ALERT`.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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
# Minimum denominator before the fallback RATE means anything. The 1h window
# straddles the 20:00 end of active hours, so the last few runs of the evening
# see a collapsing sample: on Sep 9 2026 the 20:50 run saw 25 gates, 3 of them
# transient fallbacks, and paged "12% -- Orin endpoint flaky" while the Orin
# was serving 180/183 (1.6%) over the surrounding two hours. A percentage
# computed on 25 samples is noise, not signal.
# 60 is ~15 min of active polling at the 15s interval. This does NOT weaken
# real-outage detection: a genuine Orin failure during active hours still
# produces a full window of intended calls (~190/hr) with nearly all of them
# falling back, so the denominator is large exactly when it matters.
MIN_ORIN_CALLS_FOR_RATE = 60
TRACKED_CARRIERS = {"UPS", "FEDEX", "AMAZON"}

# Disk headroom on /. Added Sep 4 2026 after the card sat at 100% full for
# ~2 days while this watchdog ran every 5 minutes and reported nothing:
# /home/pi/traces had grown to 53G (no retention, plus every day's
# uncompressed dir kept alongside its own tarball). Detection and
# notification survived because both run off /tmp, but frame archival was
# failing with ENOSPC on every poll and we were blind to it.
# 10% of the 59G card is ~6G -- roughly a month of headroom at the ~220 MB
# per day that traces accumulate, so this fires with plenty of lead time.
DISK_FREE_PCT_MIN = 10.0

# --- Silence alarm ----------------------------------------------------------
# The end-to-end backstop: if we go long enough with ZERO notifications, the
# most likely explanation stops being "quiet week" and becomes "we're broken."
# This is the only check that covers unknown-unknowns -- Sep 3 2026 slipped
# past every component check because each component WAS healthy; what failed
# was an exception path between them.
#
# Sizing (from 345 notifications over 119 days, May 7 - Sep 2 2026):
#   weekday rate 0.2824 events/active-hour (3.39/day)
#   weekend rate 0.1397 events/active-hour (1.68/day)  -- half, so a pure
#   wall-clock threshold would false-alarm every Monday. We therefore
#   threshold on LAMBDA = expected events missed (the rate integrated over
#   elapsed active time), which is weekday/weekend invariant.
#
# P(broken | silence) = P(broken) / [P(broken) + (1-P(broken)) * S(Lambda)],
# where S is the survival function and a broken system is silent with
# certainty. With a ~2% prior (measured: ~19 of 1428 active hours lost to
# incidents in the sample window) and the empirical tail running ~1.3x fatter
# than Poisson, Lambda=6.4 reaches 90% posterior and Lambda=7.0 reaches ~95%.
#
# Backtested over the full 119 days: Lambda=6.5 fires once (Jul 28-30, a
# genuine two-day delivery drought -- verified healthy via trace summaries:
# 68 gate-YES events, Stage 2 running, just no trucks). Lambda=7.0 fires
# ZERO times on healthy history and would have caught the Sep 3 outage at
# ~13:30 on Sep 4, about 1d5h in. We take 7.0: the extra 2 hours of latency
# buys the difference between ~3 false pages/year and none.
#
# This is inherently a SLOW check -- deliveries are rare (2.9/day), so no
# threshold can confirm breakage quickly from silence alone. It is a backstop
# behind the fast checks (hourly canary, LOW_DISK), not a replacement.
ACTIVE_START_HOUR, ACTIVE_END_HOUR = 8, 20
LAMBDA_WEEKDAY = 0.2824      # notifications per active-hour
LAMBDA_WEEKEND = 0.1397
SILENCE_LAMBDA_ALERT = 7.0
NOTIFICATIONS_LOG = "/home/pi/notifications.log"

# Acknowledgement baseline. Silence is measured from whichever is LATER: the
# last real notification, or the last ack. Without this the alarm re-litigates
# closed incidents forever -- on Sep 5 2026 it paged twice for silence that
# began with the Sep 3 ENOSPC outage, an outage already diagnosed, fixed, and
# verified healthy at every layer. An alarm that cannot be told "we know, it's
# handled" trains you to ignore it, which is worse than not having it.
#   Acknowledge with:  python3 watchdog.py --ack
SILENCE_ACK_FILE = "/home/pi/.watchdog_silence_ack"




# --- Out-of-band paging -----------------------------------------------------
# Until Sep 4 2026 every ALERT here was journal-only ("Future: ping Pushover"
# in the docstring since May). That meant the disk alarm and the silence alarm
# both wrote into a log nobody reads -- on Sep 3 that would have changed
# nothing. Detection without delivery isn't monitoring.
#
# Target is Ben's own device, NOT the delivery groups: these are ops alerts,
# and Greg and Maisie should never see them.
#
# Rate-limited per condition so a sustained fault (HIGH_FALLBACK fires every
# 5 min for the duration of an Orin outage) can't machine-gun the phone.
PAGE_STATE = "/home/pi/.watchdog_pages.json"
PAGE_COOLDOWN_S = 12 * 3600
# SILENCE is the slow-moving one -- once you know it is quiet, telling you
# again twice a day is noise, not signal. Ack resets it properly.
PAGE_COOLDOWN_OVERRIDE = {"SILENCE": 48 * 3600}
# Conditions where the product is actually down for Greg get priority 1
# (bypasses quiet hours); the rest are informational.
URGENT = {"SILENCE", "HIGH_FALLBACK"}


def _page_state():
    try:
        with open(PAGE_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_page_state(state):
    try:
        with open(PAGE_STATE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        # Never let bookkeeping break the watchdog -- same lesson as the
        # Sep 3 save_detection failure.
        print(f"WATCHDOG: could not write page state: {e}", file=sys.stderr)


def page(alerts):
    """Push ALERT lines to Ben's phone, one page per condition per cooldown."""
    token = os.environ.get("PUSHOVER_APP_TOKEN", "")
    user = os.environ.get("PUSHOVER_OPS_USER", "")
    device = os.environ.get("PUSHOVER_OPS_DEVICE", "")
    if not token or not user:
        return  # paging not configured -- journal-only, as before

    state = _page_state()
    now = time.time()
    sent = False
    for a in alerts:
        key = a.split(":", 1)[0].strip()
        cooldown = PAGE_COOLDOWN_OVERRIDE.get(key, PAGE_COOLDOWN_S)
        if now - state.get(key, 0) < cooldown:
            continue
        body = {
            "token": token,
            "user": user,
            "title": f"Truck detector: {key}",
            "message": a[:900],
            "priority": 1 if key in URGENT else 0,
        }
        if device:
            body["device"] = device
        try:
            req = urllib.request.Request(
                "https://api.pushover.net/1/messages.json",
                data=urllib.parse.urlencode(body).encode(),
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                ok = r.status == 200
            if ok:
                state[key] = now
                sent = True
                print(f"WATCHDOG: paged {key}")
        except Exception as e:
            # Best-effort: a paging outage must not mask the underlying alert,
            # which is already on stderr.
            print(f"WATCHDOG: page failed for {key}: {e}", file=sys.stderr)
    if sent:
        _save_page_state(state)


def expected_events_since(start, now):
    """Integrate the notification arrival rate over ACTIVE hours only.

    Returns Lambda -- how many notifications we'd have expected in this
    window if the system were healthy. Nights contribute nothing; weekend
    hours contribute at roughly half the weekday rate.
    """
    if now <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur.date() <= now.date():
        day = cur.date()
        w0 = datetime.datetime.combine(day, datetime.time(ACTIVE_START_HOUR))
        w1 = datetime.datetime.combine(day, datetime.time(ACTIVE_END_HOUR))
        lo, hi = max(cur, w0), min(now, w1)
        if hi > lo:
            rate = LAMBDA_WEEKDAY if day.weekday() < 5 else LAMBDA_WEEKEND
            total += (hi - lo).total_seconds() / 3600.0 * rate
        cur = datetime.datetime.combine(day + datetime.timedelta(days=1),
                                        datetime.time(0))
    return total


def last_notification_time():
    """Timestamp of the most recent notification, or None. Reads the audit
    log rather than the journal: the journal rotates in ~2 days on this Pi,
    which is shorter than the alarm's own threshold."""
    last = None
    try:
        with open(NOTIFICATIONS_LOG) as f:
            for line in f:
                m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
                if m:
                    last = m.group(1)
    except OSError:
        return None
    return datetime.datetime.fromisoformat(last) if last else None


def silence_baseline():
    """Start of the current silence window: the later of last notification
    and last acknowledgement."""
    last = last_notification_time()
    try:
        with open(SILENCE_ACK_FILE) as f:
            ack = datetime.datetime.fromisoformat(f.read().strip())
    except (OSError, ValueError):
        ack = None
    if last and ack:
        return max(last, ack), (ack > last)
    if ack:
        return ack, True
    return last, False


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
    if intended_orin_calls >= MIN_ORIN_CALLS_FOR_RATE and fallback_rate > FALLBACK_RATE_PCT:
        alerts.append(f"HIGH_FALLBACK: {fallback_rate:.1f}% of intended orin calls fell "
                      f"back (threshold >{FALLBACK_RATE_PCT:.0f}%, {fallbacks}/{intended_orin_calls}). "
                      f"{'Orin endpoint DOWN' if orin_gates == 0 else 'Orin endpoint flaky'}.")
    if disagreements:
        alerts.append(f"SHADOW_DISAGREE: {len(disagreements)} disagreements in last 1h. "
                      f"First: {disagreements[0]}")

    if 0 < intended_orin_calls < MIN_ORIN_CALLS_FOR_RATE and fallback_rate > FALLBACK_RATE_PCT:
        print(f"WATCHDOG: fallback {fallback_rate:.1f}% "
              f"({fallbacks}/{intended_orin_calls}) above threshold but sample "
              f"below {MIN_ORIN_CALLS_FOR_RATE} -- not alerting (low-sample noise)")

    st = os.statvfs("/")
    disk_free_pct = 100.0 * st.f_bavail / st.f_blocks
    disk_free_gb = st.f_bavail * st.f_frsize / 1024**3
    print(f"WATCHDOG: disk / free={disk_free_pct:.1f}% ({disk_free_gb:.1f} GB)")
    if disk_free_pct < DISK_FREE_PCT_MIN:
        alerts.append(f"LOW_DISK: / is {disk_free_pct:.1f}% free ({disk_free_gb:.1f} GB, "
                      f"threshold <{DISK_FREE_PCT_MIN:.0f}%). Frame archival will start "
                      f"failing with ENOSPC -- check /home/pi/traces retention.")

    base, from_ack = silence_baseline()
    if base is not None:
        silence_lambda = expected_events_since(base, datetime.datetime.now())
        quiet_h = (datetime.datetime.now() - base).total_seconds() / 3600
        src_label = "ack" if from_ack else "last notification"
        print(f"WATCHDOG: silence lambda={silence_lambda:.2f} expected-events "
              f"({quiet_h:.1f}h since {src_label} at {base:%Y-%m-%d %H:%M})")
        if silence_lambda >= SILENCE_LAMBDA_ALERT:
            alerts.append(
                f"SILENCE: no notification in {quiet_h:.0f}h -- we expected "
                f"{silence_lambda:.1f} by now (threshold {SILENCE_LAMBDA_ALERT}). "
                f"~95% confidence something is broken end-to-end. Backtest says "
                f"this never fires on healthy history. Check: canary, disk, "
                f"camera, Orin /health, Pushover.")

    for a in alerts:
        print(f"ALERT: {a}", file=sys.stderr)

    if alerts:
        page(alerts)

    sys.exit(1 if alerts else 0)


if __name__ == "__main__":
    if "--ack" in sys.argv:
        now = datetime.datetime.now().replace(microsecond=0)
        with open(SILENCE_ACK_FILE, "w") as f:
            f.write(now.isoformat())
        # Clear the page cooldown too, so a genuine NEW silence after the ack
        # can page immediately rather than waiting out the old timer.
        st = _page_state()
        st.pop("SILENCE", None)
        _save_page_state(st)
        print(f"silence alarm acknowledged; baseline reset to {now:%Y-%m-%d %H:%M}")
        sys.exit(0)
    main()
