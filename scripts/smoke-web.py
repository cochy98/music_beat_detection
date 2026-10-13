#!/usr/bin/env python3
"""Exercise real REST, SSE, Python analysis, waveform and downloads (stdlib only)."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
import uuid
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--url", default="http://localhost:8088")
    parser.add_argument("--backend", choices=["madmom", "beat-this", "ensemble"], default="madmom")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--restart-worker", action="store_true", help="Restart this Compose worker during analysis to verify recovery")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    def get(path, headers=None):
        return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}), timeout=30)

    with get("/api/health") as response:
        assert json.load(response)["workerOnline"], "Worker offline"
    boundary = uuid.uuid4().hex
    config = json.dumps({"backend": args.backend, "markerType": "beats", "debugDuration": 12})
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="config"\r\n\r\n{config}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="{args.audio.name}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + args.audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(base + "/api/jobs", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 202
        job = json.load(response)
    job_id = job["id"]
    print(f"Job {job_id} queued ({args.backend})", flush=True)
    events, errors = [], []

    def listen():
        try:
            with urllib.request.urlopen(base + f"/api/jobs/{job_id}/events", timeout=args.timeout) as response:
                assert response.headers["Content-Type"].startswith("text/event-stream")
                for line in response:
                    if line.startswith(b"data: "):
                        events.append(json.loads(line[6:])["status"])
        except Exception as exc:
            errors.append(str(exc))

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    deadline = time.monotonic() + args.timeout
    previous = ""
    restarted = False
    while time.monotonic() < deadline:
        with get(f"/api/jobs/{job_id}") as response:
            job = json.load(response)
        stage = job["stage"]
        if stage != previous:
            print(f'{job["status"]}: {stage}', flush=True)
            previous = stage
        if job["status"] in ("completed", "failed"):
            break
        if args.restart_worker and not restarted and job["status"] == "running":
            print("Restarting worker to verify pending job recovery…", flush=True)
            subprocess.run(["docker", "compose", "restart", "worker"], check=True)
            restarted = True
        time.sleep(2)
    assert job["status"] == "completed", job
    if args.restart_worker:
        assert restarted and job["attempts"] >= 2, "Job was not recovered"
    listener.join(timeout=5)
    assert not errors, errors
    assert "completed" in events, f"No terminal SSE notification: {events}"
    with get(f"/api/jobs/{job_id}/result") as response:
        report = json.load(response)
    with get(f"/api/jobs/{job_id}/preview") as response:
        preview = json.load(response)
    assert report["schema_version"] == 2
    assert len(report["markers"]) > 0, "No markers: choose a rhythmic test file"
    assert 0 < len(preview["peaks"]) <= 4000
    assert abs(preview["duration"] - report["audio"]["duration_seconds"]) < 0.001
    with get(f"/api/jobs/{job_id}/files/pcm", {"Range": "bytes=0-127"}) as response:
        assert response.status == 206
        assert len(response.read()) == 128
        assert response.headers["Content-Range"].startswith("bytes 0-127/")
    with get(f"/api/jobs/{job_id}/files/zip") as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    assert {"result.json", "markers.edl", "audio.wav", "clicks.wav", "debug.png", "preview.json", "analysis.log"} == set(archive.namelist())
    assert archive.testzip() is None
    print(json.dumps({"jobId": job_id, "markers": len(report["markers"]), "attempts": job["attempts"], "sseStates": events, "zip": "valid", "range": "valid"}), flush=True)


if __name__ == "__main__":
    main()
