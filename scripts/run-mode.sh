#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRESET="${1:-}"
if [ -z "$PRESET" ]; then
  echo "Uso: run-mode.sh <modalita> [opzioni del rilevatore]" >&2
  exit 2
fi
shift

case "$PRESET" in
  ensemble-bars)
    BACKEND=ensemble
    MARKER_TYPE=downbeats
    DETECTION_MODE=grid
    ;;
  ensemble-beats)
    BACKEND=ensemble
    MARKER_TYPE=beats
    DETECTION_MODE=grid
    ;;
  ensemble-attacks|ensemble-strict)
    BACKEND=ensemble
    MARKER_TYPE=beats
    DETECTION_MODE=attacks
    ;;
  madmom)
    BACKEND=madmom
    MARKER_TYPE=downbeats
    DETECTION_MODE=grid
    ;;
  beat-this)
    BACKEND=beat-this
    MARKER_TYPE=downbeats
    DETECTION_MODE=grid
    ;;
  *)
    echo "Modalita sconosciuta: $PRESET" >&2
    exit 2
    ;;
esac

# The complete image has its own tag so rebuilding the basic image cannot remove it.
DEFAULT_IMAGE=music-bar-detector:robust
if [ "$BACKEND" = madmom ]; then
  DEFAULT_IMAGE=music-bar-detector
fi
INPUT_NAME="${MUSIC_FILE:-music.mp3}"
OUTPUT_STEM="${INPUT_NAME##*/}"
OUTPUT_STEM="${OUTPUT_STEM%.*}"
OUTPUT_STEM="${OUTPUT_STEM:-music}"
OUTPUT_DIR="/output/$PRESET"

OPTIONS=(
  --backend "$BACKEND"
  --marker-type "$MARKER_TYPE"
  --mode "$DETECTION_MODE"
  --output "$OUTPUT_DIR/$OUTPUT_STEM.json"
  --edl "$OUTPUT_DIR/$OUTPUT_STEM.edl"
  --debug "$OUTPUT_DIR/$OUTPUT_STEM-debug.png"
  --click "$OUTPUT_DIR/$OUTPUT_STEM-clicks.wav"
  --pcm "$OUTPUT_DIR/$OUTPUT_STEM-pcm.wav"
)
if [ "$PRESET" = ensemble-strict ]; then
  OPTIONS+=(--strict)
fi

printf '[INFO] Modalita: %s; output: output/%s/\n' "$PRESET" "$PRESET"
# Extra CLI options are last: FPS, tempo, trims, paths, etc. remain overridable.
MUSIC_IMAGE="${MUSIC_IMAGE:-$DEFAULT_IMAGE}" exec "$PROJECT_DIR/start.sh" "${OPTIONS[@]}" "$@"
