"""Shared PCM decoding and high resolution attack evidence."""

from dataclasses import dataclass

import librosa
import numpy as np
import soundfile as sf
from scipy.ndimage import maximum_filter1d, uniform_filter1d


ANALYSIS_SR = 22050
HOP = 128
N_FFT = 1024


@dataclass
class Audio:
    samples: np.ndarray
    sample_rate: int
    original_sample_rate: int
    decoder: str
    warnings: list
    playback_samples: np.ndarray | None = None

    @property
    def duration(self):
        return len(self.samples) / self.sample_rate


@dataclass
class Evidence:
    times: np.ndarray
    envelope: np.ndarray
    rms: np.ndarray
    attacks: np.ndarray
    strengths: np.ndarray
    silence_threshold: float
    sample_rate: int = ANALYSIS_SR
    hop_length: int = HOP

    def energy_at(self, time, radius=0.03):
        a = max(0, int((time - radius) * self.sample_rate / self.hop_length))
        b = min(len(self.rms), int((time + radius) * self.sample_rate / self.hop_length) + 1)
        return float(np.max(self.rms[a:b], initial=0.0))


def load_audio(path):
    warnings = []
    try:
        samples, sr = sf.read(path, dtype="float32", always_2d=True)
        decoder = "soundfile"
    except (RuntimeError, sf.LibsndfileError):
        samples, sr = librosa.load(path, sr=None, mono=False)
        samples = np.asarray(samples).reshape(1, -1).T if samples.ndim == 1 else samples.T
        decoder = "librosa fallback"
        warnings.append("Soundfile could not decode this file; librosa was used.")
    if not len(samples) or not np.all(np.isfinite(samples)):
        raise ValueError("Audio is empty or contains non-finite samples.")
    mono = samples.mean(axis=1)
    channel_rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2, axis=0))
    if np.sqrt(np.mean(mono.astype(np.float64) ** 2)) < 0.25 * max(channel_rms):
        mono = samples[:, int(np.argmax(channel_rms))]
        warnings.append("Stereo phase cancellation: the strongest channel was used for analysis.")
    original_sr = sr
    if sr != 44100:
        mono = librosa.resample(mono, orig_sr=sr, target_sr=44100)
        sr = 44100
    playback = samples if original_sr == sr else librosa.resample(samples, orig_sr=original_sr, target_sr=sr, axis=0)
    return Audio(np.ascontiguousarray(mono, dtype=np.float32), sr, original_sr, decoder, warnings,
                 np.ascontiguousarray(playback, dtype=np.float32))


def _normalize_local(envelope):
    envelope = np.maximum(envelope, 0)
    scale = maximum_filter1d(envelope, size=int(4 * ANALYSIS_SR / HOP) | 1)
    floor = max(float(envelope.max(initial=0)) * 0.005, 1e-10)
    return np.clip(envelope / np.maximum(scale, floor), 0, 1)


def _transient_time(energy, sr, coarse_time):
    """Bounded leading-edge refinement, without an unrestricted energy backtrack."""
    left = max(0, round((coarse_time - 0.035) * sr))
    right = min(len(energy), round((coarse_time + 0.012) * sr) + 1)
    if right - left < 4:
        return coarse_time
    lag = max(1, round(0.003 * sr))
    previous = np.maximum(np.arange(left, right) - lag, 0)
    rise = energy[left:right] - energy[previous]
    peak = int(np.argmax(rise)) + left
    baseline = float(energy[max(0, peak - 3 * lag):max(1, peak - lag)].min(initial=energy[peak]))
    if energy[peak] < 1.2 * max(baseline, 1e-8) or rise[peak - left] <= 0:
        return coarse_time
    threshold = baseline + 0.2 * (float(energy[peak]) - baseline)
    crossing = peak
    limit = max(left, peak - 3 * lag)
    while crossing > limit and energy[crossing - 1] > threshold:
        crossing -= 1
    return crossing / sr


def analyze_attacks(audio, silence_db=-60.0, percussive=True):
    samples = librosa.resample(audio.samples, orig_sr=audio.sample_rate, target_sr=ANALYSIS_SR)
    # Explicit hop and FFT values are shared by every envelope and time conversion.
    spectrum = librosa.stft(samples, n_fft=N_FFT, hop_length=HOP)
    magnitude = np.abs(spectrum)
    channels = [magnitude]
    if percussive and np.max(magnitude, initial=0) > 0:
        _, percussion = librosa.decompose.hpss(spectrum, margin=(1, 2))
        channels.append(np.abs(percussion))
    frequencies = librosa.fft_frequencies(sr=ANALYSIS_SR, n_fft=N_FFT)
    channels.append(magnitude[(frequencies >= 30) & (frequencies < 250)])
    envelopes = []
    for channel in channels:
        db = librosa.amplitude_to_db(channel, ref=max(float(magnitude.max(initial=0)), 1e-10))
        env = librosa.onset.onset_strength(
            S=db, sr=ANALYSIS_SR, hop_length=HOP, n_fft=N_FFT, max_size=3,
        )
        envelopes.append(_normalize_local(env))
    envelope = np.max(envelopes, axis=0)
    rms = librosa.feature.rms(S=magnitude, frame_length=N_FFT, hop_length=HOP)[0]
    times = librosa.frames_to_time(np.arange(len(envelope)), sr=ANALYSIS_SR, hop_length=HOP)
    threshold = max(10 ** (silence_db / 20), float(np.percentile(rms, 95)) * 10 ** (-45 / 20))
    # Short energy smoothing gives millisecond-scale leading edges after spectral detection.
    energy = np.sqrt(np.maximum(uniform_filter1d(samples ** 2, size=max(1, round(0.002 * ANALYSIS_SR))), 0))
    candidates = []
    for env in envelopes:
        frames = librosa.onset.onset_detect(
            onset_envelope=env, sr=ANALYSIS_SR, hop_length=HOP, units="frames",
            backtrack=False, normalize=False, delta=0.10,
        )
        for frame in frames:
            if env[frame] < 0.25 or rms[min(frame, len(rms) - 1)] < threshold:
                continue
            time = _transient_time(energy, ANALYSIS_SR, float(times[frame]))
            if 0 <= time < audio.duration:
                candidates.append((time, float(env[frame])))
    # Merge estimates of the same attack from different bands, keeping the strongest.
    merged = []
    for time, strength in sorted(candidates):
        if merged and time - merged[-1][0] < 0.008:
            if strength > merged[-1][1]:
                merged[-1] = (time, strength)
        else:
            merged.append((time, strength))
    attacks = np.array([x[0] for x in merged], dtype=float)
    strengths = np.array([x[1] for x in merged], dtype=float)
    return Evidence(times, envelope, rms, attacks, strengths, threshold)
