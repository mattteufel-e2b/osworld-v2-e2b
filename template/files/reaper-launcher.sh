#!/usr/bin/env bash
set -u

# REAPER's evaluation-dialog acknowledgement is tied to host identity and is
# invalidated when E2B restores a template into a new sandbox. Reproduce the
# already-dismissed VM snapshot state by closing the startup About window once.
/opt/REAPER/reaper "$@" &
reaper_pid=$!

(
  about_seen=0
  for _ in $(seq 1 80); do
    if ! kill -0 "$reaper_pid" 2>/dev/null; then
      exit 0
    fi

    about_window_id=$(
      DISPLAY="${DISPLAY:-:0}" wmctrl -l 2>/dev/null \
        | awk 'index($0, "About REAPER") { print $1; exit }'
    )
    if [ -n "$about_window_id" ]; then
      # The SWELL window is listed just before it is ready to process
      # WM_DELETE_WINDOW. Give the first sighting a short settle period, then
      # retry by exact window id until wmctrl confirms it has disappeared.
      if [ "$about_seen" -eq 0 ]; then
        about_seen=1
        sleep 1
        continue
      fi
      DISPLAY="${DISPLAY:-:0}" wmctrl -i -c "$about_window_id" 2>/dev/null || true
    elif [ "$about_seen" -eq 1 ]; then
      exit 0
    fi
    sleep 0.25
  done
) &

wait "$reaper_pid"
