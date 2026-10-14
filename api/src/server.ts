import { Redis } from "ioredis";
import { buildApp } from "./app.js";
import { JobStore } from "./store.js";

if (process.env.PUBLIC_ORIGIN && new URL(process.env.PUBLIC_ORIGIN).origin !== process.env.PUBLIC_ORIGIN)
  throw new Error("PUBLIC_ORIGIN deve essere un'origine senza percorso.");
if (process.env.COOKIE_SECURE === "true" && !process.env.PUBLIC_ORIGIN?.startsWith("https://"))
  throw new Error("I cookie sicuri richiedono PUBLIC_ORIGIN HTTPS.");
for (const name of ["MAX_UPLOAD_MB", "MAX_ACTIVE_JOBS", "MAX_JOBS_PER_IP", "MIN_FREE_DISK_MB", "MAX_DATA_MB"]) {
  if (process.env[name] && (!Number.isSafeInteger(Number(process.env[name])) || Number(process.env[name]) <= 0))
    throw new Error(`Configurazione non valida: ${name}`);
}
const redis = new Redis(process.env.REDIS_URL ?? "redis://localhost:6379", {
  maxRetriesPerRequest: 2,
});
redis.on("error", (error) => console.error("Redis:", error.message));
const app = await buildApp({
  store: new JobStore(redis),
  dataDir: process.env.DATA_DIR ?? "/data",
  publicOrigin: process.env.PUBLIC_ORIGIN,
  allowLargeModel: process.env.ALLOW_LARGE_MODEL === "true",
  trustedProxies: process.env.TRUSTED_PROXIES?.split(",").filter(Boolean),
  secureCookies: process.env.COOKIE_SECURE === "true",
  maxActiveJobs: Number(process.env.MAX_ACTIVE_JOBS ?? 6),
  maxJobsPerIp: Number(process.env.MAX_JOBS_PER_IP ?? 2),
  minFreeBytes: Number(process.env.MIN_FREE_DISK_MB ?? 1024) * 1024 ** 2,
  maxDataBytes: Number(process.env.MAX_DATA_MB ?? 5120) * 1024 ** 2,
  maxUploadBytes: Number(process.env.MAX_UPLOAD_MB ?? 50) * 1024 * 1024,
});
await app.listen({ host: "0.0.0.0", port: Number(process.env.PORT ?? 3000) });
for (const signal of ["SIGTERM", "SIGINT"])
  process.once(signal, () => {
    void app
      .close()
      .then(() => redis.quit())
      .then(() => process.exit(0));
  });
