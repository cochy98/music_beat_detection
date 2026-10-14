"""Generate preview and package in a subprocess covered by the job deadline."""
import json
from pathlib import Path
import sys
import zipfile
from worker.main import create_preview

def finish(staging, filename, sample_rate):
    create_preview(staging / "audio.wav", staging / "preview.json")
    report_path = staging / "result.json"
    report = json.loads(report_path.read_text())
    for setting, name in {"input": filename, "output": "result.json", "edl": "markers.edl", "debug": "debug.png", "click": "clicks.wav", "pcm": "audio.wav"}.items():
        report["settings"][setting] = name
    report["audio"].update(path=filename, original_sample_rate=sample_rate, pcm_export_path="audio.wav", decoder="ffmpeg + soundfile")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    with zipfile.ZipFile(staging / "result.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("result.json", "markers.edl", "clicks.wav", "audio.wav", "debug.png", "preview.json", "analysis.log"):
            archive.write(staging / name, name)

if __name__ == "__main__":
    finish(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]))
