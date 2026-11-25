# Desktop microphone startup audit

Measured on 2026-09-16 using the local Yeti Stereo Microphone and PipeWire 1.6.8.

## Result

`desktop_dictation/toggle.sh` now requests `pw-record --latency 10ms` instead
of leaving stream buffering automatic. This is a latency request, not a promise
that recording starts exactly 10 ms after pressing the key.

| Measurement | Trials per setting | Automatic median (range) | 10 ms request median (range) |
| --- | ---: | ---: | ---: |
| Full toggle, repeated starts | 18 | 30.74 ms (17.55-51.49) | 21.72 ms (19.47-33.29) |
| Full toggle, 6 seconds idle before each start | 3 | 51.45 ms (50.02-52.41) | 34.36 ms (33.21-36.51) |
| Recorder alone, repeated starts | 4 | 25.74 ms (23.30-49.06) | 13.78 ms (12.80-15.57) |
| Recorder alone, 6 seconds idle | 3 | 46.44 ms (15.50-48.74) | 30.81 ms (30.50-32.21) |

The full-toggle median improvement was approximately 9 ms (29%) for repeated
starts and 17 ms (33%) after idle. The ranges overlap: an individual start can
still be faster with automatic buffering. These are small local samples, not
latency guarantees under all loads.

The earlier approximately 900 ms first-sample delay was **not reproduced with
the unchanged recorder** during this audit. Do not attribute its disappearance
to the new latency setting. The existing startup grace period and ALSA recovery
remain in place for slow or failed starts.

## What was timed

- Start: monotonic clock immediately before spawning `toggle.sh` (or `pw-record`
  for the recorder-only probes).
- End: first observed WAV file growth beyond its 44-byte header, polled every
  approximately 1 ms. This includes buffering before the first write, not just
  when the microphone hardware or PipeWire stream begins running.
- The full toggle published its recording path after approximately 6-9 ms.
  Recorder-only verbose logs showed stream setup taking approximately 10-16 ms
  after idle; samples became visible later.
- Physical key actuation, KDE shortcut dispatch, acoustic onset, disk flush,
  upload, transcription, and typing were not timed. The installed KDE launcher
  was checked and points directly to this project's `toggle.sh`.
- Six seconds idle means no test recorder was open during that interval; it
  does not prove that every device or other PipeWire client was suspended.

Full-toggle probes used the existing queue test harness with the real
`pw-record`, isolated runtime/state directories in `/dev/shm`, and a dummy
HTTP/typing client. All microphone audio was discarded from RAM after each
probe. No recording was uploaded, transcribed, or typed. Neither the live
Whisper server nor PipeWire was restarted. Variant order was reversed on
alternating trials to reduce order bias.

The recorder-only probes requested exactly 16,000 frames and all produced that
count at mono 16 kHz PCM16 with no timeout. This installed `pw-record` returned
status 1 on sample-count completion for every setting, including the baseline;
the verification therefore also exercised normal SIGINT Stop through the real
toggle, finalized WAV headers, and queue metadata rather than treating that
exit status as a successful end-to-end test.

## Alternatives checked

| Recorder-only setting | Repeated-start median, four probes |
| --- | ---: |
| Automatic | 25.74 ms |
| Explicit 20 ms latency | 23.28 ms |
| Explicit 10 ms latency | 13.78 ms |
| Explicit 5 ms latency | 19.99 ms |
| Explicit Yeti target, automatic latency | 25.32 ms |

Five milliseconds did not improve startup and increased write frequency.
Hard-coding the device did not help, so default-device selection and hotplug
behavior remain unchanged. The CLI advertises a 100 ms latency default, but
verbose output in the no-flag run reported `latency: 0`; the baseline above
means the actual old command, not an explicitly forced 100 ms configuration.

## Operation and trade-offs

The requested latency is logged in `dictation.log`. It is controlled by
`ASSISTANT_PIPEWIRE_LATENCY_MS`: the default is `10`, `20` requests larger
buffers, and `0` omits the latency argument to restore the old behavior. Values
must be canonical whole numbers from 0 to 1000; malformed values warn and use
10 ms without preventing recording or Stop.

There is no new persistent recorder, model load, device enumeration, or blocking
wait on the normal start path. The existing 750 ms background health check is
not the capture-start timer, and its `startup_check_ms` log is not a measurement
of first-sample latency. The 3-second failure grace and immediate Stop behavior
are unchanged.

Sample rate, channels, gain, resampling quality, filters, and lossless WAV
encoding are unchanged. WER was not measured by this startup test. Smaller
buffers require more frequent processing, so timing and underrun behavior
should be reassessed under heavy system load. The latency request can influence
the running PipeWire graph, but does not set `node.force-quantum` or change global
configuration. See the [official pw-cat manual](https://docs.pipewire.org/page_man_pw-cat_1.html)
for the buffer/CPU trade-off.

## Evidence and checks

One-off probe code and timing/event JSON remain locally under
`.tmp/recording-startup-20260916/` (ignored by Git); they contain no retained
audio. Key files are `warm.json`, `idle.json`, `toggle-before.json`,
`toggle-ab-extended.json`, and `toggle-idle-ab.json`.

The new automated cases verify the default recorder arguments, unchanged audio
format, explicit/automatic overrides, and safe handling of invalid settings.
The existing cases cover quick Stop, delayed first samples, fallback races,
preserved audio, and offline queues. The complete Python suite passed all 110
tests; `shellcheck` and `shfmt` passed for the updated toggle script.

A 20-second capture through the default toggle produced 320,058 frames
(20.004 seconds) at mono 16 kHz PCM16, finalized queue metadata, and stopped in
64 ms. Polling every 5 ms observed a maximum write-growth gap of 10.15 ms. A
`pw-top` snapshot during capture reported zero errors for the microphone and
recorder, with a 256-frame/48 kHz hardware graph quantum. These are bounded
checks, not a guarantee against dropouts under every future load. Results are
in `toggle-long.json`; its `latency_ms: null` means no environment override,
so the new 10 ms default was used.
