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
from worker.lifecycle import finalize

import numpy as np
import redis
import soundfile as sf

STREAM = "beat:jobs"
GROUP = "detectors"
DATA = Path(os.environ.get("DATA_DIR", "/data"))
TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "600"))
MAX_DURATION = int(os.environ.get("MAX_AUDIO_SECONDS", "300"))
LEASE_SECONDS = 60
RECLAIM_MS = 90000
MAX_ATTEMPTS = 3
MAX_CHANNELS = int(os.environ.get("MAX_AUDIO_CHANNELS", "2"))
MAX_SAMPLE_RATE = int(os.environ.get("MAX_SAMPLE_RATE", "96000"))
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


def run_stage(args, deadline):
    """All external stages share one wall-clock deadline and shutdown handling."""
    global ACTIVE
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Tempo massimo di elaborazione superato.")
    ACTIVE = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = ACTIVE.communicate(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        terminate(ACTIVE)
        raise TimeoutError("Tempo massimo di elaborazione superato.") from exc
    if STOP.is_set():
        raise InterruptedError("Worker in arresto")
    if ACTIVE.returncode:
        raise ValueError("File audio non decodificabile o fase di elaborazione fallita.")
    return stdout


def validate_probe(probe):
    seconds = float(probe["format"]["duration"])
    audio = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if len(audio) != 1:
        raise ValueError("Il file deve contenere una sola traccia audio.")
    channels, rate = int(audio[0]["channels"]), int(audio[0]["sample_rate"])
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_DURATION:
        raise ValueError(f"Durata audio non valida o superiore a {MAX_DURATION} secondi.")
    if not 1 <= channels <= MAX_CHANNELS or not 8000 <= rate <= MAX_SAMPLE_RATE:
        raise ValueError(f"Audio consentito: fino a {MAX_CHANNELS} canali e {MAX_SAMPLE_RATE} Hz.")
    return channels, rate


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
    deadline = time.monotonic() + TIMEOUT
    lease_lost = threading.Event()
    lease_done = threading.Event()
    log_done = threading.Event()
    reader = None

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
            terminal = finalize(client, job_id, token, "failed", "Elaborazione interrotta", 0, "Numero massimo di tentativi raggiunto.")
            return
        update(client, key, status="running", stage="Decodifica e analisi audio", progress=10, error="")
        config = json.loads(job["config"])
        if os.environ.get("ALLOW_LARGE_MODEL") == "false" and config.get("backend") != "madmom" and config.get("checkpoint", "small0") == "final0":
            raise ValueError("Il modello final0 non è disponibile su questo server. Usa small0.")
        # Filenames are produced by the API; never trust a queue entry as a filesystem path.
        if not re.fullmatch(r"source\.(mp3|wav|flac|ogg|m4a|aac|aiff|aif|opus)", job["input"]):
            raise ValueError("Nome del file sorgente non valido.")
        source = directory / job["input"]
        formats = "mp3,wav,flac,ogg,mov,aac,aiff"
        probe = json.loads(run_stage(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", formats,
                                     "-show_entries", "format=duration:stream=codec_type,channels,sample_rate", "-of", "json", str(source)], min(deadline, time.monotonic() + 30)))
        channels, original_rate = validate_probe(probe)
        staging.mkdir(parents=True)
        decoded = staging / "decoded.wav"
        run_stage(["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-format_whitelist", formats,
                   "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", str(MAX_DURATION + 1),
                   "-ar", "44100", "-ac", str(channels), "-c:a", "pcm_f32le", str(decoded)], deadline)
        with sf.SoundFile(decoded) as normalized:
            if len(normalized) / normalized.samplerate > MAX_DURATION:
                raise ValueError("Durata reale audio superiore al limite.")
        if lease_lost.is_set():
            raise InterruptedError("Lease perduta durante la decodifica")
        lines = queue.Queue(maxsize=1024)
        ACTIVE = subprocess.Popen(command(config, decoded, staging), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, start_new_session=True)

        detector = ACTIVE
        def read_logs():
            for line in detector.stdout:
                while not STOP.is_set() and not lease_lost.is_set() and not log_done.is_set():
                    try:
                        lines.put(line[:4096], timeout=0.5)
                        break
                    except queue.Full:
                        continue

        reader = threading.Thread(target=read_logs, daemon=True)
        reader.start()
        tail = []
        with (staging / "analysis.log").open("w") as log:
            while ACTIVE.poll() is None or reader.is_alive() or not lines.empty():
                if STOP.is_set() or lease_lost.is_set():
                    raise InterruptedError("Worker in arresto")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Tempo massimo di elaborazione superato ({TIMEOUT}s).")
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if log.tell() < 1024 * 1024:
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
                print(f"Detector exit code: {ACTIVE.returncode}", flush=True)
                if ACTIVE.returncode == -signal.SIGKILL:
                    raise ValueError("Analisi interrotta per risorse insufficienti. Usa un file più breve.")
                detail = next((line.removeprefix("[ERROR] ") for line in reversed(tail) if "[ERROR]" in line), "Il rilevatore non è riuscito ad analizzare questo audio.")
                raise ValueError(detail)
        update(client, key, stage="Preparazione anteprima e download", progress=85)
        run_stage([sys.executable, "-m", "worker.finish", str(staging), job["filename"], str(original_rate)], deadline)
        decoded.unlink()
        if STOP.is_set() or lease_lost.is_set() or not client.eval(RENEW, 1, lock, token, LEASE_SECONDS):
            raise InterruptedError("Lease del job scaduta")
        output = directory / "output"
        # A recovered attempt can replace outputs left by a crash before completion.
        if output.exists():
            shutil.rmtree(output)
        staging.rename(output)
        os.utime(directory, None)
        terminal = finalize(client, job_id, token, "completed", "Elaborazione completata", 100)
        if not terminal:
            raise InterruptedError("Lease perduta durante la pubblicazione")
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
            os.utime(directory, None)
            terminal = finalize(client, job_id, token, "failed", "Elaborazione fallita", 0, message)
    finally:
        log_done.set()
        lease_done.set()
        keeper.join(timeout=12)
        terminate(ACTIVE)
        if reader:
            reader.join(timeout=2)
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
                client.set(f"beat:worker:alive:{socket.gethostname()}", consumer, ex=30)
            except redis.RedisError:
                pass
            STOP.wait(10)

    if os.environ.get("REQUIRE_MODELS") == "true":
        cache = Path(os.environ.get("TORCH_HOME", "/models/torch")) / "hub" / "checkpoints"
        if not all((cache / f"beat_this-{name}.ckpt").is_file() for name in ("small0", "final0")):
            raise RuntimeError("Checkpoint mancanti: esegui il servizio models prima del worker.")
    threading.Thread(target=alive, daemon=True).start()
    for name in ["JOB_TIMEOUT_SECONDS", "MAX_AUDIO_SECONDS", "MAX_AUDIO_CHANNELS", "MAX_SAMPLE_RATE", "RETENTION_SECONDS"]:
        if name in os.environ and int(os.environ[name]) <= 0:
            raise ValueError(f"Invalid {name}")
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
