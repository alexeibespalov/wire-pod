#!/bin/bash
# Build whisper.cpp (CLI) + fetch base.en/small.en ggml models into ./wcpp, in a throwaway container.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=$(sudo -n docker inspect -f "{{.Config.Image}}" wire-pod)
sudo -n docker run --rm --network host -v "$PWD/wcpp:/out" --entrypoint bash "$IMAGE" -c "
set -euo pipefail
apt-get update -qq && apt-get install -y -qq --no-install-recommends cmake g++ make git ca-certificates curl >/dev/null
cd /tmp && git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF -DBUILD_SHARED_LIBS=OFF >/dev/null
cmake --build build -j4 --config Release --target whisper-cli whisper-server >/dev/null
cp build/bin/whisper-cli build/bin/whisper-server /out/
for m in base.en small.en; do [ -f /out/ggml-\$m.bin ] || sh ./models/download-ggml-model.sh \$m /out >/dev/null; done
ls -la /out"
