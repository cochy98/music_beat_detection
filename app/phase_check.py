"""Internal subprocess for an optional tempo-guided librosa timing check."""

import json
import sys

import librosa
import numpy as np


def main():
    with np.load(sys.argv[1], allow_pickle=False) as data:
        _, times = librosa.beat.beat_track(
            onset_envelope=data["envelope"], sr=int(data["sample_rate"]),
            hop_length=int(data["hop_length"]), bpm=data["bpm"],
            tightness=80, trim=True, units="time",
        )
    print(json.dumps(np.asarray(times).tolist(), allow_nan=False))


if __name__ == "__main__":
    main()
