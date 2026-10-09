# Build dei sorgenti
docker build -t music-bar-detector .

# Input
mkdir -p input output
input/music.mp3

# Esecuzione
docker run --rm \
  -v "$(pwd)/input:/input:ro" \
  -v "$(pwd)/output:/output" \
  music-bar-detector \
  /input/music.mp3 \
  --output /output/music.json


# Output
output/
└── music.json

{
  "audio": "/input/music.mp3",
  "duration": 187.42,
  "time_signature": "4/4",
  "tempo": {
    "librosa_bpm": 128.02,
    "madmom_bpm": 128.01
  },
  "quality": {
    "beat_tracker_agreement": 0.976
  },
  "beats": [
    0.231,
    0.700,
    1.168,
    1.637
  ],
  "bars": [
    {
      "number": 1,
      "start": 0.231
    },
    {
      "number": 2,
      "start": 2.106
    }
  ]
}