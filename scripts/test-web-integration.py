#!/usr/bin/env python3
"""Real queue, authorization, worker recovery and retention tests on an isolated stack."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import http.cookiejar
import io
import json
import math
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
import zipfile


def audio():
    sample_rate, duration = 22050, 30
    raw = bytearray(sample_rate * duration * 2)
    for beat in range(1, duration * 2):
        start = int(beat * .5 * sample_rate)
        for i in range(int(.06 * sample_rate)):
            t = i / sample_rate
            value = int(24000 * math.sin(2 * math.pi * (120 if beat % 4 == 1 else 900) * t) * math.exp(-70 * t))
            struct.pack_into('<h', raw, (start + i) * 2, value)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate); wav.writeframes(raw)
    return buf.getvalue()


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with self.request('/api/session', data=b'{}', headers={'X-Beat-Studio': '1', 'Content-Type': 'application/json'}) as response:
            assert response.status == 200

    def request(self, path, data=None, headers=None, timeout=30):
        return self.opener.open(urllib.request.Request(self.base + path, data=data, headers=headers or {}), timeout=timeout)

    def get(self, path):
        with self.request(path) as response:
            return json.load(response)

    def upload(self, data):
        boundary = uuid.uuid4().hex
        config = json.dumps({'backend': 'madmom', 'markerType': 'beats', 'beatsPerBar': '4', 'debugDuration': 5, 'librosaCheck': False})
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="config"\r\n\r\n{config}\r\n--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="test.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        with self.request('/api/jobs', data=body, headers={'Content-Type': f'multipart/form-data; boundary={boundary}', 'X-Beat-Studio': '1'}) as response:
            assert response.status == 202
            return json.load(response)['id']


def denied(call, code):
    try:
        call()
    except urllib.error.HTTPError as error:
        assert error.code == code, (error.code, error.read())
    else:
        raise AssertionError('Request should have been denied')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8089')
    parser.add_argument('--project', default='beat-studio-ci')
    args = parser.parse_args()
    if not args.project.startswith(('beat-studio-ci', 'beat-secure-test')):
        raise ValueError('This script must only control an isolated test project')
    compose = ['docker', 'compose', '-p', args.project, '-f', 'compose.yaml', '-f', 'compose.ci.yaml']
    def docker(*parts):
        subprocess.run(compose + list(parts), check=True)
    a, b = Client(args.url), Client(args.url)
    source = audio()
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda c: c.upload(source), [a, b]))
    assert all(c.get('/api/jobs/' + id)['status'] == 'queued' for c, id in zip([a, b], ids))
    assert a.get('/api/active-job')['id'] == ids[0]
    denied(lambda: a.upload(source), 429)
    for suffix in ['', '/events', '/result', '/preview', '/files/pcm']:
        denied(lambda: b.get('/api/jobs/' + ids[0] + suffix), 404)
    denied(lambda: a.request('/api/session', data=b'', headers={'X-Beat-Studio': '1', 'Origin': 'https://evil.invalid'}), 403)
    events, errors = [], []
    def listen():
        try:
            with a.request('/api/jobs/' + ids[0] + '/events', timeout=300) as response:
                for line in response:
                    if line.startswith(b'data: '): events.append(json.loads(line[6:])['status'])
        except Exception as exc: errors.append(str(exc))
    listener = threading.Thread(target=listen, daemon=True); listener.start()
    docker('up', '--no-build', '-d', 'worker')
    saw_queue = False
    for _ in range(150):
        states = [c.get('/api/jobs/' + id)['status'] for c, id in zip([a, b], ids)]
        assert states.count('running') <= 1 and 'failed' not in states, states
        saw_queue |= 'running' in states and 'queued' in states
        if states == ['completed', 'completed']: break
        time.sleep(2)
    assert states == ['completed', 'completed'] and saw_queue, states
    listener.join(5)
    assert not errors and 'completed' in events, (errors, events)
    for c, id in zip([a, b], ids):
        report = c.get('/api/jobs/' + id + '/result')
        assert report['markers'] and report['audio']['original_sample_rate'] == 22050
        assert not report['audio']['path'].startswith('/data/')
        assert 0 < len(c.get('/api/jobs/' + id + '/preview')['peaks']) <= 4000
        with c.request('/api/jobs/' + id + '/files/pcm', headers={'Range': 'bytes=0-127'}) as response:
            assert response.status == 206 and len(response.read()) == 128
            assert 'no-store' in response.headers['Cache-Control']
        with c.request('/api/jobs/' + id + '/files/zip') as response:
            assert zipfile.ZipFile(io.BytesIO(response.read())).testzip() is None
        assert c.get('/api/active-job') is None
    print('PASS concurrent uploads, serial queue, ownership, CSRF, SSE and downloads', flush=True)
    # The public POST limiter is intentionally shared across clients on the same IP.
    while True:
        try: recovery = a.upload(source); break
        except urllib.error.HTTPError as exc:
            if exc.code != 429: raise
            time.sleep(min(61, int(exc.headers.get('Retry-After', '60')) + 1))
    for _ in range(100):
        if a.get('/api/jobs/' + recovery)['status'] == 'running':
            docker('restart', 'worker'); break
        time.sleep(.2)
    else: raise AssertionError('Worker never started')
    for _ in range(150):
        job = a.get('/api/jobs/' + recovery)
        if job['status'] in ['completed', 'failed']: break
        time.sleep(2)
    assert job['status'] == 'completed' and job['attempts'] >= 2, job
    print('PASS recovery after worker restart', flush=True)
    expire = f"import os,redis; from datetime import datetime,timezone,timedelta; r=redis.Redis.from_url(os.environ['REDIS_URL']); r.hset('beat:job:{ids[0]}', 'expiresAt', (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())"
    docker('exec', '-T', 'worker', 'python', '-c', expire)
    denied(lambda: a.get('/api/jobs/' + ids[0] + '/result'), 410)
    cleanup = f"""
import os,time,uuid
from pathlib import Path
import redis
from worker.cleanup import sweep
r=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
p=Path('/data')
assert r.ttl('beat:job:{recovery}')>0
protected=str(uuid.uuid4()); d=p/protected; d.mkdir(); (d/'source.wav').write_bytes(b'audio'); r.set('beat:lease:'+protected,'test',ex=60)
orphan=str(uuid.uuid4()); old=p/orphan; old.mkdir(); os.utime(old,(time.time()-7200,time.time()-7200))
sweep(r,p)
assert d.exists() and not old.exists()
assert not (p/'{ids[0]}').exists() and not r.exists('beat:job:{ids[0]}')
assert r.xlen('beat:jobs')==0 and r.zcard('beat:admission')==0
# A throttled rewrite must remain pending even when there are no more deletions.
r.set('beat:cleanup:aof-rewrite','1',ex=3600); r.incr('beat:cleanup:aof-dirty')
sweep(r,p)
assert r.exists('beat:cleanup:aof-dirty')
r.delete('beat:cleanup:aof-rewrite')
for _ in range(100):
    if not r.info('persistence').get('aof_rewrite_in_progress'): break
    time.sleep(.1)
sweep(r,p)
assert not r.exists('beat:cleanup:aof-dirty')
r.delete('beat:lease:'+protected); (d/'source.wav').unlink(); d.rmdir()
"""
    docker('exec', '-T', 'worker', 'python', '-c', cleanup)
    print('PASS expiry, retention cleanup, orphan removal and active-job protection', flush=True)

if __name__ == '__main__': main()
