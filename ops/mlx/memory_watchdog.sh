#!/bin/bash
# memory_watchdog.sh — SIGTERM rapid-mlx when its RSS crosses a watermark.
#
# The observed failure pattern is progressive slowdown over multi-hour runs
# ending in an OOM crash mid-generation. This script polls the process RSS
# and sends a clean SIGTERM at a configurable threshold so launchd restarts
# it gracefully rather than letting the kernel OOM-kill it mid-stream. The
# in-flight generation returns a clean error that the tier router can fall
# through on, preserving the eval run or agent session.
#
# Configurable via env (with defaults):
#   MLX_RSS_LIMIT_MB   RSS watermark in MB            (default 110000 = ~110 GB)
#   MLX_POLL_INTERVAL  seconds between polls          (default 30)
#   MLX_PROC_PATTERN   pgrep -f pattern for the proc  (default: venv binary path)
set -u

RSS_LIMIT_MB="${MLX_RSS_LIMIT_MB:-110000}"
POLL_INTERVAL="${MLX_POLL_INTERVAL:-30}"
# Anchor on the binary path — avoids matching unrelated processes (grep/tail)
# whose args happen to contain the server's name.
PROC_PATTERN="${MLX_PROC_PATTERN:-/Users/andrew/.venvs/rapid-mlx/bin/rapid-mlx}"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') memory_watchdog: $*"; }

log "starting; rss_limit=${RSS_LIMIT_MB}MB interval=${POLL_INTERVAL}s proc_pattern='${PROC_PATTERN}'"

while true; do
    pid="$(pgrep -f "${PROC_PATTERN}" | head -n1)"
    if [ -z "${pid}" ]; then
        log "no process matching '${PROC_PATTERN}'; waiting"
        sleep "${POLL_INTERVAL}"
        continue
    fi

    # ps reports RSS in KB on macOS.
    rss_kb="$(ps -o rss= -p "${pid}" | tr -d ' ')"
    if [ -z "${rss_kb}" ]; then
        sleep "${POLL_INTERVAL}"
        continue
    fi
    rss_mb=$(( rss_kb / 1024 ))

    if [ "${rss_mb}" -ge "${RSS_LIMIT_MB}" ]; then
        log "pid ${pid} RSS ${rss_mb}MB >= limit ${RSS_LIMIT_MB}MB; sending SIGTERM for clean restart"
        kill -TERM "${pid}"
        # Give the server time to drain the in-flight generation and exit
        # before launchd's KeepAlive relaunches it; avoid double-killing.
        sleep 60
    fi

    sleep "${POLL_INTERVAL}"
done
