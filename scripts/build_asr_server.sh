#!/usr/bin/env bash
# Builds the local ASR engine (NVIDIA/NeMo-Speech.cpp, CPU backend, HTTP server)
# used by asr_server.py. Safe to re-run — each step skips work already done.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor/NeMo-Speech.cpp"

if [ ! -d "$VENDOR" ]; then
    git clone https://github.com/NVIDIA/NeMo-Speech.cpp "$VENDOR"
fi

cd "$VENDOR"
git submodule update --init ggml
git submodule update --init third_party/cpp-httplib

# The pinned sentencepiece commit predates a fix for GCC 13+'s stricter
# <cstdint> requirements, so the first attempt fails on a missing include.
# Patch the header and retry once if that specific failure shows up.
export CMAKE_POLICY_VERSION_MINIMUM=3.5
if [ ! -f .deps/sentencepiece/lib/libsentencepiece.a ]; then
    if ! JOBS="$(nproc)" bash scripts/build_sentencepiece_static.sh; then
        HEADER=".deps/sentencepiece-build/source/src/sentencepiece_processor.h"
        if [ -f "$HEADER" ] && ! grep -q "#include <cstdint>" "$HEADER"; then
            echo "Patching sentencepiece for GCC 13+ (missing <cstdint>) and retrying..."
            sed -i '0,/#include <cstring>/s//#include <cstdint>\n#include <cstring>/' "$HEADER"
            JOBS="$(nproc)" bash scripts/build_sentencepiece_static.sh
        else
            exit 1
        fi
    fi
fi

scripts/configure.sh cpu-server
cmake --build --preset cpu-server -j"$(nproc)"

echo
echo "Built: $VENDOR/build/cpu-server/bin/nemo-speech"
echo "Run the server with: uv run asr_server.py"
