"""Redis Streams consumer. One CPU analysis per worker, with a renewable lease."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import numpy as np
import redis
import soundfile as sf

STREAM = "beat:jobs"
GROUP = "detectors"
DATA = Path(os.environ.get("DATA_DIR", "/data"))
TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
MAX_DURATION = int(os.environ.get("MAX_AUDIO_SECONDS", "3600"))
LEASE_SECONDS = 60
RECLAIM_MS = 90000
MAX_ATTEMPTS = 3
STOP = threading.Event()
ACTIVE = None
RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def command(config, source, output):
    """Translate only allowlisted settings into argv; never invoke a shell."""
    args = [sys.executable, "-u", "-m", "app.detect_bars", str(source),
            "--output", str(output / "result.json"), "--edl", str(output / "markers.edl"),
            "--debug", str(output / "debug.png"), "--click", str(output / "clicks.wav"),
            "--pcm", str(output / "audio.wav"), "--device", "cpu"]
    values = {"backend": "backend", "markerType": "marker-type", "mode": "mode",
              "beatsPerBar": "beats-per-bar", "bpm": "bpm", "minBpm": "min-bpm", "maxBpm": "max-bpm",
              "transitionLambda": "transition-lambda", "activationThreshold": "activation-threshold",
              "checkpoint": "checkpoint", "minConfidence": "min-confidence", "maxSnapMs": "max-snap-ms",
              "silenceDb": "silence-db", "fps": "fps", "timelineStart": "timeline-start",
              "audioStart": "audio-start", "sourceIn": "source-in", "sourceOut": "source-out",
              "offsetMs": "offset-ms", "debugStart": "debug-start", "debugDuration": "debug-duration"}
    for key, flag in values.items():
        if config.get(key) is not None:
            args.extend(["--" + flag, str(config[key])])
    for key, flag in {"dropFrame": "drop-frame", "strict": "strict", "beatThisDbn": "beat-this-dbn"}.items():
        if config.get(key):
            args.append("--" + flag)
    for key, flag in {"refine": "no-refine", "percussive": "no-percussive", "librosaCheck": "no-librosa-check"}.items():
        if config.get(key) is False:
            args.append("--" + flag)
    return args


def create_preview(pcm, destination):
    """At most 4000 real waveform min/max pairs, streamed from the exported PCM."""
    peaks = []
    with sf.SoundFile(pcm) as audio:
        duration = len(audio) / audio.samplerate
        blocksize = max(1, math.ceil(len(audio) / 4000))
        for block in audio.blocks(blocksize=blocksize, always_2d=True):
            peaks.append([float(np.min(block)), float(np.max(block))])
    destination.write_text(json.dumps({"duration": duration, "secondsPerPeak": blocksize / audio.samplerate, "peaks": peaks}, allow_nan=False))


def terminate(process):
    if process and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            pass


def shutdown(*_):
    STOP.set()
    terminate(ACTIVE)


def update(client, key, **values):
    client.hset(key, mapping={**values, "updatedAt": now()})


def process_job(client, consumer, message_id, job_id):
    global ACTIVE
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", job_id):
        client.xack(STREAM, GROUP, message_id)
        client.xdel(STREAM, message_id)
        return
    key = f"beat:job:{job_id}"
    lock = f"beat:lease:{job_id}"
    token = str(uuid.uuid4())
    if not client.set(lock, token, nx=True, ex=LEASE_SECONDS):
        return
    directory = DATA / job_id
    staging = directory / f"work-{token}"
    terminal = False
    lease_lost = threading.Event()
    lease_done = threading.Event()

    def keep_lease():
        while not lease_done.wait(10):
            try:
                if not client.eval(RENEW, 1, lock, token, LEASE_SECONDS):
                    raise InterruptedError("Lease perduta")
                client.xclaim(STREAM, GROUP, consumer, 0, [message_id], justid=True)
            except (redis.RedisError, InterruptedError):
                lease_lost.set()
                terminate(ACTIVE)
                return

    keeper = threading.Thread(target=keep_lease, daemon=True)
    keeper.start()
    try:
        job = client.hgetall(key)
        if not job or job.get("status") in ("completed", "failed"):
            terminal = True
            return
        attempts = client.hincrby(key, "attempts", 1)
        if attempts > MAX_ATTEMPTS:
            update(client, key, status="failed", stage="Elaborazione interrotta", error="Numero massimo di tentativi raggiunto.", progress=0)
            terminal = True
            return
        update(client, key, status="running", stage="Decodifica e analisi audio", progress=10, error="")
        config = json.loads(job["config"])
        # Filenames are produced by the API; never trust a queue entry as a filesystem path.
        if not re.fullmatch(r"source\.(mp3|wav|flac|ogg|m4a|aac|aiff|aif|opus)", job["input"]):
            raise ValueError("Nome del file sorgente non valido.")
        source = directory / job["input"]
        duration = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
                                  check=True, capture_output=True, text=True, timeout=30)
        seconds = float(json.loads(duration.stdout)["format"]["duration"])
        if not math.isfinite(seconds) or not 0 < seconds <= MAX_DURATION:
            raise ValueError(f"Durata audio non valida o superiore a {MAX_DURATION} secondi.")
        staging.mkdir(parents=True)
        lines = queue.Queue()
        ACTIVE = subprocess.Popen(command(config, source, staging), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, start_new_session=True)

        def read_logs():
            for line in ACTIVE.stdout:
                lines.put(line)

        reader = threading.Thread(target=read_logs, daemon=True)
        reader.start()
        started = time.monotonic()
        tail = []
        with (staging / "analysis.log").open("w") as log:
            while ACTIVE.poll() is None or reader.is_alive() or not lines.empty():
                if STOP.is_set() or lease_lost.is_set():
                    raise InterruptedError("Worker in arresto")
                elapsed = time.monotonic() - started
                if elapsed > TIMEOUT:
                    raise TimeoutError(f"Tempo massimo di elaborazione superato ({TIMEOUT}s).")
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                log.write(line)
                log.flush()
                tail = (tail + [line.strip()])[-12:]
                if "Tracking with" in line:
                    update(client, key, stage="Rilevamento del ritmo", progress=35)
                elif "Checking onset" in line:
                    update(client, key, stage="Verifica e affinamento dei marker", progress=65)
            if STOP.is_set() or lease_lost.is_set():
                raise InterruptedError("Worker interrotto o lease perduta")
            if ACTIVE.returncode:
                detail = next((line.removeprefix("[ERROR] ") for line in reversed(tail) if "[ERROR]" in line), "Il rilevatore non è riuscito ad analizzare questo audio.")
                raise ValueError(detail)
        update(client, key, stage="Preparazione anteprima e download", progress=85)
        create_preview(staging / "audio.wav", staging / "preview.json")
        # Keep the audit report's output paths valid after the staging directory is moved.
        report_path = staging / "result.json"
        report = json.loads(report_path.read_text())
        for setting, name in {"output": "result.json", "edl": "markers.edl", "debug": "debug.png", "click": "clicks.wav", "pcm": "audio.wav"}.items():
            report["settings"][setting] = str(directory / "output" / name)
        report["audio"]["pcm_export_path"] = str(directory / "output" / "audio.wav")
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        with zipfile.ZipFile(staging / "result.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ("result.json", "markers.edl", "clicks.wav", "audio.wav", "debug.png", "preview.json", "analysis.log"):
                archive.write(staging / name, name)
        if STOP.is_set() or lease_lost.is_set() or not client.eval(RENEW, 1, lock, token, LEASE_SECONDS):
            raise InterruptedError("Lease del job scaduta")
        output = directory / "output"
        # A recovered attempt can replace outputs left by a crash before completion.
        if output.exists():
            shutil.rmtree(output)
        staging.rename(output)
        update(client, key, status="completed", stage="Elaborazione completata", progress=100)
        terminal = True
    except (redis.RedisError, InterruptedError):
        # Keep the pending message. A replacement worker recovers it after 90 seconds.
        terminate(ACTIVE)
        raise
    except Exception as exc:
        terminate(ACTIVE)
        print(f"Job {job_id} failed: {exc}", flush=True)
        if lease_lost.is_set() or STOP.is_set():
            raise InterruptedError("Worker interrotto o lease perduta") from exc
        if client.eval(RENEW, 1, lock, token, LEASE_SECONDS):
            message = str(exc)[:1000]
            if isinstance(exc, (subprocess.CalledProcessError, KeyError)):
                message = "File audio non decodificabile. Verifica che il file contenga audio valido."
            update(client, key, status="failed", stage="Elaborazione fallita", error=message, progress=0)
            terminal = True
    finally:
        lease_done.set()
        keeper.join(timeout=12)
        terminate(ACTIVE)
        ACTIVE = None
        shutil.rmtree(staging, ignore_errors=True)
        if terminal:
            client.xack(STREAM, GROUP, message_id)
            client.xdel(STREAM, message_id)
        client.eval(RELEASE, 1, lock, token)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True,
                                  socket_timeout=10, socket_connect_timeout=5)
    consumer = f"{socket.gethostname()}-{uuid.uuid4()}"

    def alive():
        while not STOP.is_set():
            try:
                client.set("beat:worker:alive", consumer, ex=30)
            except redis.RedisError:
                pass
            STOP.wait(10)

    threading.Thread(target=alive, daemon=True).start()
    print(f"Worker {consumer} ready", flush=True)
    while not STOP.is_set():
        try:
            try:
                client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            claimed = client.xautoclaim(STREAM, GROUP, consumer, RECLAIM_MS, start_id="0-0", count=1)
            entries = claimed[1]
            if not entries:
                batches = client.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1, block=5000)
                entries = batches[0][1] if batches else []
            for message_id, fields in entries:
                process_job(client, consumer, message_id, fields.get("jobId", ""))
        except (redis.RedisError, InterruptedError) as exc:
            print(f"Worker waiting for recovery: {exc}", flush=True)
            STOP.wait(5)
    client.close()


if __name__ == "__main__":
    main()
