#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
docker build --platform "${DOCKER_PLATFORM:-linux/arm64}" \
  --build-arg "WITH_BEAT_THIS=${WITH_BEAT_THIS:-0}" \
  --no-cache -t "${MUSIC_IMAGE:-music-bar-detector}" .
