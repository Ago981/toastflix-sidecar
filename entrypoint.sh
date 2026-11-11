#!/bin/bash
set -e

export PYTHONPATH="${PYTHONPATH:-/app}"

ENABLE_WARP="${ENABLE_WARP:-true}"
WARP_LICENSE_KEY="${WARP_LICENSE_KEY:-}"
WARP_PROXY_HOST="127.0.0.1"
WARP_PROXY_PORT="1080"
WARP_DIR="${WG_TUNNEL_DIR:-/tmp/toastflix-warp}"
WARP_CONFIG_FILE="${WARP_CONFIG_FILE:-/app/data/warp.conf}"
WARP_GENERATOR="${WARP_GENERATOR:-/usr/local/bin/warp-register}"
WARPCTL="/app/scripts/wireproxy_ctl.sh"
export WARP_CONFIG_FILE

start_userspace_warp() {
    echo "[WARP] Starting Cloudflare WARP via saved config + wireproxy userspace SOCKS5..."

    if ! command -v "$WARP_GENERATOR" >/dev/null 2>&1 || \
       ! command -v wireproxy >/dev/null 2>&1; then
        echo "[WARP] WARP generator or wireproxy binary not found. Skipping WARP."
        return 1
    fi

    # Verify or ensure directory exists
    config_dir="$(dirname "$WARP_CONFIG_FILE")"
    if [ ! -d "$config_dir" ]; then
        mkdir -p "$config_dir" 2>/dev/null || true
    fi

    if [ ! -w "$config_dir" ] && [ ! -w "$WARP_CONFIG_FILE" ]; then
        echo "[WARP] Warning: ${config_dir} is not writable by current user ($(whoami)). Falling back to /tmp/warp.conf"
        WARP_CONFIG_FILE="/tmp/warp.conf"
        export WARP_CONFIG_FILE
    fi

    mkdir -p "$WARP_DIR" 2>/dev/null || true

    if [ ! -s "$WARP_CONFIG_FILE" ]; then
        echo "[WARP] No saved WARP config; registering once and saving to ${WARP_CONFIG_FILE}."
        temp_config="${WARP_CONFIG_FILE}.tmp.$$"
        generator_args=()
        if [ -n "$WARP_LICENSE_KEY" ]; then
            generator_args+=(--license "$WARP_LICENSE_KEY")
        fi
        if ! WARP_DNS="1.1.1.1, 1.0.0.1" \
             WARP_MTU="1280" \
             WARP_ALLOWED_IPS="0.0.0.0/0" \
             WARP_PERSISTENT_KEEPALIVE="25" \
             WARP_DEVICE_TYPE="Linux" \
             WARP_LOCALE="en_US" \
             "$WARP_GENERATOR" "${generator_args[@]}" > "$temp_config"; then
            rm -f "$temp_config"
            echo "[WARP] WARP registration failed; saved config was not changed." >&2
            return 1
        fi
        chmod 600 "$temp_config"
        mv -f "$temp_config" "$WARP_CONFIG_FILE"
        echo "[WARP] Successfully registered and saved config to ${WARP_CONFIG_FILE}."
    else
        echo "[WARP] Reusing saved WARP config from ${WARP_CONFIG_FILE}."
    fi

    "$WARPCTL" start || return 1

    echo "[WARP] Waiting for wireproxy SOCKS5 on ${WARP_PROXY_HOST}:${WARP_PROXY_PORT}..."
    for i in $(seq 1 20); do
        if ! "$WARPCTL" status >/dev/null 2>&1; then
            echo "[WARP] wireproxy exited during startup."
            return 1
        fi
        if nc -z "$WARP_PROXY_HOST" "$WARP_PROXY_PORT" && \
           "$WARPCTL" probe >/dev/null 2>&1; then
            echo "[WARP] Cloudflare WARP userspace SOCKS5 ready on ${WARP_PROXY_HOST}:${WARP_PROXY_PORT}."
            return 0
        fi
        sleep 1
    done

    echo "[WARP] wireproxy SOCKS5 port open but probe failed or timed out."
    "$WARPCTL" stop >/dev/null 2>&1 || true
    return 1
}

cleanup() {
    echo "[WARP] Stopping wireproxy..."
    "$WARPCTL" stop >/dev/null 2>&1 || true
}

forward_signal() {
    sig="$1"
    if [ -n "${APP_PID:-}" ] && kill -0 "$APP_PID" 2>/dev/null; then
        kill "-$sig" "$APP_PID" 2>/dev/null || true
    fi
    cleanup
}

trap 'forward_signal TERM; exit 0' TERM
trap 'forward_signal INT; exit 0' INT
trap cleanup EXIT

# Check if WARP is disabled or if running in an environment without UDP (like Render)
if [ -n "${RENDER:-}" ] && [ -z "${ENABLE_WARP:-}" ]; then
    echo "[WARP] Render environment detected (outbound UDP is blocked on Render). Disabling wireproxy."
    ENABLE_WARP="false"
fi

case "$(printf '%s' "${ENABLE_WARP:-true}" | tr '[:upper:]' '[:lower:]')" in
    false|0|no|off|disable|disabled)
        echo "[WARP] ENABLE_WARP is false; skipping userspace WARP wireproxy startup."
        ;;
    *)
        start_userspace_warp || echo "[WARP] WARP unavailable; toastflix-sidecar will continue in direct mode."
        ;;
esac

echo "Starting ToastFlix Sidecar..."
cd /app || exit 1

APP_PORT="${PORT:-3107}"

if [ $# -gt 0 ]; then
    "$@" &
else
    uvicorn app:app --host 0.0.0.0 --port "$APP_PORT" &
fi
APP_PID=$!

wait "$APP_PID"
EXIT_CODE=$?
exit $EXIT_CODE
