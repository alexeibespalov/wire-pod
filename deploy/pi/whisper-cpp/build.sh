#!/bin/bash
# Build whisper.cpp's whisper-server and fetch the base.en model into ./ (Vector's own
# speech-to-text; Home Assistant's Whisper container is separate and untouched).
# Built inside the wire-pod image so the binary matches the runtime it's started in.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=$(sudo -n docker inspect -f "{{.Config.Image}}" wire-pod)
sudo -n docker run --rm --network host -v "$PWD:/out" --entrypoint bash "$IMAGE" -c "
set -euo pipefail
apt-get update -qq && apt-get install -y -qq --no-install-recommends cmake g++ make git ca-certificates curl >/dev/null
cd /tmp && git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF -DBUILD_SHARED_LIBS=OFF >/dev/null
cmake --build build -j4 --config Release --target whisper-server >/dev/null
cp build/bin/whisper-server /out/
[ -f /out/ggml-base.en.bin ] || sh ./models/download-ggml-model.sh base.en /out >/dev/null
ls -la /out"
