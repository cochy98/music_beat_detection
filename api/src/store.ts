import { Redis } from "ioredis";
import type { Job } from "../../shared/config.js";

export const STREAM = "beat:jobs";
export const jobKey = (id: string) => `beat:job:${id}`;
const GLOBAL = "beat:admission";
const sets = (owner: string, ip: string) => [GLOBAL, `beat:admission:owner:${owner}`, `beat:admission:ip:${ip}`];
const RESERVE = `
for i=1,3 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', ARGV[1])
  if redis.call('ZCARD', KEYS[i]) >= tonumber(ARGV[i+3]) then return i end
end
for i=1,3 do
  redis.call('ZADD', KEYS[i], ARGV[2], ARGV[3])
  if i > 1 then redis.call('EXPIRE', KEYS[i], ARGV[7]) end
end
return 0`;
const COMMIT = `
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
if tonumber(redis.call('ZSCORE', KEYS[1], ARGV[1])) <= tonumber(ARGV[2]) then return 0 end
local streamType=redis.call('TYPE', KEYS[5]).ok
if streamType ~= 'none' and streamType ~= 'stream' then return redis.error_reply('Invalid queue type') end
if redis.call('EXISTS', KEYS[4]) == 1 then return redis.error_reply('Duplicate job') end
local values=cjson.decode(ARGV[3])
for k,v in pairs(values) do redis.call('HSET', KEYS[4], k, tostring(v)) end
local message=redis.call('XADD', KEYS[5], '*', 'jobId', ARGV[1])
redis.call('HSET', KEYS[4], 'streamId', message)
for i=1,3 do redis.call('ZADD', KEYS[i], 9000000000000000, ARGV[1]); redis.call('PERSIST', KEYS[i]) end
return 1`;
export class JobStore {
  constructor(readonly redis: Redis) {}
  async get(id: string, owner?: string): Promise<Job | null> {
    const data = await this.redis.hgetall(jobKey(id));
    if (!data.id || (owner !== undefined && data.owner !== owner)) return null;
    return {
      id: data.id, filename: data.filename, status: data.status as Job["status"],
      stage: data.stage, progress: Number(data.progress), createdAt: data.createdAt,
      updatedAt: data.updatedAt, config: JSON.parse(data.config), attempts: Number(data.attempts || 0),
      ...(data.error ? { error: data.error } : {}),
      ...(data.expiresAt ? { expiresAt: data.expiresAt } : {}),
    };
  }
  async reserve(id: string, owner: string, ip: string, maxActive: number, maxPerIp: number) {
    const now = Date.now();
    return Number(await this.redis.eval(RESERVE, 3, ...sets(owner, ip), now, now + 600_000, id, maxActive, 1, maxPerIp, 86400));
  }
  async release(id: string, owner: string, ip: string) {
    await this.redis.multi().zrem(GLOBAL, id).zrem(sets(owner, ip)[1], id).zrem(sets(owner, ip)[2], id).exec();
  }
  async enqueue(job: Job, input: string, owner: string, ip: string) {
    const values = { ...job, config: JSON.stringify(job.config), input, owner, ip, error: "" };
    const committed = await this.redis.eval(COMMIT, 5, ...sets(owner, ip), jobKey(job.id), STREAM, job.id, Date.now(), JSON.stringify(values));
    if (committed !== 1) throw new Error("Prenotazione upload scaduta.");
  }
  private eventKeys(owner: string, ip: string) {
    return ["beat:events", `beat:events:owner:${owner}`, `beat:events:ip:${ip}`];
  }
  async acquireEvents(token: string, owner: string, ip: string) {
    const now = Date.now();
    return Number(await this.redis.eval(RESERVE, 3, ...this.eventKeys(owner, ip), now, now + 45_000, token, 50, 2, 10, 90)) === 0;
  }
  async renewEvents(token: string, owner: string, ip: string) {
    const keys = this.eventKeys(owner, ip);
    const result = await this.redis.eval(`
for i=1,3 do if not redis.call('ZSCORE', KEYS[i], ARGV[1]) then return 0 end end
for i=1,3 do redis.call('ZADD', KEYS[i], ARGV[2], ARGV[1]); redis.call('EXPIRE', KEYS[i],90) end
return 1`, 3, ...keys, token, Date.now() + 45_000);
    return result === 1;
  }
  async releaseEvents(token: string, owner: string, ip: string) {
    await this.redis.multi().zrem(this.eventKeys(owner, ip)[0], token).zrem(this.eventKeys(owner, ip)[1], token).zrem(this.eventKeys(owner, ip)[2], token).exec();
  }
}
