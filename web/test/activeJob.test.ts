import { test } from "node:test";
import assert from "node:assert/strict";
import { loadActiveJob, rememberActiveJob, forgetActiveJob } from "../src/activeJob.ts";

test("reload restores an active job; completion clears it without clearing a newer job", () => {
  const storage = new Map<string, string>();
  Object.assign(globalThis, { sessionStorage: { getItem: (k: string) => storage.get(k) ?? null, setItem: (k: string, v: string) => storage.set(k, v), removeItem: (k: string) => storage.delete(k) } });
  const first = "00000000-0000-4000-8000-000000000001", second = "00000000-0000-4000-8000-000000000002";
  assert.equal(loadActiveJob(), "");
  rememberActiveJob(first);
  assert.equal(loadActiveJob(), first);
  rememberActiveJob(second);
  forgetActiveJob(first);
  assert.equal(loadActiveJob(), second);
  forgetActiveJob(second);
  assert.equal(loadActiveJob(), "");
  storage.set("beat-active-job", "../../etc/passwd");
  assert.equal(loadActiveJob(), "");
});
