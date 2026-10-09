"""Conservative transient refinement, quality flags, and timeline placement."""

import math

import numpy as np

from .tracking import local_periods


def nearest_distance(times, time):
    if not len(times):
        return None
    index = np.searchsorted(times, time)
    neighbors = times[max(0, index - 1):min(len(times), index + 1)]
    return float(np.min(np.abs(neighbors - time)))


def refine_time(time, period, evidence, max_snap, enabled=True, min_strength=0.45):
    tolerance = min(max_snap, 0.1 * period)
    a = np.searchsorted(evidence.attacks, time - tolerance, side="left")
    b = np.searchsorted(evidence.attacks, time + tolerance, side="right")
    candidates = []
    for index in range(a, b):
        strength = float(evidence.strengths[index])
        if strength < min_strength:
            continue
        distance = abs(float(evidence.attacks[index]) - time)
        # Quiet attacks require close timing plus strong agreement of both neural models.
        if strength < 0.45 and distance > min(0.020, 0.04 * period):
            continue
        merit = strength * math.exp(-0.5 * (distance / max(tolerance * 0.65, 1e-6)) ** 2)
        candidates.append((merit, index))
    if not candidates:
        return time, None, 0.0, False
    candidates.sort(reverse=True)
    _, best = candidates[0]
    ambiguous = (len(candidates) > 1 and candidates[1][0] >= 0.85 * candidates[0][0]
                 and abs(evidence.attacks[best] - evidence.attacks[candidates[1][1]]) > 0.012)
    target = float(evidence.attacks[best])
    refined = target if enabled and not ambiguous else time
    return refined, target, float(evidence.strengths[best]), ambiguous


def assess_beats(tracking, evidence, duration, config, secondary=None, librosa_times=None):
    periods = local_periods(tracking.times)
    events = []
    bar_number = 0
    for index, (raw_time, position, period) in enumerate(zip(tracking.times, tracking.positions, periods)):
        if position == 1:
            bar_number += 1
        model_score = tracking.probability_at(float(raw_time), downbeat=position == 1)
        reference_times = []
        if secondary is not None:
            reference_times = secondary.downbeats if position == 1 else secondary.times
        reference_distance = nearest_distance(reference_times, float(raw_time))
        min_strength = (0.25 if model_score >= 0.75 and reference_distance is not None
                        and reference_distance <= 0.03 and secondary.probability_at(float(raw_time), position == 1) >= 0.3 else 0.45)
        refined, attack, strength, ambiguous = refine_time(
            float(raw_time), float(period), evidence, config.max_snap_ms / 1000,
            not config.no_refine, min_strength,
        )
        # A bounded snap must also keep distance from the adjacent model predictions.
        lower = (tracking.times[index - 1] + raw_time) / 2 if index else 0
        upper = (tracking.times[index + 1] + raw_time) / 2 if index + 1 < len(tracking.times) else duration
        if not lower <= refined < upper:
            refined, ambiguous = float(raw_time), True
        flags = []
        energy = evidence.energy_at(refined)
        silent = energy < evidence.silence_threshold
        if silent:
            flags.append("silence_or_very_low_energy")
        if ambiguous:
            flags.append("ambiguous_attacks")
        if attack is None:
            flags.append("no_nearby_attack")
        if model_score < 0.3:
            flags.append("weak_model_activation")
        neighboring = np.diff(tracking.times[max(0, index - 1):min(len(tracking.times), index + 2)])
        deviation = float(np.max(np.abs(neighboring / period - 1), initial=0))
        rhythm_score = math.exp(-3 * deviation)
        if deviation > 0.25:
            flags.append("irregular_local_pulse")
        # Penalize corrections that introduce a large discontinuity compared with raw intervals.
        if refined != raw_time and deviation < 0.15:
            new_deviation = max(abs((refined - tracking.times[index - 1]) / period - 1) if index else 0,
                                abs((tracking.times[index + 1] - refined) / period - 1) if index + 1 < len(tracking.times) else 0)
            if new_deviation > max(0.15, deviation + 0.08):
                refined = float(raw_time)
                flags.append("refinement_rejected_by_rhythm")
        checks, distances = [], {}
        tolerance = min(0.07, 0.15 * period)
        if secondary is not None:
            reference = secondary.downbeats if position == 1 else secondary.times
            distance = nearest_distance(reference, float(raw_time))
            distances[secondary.backend] = distance
            checks.append(0.0 if distance is None else math.exp(-0.5 * (distance / tolerance) ** 2))
            if distance is None or distance > tolerance:
                flags.append("neural_trackers_disagree")
        if librosa_times is not None and len(librosa_times):
            distance = nearest_distance(librosa_times, refined)
            distances["librosa_tempo_guided"] = distance
            # Guided librosa verifies timing only, not downbeat identity.
            checks.append(math.exp(-0.5 * (distance / tolerance) ** 2))
        attack_score = 0.0 if attack is None else strength * math.exp(-0.5 * ((refined - attack) / 0.025) ** 2)
        confidence = (0.45 * model_score + 0.30 * attack_score + 0.15 * rhythm_score + 0.10 * np.mean(checks)
                      if checks else 0.50 * model_score + 0.30 * attack_score + 0.20 * rhythm_score)
        if silent:
            confidence = min(confidence, 0.15)
        if ambiguous:
            confidence = min(confidence, 0.55)
        confirmed = attack is not None and not ambiguous and not silent and abs(refined - attack) <= 0.025
        events.append({
            "beat": index + 1, "beat_position": int(position), "bar": bar_number or None,
            "is_downbeat": bool(position == 1), "original_source_seconds": float(raw_time),
            "source_start_seconds": float(refined), "refinement_ms": float((refined - raw_time) * 1000),
            "nearest_attack_seconds": attack, "attack_strength": strength, "attack_confirmed": bool(confirmed),
            "attack_strength_threshold": min_strength,
            "model_activation": model_score, "local_bpm": float(60 / period),
            "rhythm_score": float(rhythm_score), "rms_dbfs": float(20 * np.log10(max(energy, 1e-12))),
            "tracker_distance_ms": {key: None if value is None else value * 1000 for key, value in distances.items()},
            "confidence": float(confidence), "quality_flags": flags,
            "needs_review": bool(flags or confidence < config.min_confidence),
        })
    return events


def build_bars(events, duration):
    downbeats = [event for event in events if event["is_downbeat"]]
    bars = []
    for i, event in enumerate(downbeats):
        end = downbeats[i + 1]["source_start_seconds"] if i + 1 < len(downbeats) else duration
        if end <= event["source_start_seconds"]:
            continue
        bar = dict(event)
        bar.update(source_end_seconds=float(end), duration_seconds=float(end - event["source_start_seconds"]),
                   partial_final_bar=i + 1 == len(downbeats))
        bars.append(bar)
    return bars


def place_markers(events, rate, config, duration):
    """Apply clip trim/offset, round once, and retain every exclusion reason."""
    timeline_start = rate.parse_timecode(config.timeline_start)
    clip_start = rate.parse_timecode(config.audio_start or config.timeline_start)
    if clip_start < timeline_start:
        raise ValueError("--audio-start must not precede --timeline-start.")
    source_out = duration if config.source_out is None else config.source_out
    if not 0 <= config.source_in < source_out <= duration + 1e-6:
        raise ValueError("Require 0 <= --source-in < --source-out <= audio duration.")
    selected = []
    for event in events:
        event["export_status"] = "not_selected"
        if config.marker_type == "downbeats" and not event["is_downbeat"]:
            continue
        time = event["source_start_seconds"]
        if not config.source_in <= time < source_out:
            event["export_status"] = "outside_clip"
            continue
        if config.mode == "attacks" and not event["attack_confirmed"]:
            event["export_status"] = "no_confirmed_attack"
            continue
        if config.strict and (event["needs_review"] or event["confidence"] < config.min_confidence):
            event["export_status"] = "requires_review"
            continue
        relative = time - config.source_in + config.offset_ms / 1000
        if relative < 0 or relative >= source_out - config.source_in:
            event["export_status"] = "offset_outside_clip"
            continue
        offset_frames = rate.seconds_to_frames(relative)
        if float(offset_frames / rate.rate) >= source_out - config.source_in:
            event["export_status"] = "quantized_outside_clip"
            continue
        frame = clip_start + offset_frames
        # Validate both marker and its one-frame EDL end before writing anything.
        timecode = rate.format_timecode(frame)
        rate.format_timecode(frame + 1)
        event.update(
            timeline_frame=frame, timeline_timecode=timecode,
            timeline_start_seconds=float(frame / rate.rate),
            export_source_seconds=float(config.source_in + offset_frames / rate.rate),
            quantization_error_ms=float((offset_frames / rate.rate - relative) * 1000),
            marker_color="ResolveColorYellow" if event["needs_review"] else "ResolveColorBlue",
            export_status="exported",
        )
        selected.append(event)
    # Several sub-frame detections may target one video frame; keep the better supported one.
    by_frame = {}
    for event in selected:
        frame = event["timeline_frame"]
        previous = by_frame.get(frame)
        if previous is None:
            by_frame[frame] = event
        elif (not event["needs_review"], event["confidence"]) > (not previous["needs_review"], previous["confidence"]):
            previous["export_status"] = "duplicate_frame"
            by_frame[frame] = event
        else:
            event["export_status"] = "duplicate_frame"
    return [by_frame[frame] for frame in sorted(by_frame)]
