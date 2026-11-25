#!/usr/bin/env bash
set -euo pipefail

runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-$runtime/.ydotool_socket}"
modifier_state_file="${WAYLAND_MODIFIER_STATE_FILE:-$runtime/wayland_modifier_state.env}"
lock="$runtime/assistant-ydotool-type.lock"
insert_mode="${ASSISTANT_INSERT_MODE:-paste}"
paste_settle_s="${ASSISTANT_PASTE_SETTLE_S:-0.02}"
paste_restore_delay_s="${ASSISTANT_PASTE_RESTORE_DELAY_S:-0.15}"
transient_mime="application/x-assistant-dictation-transient"

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
clipboard_backend=""
clipboard_swapped=0
wl_backup_file=""
wl_had_clipboard=0

find_kdotool() {
	local tool
	tool="$(command -v kdotool || true)"
	if [[ -z "$tool" && -x "$HOME/.local/bin/kdotool" ]]; then
		tool="$HOME/.local/bin/kdotool"
	elif [[ -z "$tool" && -x "$HOME/.cargo/bin/kdotool" ]]; then
		tool="$HOME/.cargo/bin/kdotool"
	fi
	printf '%s' "$tool"
}

paste_shortcut_for_active_window() {
	local focus_tool active_class=""
	focus_tool="$(find_kdotool)"
	if [[ -n "$focus_tool" ]]; then
		active_class="$(timeout 0.2s "$focus_tool" getactivewindow getwindowclassname 2>/dev/null || true)"
	fi
	case "${active_class,,}" in
	konsole | org.kde.konsole | kitty | alacritty | wezterm | org.wezfurlong.wezterm | foot | gnome-terminal | gnome-terminal-server | ghostty | com.mitchellh.ghostty)
		printf 'ctrl_shift_v'
		;;
	*)
		printf 'ctrl_v'
		;;
	esac
}

restore_clipboard() {
	((clipboard_swapped)) || return 0
	clipboard_swapped=0
	case "$clipboard_backend" in
	copyq)
		timeout 1s copyq eval "if (size() > 0) { var item = getItem(0); var payload = {}; for (var f in item) { if (!f.startsWith('application/x-copyq-') && item[f].length > 0) payload[f] = item[f]; } copy(payload); } else { copy(''); }" >/dev/null 2>&1 || true
		;;
	wl)
		if ((wl_had_clipboard)) && [[ -n "$wl_backup_file" && -f "$wl_backup_file" ]]; then
			wl-copy --type text/plain <"$wl_backup_file" >/dev/null 2>&1 || true
		else
			wl-copy --clear >/dev/null 2>&1 || true
		fi
		;;
	esac
}

cleanup() {
	if [[ -n "$type_pid" ]]; then
		kill -TERM "$type_pid" 2>/dev/null || true
		wait "$type_pid" 2>/dev/null || true
	fi
	if [[ -n "$watch_pid" ]]; then
		kill -TERM "$watch_pid" 2>/dev/null || true
		wait "$watch_pid" 2>/dev/null || true
	fi
	restore_clipboard
	if [[ -n "$wl_backup_file" ]]; then
		rm -f -- "$wl_backup_file"
		wl_backup_file=""
	fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

paste_via_clipboard() {
	if modifier_held; then
		printf 'wayland-type-helper: refusing to type because a modifier key is currently held\n' >&2
		return 5
	fi

	if command -v copyq >/dev/null 2>&1; then
		if timeout 1s copyq eval "var t = input(); copy({'text/plain;charset=utf-8': t, 'text/plain': t, 'UTF8_STRING': t, '${transient_mime}': '1'});" < <(printf '%s' "$text") >/dev/null 2>&1; then
			clipboard_backend="copyq"
			clipboard_swapped=1
		fi
	fi

	if ((clipboard_swapped == 0)) && command -v wl-copy >/dev/null 2>&1; then
		wl_backup_file="$(mktemp "$runtime/assistant-clip-backup.XXXXXX")"
		if command -v wl-paste >/dev/null 2>&1 && timeout 0.5s wl-paste --no-newline --type text/plain >"$wl_backup_file" 2>/dev/null; then
			wl_had_clipboard=1
		else
			wl_had_clipboard=0
		fi
		if wl-copy --type text/plain < <(printf '%s' "$text") >/dev/null 2>&1; then
			clipboard_backend="wl"
			clipboard_swapped=1
		else
			return 1
		fi
	fi

	((clipboard_swapped)) || return 1

	if [[ "$paste_settle_s" != "0" ]]; then
		sleep "$paste_settle_s"
	fi

	if modifier_held; then
		printf 'wayland-type-helper: refusing to type because a modifier key is currently held\n' >&2
		return 5
	fi

	if [[ "$(paste_shortcut_for_active_window)" == "ctrl_shift_v" ]]; then
		ydotool key --key-delay 0 29:1 42:1 47:1 47:0 42:0 29:0
	else
		ydotool key --key-delay 0 29:1 47:1 47:0 29:0
	fi

	if [[ "$paste_restore_delay_s" != "0" ]]; then
		sleep "$paste_restore_delay_s"
	fi
	restore_clipboard
	return 0
}

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

if [[ "$insert_mode" == "paste" ]]; then
	paste_status=0
	paste_via_clipboard || paste_status=$?
	if ((paste_status == 0)); then
		exit 0
	elif ((paste_status == 5)); then
		exit 5
	fi
	# Fall through to direct ydotool typing when no clipboard provider is available.
fi

# Pace only the opening characters so web editors can process the first input
# before receiving the rest at full speed. Preserve stdin exactly, without a
# dummy key, clipboard operation, or extra newline.
type_chunk "${text:0:3}" 20 10
if ((${#text} > 3)); then
	sleep 0.03
	type_chunk "${text:3}" 0 0
fi
