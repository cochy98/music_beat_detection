#!/bin/bash

set -x 

# docker run --rm \
#   -v "$(pwd)/input:/input:ro" \
#   -v "$(pwd)/output:/output" \
#   music-bar-detector \
#   /input/music.mp3 \
#   --output /output/music.json

# Con json + debug
docker run --rm \
  -v "$(pwd)/input:/input:ro" \
  -v "$(pwd)/output:/output" \
  music-bar-detector \
  /input/music.mp3 \
  --fps 25 \
  --output /output/music.json \
  --timeline-start 01:00:00:00 \
  --edl /output/music.edl \
  --debug /output/music-debug.png

# con tempo di inizio timeline default 01:00:00:00 (formato HH:MM:SS:FF)
# docker run --rm \
#   -v "$(pwd)/input:/input:ro" \
#   -v "$(pwd)/output:/output" \
#   music-bar-detector \
#   /input/music.mp3 \
#   --edl /output/music.edl

# Esempio con timeline start differente da 01:00:00:00 (formato HH:MM:SS:FF)
# docker run --rm \
#   -v "$(pwd)/input:/input:ro" \
#   -v "$(pwd)/output:/output" \
#   music-bar-detector \
#   /input/music.mp3 \
#   --timeline-start 01:00:07:28 \
#   --edl /output/music.edl


# timeline a 24fps
# docker run --rm \
#   -v "$(pwd)/input:/input:ro" \
#   -v "$(pwd)/output:/output" \
#   music-bar-detector \
#   /input/music.mp3 \
#   --timeline-start 01:00:07:23 \
#   --fps 24 \
#   --edl /output/music.edl

# Con json + debug + tempo di inizio differente
# docker run --rm \
#   -v "$(pwd)/input:/input:ro" \
#   -v "$(pwd)/output:/output" \
#   music-bar-detector \
#   /input/music.mp3 \
#   --timeline-start 01:00:07:28 \
#   --fps 25 \
#   --output /output/music.json \
#   --edl /output/music.edl \
#   --debug /output/music-debug.png

EXIT_CODE=$?

echo ""
echo "Docker exit code: $EXIT_CODE"

exit $EXIT_CODE