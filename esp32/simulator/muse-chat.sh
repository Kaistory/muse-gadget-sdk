#!/usr/bin/env bash
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Starts the simulator's conversation demo, building the simulator first if
# needed. Replies come from the OpenAI-compatible endpoint at MUSE_DEMO_API_URL
# (default http://127.0.0.1:8045/v1) when it answers, or else the built-in rules.
#
#   esp32/simulator/muse-chat.sh                     # default model
#   esp32/simulator/muse-chat.sh gemini-3.5-flash    # another model
#   esp32/simulator/muse-chat.sh --offline           # built-in rules only

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM="$HERE/build/muse_simulator"
API_URL="${MUSE_DEMO_API_URL:-http://127.0.0.1:8045/v1}"
MODEL="${MUSE_DEMO_MODEL:-claude-sonnet-4-6}"
OFFLINE=0

for arg in "$@"; do
    case "$arg" in
        --offline) OFFLINE=1 ;;
        -h|--help) sed -n '16,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) MODEL="$arg" ;;
    esac
done

if [ ! -x "$SIM" ]; then
    echo "Building the simulator (first run only, a few minutes)..."
    if ! command -v cmake >/dev/null || ! command -v ninja >/dev/null; then
        echo "cmake and ninja are needed: sudo apt-get install -y build-essential cmake ninja-build" >&2
        exit 1
    fi
    cmake -S "$HERE" -B "$HERE/build" -G Ninja -DCMAKE_BUILD_TYPE=Debug >/dev/null
    cmake --build "$HERE/build" --parallel >/dev/null
fi

if [ "$OFFLINE" = 0 ] && curl -fsS -m 3 "$API_URL/models" >/dev/null 2>&1; then
    exec python3 "$HERE/chat_demo.py" --api-url "$API_URL" --model "$MODEL"
fi
if [ "$OFFLINE" = 0 ]; then
    echo "No model at $API_URL (is antigravity-tools running?); using the built-in replies."
fi
exec env -u MUSE_DEMO_API_URL python3 "$HERE/chat_demo.py"
