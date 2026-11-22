#!/bin/zsh
# Double-click in Finder (opens in Terminal) to restart the Shorts loop with the latest code and config.
# Stops a running loop gracefully (it finishes its current video or clip), then starts a new one here.
# Extra arguments are passed to the loop, e.g.  ./restart_loop.command --profile cs2
cd "$(dirname "$0")" || exit 1

if pgrep -f "[Pp]ython.* main.py loop" >/dev/null; then
  echo "Asking the running loop to stop (it finishes its current video or clip first)..."
  .venv/bin/python main.py stop
  waited=0
  while pgrep -f "[Pp]ython.* main.py loop" >/dev/null; do
    sleep 5
    waited=$((waited + 5))
    (( waited % 60 == 0 )) && echo "  still finishing its current video... ($((waited / 60)) min)"
  done
  echo "Old loop stopped."
fi

echo "Starting the loop: GTA 6, YouTube + Twitch every 90 min, review only. Ctrl+C to stop."
exec caffeinate -is .venv/bin/python -u main.py loop --top 3 --every 90 "$@"
