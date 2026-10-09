#!/usr/bin/env python3
"""Detect musical cut points, retain diagnostics, and export Resolve marker EDLs."""

import argparse
from collections import Counter
import importlib.metadata
import math
from pathlib import Path
import sys

import numpy as np

from .audio import analyze_attacks, load_audio
from .export import create_click_track, create_debug_plot, create_edl, create_pcm_copy, write_json
from .markers import assess_beats, build_bars, place_markers
from .timecode import FrameRate, add_frames_to_timecode, parse_timecode, seconds_to_timecode
from .tracking import Tracking, librosa_verification, local_periods, run_tracking

DEFAULT_FPS = "25"
DEFAULT_TIMELINE_START = "01:00:00:00"


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Detect beats/downbeats and export audited DaVinci Resolve marker EDLs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="Input audio file; analysis uses the complete source")
    parser.add_argument("--output", help="Detailed JSON report")
    parser.add_argument("--edl", help="Resolve timeline marker EDL")
    parser.add_argument("--debug", help="Waveform, attacks, and marker quality PNG")
    parser.add_argument("--click", help="Audio mixed with exported markers as audible clicks (WAV)")
    parser.add_argument("--pcm", help="Decoded WAV copy for Resolve, with the same sample origin as analysis")
    parser.add_argument("--fps", default=DEFAULT_FPS, help="Integer, NTSC decimal, or exact fraction (24000/1001)")
    parser.add_argument("--drop-frame", action="store_true", help="Drop-frame labels at 29.97/59.94 FPS")
    parser.add_argument("--timeline-start", default=DEFAULT_TIMELINE_START, help="Timeline origin timecode")
    parser.add_argument("--audio-start", help="Absolute timecode where the trimmed audio clip starts")
    parser.add_argument("--source-in", type=float, default=0, help="Source trim-in, seconds")
    parser.add_argument("--source-out", type=float, help="Source trim-out, seconds (exclusive)")
    parser.add_argument("--offset-ms", type=float, default=0, help="Manual export offset; positive delays markers")
    parser.add_argument("--backend", choices=("madmom", "beat-this", "ensemble"), default="madmom",
                        help="Ensemble uses Beat This! positions and checks against madmom")
    parser.add_argument("--checkpoint", default="final0", help="Beat This! checkpoint name or local .ckpt path")
    parser.add_argument("--device", choices=("cpu", "cuda", "mps"), default="cpu", help="Beat This! compute device")
    parser.add_argument("--beat-this-dbn", action="store_true", help="Decode Beat This! with the configured DBN")
    parser.add_argument("--beats-per-bar", default="auto", help="DBN meter candidates, e.g. 4 or 3,4; auto tests 3 and 4")
    parser.add_argument("--min-bpm", type=float, default=40, help="DBN lower tempo bound")
    parser.add_argument("--max-bpm", type=float, default=240, help="DBN upper tempo bound")
    parser.add_argument("--bpm", type=float, help="DBN tempo hint; narrows bounds to +/-15%%")
    parser.add_argument("--transition-lambda", type=float, default=100, help="DBN tempo stability; larger prefers steady tempo")
    parser.add_argument("--activation-threshold", type=float, default=0.05, help="DBN activation boundary threshold")
    parser.add_argument("--marker-type", choices=("beats", "downbeats"), default="downbeats", help="Every pulse or measure starts")
    parser.add_argument("--mode", choices=("grid", "attacks"), default="grid", help="Grid includes implicit pulses; attacks requires audible evidence")
    parser.add_argument("--strict", action="store_true", help="Export only markers that need no review")
    parser.add_argument("--min-confidence", type=float, default=0.65, help="Quality review threshold (heuristic, not probability)")
    parser.add_argument("--max-snap-ms", type=float, default=50, help="Maximum refinement; also capped at 10%% of the local beat period")
    parser.add_argument("--no-refine", action="store_true", help="Retain model times for a baseline comparison")
    parser.add_argument("--no-percussive", action="store_true", help="Skip HPSS; still analyze full mix and bass attacks")
    parser.add_argument("--no-librosa-check", action="store_true", help="Skip the auxiliary tempo-guided phase check")
    parser.add_argument("--silence-db", type=float, default=-60, help="Absolute low-energy floor, dBFS")
    parser.add_argument("--debug-start", type=float, default=0, help="Debug plot start in source seconds")
    parser.add_argument("--debug-duration", type=float, help="Debug plot length in seconds; default is remaining audio")
    args = parser.parse_args(argv)
    args.meter_mode = args.beats_per_bar
    try:
        args.beats_per_bar = [3, 4] if args.meter_mode == "auto" else sorted(set(map(int, args.meter_mode.split(","))))
    except ValueError:
        parser.error("--beats-per-bar must be auto or comma-separated integers")
    if not args.beats_per_bar or any(meter < 2 or meter > 12 for meter in args.beats_per_bar):
        parser.error("--beats-per-bar candidates must be between 2 and 12")
    numeric = ("source_in", "source_out", "offset_ms", "min_bpm", "max_bpm", "bpm", "transition_lambda",
               "activation_threshold", "min_confidence", "max_snap_ms", "silence_db", "debug_start", "debug_duration")
    if any(getattr(args, name) is not None and not math.isfinite(getattr(args, name)) for name in numeric):
        parser.error("Numeric options must be finite")
    if args.bpm is not None:
        if args.bpm <= 0:
            parser.error("--bpm must be positive")
        args.min_bpm, args.max_bpm = max(args.min_bpm, args.bpm * 0.85), min(args.max_bpm, args.bpm * 1.15)
    if not 10 <= args.min_bpm < args.max_bpm <= 600:
        parser.error("Require 10 <= --min-bpm < --max-bpm <= 600")
    if args.transition_lambda <= 0 or not 0 <= args.activation_threshold < 1:
        parser.error("Require positive --transition-lambda and 0 <= --activation-threshold < 1")
    if not 0 <= args.min_confidence <= 1 or not 0 <= args.max_snap_ms <= 150:
        parser.error("Require 0 <= --min-confidence <= 1 and 0 <= --max-snap-ms <= 150")
    if args.silence_db > 0 or args.debug_start < 0 or (args.debug_duration is not None and args.debug_duration <= 0):
        parser.error("Require --silence-db <= 0, --debug-start >= 0 and positive --debug-duration")
    if (args.meter_mode != "auto" or args.bpm is not None or args.min_bpm != 40 or args.max_bpm != 240
            or args.transition_lambda != 100 or args.activation_threshold != 0.05):
        args.beat_this_dbn = True
    return args


def package_versions():
    versions = {}
    for name in ("numpy", "scipy", "librosa", "soundfile", "madmom-modern", "beat-this", "torch", "torchaudio"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


def make_report(args, audio, evidence, primary, secondary, events, bars, markers, warnings):
    rate = FrameRate.parse(args.fps, args.drop_frame)
    intervals = np.diff(primary.times)
    bpm = float(60 / np.median(intervals)) if len(intervals) else None
    periods = local_periods(primary.times)
    meter_counts = Counter(int(x) for x in np.diff(np.flatnonzero(primary.positions == 1)))
    return {
        "schema_version": 2, "algorithm_version": "2.0",
        "audio": {"path": str(Path(args.input)), "duration_seconds": audio.duration,
                  "original_sample_rate": audio.original_sample_rate, "analysis_pcm_sample_rate": audio.sample_rate,
                  "decoder": audio.decoder, "pcm_export_path": args.pcm},
        "timeline": {"start_timecode": rate.format_timecode(rate.parse_timecode(args.timeline_start)),
                     "audio_start_timecode": rate.format_timecode(rate.parse_timecode(args.audio_start or args.timeline_start)),
                     "fps": float(rate.rate), "fps_fraction": str(rate.rate), "nominal_fps": rate.nominal,
                     "format": "drop-frame" if rate.drop_frame else "non-drop-frame",
                     "source_in_seconds": args.source_in, "source_out_seconds": args.source_out or audio.duration,
                     "manual_offset_ms": args.offset_ms},
        "analysis": {"backend": primary.backend, "secondary_backend": secondary.backend if secondary else None,
                     "tracker": primary.metadata, "estimated_bpm": bpm,
                     "local_bpm_range": [float(np.min(60 / periods)), float(np.max(60 / periods))] if len(periods) else None,
                     "observed_beats_per_bar": dict(sorted(meter_counts.items())),
                     "beats_detected": len(events), "bars_detected": len(bars),
                     "attacks_detected": len(evidence.attacks), "markers_exported": len(markers),
                     "markers_requiring_review": sum(event["needs_review"] for event in markers),
                     "refined_beats": sum(abs(event["refinement_ms"]) > 0.01 for event in events),
                     "export_status_counts": dict(Counter(event["export_status"] for event in events)),
                     "confidence_definition": "Heuristic evidence score; not a calibrated probability of correctness.",
                     "warnings": warnings, "dependency_versions": package_versions()},
        "settings": vars(args), "beats": events, "bars": bars, "markers": markers,
    }


def main(argv=None):
    args = parse_arguments(argv)
    try:
        source = Path(args.input).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Input file not found: {args.input}")
        destinations = [Path(path).resolve() for path in (args.output, args.edl, args.debug, args.click, args.pcm) if path]
        if source in destinations or len(destinations) != len(set(destinations)):
            raise ValueError("Outputs must be distinct paths and cannot overwrite the input audio.")
        if args.click and Path(args.click).suffix.lower() != ".wav":
            raise ValueError("--click output must have a .wav extension.")
        if args.pcm and Path(args.pcm).suffix.lower() != ".wav":
            raise ValueError("--pcm output must have a .wav extension.")
        rate = FrameRate.parse(args.fps, args.drop_frame)
        rate.parse_timecode(args.timeline_start)
        rate.parse_timecode(args.audio_start or args.timeline_start)
        print("[INFO] Decoding audio once for all trackers...")
        audio = load_audio(str(source))
        if not 0 <= args.source_in < (args.source_out if args.source_out is not None else audio.duration) <= audio.duration + 1e-6:
            raise ValueError("Require 0 <= --source-in < --source-out <= audio duration.")
        if args.debug_start >= audio.duration:
            raise ValueError("--debug-start must lie within the audio.")
        print(f"[INFO] Audio: {audio.duration:.3f}s; high resolution attack analysis...")
        evidence = analyze_attacks(audio, args.silence_db, not args.no_percussive)
        warnings = list(audio.warnings)
        if np.max(np.abs(audio.samples)) < 1e-8:
            primary = Tracking(np.array([]), np.array([], dtype=int), np.array([]), np.array([]), 100,
                               args.backend, {"skipped": "silent input"})
            secondary = None
        else:
            print(f"[INFO] Tracking with {args.backend}...")
            primary, secondary = run_tracking(audio, args)
        if not len(primary.times):
            warnings.append("No beats detected. An empty marker export is produced.")
        elif not len(primary.downbeats) and args.marker_type == "downbeats":
            warnings.append("No downbeats detected. Try --marker-type beats or another backend.")
        verification = None
        if not args.no_librosa_check and len(primary.times) >= 2:
            print("[INFO] Checking onset phase with tempo-guided librosa...")
            try:
                verification = librosa_verification(evidence, primary)
                if not len(verification):
                    warnings.append("Librosa phase check found no supported pulses.")
            except (RuntimeError, OSError, ValueError) as exc:
                warnings.append(str(exc))
        events = assess_beats(primary, evidence, audio.duration, args, secondary, verification)
        markers = place_markers(events, rate, args, audio.duration)
        bars = build_bars(events, audio.duration)
        report = make_report(args, audio, evidence, primary, secondary, events, bars, markers, warnings)
        if args.output:
            write_json(report, args.output)
        if args.edl:
            create_edl(markers, args.edl, rate)
        if args.debug:
            create_debug_plot(audio, evidence, events, args.debug, args.debug_start, args.debug_duration)
        if args.click:
            create_click_track(audio, markers, args.click)
        if args.pcm:
            create_pcm_copy(audio, args.pcm)
        for warning in warnings:
            print(f"[WARN] {warning}")
        analysis = report["analysis"]
        print(f"[INFO] {len(events)} beats, {len(bars)} bars, {len(markers)} exported markers; "
              f"{analysis['markers_requiring_review']} need review.")
        print(f"[INFO] {analysis['refined_beats']} bounded timing refinements; "
              f"video frame duration {1000 / float(rate.rate):.3f} ms.")
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
