#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p input output models
# Options passed to this script can override these defaults.
docker run --rm --cpus "${MUSIC_CPUS:-4}" \
  -v "$(pwd)/input:/input:ro" \
  -v "$(pwd)/output:/output" \
  -v "$(pwd)/models:/models" \
  "${MUSIC_IMAGE:-music-bar-detector}" \
  "/input/${MUSIC_FILE:-music.mp3}" \
  --backend "${BEAT_BACKEND:-madmom}" \
  --fps 25 --timeline-start 01:00:00:00 \
  --output /output/music.json \
  --edl /output/music.edl \
  --debug /output/music-debug.png \
  --click /output/music-clicks.wav \
  "$@"
