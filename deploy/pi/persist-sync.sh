#!/usr/bin/env bash
# Workaround for an upstream wire-pod Docker bug:
# the image symlinks chipper/apiConfig.json (etc.) into /data, but wire-pod saves
# settings with WriteFileAtomic (write temp + rename), which REPLACES the symlink
# with a regular file inside the container. Settings then vanish on container recreate.
#
# This wrapper runs wire-pod and copies any such regular file back to /data
# every few seconds and on shutdown.
APP=/opt/wire-pod
DATA="${WIREPOD_DATA_DIR:-/data}"
FILES="chipper/apiConfig.json chipper/botConfig.json chipper/customIntents.json chipper/pico.key chipper/useepod chipper/source.sh"

sync_back() {
    for f in $FILES; do
        src="$APP/$f"; dst="$DATA/$f"
        if [ -f "$src" ] && [ ! -L "$src" ] && ! cmp -s "$src" "$dst"; then
            cp -p "$src" "$dst.tmp.$$" && mv -f "$dst.tmp.$$" "$dst" \
                && echo "[persist-sync] saved $f to $DATA"
        fi
    done
}

"$APP/chipper/start.sh" &
PID=$!
trap 'sync_back; kill -TERM "$PID" 2>/dev/null; wait "$PID"; sync_back; exit 0' TERM INT

while kill -0 "$PID" 2>/dev/null; do
    sleep 3
    sync_back
done
sync_back
wait "$PID"
