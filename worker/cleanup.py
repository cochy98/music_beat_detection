"""Retention sweeper. Never remove an active job or an upload reservation."""
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import redis
from worker.lifecycle import finalize, RETENTION

ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
MAX_AGE = int(os.environ.get("MAX_QUEUE_AGE_SECONDS", "7200"))
ORPHAN_AGE = int(os.environ.get("ORPHAN_GRACE_SECONDS", "3600"))

def saved_time(value, fallback):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return fallback


def sweep(client, data, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    removed = False
    data.mkdir(parents=True, exist_ok=True)
    for directory in data.iterdir():
        if not ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
            continue
        job_id = directory.name
        key, lock = f"beat:job:{job_id}", f"beat:lease:{job_id}"
        if client.exists(lock):
            continue
        token = str(uuid.uuid4())
        if not client.set(lock, token, nx=True, ex=60):
            continue
        try:
            job = client.hgetall(key)
            if job and job.get("status") in ("queued", "running"):
                since = saved_time(job.get("updatedAt") or job.get("createdAt"), directory.stat().st_mtime)
                if timestamp - since > MAX_AGE:
                    if finalize(client, job_id, token, "failed", "Analisi scaduta", 0, "Tempo massimo di attesa superato. Riprova.") and job.get("streamId"):
                        try:
                            client.xack("beat:jobs", "detectors", job["streamId"])
                        except redis.ResponseError as exc:
                            if "NOGROUP" not in str(exc):
                                raise
                        client.xdel("beat:jobs", job["streamId"])
                continue
            if job.get("status") in ("completed", "failed"):
                expiry = job.get("expiresAt")
                expired = saved_time(expiry, directory.stat().st_mtime + RETENTION) <= timestamp if expiry else timestamp - saved_time(job.get("updatedAt"), directory.stat().st_mtime) >= RETENTION
            else:
                reservation = client.zscore("beat:admission", job_id)
                if reservation and timestamp * 1000 < reservation < 9000000000000000:
                    continue
                expired = timestamp - directory.stat().st_mtime > ORPHAN_AGE
            if not expired:
                continue
            if job.get("streamId"):
                client.xdel("beat:jobs", job["streamId"])
            shutil.rmtree(directory)
            removed = True
            with client.pipeline() as pipe:
                pipe.delete(key)
                pipe.zrem("beat:cleanup", job_id)
                pipe.zrem("beat:admission", job_id)
                if job:
                    pipe.zrem(f"beat:admission:owner:{job.get('owner', '')}", job_id)
                    pipe.zrem(f"beat:admission:ip:{job.get('ip', '')}", job_id)
                pipe.execute()
        finally:
            client.eval("if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0", 1, lock, token)
    # Metadata with no directory (for example after manual file removal) also expires.
    for job_id in client.zrangebyscore("beat:cleanup", "-inf", timestamp):
        if ID.fullmatch(job_id) and not (data / job_id).exists() and not client.exists(f"beat:lease:{job_id}"):
            client.delete(f"beat:job:{job_id}")
            removed = True
            client.zrem("beat:cleanup", job_id)
    client.zremrangebyscore("beat:admission", "-inf", timestamp * 1000)
    # Keep a dirty generation until an AOF rewrite can run, even if traffic stops.
    dirty = "beat:cleanup:aof-dirty"
    if removed:
        client.incr(dirty)
    generation = client.get(dirty)
    if generation:
        persistence = client.info("persistence")
        if persistence.get("aof_enabled") and not persistence.get("aof_rewrite_in_progress") and client.set("beat:cleanup:aof-rewrite", "1", nx=True, ex=3600):
            try:
                client.bgrewriteaof()
                client.eval("if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0", 1, dirty, generation)
            except redis.ResponseError:
                client.delete("beat:cleanup:aof-rewrite")
                raise

def main():
    if min(RETENTION, MAX_AGE, ORPHAN_AGE, int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "300"))) <= 0:
        raise ValueError("Retention, queue age and cleanup intervals must be positive")
    client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True, socket_timeout=10, socket_connect_timeout=5)
    while True:
        try:
            sweep(client, Path(os.environ.get("DATA_DIR", "/data")))
            client.set("beat:cleanup:alive", "1", ex=600)
        except (redis.RedisError, OSError, ValueError) as exc:
            print(f"Cleanup failed: {exc}", flush=True)
        time.sleep(int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "300")))

if __name__ == "__main__":
    main()
