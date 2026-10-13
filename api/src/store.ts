import { Redis } from "ioredis";
import type { Job } from "../../shared/config.js";

export const STREAM = "beat:jobs";
export const jobKey = (id: string) => `beat:job:${id}`;
export class JobStore {
  constructor(readonly redis: Redis) {}
  async get(id: string): Promise<Job | null> {
    const data = await this.redis.hgetall(jobKey(id));
    if (!data.id) return null;
    return {
      id: data.id,
      filename: data.filename,
      status: data.status as Job["status"],
      stage: data.stage,
      progress: Number(data.progress),
      createdAt: data.createdAt,
      updatedAt: data.updatedAt,
      config: JSON.parse(data.config),
      attempts: Number(data.attempts || 0),
      ...(data.error ? { error: data.error } : {}),
    };
  }
  async enqueue(job: Job, input: string) {
    // The metadata and stream entry become visible together, also across API restarts.
    const result = await this.redis
      .multi()
      .hset(jobKey(job.id), {
        ...job,
        config: JSON.stringify(job.config),
        input,
        error: "",
      })
      .xadd(STREAM, "*", "jobId", job.id)
      .exec();
    if (!result || result.some(([error]) => error))
      throw new Error("Impossibile accodare il job.");
  }
}
