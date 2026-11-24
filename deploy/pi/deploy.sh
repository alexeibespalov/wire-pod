#!/usr/bin/env bash
# Sync deploy/pi/ to the Pi and (re)start the stack.
#
#   ./deploy/pi/deploy.sh            # sync + `docker compose up -d` (recreates only changed services)
#   ./deploy/pi/deploy.sh --dry-run  # show what would change, touch nothing
#
# Never touches the Pi's .env / cortana.env (secrets) or the data dirs (wire-pod state,
# Vector's registration, agent-data, cortana-data).
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

# deploy/pi/*  ->  ~/wire-pod/   (--checksum: only real content changes are listed)
SYNC_OUT="$("${RSYNC[@]}" --checksum \
    --exclude '.env' --exclude '.env.example' --exclude '.gitignore' \
    --exclude 'cortana.env' --exclude 'cortana.env.example' \
    --exclude 'data/' --exclude 'agent-data/' --exclude 'vector-agent/' --exclude 'agent-core/' \
    --exclude 'vector.common.env' \
    --exclude 'cortana-data/' --exclude 'cortana-agent/' \
    --exclude 'deploy.sh' --exclude 'README.md' \
    --exclude 'intent-data/en-US.original.json' \
    "$HERE/" "$PI_HOST:$PI_DIR/")"
echo "$SYNC_OUT"

# wire-pod reads bind-mounted overlays only at startup, and compose doesn't notice
# content changes in bind mounts -> restart it ourselves when one changed.
RESTART_WIREPOD=0
if grep -qE '^<f[^ ]* (intent-data/en-US\.json|persist-sync\.sh)$' <<<"$SYNC_OUT"; then
    RESTART_WIREPOD=1
fi

# <repo>/vector-agent/  ->  ~/wire-pod/vector-agent/  (exact mirror); same for cortana-agent
# and for agent-core, the brain both of them are built on
"${RSYNC[@]}" --delete --exclude '*.egg-info/' --exclude '.pytest_cache/' "$HERE/../../agent-core/" "$PI_HOST:$PI_DIR/agent-core/"
"${RSYNC[@]}" --delete "$HERE/../../vector-agent/" "$PI_HOST:$PI_DIR/vector-agent/"
"${RSYNC[@]}" --delete --exclude '.pytest_cache/' "$HERE/../../cortana-agent/" "$PI_HOST:$PI_DIR/cortana-agent/"

if [[ ${#DRY[@]} -gt 0 ]]; then
    echo "(dry run: not restarting anything)"
    exit 0
fi

"${SSH[@]}" "cd $PI_DIR && test -f .env || { echo 'ERROR: ~/$PI_DIR/.env missing (see .env.example)'; exit 1; }
  test -f cortana.env || { echo 'ERROR: ~/$PI_DIR/cortana.env missing (see cortana.env.example)'; exit 1; }
  touch vector.common.env                                    # profile-independent settings (may be empty)
  chmod 600 .env cortana.env vector.common.env
  mkdir -p agent-data/face && sudo -n chown -R 10001 agent-data   # vector-agent runs as uid 10001
  mkdir -p cortana-data && sudo -n chown -R 10002 cortana-data    # cortana-agent runs as uid 10002
  sudo -n docker compose up -d --build --remove-orphans
  if [ $RESTART_WIREPOD = 1 ]; then echo 'overlay changed: restarting wire-pod'; sudo -n docker compose restart wire-pod; fi
  sudo -n docker compose ps --format '{{.Name}}\t{{.Status}}'"
