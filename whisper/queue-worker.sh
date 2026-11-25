#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/assistant-desktop-dictation"
state_root="${XDG_STATE_HOME:-$HOME/.local/state}/assistant-desktop-dictation"
queue_dir="$state_root/queue"
worker_lock="$runtime_root/worker.lock"
wakeup_fifo="$runtime_root/worker-wakeup.fifo"
initial_retry_delay_s="${ASSISTANT_QUEUE_RETRY_INITIAL_S:-1}"
retry_delay_s="$initial_retry_delay_s"
max_retry_delay_s="${ASSISTANT_QUEUE_RETRY_MAX_S:-30}"

if [[ "${1:-}" == "--background" ]]; then
	nohup "$script_dir/queue-worker.sh" </dev/null 8>&- 9>&- &
	exit 0
fi

mkdir -p "$queue_dir" "$runtime_root"
# Never retain the recorder lock, even when invoked by an older toggle process.
exec 8>&-
if [[ ! -p "$wakeup_fifo" ]]; then
	mkfifo -m 600 "$wakeup_fifo" 2>/dev/null || [[ -p "$wakeup_fifo" ]]
fi
exec 7<>"$wakeup_fifo"
exec 9>"$worker_lock"
if ! flock -n 9; then
	printf 'wake\n' >&7
	exit 0
fi

log() {
	printf '%s - %s\n' "$(date '+%Y-%m-%d %H:%M:%S.%3N')" "$*" >&2
}

log "Queue worker ready pid=$$; waiting for completed recordings or server startup"

shopt -s nullglob
while true; do
	recording=""
	recordings=("$queue_dir"/recording-*.wav)
	current_recording=""
	if [[ -f "$runtime_root/current-audio-path" ]]; then
		current_recording="$(<"$runtime_root/current-audio-path")" || true
	fi
	for candidate in "${recordings[@]}"; do
		if [[ "$candidate" != "$current_recording" ]]; then
			recording="$candidate"
			break
		fi
	done
	if [[ -z "$recording" ]]; then
		# Stay available so an enqueue cannot race an empty-queue worker exit.
		IFS= read -r -u 7 _ || exit 0
		retry_delay_s="$initial_retry_delay_s"
		continue
	fi
	metadata="${recording%.wav}.json"

	if UV_CACHE_DIR="${UV_CACHE_DIR:-/data/.cache/uv}" \
		uv run --project "$script_dir/.." python "$script_dir/transcribe-and-type.py" "$recording" 7>&-; then
		/usr/bin/rm -f -- "$recording" "$metadata"
		retry_delay_s="$initial_retry_delay_s"
	else
		log "Queue paused after failed transcription; preserved $recording; retrying in ${retry_delay_s}s or when woken"
		if IFS= read -r -t "$retry_delay_s" -u 7 _; then
			log "Queue wakeup received; retrying pending recording now"
			retry_delay_s="$initial_retry_delay_s"
		else
			retry_delay_s="$((retry_delay_s * 2))"
			if ((retry_delay_s > max_retry_delay_s)); then
				retry_delay_s="$max_retry_delay_s"
			fi
		fi
	fi
done
