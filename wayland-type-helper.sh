#!/usr/bin/env bash
set -euo pipefail

runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-$runtime/.ydotool_socket}"
modifier_state_file="${WAYLAND_MODIFIER_STATE_FILE:-$runtime/wayland_modifier_state.env}"
lock="$runtime/assistant-ydotool-type.lock"

exec 9>"$lock"
flock -x 9

text=""
IFS= read -r -d '' text || true
[[ -n "$text" ]] || exit 0

ydotool_ready() {
    ydotool key --key-delay 0 29:0 >/dev/null 2>&1
}

ensure_ydotoold() {
    ydotool_ready && return 0
    systemctl --user start ydotool.service >/dev/null 2>&1 ||
        systemctl --user start ydotoold.service >/dev/null 2>&1 ||
        true
    for _ in {1..5}; do
        ydotool_ready && return 0
        sleep 0.05
    done
    printf 'wayland-type-helper: ydotoold is not reachable at %s\n' "$YDOTOOL_SOCKET" >&2
    return 4
}

modifier_held() {
    [[ -f "$modifier_state_file" ]] || return 1
    # shellcheck disable=SC1090
    source "$modifier_state_file"
    [[ "${META:-0}" == 1 || "${SHIFT:-0}" == 1 || "${CTRL:-0}" == 1 || "${ALT:-0}" == 1 ]]
}

ensure_ydotoold
if modifier_held; then
    printf 'wayland-type-helper: refusing to type because a modifier key is currently held\n' >&2
    exit 5
fi

# Release stale key state only after proving the user is not holding a modifier.
ydotool key --key-delay 0 110:0 42:0 54:0 29:0 97:0 56:0 100:0 125:0 126:0 47:0 \
    >/dev/null 2>&1 || true
sleep 0.05

type_pid=""
watch_pid=""
typing_started=0

cleanup_typing() {
    if [[ -n "$type_pid" ]]; then
        kill -TERM "$type_pid" 2>/dev/null || true
        wait "$type_pid" 2>/dev/null || true
    fi
    if [[ -n "$watch_pid" ]]; then
        kill -TERM "$watch_pid" 2>/dev/null || true
        wait "$watch_pid" 2>/dev/null || true
    fi
}

trap cleanup_typing EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

watch_for_modifier() {
    while kill -0 "$type_pid" 2>/dev/null; do
        if modifier_held; then
            printf 'wayland-type-helper: refusing to continue typing because a modifier key was pressed during typing\n' >&2
            kill -TERM "$type_pid" 2>/dev/null || true
            return 5
        fi
        sleep 0.01
    done
    return 0
}

type_chunk() {
    local chunk="$1" delay="$2" hold="$3"
    if modifier_held; then
        if ((typing_started)); then
            printf 'wayland-type-helper: refusing to continue typing because a modifier key was pressed during typing\n' >&2
        else
            printf 'wayland-type-helper: refusing to type because a modifier key is currently held\n' >&2
        fi
        return 5
    fi

    ydotool type --key-delay "$delay" --key-hold "$hold" --file=- < <(printf '%s' "$chunk") &
    type_pid=$!
    typing_started=1
    watch_for_modifier &
    watch_pid=$!

    local type_status=0 watch_status=0
    wait "$type_pid" || type_status=$?
    type_pid=""
    if kill -0 "$watch_pid" 2>/dev/null; then
        kill -TERM "$watch_pid" 2>/dev/null || true
    fi
    wait "$watch_pid" || watch_status=$?
    watch_pid=""

    if ((watch_status == 5)); then
        return 5
    fi
    return "$type_status"
}

# Pace only the opening characters so web editors can process the first input
# before receiving the rest at full speed. Preserve stdin exactly, without a
# dummy key, clipboard operation, or extra newline.
type_chunk "${text:0:3}" 20 10
if ((${#text} > 3)); then
    sleep 0.03
    type_chunk "${text:3}" 0 0
fi
