import { test } from "node:test";
import assert from "node:assert/strict";
import { Redis } from "ioredis";
import { randomUUID } from "node:crypto";
import { JobStore, STREAM } from "../src/store.ts";
import { defaults, type Job } from "../../shared/config.ts";

test("Redis enforces admission atomically, releases slots and bounds SSE", { skip: !process.env.TEST_REDIS_URL }, async () => {
  assert.equal(new URL(process.env.TEST_REDIS_URL!).pathname, "/15", "Use database 15 reserved for isolated tests");
  const redis = new Redis(process.env.TEST_REDIS_URL!, { maxRetriesPerRequest: 1 });
  const store = new JobStore(redis);
  let initialized = false;
  try {
    assert.equal(await redis.dbsize(), 0, "Use a fresh isolated Redis database for this test");
    initialized = true;
    const ids = Array.from({ length: 12 }, () => randomUUID());
    const admitted = await Promise.all(ids.map((id, i) => store.reserve(id, `owner${i}`, `ip${i}`, 6, 2)));
    assert.equal(admitted.filter((x) => x === 0).length, 6);
    assert.equal(await redis.zcard("beat:admission"), 6);
    const index = admitted.indexOf(0), id = ids[index];
    assert.equal(await store.reserve(randomUUID(), `owner${index}`, `ip${index}`, 20, 2), 2);
    const now = new Date().toISOString();
    const job: Job = { id, filename: "test.wav", status: "queued", stage: "In coda", progress: 0, createdAt: now, updatedAt: now, config: defaults, attempts: 0 };
    await store.enqueue(job, "source.wav", `owner${index}`, `ip${index}`);
    assert.equal(await redis.xlen(STREAM), 1);
    assert.equal((await store.get(id, `owner${index}`))?.id, id);
    assert.equal(await store.get(id, "someone-else"), null);
    await store.release(id, `owner${index}`, `ip${index}`);
    assert.equal(await redis.zcard("beat:admission"), 5);
    const expired = randomUUID();
    await store.reserve(expired, "expired", "expired", 6, 2);
    await redis.zadd("beat:admission", Date.now() - 1, expired);
    await assert.rejects(store.enqueue({ ...job, id: expired }, "source.wav", "expired", "expired"));
    assert.equal(await redis.exists(`beat:job:${expired}`), 0);
    assert.equal(await store.acquireEvents("a", "viewer", "shared-ip"), true);
    assert.equal(await store.acquireEvents("b", "viewer", "shared-ip"), true);
    assert.equal(await store.acquireEvents("c", "viewer", "shared-ip"), false);
    await store.releaseEvents("a", "viewer", "shared-ip");
    assert.equal(await store.acquireEvents("c", "viewer", "shared-ip"), true);
    await redis.zrem("beat:events", "c");
    assert.equal(await store.renewEvents("c", "viewer", "shared-ip"), false);
  } finally {
    if (initialized) await redis.flushdb();
    await redis.quit();
  }
});
