#!/bin/bash
# Build wire-pod's `chipper` binary from the upstream image we run, plus our patches, into
# ./chipper. compose.yaml bind-mounts it over the image's binary.
#
# Why: upstream opens a gRPC stream to Vector on every reply (behaviour control, and a
# robot_state event stream for interrupts) with context.Background() and never closes it.
# The streams pile up until the wire-pod<->robot connection idles out; with kids chatting
# nonstop it never does, Vector's gateway buffers fill (~37 streams) and his gRPC stops
# answering: wire-pod hangs on BatteryState, cloud-with-X face, no replies.
#
# Run on the Pi after the upstream image changes:  ~/wire-pod/chipper-patch/build.sh
# Revert: remove the chipper bind-mount line from compose.yaml and `docker compose up -d`.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=$(sudo -n docker inspect -f '{{.Config.Image}}' wire-pod)
sudo -n docker run --rm --network host --entrypoint bash \
  -v "$PWD:/patch" -v wirepod-build-go:/opt/wire-pod/go -v wirepod-build-mod:/root/go -v wirepod-build-apt:/var/cache/apt \
  "$IMAGE" -c '
set -euo pipefail
command -v gcc >/dev/null || { apt-get update -qq && apt-get install -y -qq --no-install-recommends gcc libc6-dev pkg-config libopus-dev libogg-dev libopusfile-dev >/dev/null; }
cd /opt/wire-pod
for p in /patch/*.patch; do echo "applying $p"; git apply -p1 "$p"; done
source ./goenv.sh
getGo
[ -f "${VOSK_DIR}/vosk_api.h" ] || VOSK_DIR=/opt/vosk/libvosk
cd chipper
export CGO_ENABLED=1 CGO_CFLAGS="-I${VOSK_DIR}" CGO_LDFLAGS="-L ${VOSK_DIR} -lvosk -ldl -lpthread"
"$GO" build -tags nolibopusfile \
  -ldflags "-X github.com/kercre123/wire-pod/chipper/pkg/vars.CommitSHA=$(git rev-parse --short HEAD)-streamfix" \
  -o /patch/chipper.new cmd/vosk/main.go
mv /patch/chipper.new /patch/chipper
echo "built $(ls -la /patch/chipper)"'
