"""Atomic terminal state, temporary metadata and admission release."""
from datetime import datetime, timezone, timedelta
import os
import uuid

RETENTION = int(os.environ.get("RETENTION_SECONDS", "86400"))
FINALIZE = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local owner=redis.call('HGET', KEYS[1], 'owner') or ''
local ip=redis.call('HGET', KEYS[1], 'ip') or ''
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'stage', ARGV[3], 'progress', ARGV[4], 'error', ARGV[5], 'updatedAt', ARGV[6], 'finishedAt', ARGV[6], 'expiresAt', ARGV[7])
redis.call('EXPIRE', KEYS[1], ARGV[8])
redis.call('ZADD', 'beat:cleanup', ARGV[9], ARGV[10])
redis.call('ZREM', 'beat:admission', ARGV[10])
redis.call('ZREM', 'beat:admission:owner:' .. owner, ARGV[10])
redis.call('ZREM', 'beat:admission:ip:' .. ip, ARGV[10])
return 1
"""

def finalize(client, job_id, token, status, stage, progress, error=""):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=RETENTION)
    return bool(client.eval(FINALIZE, 2, f"beat:job:{job_id}", f"beat:lease:{job_id}", token,
                            status, stage, progress, error, now.isoformat(), expires.isoformat(),
                            RETENTION + 86400, expires.timestamp(), job_id))
