"""Resolve-compatible EDL, audit report, visual diagnostics, and listening preview."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import soundfile as sf


def _destination(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_json(data, path):
    _destination(path).write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def create_edl(markers, path, rate):
    lines = ["TITLE: musical markers", "FCM: DROP FRAME" if rate.drop_frame else "FCM: NON-DROP FRAME", ""]
    for index, marker in enumerate(markers, 1):
        start = marker["timeline_timecode"]
        end = rate.format_timecode(marker["timeline_frame"] + 1)
        label = f"BAR {marker['bar']:03d}" if marker["is_downbeat"] else f"BEAT {marker['beat']:03d}"
        if marker["needs_review"]:
            label += " CHECK"
        lines.append(f"{index:03d}  001      V     C        {start} {end} {start} {end}  ")
        lines.append(f" |C:{marker['marker_color']} |M:{label} |D:1")
        lines.append("")
    _destination(path).write_text("\n".join(lines), encoding="utf-8")


def create_click_track(audio, markers, path):
    # Reproduce exported video-frame positions, including trim, offset, and rounding.
    mixed = (audio.playback_samples if audio.playback_samples is not None else audio.samples).copy() * 0.70
    length = round(0.025 * audio.sample_rate)
    t = np.arange(length) / audio.sample_rate
    for marker in markers:
        start = round(marker["export_source_seconds"] * audio.sample_rate)
        if not 0 <= start < len(mixed):
            continue
        frequency = 1600 if marker["is_downbeat"] else 1000
        click = 0.35 * np.cos(2 * np.pi * frequency * t) * np.exp(-t / 0.006)
        count = min(length, len(mixed) - start)
        mixed[start:start + count] += click[:count, None] if mixed.ndim == 2 else click[:count]
    mixed /= max(1.0, float(np.max(np.abs(mixed))) / 0.98)
    sf.write(_destination(path), mixed, audio.sample_rate, subtype="PCM_24")


def create_pcm_copy(audio, path):
    samples = audio.playback_samples if audio.playback_samples is not None else audio.samples
    sf.write(_destination(path), samples, audio.sample_rate, subtype="FLOAT")


def create_debug_plot(audio, evidence, events, path, start=0, duration=None):
    end = audio.duration if duration is None else min(audio.duration, start + duration)
    a, b = round(start * audio.sample_rate), round(end * audio.sample_rate)
    visible = audio.samples[a:b]
    stride = max(1, int(np.ceil(len(visible) / 12000)))
    count = len(visible) // stride
    blocks = visible[:count * stride].reshape(count, stride)
    x = (a + np.arange(count) * stride) / audio.sample_rate
    figure, axes = plt.subplots(2, 1, figsize=(18, 7), sharex=True)
    axes[0].fill_between(x, blocks.min(axis=1), blocks.max(axis=1), color="#8a95a5", alpha=0.7)
    mask = (evidence.times >= start) & (evidence.times <= end)
    axes[1].plot(evidence.times[mask], evidence.envelope[mask], color="#555555", linewidth=0.7, label="Attack envelope")
    attacks = evidence.attacks[(evidence.attacks >= start) & (evidence.attacks <= end)]
    axes[1].vlines(attacks, 0, 0.15, color="#777777", alpha=0.35, label="Attack candidates")
    visible_events = [event for event in events if start <= event["source_start_seconds"] <= end]
    for event in visible_events:
        color = "#d69716" if event["needs_review"] else "#1878bd"
        if event["export_status"] != "exported":
            color = "#c7c7c7"
        axes[0].axvline(event["original_source_seconds"], color="#999999", linestyle=":", linewidth=0.6, alpha=0.5)
        axes[0].axvline(event["source_start_seconds"], color=color, linewidth=1.1 if event["is_downbeat"] else 0.6, alpha=0.8)
    axes[1].scatter([event["source_start_seconds"] for event in visible_events],
                    [event["confidence"] for event in visible_events],
                    c=["#d69716" if event["needs_review"] else "#1878bd" for event in visible_events],
                    s=15, label="Quality score (not probability)")
    axes[0].set(title="Musical markers: blue = supported, yellow = review, grey = excluded; dotted = model time", ylabel="Amplitude")
    axes[1].set(xlabel="Source time (seconds)", ylabel="Evidence / quality", ylim=(-0.03, 1.05), xlim=(start, end))
    axes[1].legend(loc="upper right")
    figure.tight_layout()
    figure.savefig(_destination(path), dpi=150)
    plt.close(figure)
