#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_BEAT_THIS=1 MUSIC_IMAGE="${MUSIC_IMAGE:-music-bar-detector:robust}" exec "$SCRIPT_DIR/build.sh" "$@"
