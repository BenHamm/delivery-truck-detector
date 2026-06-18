#!/bin/bash
# Heuristic restart of qwen3vl when /health stops responding.
#
# Background: on 2026-06-18 the qwen3vl process became unreachable on
# port 8080 sometime overnight while the Orin kernel itself stayed up.
# Pi spent 6h+ falling back to OpenRouter Gemini before manual recovery.
# This script catches that class of silent service hang within ~3 min.
#
# Logic: if /health returns non-200 (or no response within 5s) for 3
# consecutive runs, restart qwen3vl.service and reset the counter.

set -u
STATE_DIR=/var/lib/qwen3vl-watchdog
STATE_FILE=$STATE_DIR/fail_count
THRESHOLD=3
URL=http://localhost:8080/health

mkdir -p "$STATE_DIR"

if curl --max-time 5 -sS -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null | grep -q '^200$'; then
  # healthy -- reset counter
  echo 0 > "$STATE_FILE"
  exit 0
fi

# unhealthy
COUNT=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo "$COUNT" > "$STATE_FILE"
logger -t qwen3vl-watchdog "/health check failed ($COUNT/$THRESHOLD consecutive)"

if [ "$COUNT" -ge "$THRESHOLD" ]; then
  logger -t qwen3vl-watchdog "restarting qwen3vl.service (failed $COUNT consecutive /health checks)"
  systemctl restart qwen3vl.service
  echo 0 > "$STATE_FILE"
fi
