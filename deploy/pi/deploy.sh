#!/usr/bin/env bash
# Sync deploy/pi/ to the Pi and (re)start the stack.
#
#   ./deploy/pi/deploy.sh            # sync + `docker compose up -d` (recreates only changed services)
#   ./deploy/pi/deploy.sh --dry-run  # show what would change, touch nothing
#
# Never touches the Pi's .env (secrets) or data/ (wire-pod state, Vector's registration).
set -euo pipefail

PI_HOST="${PI_HOST:-john@192.168.1.39}"
PI_KEY="${PI_KEY:-$HOME/.ssh/rpi_key}"
PI_DIR="${PI_DIR:-wire-pod}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH=(ssh -i "$PI_KEY" -o IdentitiesOnly=yes "$PI_HOST")

DRY=()
[[ "${1:-}" == "--dry-run" ]] && DRY=(--dry-run)

# Regenerate the pruned intent list from the stock file in this fork.
python3 -I "$HERE/intent-data/build_intents.py" \
    "$HERE/../../chipper/intent-data/en-US.json" "$HERE/intent-data/en-US.json"

RSYNC=(rsync -av --itemize-changes "${DRY[@]}" -e "ssh -i $PI_KEY -o IdentitiesOnly=yes"
       --exclude '__pycache__/' --exclude '.venv/')

# deploy/pi/*  ->  ~/wire-pod/
"${RSYNC[@]}" \
    --exclude '.env' --exclude '.env.example' --exclude '.gitignore' \
    --exclude 'data/' --exclude 'agent-data/' --exclude 'vector-agent/' \
    --exclude 'deploy.sh' --exclude 'README.md' \
    --exclude 'intent-data/en-US.original.json' \
    "$HERE/" "$PI_HOST:$PI_DIR/"

# <repo>/vector-agent/  ->  ~/wire-pod/vector-agent/  (exact mirror)
"${RSYNC[@]}" --delete "$HERE/../../vector-agent/" "$PI_HOST:$PI_DIR/vector-agent/"

if [[ ${#DRY[@]} -gt 0 ]]; then
    echo "(dry run: not restarting anything)"
    exit 0
fi

"${SSH[@]}" "cd $PI_DIR && test -f .env || { echo 'ERROR: ~/$PI_DIR/.env missing (see .env.example)'; exit 1; }
  chmod 600 .env
  mkdir -p agent-data && sudo -n chown -R 10001 agent-data   # vector-agent runs as uid 10001
  sudo -n docker compose up -d --build --remove-orphans
  sudo -n docker compose ps --format '{{.Name}}\t{{.Status}}'"
