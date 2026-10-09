import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from app.audio import Audio, Evidence, analyze_attacks, load_audio
from app.detect_bars import main, parse_arguments
from app.evaluate import evaluate, match_events
from app.export import create_click_track, create_edl, create_pcm_copy
from app.markers import assess_beats, build_bars, place_markers, refine_time
from app.timecode import FrameRate
from app.tracking import Tracking, dbn_activations, positions_from_downbeats


def evidence(attacks=(), strengths=None, silent=False):
    attacks = np.array(attacks, dtype=float)
    return Evidence(np.arange(0, 8, .01), np.ones(800),
                    np.full(800, 0.0 if silent else 0.1), attacks,
                    np.ones(len(attacks)) if strengths is None else np.asarray(strengths),
                    1e-4, sample_rate=100, hop_length=1)


def tracking(times, positions=None, probability=0.9):
    times = np.asarray(times, dtype=float)
    positions = np.arange(len(times)) % 4 + 1 if positions is None else np.asarray(positions)
    activation = np.full(800, probability)
    return Tracking(times, positions, activation, activation, 100, 'test')


def config(*args):
    return parse_arguments(['source.wav', *args])


class TimecodeTests(unittest.TestCase):
    def test_fractional_start_has_no_hour_offset(self):
        for fps, nominal in [('23.976', 24), ('29.97', 30), ('59.94', 60)]:
            with self.subTest(fps=fps):
                rate = FrameRate.parse(fps)
                start = rate.parse_timecode('01:00:00:00')
                self.assertEqual(start, nominal * 3600)
                self.assertEqual(rate.format_timecode(start), '01:00:00:00')
                frames = rate.seconds_to_frames(float(1000 / rate.rate))
                self.assertEqual(frames, 1000)
                self.assertEqual(rate.parse_timecode(rate.format_timecode(start + frames)) - start, 1000)

    def test_drop_frame_boundaries(self):
        rate = FrameRate.parse('29.97', True)
        self.assertEqual(rate.format_timecode(1799), '00:00:59;29')
        self.assertEqual(rate.format_timecode(1800), '00:01:00;02')
        self.assertEqual(rate.format_timecode(17982), '00:10:00;00')
        self.assertEqual(rate.format_timecode(107892), '01:00:00;00')
        fast = FrameRate.parse('59.94', True)
        self.assertEqual(fast.format_timecode(3600), '00:01:00;04')
        self.assertEqual(fast.format_timecode(35964), '00:10:00;00')

    def test_roundtrips_across_hours_and_minute_boundaries(self):
        rng = np.random.default_rng(12)
        for fps, drop in [('25', False), ('24000/1001', False), ('30000/1001', True), ('60000/1001', True)]:
            rate = FrameRate.parse(fps, drop)
            for frame in [0, 1, 1798, 1799, 1800, 17981, 17982, 107891, 107892, *rng.integers(0, 1_000_000, 100)]:
                with self.subTest(fps=fps, frame=frame):
                    self.assertEqual(rate.parse_timecode(rate.format_timecode(int(frame))), frame)

    def test_invalid_rates_and_labels(self):
        for fps in ['0', '-25', 'nan', 'inf', '23.98', '24.5']:
            with self.subTest(fps=fps), self.assertRaises(ValueError):
                FrameRate.parse(fps)
        with self.assertRaises(ValueError):
            FrameRate.parse('25', True)
        for label in ['01:60:00:00', '00:00:60:00', '00:00:00:25', '-1:00:00:00', '00:00:00;00']:
            with self.subTest(label=label), self.assertRaises(ValueError):
                FrameRate.parse('25').parse_timecode(label)
        with self.assertRaises(ValueError):
            FrameRate.parse('29.97', True).parse_timecode('00:01:00;00')

    def test_nearest_frame_error_and_ties(self):
        rate = FrameRate.parse('25')
        self.assertEqual(rate.seconds_to_frames(.02), 1)
        self.assertEqual(rate.seconds_to_frames(-.02), -1)
        for time in np.linspace(0, 3600, 1000):
            self.assertLessEqual(abs(float(rate.seconds_to_frames(float(time)) / rate.rate) - time), .02000001)


class RefinementTests(unittest.TestCase):
    def test_sigmoid_extremes_leave_valid_nonbeat_probability_for_dbn(self):
        activations = dbn_activations([0, 1, 1, 0, .6], [0, 1, 0, 1, .2])
        self.assertTrue(np.all(activations > 0))
        self.assertTrue(np.all(activations.sum(axis=1) < 1))
        self.assertTrue(np.all(np.isfinite(np.log(1 - activations.sum(axis=1)))))
        np.testing.assert_allclose(activations[-1], [.4, .2])

    def test_corrects_early_and_late_predictions(self):
        ev = evidence([1.0, 1.5, 2.0, 2.5])
        tr = tracking([1.025, 1.478, 2.027, 2.485])
        events = assess_beats(tr, ev, 4, config())
        np.testing.assert_allclose([event['source_start_seconds'] for event in events], ev.attacks)
        self.assertTrue(all(event['attack_confirmed'] for event in events))
        self.assertTrue(all(abs(event['refinement_ms']) <= 50 for event in events))

    def test_rejects_distant_or_weak_attacks(self):
        self.assertEqual(refine_time(1, .5, evidence([1.10]), .05)[0], 1)
        self.assertEqual(refine_time(1, .5, evidence([1.01], [.2]), .05)[0], 1)

    def test_zero_snap_still_confirms_an_exact_attack(self):
        result = refine_time(1, .5, evidence([1]), 0)
        self.assertEqual(result[1], 1)
        self.assertFalse(result[3])

    def test_ambiguous_flams_do_not_move_marker(self):
        result = refine_time(1, .5, evidence([.98, 1.02]), .05)
        self.assertEqual(result[0], 1)
        self.assertTrue(result[3])
        event = assess_beats(tracking([1, 1.5]), evidence([.98, 1.02, 1.5]), 3, config())[0]
        self.assertTrue(event['needs_review'])
        self.assertFalse(event['attack_confirmed'])

    def test_fast_pulse_limits_snap_and_preserves_order(self):
        tr = tracking([1, 1.2, 1.4, 1.6])
        ev = evidence([1.04, 1.19, 1.42, 1.61])
        events = assess_beats(tr, ev, 3, config('--max-snap-ms', '150'))
        times = np.array([event['source_start_seconds'] for event in events])
        self.assertTrue(np.all(np.diff(times) > 0))
        self.assertTrue(np.all(np.abs(times - tr.times) <= .02000001))

    def test_no_refine_is_a_true_model_time_baseline(self):
        tr = tracking([1.03, 1.53, 2.03])
        events = assess_beats(tr, evidence([1, 1.5, 2]), 3, config('--no-refine'))
        np.testing.assert_allclose([event['source_start_seconds'] for event in events], tr.times)
        self.assertTrue(all(event['refinement_ms'] == 0 for event in events))

    def test_silence_and_model_disagreement_are_audited(self):
        tr = tracking([1, 1.5, 2])
        secondary = tracking([1.2, 1.7, 2.2])
        events = assess_beats(tr, evidence(silent=True), 3, config(), secondary)
        self.assertTrue(all(event['confidence'] <= .15 for event in events))
        self.assertIn('neural_trackers_disagree', events[0]['quality_flags'])
        self.assertIn('silence_or_very_low_energy', events[0]['quality_flags'])

    def test_quiet_attacks_need_close_agreement_of_both_models(self):
        tr = tracking([1, 1.5])
        ev = evidence([1.005, 1.535], [.3, .3])
        alone = assess_beats(tr, ev, 3, config())
        agreed = assess_beats(tr, ev, 3, config(), secondary=tracking([1, 1.5]))
        self.assertFalse(alone[0]['attack_confirmed'])
        self.assertTrue(agreed[0]['attack_confirmed'])
        self.assertFalse(agreed[1]['attack_confirmed'])

    def test_bar_identity_and_pickup_are_preserved(self):
        beats = np.arange(.2, 3, .5)
        positions = positions_from_downbeats(beats, [1.2, 2.7])
        np.testing.assert_array_equal(positions, [0, 0, 1, 2, 3, 1])
        events = assess_beats(tracking(beats, positions), evidence(beats), 3.1, config())
        bars = build_bars(events, 3.1)
        self.assertEqual([bar['bar'] for bar in bars], [1, 2])
        self.assertAlmostEqual(bars[-1]['source_end_seconds'], 3.1)
        self.assertTrue(bars[-1]['partial_final_bar'])


class PlacementTests(unittest.TestCase):
    def test_clip_position_trim_and_offset_at_fractional_rate(self):
        cfg = config('--fps', '23.976', '--audio-start', '01:00:10:00', '--source-in', '1',
                     '--source-out', '3', '--offset-ms', '20', '--marker-type', 'beats')
        tr = tracking([.5, 1.5, 2, 3])
        events = assess_beats(tr, evidence(tr.times), 4, cfg)
        rate = FrameRate.parse(cfg.fps)
        markers = place_markers(events, rate, cfg, 4)
        self.assertEqual(len(markers), 2)
        self.assertEqual(markers[0]['timeline_frame'], rate.parse_timecode(cfg.audio_start) + rate.seconds_to_frames(.52))
        self.assertEqual(events[0]['export_status'], 'outside_clip')
        self.assertEqual(events[-1]['export_status'], 'outside_clip')
        self.assertLessEqual(abs(markers[0]['quantization_error_ms']), 1000 / float(rate.rate) / 2)

    def test_attack_and_strict_modes_do_not_export_unsupported_beats(self):
        tr = tracking([1, 1.5, 2])
        for args in [('--mode', 'attacks'), ('--strict',)]:
            cfg = config('--marker-type', 'beats', *args)
            events = assess_beats(tr, evidence([1, 2]), 3, cfg)
            markers = place_markers(events, FrameRate.parse('25'), cfg, 3)
            self.assertEqual([event['beat'] for event in markers], [1, 3])
            self.assertEqual(markers[-1]['bar'], 1)

    def test_duplicate_video_frames_keep_stronger_marker(self):
        cfg = config('--marker-type', 'beats', '--no-refine')
        events = assess_beats(tracking([1, 1.01]), evidence([1]), 2, cfg)
        events[0]['needs_review'], events[1]['needs_review'] = False, False
        events[0]['confidence'], events[1]['confidence'] = .7, .9
        markers = place_markers(events, FrameRate.parse('25'), cfg, 2)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['beat'], 2)
        self.assertEqual(events[0]['export_status'], 'duplicate_frame')

    def test_negative_offset_does_not_clamp_marker_to_clip_start(self):
        cfg = config('--marker-type', 'beats', '--offset-ms', '-50')
        events = assess_beats(tracking([.01, .5]), evidence([.01, .5]), 1, cfg)
        markers = place_markers(events, FrameRate.parse('25'), cfg, 1)
        self.assertEqual(len(markers), 1)
        self.assertEqual(events[0]['export_status'], 'offset_outside_clip')

    def test_rounding_cannot_put_marker_after_clip_end(self):
        cfg = config('--marker-type', 'beats', '--no-refine')
        events = assess_beats(tracking([1.025]), evidence([1.025]), 1.03, cfg)
        self.assertEqual(place_markers(events, FrameRate.parse('25'), cfg, 1.03), [])
        self.assertEqual(events[0]['export_status'], 'quantized_outside_clip')

    def test_edl_one_frame_duration_and_review_colors(self):
        cfg = config('--marker-type', 'beats', '--fps', '29.97', '--drop-frame')
        events = assess_beats(tracking([.5, 1]), evidence([.5]), 2, cfg)
        rate = FrameRate.parse(cfg.fps, True)
        markers = place_markers(events, rate, cfg, 2)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'markers.edl'
            create_edl(markers, path, rate)
            lines = path.read_text().splitlines()
            self.assertIn('FCM: DROP FRAME', lines)
            self.assertIn('ResolveColorYellow', path.read_text())
            for line in lines:
                if line[:3].isdigit():
                    codes = line.split()[4:8]
                    self.assertEqual(rate.parse_timecode(codes[1]) - rate.parse_timecode(codes[0]), 1)

    def test_clicks_use_quantized_export_positions(self):
        cfg = config('--marker-type', 'beats')
        events = assess_beats(tracking([.513]), evidence([.513]), 1, cfg)
        markers = place_markers(events, FrameRate.parse('25'), cfg, 1)
        audio = Audio(np.zeros(44100, dtype=np.float32), 44100, 44100, 'test', [])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'click.wav'
            create_click_track(audio, markers, path)
            samples, sr = sf.read(path)
            self.assertEqual(sr, 44100)
            start = round(markers[0]['export_source_seconds'] * sr)
            self.assertEqual(np.flatnonzero(np.abs(samples) > 1e-5)[0], start)


class AudioTests(unittest.TestCase):
    def test_transient_detection_recovers_known_attacks_without_silence_peaks(self):
        sr = 44100
        times = np.array([.5, 1, 1.5, 2])
        samples = np.zeros(sr * 3, dtype=np.float32)
        rng = np.random.default_rng(7)
        for i, time in enumerate(times):
            length = round(.08 * sr)
            t = np.arange(length) / sr
            hit = (.6 * np.cos(2 * np.pi * (80 if i % 2 else 1500) * t)
                   + .15 * rng.standard_normal(length)) * np.exp(-t / .015)
            start = round(time * sr)
            samples[start:start + length] = hit
        audio = Audio(samples, sr, sr, 'test', [])
        ev = analyze_attacks(audio)
        self.assertTrue(len(ev.attacks))
        for time in times:
            self.assertLess(float(np.min(np.abs(ev.attacks - time))), .008)
        self.assertTrue(np.all(ev.attacks > .4))
        self.assertTrue(np.all(ev.attacks < 2.2))
        raw = times + .024
        events = assess_beats(tracking(raw), ev, 3, config())
        refined = np.array([event['source_start_seconds'] for event in events])
        self.assertLess(np.mean(np.abs(refined - times)), np.mean(np.abs(raw - times)))
        self.assertLess(np.max(np.abs(refined - times)), .008)

    def test_silent_audio_produces_no_attacks(self):
        audio = Audio(np.zeros(44100, dtype=np.float32), 44100, 44100, 'test', [])
        ev = analyze_attacks(audio)
        self.assertEqual(len(ev.attacks), 0)
        self.assertTrue(np.all(ev.rms == 0))

    def test_stereo_cancellation_and_resampling_keep_source_duration(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'phase.wav'
            samples = np.sin(2 * np.pi * 200 * np.arange(48000) / 48000).astype(np.float32) * .3
            sf.write(path, np.column_stack((samples, -samples)), 48000, subtype='FLOAT')
            audio = load_audio(path)
            self.assertTrue(audio.warnings)
            self.assertEqual(audio.sample_rate, 44100)
            self.assertAlmostEqual(audio.duration, 1, places=3)
            self.assertGreater(np.max(np.abs(audio.samples)), .25)
            self.assertEqual(audio.playback_samples.shape, (44100, 2))
            pcm = Path(root) / 'decoded.wav'
            create_pcm_copy(audio, pcm)
            decoded, decoded_sr = sf.read(pcm, always_2d=True)
            self.assertEqual(decoded_sr, 44100)
            np.testing.assert_allclose(decoded, audio.playback_samples, atol=1e-7)

    def test_silent_cli_exports_finite_empty_report_without_running_models(self):
        with tempfile.TemporaryDirectory() as root:
            path, report, edl = (Path(root) / name for name in ['silence.wav', 'silence.json', 'silence.edl'])
            sf.write(path, np.zeros(44100), 44100)
            with patch('app.detect_bars.run_tracking', side_effect=AssertionError('silent input must not run models')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main([str(path), '--output', str(report), '--edl', str(edl)]), 0)
            data = json.loads(report.read_text())
            self.assertEqual(data['markers'], [])
            self.assertIsNone(data['analysis']['estimated_bpm'])
            self.assertNotIn('NaN', report.read_text())

    def test_auxiliary_crash_preserves_primary_detection_and_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            path, report = (Path(root) / name for name in ['music.wav', 'music.json'])
            sf.write(path, np.sin(np.arange(44100) * .1) * .1, 44100)
            with patch('app.detect_bars.run_tracking', return_value=(tracking([.2, .7]), None)):
                with patch('app.detect_bars.librosa_verification', side_effect=RuntimeError('Auxiliary process failed')):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main([str(path), '--output', str(report), '--marker-type', 'beats']), 0)
            data = json.loads(report.read_text())
            self.assertEqual(data['analysis']['beats_detected'], 2)
            self.assertEqual(data['analysis']['warnings'], ['Auxiliary process failed'])


class EvaluationTests(unittest.TestCase):
    def test_one_reference_cannot_match_multiple_detections(self):
        metrics = evaluate([1, 2], [1, 1.01, 2], .02)
        self.assertEqual(metrics['matched_events'], 2)
        self.assertEqual(metrics['false_positives'], 1)
        self.assertEqual(metrics['mean_absolute_error_ms'], 0)
        self.assertAlmostEqual(metrics['precision'], 2 / 3)

    def test_assignment_maximizes_matches_before_minimizing_error(self):
        pairs = match_events(np.array([1, 1.08]), np.array([.96, 1.03]), .06)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], (1, .96))
        self.assertEqual(pairs[1], (1.08, 1.03))

    def test_empty_reference_and_missed_events_are_explicit(self):
        metrics = evaluate([1, 2], [], .02)
        self.assertEqual(metrics['missed_events'], 2)
        self.assertIsNone(metrics['mean_absolute_error_ms'])
        self.assertEqual(evaluate([], [], .02)['matched_events'], 0)

    def test_invalid_annotations_and_options_are_rejected(self):
        for times in [[float('nan')], [-1], [float('inf')]]:
            with self.assertRaises(ValueError):
                evaluate(times, [1], .02)
        for options in [('--min-confidence', 'nan'), ('--max-snap-ms', '-1'),
                        ('--beats-per-bar', '1'), ('--min-bpm', '200', '--max-bpm', '100')]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                config(*options)


if __name__ == '__main__':
    unittest.main()
