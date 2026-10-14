import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from worker.main import command, create_preview


class WorkerTest(unittest.TestCase):
    def test_cli_mapping_and_no_shell(self):
        args = command({"backend": "ensemble", "markerType": "beats", "strict": True,
                        "dropFrame": True, "beatThisDbn": True, "refine": False,
                        "percussive": False, "librosaCheck": False, "sourceIn": 0,
                        "sourceOut": 5, "offsetMs": -40, "audioStart": "01:00:00:00"},
                       Path("/data/a song.wav"), Path("/data/output"))
        self.assertIn("/data/a song.wav", args)
        for flag in ("--backend", "--marker-type", "--strict", "--drop-frame", "--beat-this-dbn",
                     "--no-refine", "--no-percussive", "--no-librosa-check", "--source-in", "--source-out", "--offset-ms", "--audio-start"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--offset-ms") + 1], "-40")
        self.assertEqual(args[args.index("--device") + 1], "cpu")

    def test_preview_is_bounded_and_comes_from_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source.wav", Path(tmp) / "preview.json"
            samples = np.zeros((20000, 2))
            samples[100] = [0.8, -0.8]
            sf.write(source, samples, 10000, subtype="FLOAT")
            create_preview(source, output)
            preview = json.loads(output.read_text())
            self.assertEqual(preview["duration"], 2)
            self.assertEqual(preview["secondsPerPeak"], 0.0005)
            self.assertLessEqual(len(preview["peaks"]), 4000)
            self.assertAlmostEqual(max(p[1] for p in preview["peaks"]), 0.8)
            self.assertAlmostEqual(min(p[0] for p in preview["peaks"]), -0.8)



class SafetyTests(unittest.TestCase):
    def test_probe_rejects_unbounded_audio(self):
        from worker.main import validate_probe
        valid = {"format": {"duration": "10"}, "streams": [{"codec_type": "audio", "channels": 2, "sample_rate": "44100"}]}
        self.assertEqual(validate_probe(valid), (2, 44100))
        for duration, channels, rate in [("nan", 2, "44100"), ("999999", 2, "44100"), ("10", 8, "44100"), ("10", 2, "384000")]:
            bad = {"format": {"duration": duration}, "streams": [{"codec_type": "audio", "channels": channels, "sample_rate": rate}]}
            with self.assertRaises(ValueError):
                validate_probe(bad)

    def test_deadline_terminates_a_stalled_stage(self):
        import sys, time
        from worker.main import run_stage
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            run_stage([sys.executable, "-c", "import time; time.sleep(30)"], started + 0.1)
        self.assertLess(time.monotonic() - started, 6)


if __name__ == "__main__":
    unittest.main()
