import { Redis } from "ioredis";
import { buildApp } from "./app.js";
import { JobStore } from "./store.js";

const redis = new Redis(process.env.REDIS_URL ?? "redis://localhost:6379", {
  maxRetriesPerRequest: 2,
});
redis.on("error", (error) => console.error("Redis:", error.message));
const app = await buildApp({
  store: new JobStore(redis),
  dataDir: process.env.DATA_DIR ?? "/data",
  maxUploadBytes: Number(process.env.MAX_UPLOAD_MB ?? 200) * 1024 * 1024,
});
await app.listen({ host: "0.0.0.0", port: Number(process.env.PORT ?? 3000) });
for (const signal of ["SIGTERM", "SIGINT"])
  process.once(signal, () => {
    void app
      .close()
      .then(() => redis.quit())
      .then(() => process.exit(0));
  });
