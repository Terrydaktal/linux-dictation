#!/usr/bin/env bash
set -euo pipefail

script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
script_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)"
desktop_dir="$(cd -- "$script_dir/.." && pwd)"
runtime_root="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/rambler-desktop-dictation"
state_root="${XDG_STATE_HOME:-$HOME/.local/state}/rambler-desktop-dictation"
sock_path="$runtime_root/control.sock"
toggle_lock="$runtime_root/toggle.lock"
log_file="$state_root/dictation.log"

mkdir -p "$runtime_root" "$state_root"
exec 9>"$toggle_lock"
flock -x 9

send_cmd() {
	local cmd="$1"
	if [[ ! -S "$sock_path" ]]; then
		return 1
	fi
	if command -v socat >/dev/null 2>&1; then
		printf '%s\n' "$cmd" | socat -t 2 -T 5 - "UNIX-CONNECT:$sock_path" 2>/dev/null
		return $?
	fi
	python3 -c '
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5.0)
s.connect(sys.argv[1])
s.sendall((sys.argv[2] + "\n").encode())
sys.stdout.write(s.recv(256).decode("utf-8", errors="replace"))
' "$sock_path" "$cmd" 2>/dev/null
}

start_daemon() {
	local venv_python="$desktop_dir/.venv/bin/python"
	if [[ -x "$venv_python" ]]; then
		nohup env PYTHONPATH="$desktop_dir${PYTHONPATH:+:$PYTHONPATH}" \
			"$venv_python" "$script_dir/stream-daemon.py" \
			</dev/null >>"$log_file" 2>&1 9>&- &
	else
		nohup env PYTHONPATH="$desktop_dir${PYTHONPATH:+:$PYTHONPATH}" \
			UV_CACHE_DIR="${UV_CACHE_DIR:-/data/.cache/uv}" \
			uv run --no-sync --project "$desktop_dir" \
			python "$script_dir/stream-daemon.py" \
			</dev/null >>"$log_file" 2>&1 9>&- &
	fi

	for _ in {1..100}; do
		if [[ -S "$sock_path" ]] && send_cmd status >/dev/null 2>&1; then
			return 0
		fi
		sleep 0.02
	done
	printf '%s - ERROR: Rambler live-streaming daemon did not become ready\n' \
		"$(date '+%Y-%m-%d %H:%M:%S.%3N')" >>"$log_file"
	return 1
}

if ! send_cmd toggle >/dev/null; then
	start_daemon
	send_cmd toggle >/dev/null
fi
