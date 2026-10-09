"""Interchangeable neural trackers, with their activation evidence preserved."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np


@dataclass
class Tracking:
    times: np.ndarray
    positions: np.ndarray
    beat_probability: np.ndarray
    downbeat_probability: np.ndarray
    activation_fps: float
    backend: str
    metadata: dict = field(default_factory=dict)

    def probability_at(self, time, downbeat=False):
        values = self.downbeat_probability if downbeat else self.beat_probability
        frame = round(time * self.activation_fps)
        start = max(0, frame - 1)
        end = min(len(values), frame + 2)
        return float(np.clip(np.max(values[start:end], initial=0.0), 0, 1))

    @property
    def downbeats(self):
        return self.times[self.positions == 1]


def _dbn(activations, fps, config):
    from madmom.features.downbeats import DBNDownBeatTrackingProcessor

    return np.asarray(DBNDownBeatTrackingProcessor(
        beats_per_bar=config.beats_per_bar, min_bpm=config.min_bpm,
        max_bpm=config.max_bpm, fps=fps, correct=True,
        transition_lambda=config.transition_lambda, threshold=config.activation_threshold,
    )(activations)).reshape(-1, 2)


def madmom_tracking(audio, config):
    from madmom.audio.signal import Signal
    from madmom.features.downbeats import RNNDownBeatProcessor

    # Passing PCM avoids a second MP3 decoder and preserves the common sample origin.
    signal = Signal(audio.samples, sample_rate=audio.sample_rate, num_channels=1)
    activations = np.asarray(RNNDownBeatProcessor()(signal))
    events = _dbn(activations, 100, config)
    return Tracking(
        events[:, 0], events[:, 1].astype(int),
        np.clip(activations.sum(axis=1), 0, 1), activations[:, 1], 100, "madmom",
        {"beats_per_bar_candidates": config.beats_per_bar, "decoder": "DBN"},
    )


def positions_from_downbeats(beats, downbeats):
    """Keep pickup beats unclassified; do not invent a downbeat at the file start."""
    positions = np.zeros(len(beats), dtype=int)
    if not len(beats):
        return positions
    indices = sorted({int(np.argmin(np.abs(beats - time))) for time in downbeats
                      if np.min(np.abs(beats - time)) <= 0.08})
    for k, start in enumerate(indices):
        end = indices[k + 1] if k + 1 < len(indices) else len(beats)
        positions[start:end] = np.arange(1, end - start + 1)
    return positions


def dbn_activations(beat_probability, downbeat_probability):
    """Convert independent sigmoid heads to exclusive probabilities with non-beat mass."""
    epsilon = 1e-5
    beat = np.asarray(beat_probability, dtype=np.float64)
    downbeat = np.asarray(downbeat_probability, dtype=np.float64)
    activations = np.column_stack((
        np.clip(beat - downbeat, epsilon, 1 - 2 * epsilon),
        np.clip(downbeat, epsilon, 1 - 2 * epsilon),
    ))
    activations *= np.minimum(1, (1 - epsilon) / activations.sum(axis=1))[:, None]
    return activations


def beat_this_tracking(audio, config):
    try:
        from beat_this.inference import Audio2Frames
        from beat_this.model.postprocessor import Postprocessor
    except ImportError as exc:
        raise RuntimeError(
            "Beat This! is not installed. Build with WITH_BEAT_THIS=1 ./build.sh "
            "or install requirements-beat-this.txt."
        ) from exc
    processor = Audio2Frames(checkpoint_path=config.checkpoint, device=config.device, float16=False)
    beat_logits, downbeat_logits = processor(audio.samples, audio.sample_rate)
    beat_probability = beat_logits.sigmoid().detach().cpu().numpy()
    downbeat_probability = downbeat_logits.sigmoid().detach().cpu().numpy()
    if config.beat_this_dbn:
        # madmom expects mutually exclusive non-downbeat and downbeat probabilities.
        activations = dbn_activations(beat_probability, downbeat_probability)
        events = _dbn(activations, 50, config)
        beats, positions = events[:, 0], events[:, 1].astype(int)
    else:
        beats, downbeats = Postprocessor(type="minimal", fps=50)(beat_logits, downbeat_logits)
        positions = positions_from_downbeats(beats, downbeats)
    return Tracking(
        np.asarray(beats), positions, beat_probability, downbeat_probability, 50, "beat-this",
        {"checkpoint": config.checkpoint, "device": config.device,
         "decoder": "DBN" if config.beat_this_dbn else "minimal"},
    )


def run_tracking(audio, config):
    if config.backend == "madmom":
        primary, secondary = madmom_tracking(audio, config), None
    elif config.backend == "beat-this":
        primary, secondary = beat_this_tracking(audio, config), None
    else:
        # Keep the modern model's sequence intact. The other tracker is evidence,
        # not a union of detections (which would manufacture extra cut points).
        primary = beat_this_tracking(audio, config)
        secondary = madmom_tracking(audio, config)
    for result in (primary, secondary):
        if result is None:
            continue
        valid = np.isfinite(result.times) & (result.times >= 0) & (result.times < audio.duration)
        result.times, result.positions = result.times[valid], result.positions[valid]
        order = np.argsort(result.times)
        result.times, result.positions = result.times[order], result.positions[order]
        unique = np.r_[True, np.diff(result.times) > 1e-6] if len(result.times) else np.array([], dtype=bool)
        result.times, result.positions = result.times[unique], result.positions[unique]
    return primary, secondary


def local_periods(times):
    if len(times) < 2:
        return np.full(len(times), 0.5)
    intervals = np.diff(times)
    return np.array([np.median(intervals[max(0, i - 3):min(len(intervals), i + 4)])
                     for i in range(len(times))])


def librosa_verification(evidence, tracking):
    """Isolate optional native/JIT kernels so a crash cannot destroy the main detection."""
    if len(tracking.times) < 2 or not np.any(evidence.envelope):
        return np.array([])
    periods = local_periods(tracking.times)
    bpm = np.interp(evidence.times, tracking.times, 60 / periods)
    with tempfile.TemporaryDirectory(prefix="beat-phase-") as directory:
        path = Path(directory) / "evidence.npz"
        np.savez(path, envelope=evidence.envelope, bpm=bpm,
                 sample_rate=evidence.sample_rate, hop_length=evidence.hop_length)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "app.phase_check", str(path)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "NUMBA_CPU_NAME": "generic"},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Librosa phase check timed out; neural detections are retained.") from exc
        if result.returncode:
            raise RuntimeError(f"Librosa phase check failed (exit {result.returncode}); neural detections are retained.")
        return np.asarray(json.loads(result.stdout), dtype=float)
