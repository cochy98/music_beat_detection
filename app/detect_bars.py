#!/usr/bin/env python3

import argparse
import json
import os
import sys

import librosa
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

from madmom.features.downbeats import (
    RNNDownBeatProcessor,
    DBNDownBeatTrackingProcessor,
)


# ============================================================
# CONFIG
# ============================================================

DEFAULT_FPS = 25.0
DEFAULT_TIMELINE_START = "01:00:00:00"

ONSET_MATCH_TOLERANCE = 0.12


# ============================================================
# TIMECODE
# ============================================================

def parse_timecode(timecode, fps):
    """
    Convert HH:MM:SS:FF to seconds.
    Non-drop-frame, integer FPS.
    """

    parts = timecode.strip().split(":")

    if len(parts) != 4:
        raise ValueError(
            f"Invalid timecode '{timecode}'. "
            "Expected HH:MM:SS:FF"
        )

    hours, minutes, seconds, frames = map(int, parts)

    fps_int = int(round(fps))

    if frames < 0 or frames >= fps_int:
        raise ValueError(
            f"Invalid frame {frames} for {fps_int} fps. "
            f"Valid range: 0-{fps_int - 1}"
        )

    total_seconds = (
        hours * 3600
        + minutes * 60
        + seconds
        + frames / fps
    )

    return total_seconds


def seconds_to_timecode(seconds, fps):
    """
    Convert seconds to HH:MM:SS:FF.
    Non-drop-frame, integer FPS.
    """

    if seconds < 0:
        seconds = 0.0

    fps_int = int(round(fps))

    total_frames = int(round(seconds * fps))

    frames = total_frames % fps_int
    total_seconds = total_frames // fps_int

    sec = total_seconds % 60
    total_minutes = total_seconds // 60

    minutes = total_minutes % 60
    hours = total_minutes // 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{sec:02d}:"
        f"{frames:02d}"
    )


def add_frames_to_timecode(timecode, frames_to_add, fps):
    """
    Add frames to a non-drop-frame timecode.
    """

    hours, minutes, seconds, frames = map(
        int,
        timecode.split(":")
    )

    fps_int = int(round(fps))

    total_frames = (
        ((hours * 60 + minutes) * 60 + seconds) * fps_int
        + frames
    )

    total_frames += frames_to_add

    new_frames = total_frames % fps_int

    total_seconds = total_frames // fps_int

    new_seconds = total_seconds % 60

    total_minutes = total_seconds // 60

    new_minutes = total_minutes % 60

    new_hours = total_minutes // 60

    return (
        f"{new_hours:02d}:"
        f"{new_minutes:02d}:"
        f"{new_seconds:02d}:"
        f"{new_frames:02d}"
    )


# ============================================================
# AUDIO
# ============================================================

def load_audio(audio_path):
    """
    Load audio as mono waveform.
    """

    print("[INFO] Loading audio...")

    audio, sample_rate = librosa.load(
        audio_path,
        sr=None,
        mono=True,
    )

    duration = len(audio) / sample_rate

    print(
        f"[INFO] Audio loaded: "
        f"{duration:.2f}s @ {sample_rate} Hz"
    )

    return audio, sample_rate, duration


# ============================================================
# ONSET DETECTION
# ============================================================

def detect_onsets(audio, sample_rate):
    """
    Detect onset positions using librosa.

    This is used as an auxiliary confidence signal.
    It does NOT determine bar positions.
    """

    print("[INFO] Calculating onset envelope...")

    onset_envelope = librosa.onset.onset_strength(
        y=audio,
        sr=sample_rate,
    )

    print("[INFO] Detecting onsets...")

    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        units="frames",
    )

    onset_times = librosa.frames_to_time(
        onset_frames,
        sr=sample_rate,
    )

    print(
        f"[INFO] Detected {len(onset_times)} onset events"
    )

    return np.asarray(onset_times)


# ============================================================
# MADMOM
# ============================================================

def detect_madmom(audio_path):
    """
    Detect beats and downbeats using madmom-modern.

    Returns:
        ndarray with rows:
        [time_seconds, beat_position]
    """

    print("[INFO] Running madmom downbeat detection...")

    rnn_processor = RNNDownBeatProcessor()

    activations = rnn_processor(audio_path)

    dbn_processor = DBNDownBeatTrackingProcessor(
        beats_per_bar=[4],
        min_bpm=60,
        max_bpm=200,
        fps=100,
        correct=True,
    )

    result = dbn_processor(activations)

    result = np.asarray(result)

    if result.size == 0:
        raise RuntimeError(
            "madmom did not detect any beats/downbeats."
        )

    print(
        f"[INFO] madmom detected {len(result)} beat events"
    )

    return result


# ============================================================
# BPM
# ============================================================

def estimate_bpm_from_madmom(madmom_result):
    """
    Estimate BPM from madmom beat intervals.

    Avoids librosa.feature.tempo(), which can invoke
    problematic native code on some Apple Silicon/Docker
    combinations.
    """

    beat_times = madmom_result[:, 0]

    if len(beat_times) < 2:
        return None

    intervals = np.diff(beat_times)

    intervals = intervals[
        (intervals > 0.2)
        & (intervals < 2.0)
    ]

    if len(intervals) == 0:
        return None

    median_interval = np.median(intervals)

    bpm = 60.0 / median_interval

    return float(bpm)


# ============================================================
# DOWNBEATS
# ============================================================

def extract_downbeats(madmom_result):
    """
    Extract beat-position 1 events.
    """

    downbeats = madmom_result[
        madmom_result[:, 1] == 1
    ]

    if len(downbeats) == 0:
        raise RuntimeError(
            "madmom did not detect any downbeats."
        )

    downbeat_times = downbeats[:, 0]

    print(
        f"[INFO] Detected {len(downbeat_times)} downbeats"
    )

    return np.asarray(downbeat_times)


# ============================================================
# CONFIDENCE
# ============================================================

def calculate_confidence(
    downbeat_time,
    onset_times,
):
    """
    Auxiliary confidence score.

    1.0 = downbeat agrees with a detected onset
    0.5 = no nearby onset

    IMPORTANT:
    This function never moves the detected downbeat.
    """

    if len(onset_times) == 0:
        return 0.5

    distance = np.min(
        np.abs(onset_times - downbeat_time)
    )

    if distance <= ONSET_MATCH_TOLERANCE:
        return 1.0

    return 0.5


# ============================================================
# BAR BUILDING
# ============================================================

def build_bars(
    downbeat_times,
    audio_duration,
    onset_times,
    timeline_start_seconds,
    fps,
):
    """
    Build musical bars from consecutive downbeats.

    Each bar starts at a downbeat and ends at the next
    downbeat. The final bar ends at the audio duration.
    """

    bars = []

    for index, start in enumerate(downbeat_times):

        if index + 1 < len(downbeat_times):
            end = downbeat_times[index + 1]
        else:
            end = audio_duration

        if end <= start:
            continue

        duration = end - start

        timeline_seconds = (
            timeline_start_seconds + start
        )

        timeline_timecode = seconds_to_timecode(
            timeline_seconds,
            fps,
        )

        confidence = calculate_confidence(
            start,
            onset_times,
        )

        bar = {
            "bar": len(bars) + 1,
            "source_start_seconds": float(start),
            "source_end_seconds": float(end),
            "duration_seconds": float(duration),
            "timeline_start_seconds": float(
                timeline_seconds
            ),
            "timeline_timecode": timeline_timecode,
            "confidence": confidence,
        }

        bars.append(bar)

    print(
        f"[INFO] Built {len(bars)} musical bars"
    )

    return bars


# ============================================================
# JSON
# ============================================================

def create_json(
    bars,
    output_path,
    audio_path,
    duration,
    bpm,
    fps,
    timeline_start,
):
    """
    Write detection results to JSON.
    """

    data = {
        "audio": {
            "path": audio_path,
            "duration_seconds": duration,
        },
        "timeline": {
            "start_timecode": timeline_start,
            "fps": fps,
            "format": "non-drop-frame",
        },
        "analysis": {
            "estimated_bpm": bpm,
            "bars_detected": len(bars),
        },
        "bars": bars,
    }

    print(
        f"[INFO] Creating JSON: {output_path}"
    )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(
        f"[INFO] JSON written: {output_path}"
    )


# ============================================================
# DAVINCI RESOLVE MARKER EDL
# ============================================================

def create_edl(
    bars,
    output_path,
    fps,
):
    """
    Create a DaVinci Resolve Timeline Marker EDL.

    Format is based on the EDL exported directly by
    DaVinci Resolve using:

        Timeline > Export > Timeline Markers to EDL

    Each bar becomes a one-frame marker located at the
    beginning of the musical bar.
    """

    print(
        f"[INFO] Creating Resolve Marker EDL: "
        f"{output_path}"
    )

    lines = [
        "TITLE: automatic musical bar markers",
        "FCM: NON-DROP FRAME",
        "",
    ]

    for index, bar in enumerate(bars, start=1):

        tc = bar["timeline_timecode"]

        # Resolve expects a one-frame event.
        next_tc = add_frames_to_timecode(
            tc,
            1,
            fps,
        )

        # Main EDL event
        lines.append(
            f"{index:03d}  001      V     C        "
            f"{tc} {next_tc} {tc} {next_tc}  "
        )

        # Marker metadata
        lines.append(
            f" |C:ResolveColorBlue "
            f"|M:BAR {index:03d} "
            f"|D:1"
        )

        lines.append("")

    with open(
        output_path,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write("\n".join(lines))

    print(
        f"[INFO] Resolve Marker EDL written: "
        f"{output_path}"
    )


# ============================================================
# DEBUG IMAGE
# ============================================================

def create_debug_plot(
    audio,
    sample_rate,
    bars,
    output_path,
):
    """
    Create a waveform/debug image showing detected bars.
    """

    print(
        f"[INFO] Creating debug image: {output_path}"
    )

    duration = len(audio) / sample_rate

    time_axis = np.linspace(
        0,
        duration,
        len(audio),
    )

    plt.figure(figsize=(16, 6))

    plt.plot(
        time_axis,
        audio,
        linewidth=0.5,
    )

    for bar in bars:

        source_time = bar[
            "source_start_seconds"
        ]

        plt.axvline(
            source_time,
            linewidth=1.0,
        )

    plt.xlabel("Time (seconds)")
    plt.ylabel("Amplitude")
    plt.title("Detected Musical Bars")

    plt.tight_layout()

    plt.savefig(
        output_path,
        dpi=150,
    )

    plt.close()

    print(
        f"[INFO] Debug image written: {output_path}"
    )


# ============================================================
# ARGUMENTS
# ============================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Detect musical bars and create "
            "DaVinci Resolve Timeline Marker EDL."
        )
    )

    parser.add_argument(
        "input",
        help="Input audio file",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file",
    )

    parser.add_argument(
        "--edl",
        default=None,
        help="DaVinci Resolve Marker EDL",
    )

    parser.add_argument(
        "--debug",
        default=None,
        help="Debug waveform PNG",
    )

    parser.add_argument(
        "--timeline-start",
        default=DEFAULT_TIMELINE_START,
        help=(
            "Timeline start timecode "
            "(default: 01:00:00:00)"
        ),
    )

    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_FPS,
        help="Timeline FPS (default: 25)",
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main():

    args = parse_arguments()

    try:

        # ----------------------------------------------------
        # Validate input
        # ----------------------------------------------------

        if not os.path.isfile(args.input):
            raise FileNotFoundError(
                f"Input file not found: {args.input}"
            )

        if args.fps <= 0:
            raise ValueError(
                "FPS must be greater than zero."
            )

        # ----------------------------------------------------
        # Timeline start
        # ----------------------------------------------------

        timeline_start_seconds = parse_timecode(
            args.timeline_start,
            args.fps,
        )

        # ----------------------------------------------------
        # Audio
        # ----------------------------------------------------

        audio, sample_rate, duration = load_audio(
            args.input
        )

        # ----------------------------------------------------
        # Onsets
        # ----------------------------------------------------

        onset_times = detect_onsets(
            audio,
            sample_rate,
        )

        # ----------------------------------------------------
        # madmom
        # ----------------------------------------------------

        madmom_result = detect_madmom(
            args.input
        )

        # ----------------------------------------------------
        # BPM
        # ----------------------------------------------------

        bpm = estimate_bpm_from_madmom(
            madmom_result
        )

        if bpm is not None:
            print(
                f"[INFO] Estimated BPM: {bpm:.2f}"
            )
        else:
            print(
                "[WARN] Could not estimate BPM"
            )

        # ----------------------------------------------------
        # Downbeats
        # ----------------------------------------------------

        downbeat_times = extract_downbeats(
            madmom_result
        )

        # ----------------------------------------------------
        # Bars
        # ----------------------------------------------------

        bars = build_bars(
            downbeat_times=downbeat_times,
            audio_duration=duration,
            onset_times=onset_times,
            timeline_start_seconds=timeline_start_seconds,
            fps=args.fps,
        )

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        if args.output:

            create_json(
                bars=bars,
                output_path=args.output,
                audio_path=args.input,
                duration=duration,
                bpm=bpm,
                fps=args.fps,
                timeline_start=args.timeline_start,
            )

        # ----------------------------------------------------
        # Resolve EDL
        # ----------------------------------------------------

        if args.edl:

            create_edl(
                bars=bars,
                output_path=args.edl,
                fps=args.fps,
            )

        # ----------------------------------------------------
        # Debug
        # ----------------------------------------------------

        if args.debug:

            create_debug_plot(
                audio=audio,
                sample_rate=sample_rate,
                bars=bars,
                output_path=args.debug,
            )

        print("")
        print("[INFO] Detection completed successfully.")
        print(
            f"[INFO] Bars detected: {len(bars)}"
        )

        return 0

    except Exception as exc:

        print(
            f"[ERROR] {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    sys.exit(main())