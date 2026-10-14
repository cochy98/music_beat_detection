import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  mkdtemp,
  mkdir,
  readdir,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { buildApp } from "../src/app.js";
import { JobStore } from "../src/store.js";
import { configSchema, type Job } from "../../shared/config.js";

let directory: string;
let app: Awaited<ReturnType<typeof buildApp>>;
let jobs: Map<string, Job>;
let failQueue = false;
let address = 0;
const cookie = `beat_session=${"a".repeat(64)}`;
const owners = new Map<string, string>();
beforeEach(async () => {
  directory = await mkdtemp(path.join(os.tmpdir(), "beat-api-"));
  jobs = new Map();
  failQueue = false;
  owners.clear();
  address = 0;
  const store = {
    redis: { ping: async () => "PONG", exists: async () => 1 },
    get: async (id: string, owner: string) => owners.get(id) === owner ? jobs.get(id) ?? null : null,
    reserve: async () => 0,
    release: async () => {},
    acquireEvents: async () => true,
    renewEvents: async () => true,
    releaseEvents: async () => {},
    enqueue: async (job: Job, input: string, owner: string) => {
      if (failQueue) throw new Error("Queue unavailable");
      jobs.set(job.id, job);
      owners.set(job.id, owner);
    },
  } as unknown as JobStore;
  app = await buildApp({
    store,
    dataDir: directory,
    maxUploadBytes: 32,
    logger: false,
    rateLimitRedis: false,
    minFreeBytes: 1,
  });
  const inject = app.inject.bind(app);
  app.inject = ((options: any) => {
    const request = typeof options === "string" ? { url: options } : options;
    return inject({ ...request, remoteAddress: request.remoteAddress ?? `192.0.2.${++address}`, headers: { cookie, ...request.headers } });
  }) as typeof app.inject;
});
afterEach(async () => {
  await app.close();
  await rm(directory, { recursive: true, force: true });
});

function multipart(
  config: unknown = {},
  audio: Buffer = Buffer.from("audio"),
  filename = "track.wav",
  reverse = false,
) {
  const boundary = "beat-test-boundary";
  const file = Buffer.concat([
    Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="audio"; filename="${filename}"\r\nContent-Type: audio/wav\r\n\r\n`,
    ),
    audio,
    Buffer.from("\r\n"),
  ]);
  const settings = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="config"\r\n\r\n${JSON.stringify(config)}\r\n`,
  );
  return {
    method: "POST" as const,
    url: "/api/jobs",
    headers: { "x-beat-studio": "1", "content-type": `multipart/form-data; boundary=${boundary}` },
    payload: Buffer.concat(
      reverse
        ? [file, settings, Buffer.from(`--${boundary}--\r\n`)]
        : [settings, file, Buffer.from(`--${boundary}--\r\n`)],
    ),
  };
}

test("uploads are isolated, queued and preserve validated settings regardless of field order", async () => {
  const responses = await Promise.all([
    app.inject(
      multipart(
        { backend: "ensemble", markerType: "beats", sourceIn: 0.5 },
        undefined,
        "../../track.wav",
      ),
    ),
    app.inject(multipart({ backend: "madmom" }, undefined, "track.wav", true)),
  ]);
  for (const response of responses)
    assert.equal(response.statusCode, 202, response.body);
  const [a, b] = responses.map((r) => r.json<Job>());
  assert.notEqual(a.id, b.id);
  assert.equal(a.filename, "track.wav");
  assert.equal(a.config.backend, "ensemble");
  assert.equal(a.config.sourceIn, 0.5);
  assert.equal(a.status, "queued");
  assert.equal(
    await readFile(path.join(directory, a.id, "source.wav"), "utf8"),
    "audio",
  );
  assert.equal((await app.inject(`/api/jobs/${a.id}`)).json<Job>().id, a.id);
  assert.equal((await app.inject(`/api/jobs/${a.id}/result`)).statusCode, 409);
});

test("invalid settings, unsafe options, unsupported/empty/oversize uploads are rejected and removed", async () => {
  const requests = [
    multipart({ maxBpm: 20, minBpm: 40 }),
    multipart({ backend: "shell" }),
    multipart({ checkpoint: "/tmp/evil.ckpt" }),
    multipart({ sourceIn: 5, sourceOut: 2 }),
    multipart({ device: "cuda" }),
    multipart({ backend: "ensemble", checkpoint: "final0" }),
    multipart({}, Buffer.alloc(0)),
    multipart({}, Buffer.alloc(33)),
    multipart({}, undefined, "track.exe"),
  ];
  for (const request of requests) {
    const response = await app.inject(request);
    assert.ok(
      response.statusCode >= 400 && response.statusCode < 500,
      response.body,
    );
  }
  assert.equal(jobs.size, 0);
  assert.deepEqual(await readdir(directory), []);
});

test("queue failure removes uploaded file and returns a controlled error", async () => {
  failQueue = true;
  assert.equal((await app.inject(multipart())).statusCode, 500);
  assert.deepEqual(await readdir(directory), []);
});

test("completed results and byte range playback; artifact paths are allowlisted", async () => {
  const job = (await app.inject(multipart())).json<Job>();
  job.status = "completed";
  jobs.set(job.id, job);
  const out = path.join(directory, job.id, "output");
  await mkdir(out);
  await writeFile(
    path.join(out, "result.json"),
    JSON.stringify({ markers: [] }),
  );
  await writeFile(path.join(out, "audio.wav"), "0123456789");
  assert.deepEqual((await app.inject(`/api/jobs/${job.id}/result`)).json(), {
    markers: [],
  });
  const response = await app.inject({
    url: `/api/jobs/${job.id}/files/pcm`,
    headers: { range: "bytes=3-5" },
  });
  assert.equal(response.statusCode, 206);
  assert.equal(response.body, "345");
  assert.equal(response.headers["content-range"], "bytes 3-5/10");
  assert.equal(
    (
      await app.inject({
        url: `/api/jobs/${job.id}/files/pcm`,
        headers: { range: "bytes=-3" },
      })
    ).body,
    "789",
  );
  assert.equal(
    (
      await app.inject({
        url: `/api/jobs/${job.id}/files/pcm`,
        headers: { range: "bytes=20-" },
      })
    ).statusCode,
    416,
  );
  assert.equal(
    (await app.inject(`/api/jobs/${job.id}/files/source`)).statusCode,
    400,
  );
  assert.equal((await app.inject("/api/jobs/not-a-uuid")).statusCode, 400);
  assert.equal(
    (await app.inject("/api/jobs/00000000-0000-4000-8000-000000000000"))
      .statusCode,
    404,
  );
});

test("timecode validation handles fractional FPS, dropped labels and origin ordering", () => {
  assert.equal(
    configSchema.safeParse({ fps: "25", timelineStart: "01:00:00:25" }).success,
    false,
  );
  assert.equal(
    configSchema.safeParse({ fps: "25", dropFrame: true }).success,
    false,
  );
  assert.equal(
    configSchema.safeParse({ timelineStart: "01:00:00;00" }).success,
    false,
  );
  assert.equal(
    configSchema.safeParse({
      fps: "29.97",
      dropFrame: true,
      timelineStart: "01:01:00;00",
    }).success,
    false,
  );
  assert.equal(
    configSchema.safeParse({
      fps: "29.97",
      dropFrame: true,
      timelineStart: "01:01:00;02",
    }).success,
    true,
  );
  assert.equal(
    configSchema.safeParse({ audioStart: "00:59:59:24" }).success,
    false,
  );
  assert.equal(
    configSchema.safeParse({ bpm: 300, minBpm: 40, maxBpm: 240 }).success,
    false,
  );
});


test("anonymous session cookie, CSRF and ownership protect every job route", async () => {
  const response = await app.inject({ method: "POST", url: "/api/session", headers: { "x-beat-studio": "1", cookie: "" } });
  assert.equal(response.statusCode, 200);
  assert.match(String(response.headers["set-cookie"]), /HttpOnly; SameSite=Strict/);
  const foreign = await app.inject({ ...multipart(), headers: { ...multipart().headers, origin: "https://evil.example" } });
  assert.equal(foreign.statusCode, 403);
  const missing = await app.inject({ ...multipart(), headers: { "content-type": multipart().headers["content-type"] } });
  assert.equal(missing.statusCode, 403);
  const job = (await app.inject(multipart())).json<Job>();
  for (const suffix of ["", "/events", "/result", "/preview", "/files/pcm"]) {
    assert.equal((await app.inject({ url: `/api/jobs/${job.id}${suffix}`, headers: { cookie: `beat_session=${"b".repeat(64)}` } })).statusCode, 404);
  }
  assert.equal((await app.inject(`/api/jobs/${job.id}`)).headers["cache-control"], "private, no-store");
});

test("expired results return 410 and do not expose files", async () => {
  const job = (await app.inject(multipart())).json<Job>();
  job.status = "completed";
  job.expiresAt = new Date(Date.now() - 1000).toISOString();
  jobs.set(job.id, job);
  assert.equal((await app.inject(`/api/jobs/${job.id}/result`)).statusCode, 410);
});


test("production sessions use a Secure host-only cookie and reject the development cookie", async () => {
  const secure = await buildApp({ store: { redis: {} } as JobStore, dataDir: directory, logger: false, rateLimitRedis: false, secureCookies: true, publicOrigin: "https://battute.duckdns.org" });
  try {
    const response = await secure.inject({ method: "POST", url: "/api/session", headers: { "x-beat-studio": "1", origin: "https://battute.duckdns.org" } });
    assert.equal(response.statusCode, 200);
    assert.match(String(response.headers["set-cookie"]), /^__Host-beat_session=[a-f0-9]{64}; Path=\/; HttpOnly; SameSite=Strict; Max-Age=172800; Secure$/);
    assert.equal((await secure.inject(multipart())).statusCode, 401);
  } finally { await secure.close(); }
});


test("API shutdown closes active SSE connections instead of hanging", { timeout: 3000 }, async () => {
  const job = (await app.inject(multipart())).json<Job>();
  const pending = Promise.resolve(app.inject(`/api/jobs/${job.id}/events`));
  await new Promise((resolve) => setImmediate(resolve));
  await app.close();
  const response = await pending;
  assert.equal(response.statusCode, 200);
  assert.match(response.body, /event: job/);
});
