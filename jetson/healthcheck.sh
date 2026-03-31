#!/bin/bash
# Health check for Greg's truck detector Pi + camera
# Runs every 5 min via cron. After 3 consecutive failures, alerts via Signal.

STATE_FILE="/tmp/truck_detector_health_failures"
MAX_FAILURES=3
PI_IP="100.73.243.128"
SIGNAL_API="http://localhost:8282/api/v1/rpc"
SIGNAL_ACCOUNT="+13603583383"
SIGNAL_RECIPIENT="eba7746f-5cda-4981-aeac-98ae2fc2849e"

# Read current failure count
FAILURES=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

# Check 1: Can we reach the Pi over Tailscale?
if ! ping -c 1 -W 5 "$PI_IP" > /dev/null 2>&1; then
    FAILURES=$((FAILURES + 1))
    echo "$FAILURES" > "$STATE_FILE"

    if [ "$FAILURES" -eq "$MAX_FAILURES" ]; then
        # Send Signal alert
        curl -s "$SIGNAL_API" \
            -H "Content-Type: application/json" \
            -d "{\"jsonrpc\":\"2.0\",\"method\":\"send\",\"params\":{\"account\":\"$SIGNAL_ACCOUNT\",\"recipients\":[\"$SIGNAL_RECIPIENT\"],\"message\":\"⚠️ Truck detector Pi (greg-cam-bridge) has been unreachable for 15 minutes. Camera may be down. Check on it.\"},\"id\":1}" \
            > /dev/null 2>&1
        logger -t truck-healthcheck "ALERT: Pi unreachable for $MAX_FAILURES consecutive checks, Signal sent"
    elif [ "$FAILURES" -gt "$MAX_FAILURES" ]; then
        # Already alerted, log only every 12 checks (1 hour)
        if [ $((FAILURES % 12)) -eq 0 ]; then
            logger -t truck-healthcheck "STILL DOWN: Pi unreachable for $((FAILURES * 5)) minutes"
        fi
    else
        logger -t truck-healthcheck "WARN: Pi unreachable ($FAILURES/$MAX_FAILURES)"
    fi
    exit 0
fi

# Check 2: Is the detector service running?
DETECTOR_STATUS=$(sshpass -p "raspberry" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no pi@"$PI_IP" "systemctl is-active detector" 2>/dev/null)

if [ "$DETECTOR_STATUS" != "active" ]; then
    FAILURES=$((FAILURES + 1))
    echo "$FAILURES" > "$STATE_FILE"

    if [ "$FAILURES" -eq "$MAX_FAILURES" ]; then
        curl -s "$SIGNAL_API" \
            -H "Content-Type: application/json" \
            -d "{\"jsonrpc\":\"2.0\",\"method\":\"send\",\"params\":{\"account\":\"$SIGNAL_ACCOUNT\",\"recipients\":[\"$SIGNAL_RECIPIENT\"],\"message\":\"⚠️ Truck detector Pi is reachable but detector service is not running. Status: $DETECTOR_STATUS\"},\"id\":1}" \
            > /dev/null 2>&1
        logger -t truck-healthcheck "ALERT: Detector service not active ($DETECTOR_STATUS), Signal sent"
    else
        logger -t truck-healthcheck "WARN: Detector service $DETECTOR_STATUS ($FAILURES/$MAX_FAILURES)"
    fi
    exit 0
fi

# All good — reset failure count
if [ "$FAILURES" -gt 0 ]; then
    logger -t truck-healthcheck "RECOVERED: Pi and detector OK after $FAILURES failures"
    # Send recovery message if we had alerted
    if [ "$FAILURES" -ge "$MAX_FAILURES" ]; then
        curl -s "$SIGNAL_API" \
            -H "Content-Type: application/json" \
            -d "{\"jsonrpc\":\"2.0\",\"method\":\"send\",\"params\":{\"account\":\"$SIGNAL_ACCOUNT\",\"recipients\":[\"$SIGNAL_RECIPIENT\"],\"message\":\"✅ Truck detector Pi is back online and detector service is running.\"},\"id\":1}" \
            > /dev/null 2>&1
    fi
fi
echo 0 > "$STATE_FILE"
