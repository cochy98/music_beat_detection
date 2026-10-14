import Fastify, { type FastifyRequest } from "fastify";
import multipart from "@fastify/multipart";
import rateLimit from "@fastify/rate-limit";
import { randomUUID } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, readFile, rm, stat } from "node:fs/promises";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import {
  configSchema,
  algorithms,
  defaults,
  type Job,
} from "../../shared/config.js";
import { JobStore } from "./store.js";
import { cookieName, hash, newSession, session } from "./security.js";
import { storageAvailable } from "./storage.js";

const idSchema = {
  type: "object",
  required: ["id"],
  properties: { id: { type: "string", format: "uuid" } },
} as const;
const extensions = new Set([
  ".mp3",
  ".wav",
  ".flac",
  ".ogg",
  ".m4a",
  ".aac",
  ".aiff",
  ".aif",
  ".opus",
]);
const artifacts: Record<string, [string, string]> = {
  json: ["result.json", "application/json"],
  edl: ["markers.edl", "application/octet-stream"],
  clicks: ["clicks.wav", "audio/wav"],
  pcm: ["audio.wav", "audio/wav"],
  debug: ["debug.png", "image/png"],
  zip: ["result.zip", "application/zip"],
};
const failure = (statusCode: number, message: string) =>
  Object.assign(new Error(message), { statusCode });

export async function buildApp(options: {
  store: JobStore;
  dataDir: string;
  maxUploadBytes?: number;
  logger?: boolean;
  rateLimitRedis?: boolean;
  publicOrigin?: string;
  allowLargeModel?: boolean;
  trustedProxies?: string[];
  secureCookies?: boolean;
  maxActiveJobs?: number;
  maxJobsPerIp?: number;
  minFreeBytes?: number;
  maxDataBytes?: number;
}) {
  const { store, dataDir } = options;
  const maxUploadBytes = options.maxUploadBytes ?? 50 * 1024 * 1024;
  const app = Fastify({
    logger: options.logger === false ? false : { redact: ["req.headers.cookie", "res.headers.set-cookie"] },
    trustProxy: options.trustedProxies ?? false,
    requestTimeout: 300_000,
    bodyLimit: 65536,
  });
  const streams = new Set<() => void>();
  app.addHook("preClose", async () => { for (const stop of streams) stop(); });
  await app.register(multipart, {
    limits: {
      fileSize: maxUploadBytes,
      files: 1,
      fields: 1,
      parts: 2,
      fieldSize: 16384,
    },
  });
  await app.register(rateLimit, { max: 120, timeWindow: "1 minute", redis: options.rateLimitRedis === false ? undefined : store.redis, nameSpace: "beat:rate:" });
  app.addHook("onRequest", async (request, reply) => {
    reply.header("Cache-Control", "private, no-store");
    if (request.method === "POST") {
      const origin = options.publicOrigin ?? `${request.protocol}://${request.host}`;
      if (request.headers["x-beat-studio"] !== "1" ||
          (request.headers.origin && request.headers.origin !== origin) ||
          request.headers["sec-fetch-site"] === "cross-site")
        throw failure(403, "Richiesta non consentita da questa origine.");
    }
  });
  app.post("/api/session", { config: { rateLimit: { max: 30, timeWindow: "1 minute" } } }, async (request, reply) => {
    const token = session(request, options.secureCookies) ?? newSession();
    reply.header("Set-Cookie", `${cookieName(options.secureCookies)}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=172800${options.secureCookies ? "; Secure" : ""}`);
    return { ready: true };
  });
  app.setErrorHandler((error, request, reply) => {
    const details = error as { statusCode?: number; message: string };
    const status = details.statusCode ?? 500;
    if (status >= 500) request.log.error(error);
    reply
      .code(status)
      .send({
        error:
          status >= 500
            ? "Servizio temporaneamente non disponibile."
            : details.message,
      });
  });
  app.get("/api/health", async () => {
    await store.redis.ping();
    return {
      status: "ok",
      workerOnline: Boolean(await store.redis.exists("beat:worker:alive")),
      cleanupOnline: Boolean(await store.redis.exists("beat:cleanup:alive")),
    };
  });
  app.get("/api/config", async () => ({
    algorithms,
    defaults,
    maxUploadBytes,
    allowedCheckpoints: options.allowLargeModel ? ["small0", "final0"] : ["small0"],
  }));
  app.get("/api/active-job", async (request) => {
    const token = session(request, options.secureCookies);
    if (!token) return null;
    const owner = hash(token);
    for (const id of await store.redis.zrange(`beat:admission:owner:${owner}`, 0, -1)) {
      const job = await store.get(id, owner);
      if (job?.status === "queued" || job?.status === "running") return job;
    }
    return null;
  });
  const getJob = async (id: string, request: FastifyRequest) => {
    const token = session(request, options.secureCookies);
    if (!token) throw failure(404, "Job non trovato.");
    const job = await store.get(id, hash(token));
    if (!job) throw failure(404, "Job non trovato.");
    if (job.expiresAt && Date.parse(job.expiresAt) <= Date.now())
      throw failure(410, "Analisi scaduta. Carica nuovamente il file.");
    return job;
  };
  app.post(
    "/api/jobs",
    { config: { rateLimit: { max: 3, timeWindow: "1 minute" } } },
    async (request, reply) => {
      const token = session(request, options.secureCookies);
      if (!token) throw failure(401, "Inizializza la sessione prima di caricare un file.");
      const owner = hash(token), ip = hash(request.ip);
      const id = randomUUID();
      const capacity = options.maxActiveJobs ?? 6;
      const reserved = await store.reserve(id, owner, ip, capacity, options.maxJobsPerIp ?? 2);
      if (reserved) throw failure(429, reserved === 1 ? "Coda piena. Riprova più tardi." : reserved === 2 ? "Hai già un'analisi attiva. Attendi il completamento." : "Limite di analisi per questa rete raggiunto. Riprova più tardi.");
      const directory = path.join(dataDir, id);
      let filename = "",
        input = "",
        config: unknown;
      let committed = false, uncertain = false;
      try {
        await mkdir(dataDir, { recursive: true });
        if (!await storageAvailable(dataDir, maxUploadBytes * capacity + 1024 ** 3, options.minFreeBytes ?? 1024 ** 3, options.maxDataBytes ?? 5 * 1024 ** 3))
          throw failure(503, "Spazio temporaneo insufficiente. Riprova più tardi.");
        await mkdir(directory, { recursive: true });
        for await (const part of request.parts()) {
          if (part.type === "file") {
            if (part.fieldname !== "audio") {
              part.file.resume();
              throw failure(400, "Il file deve essere nel campo audio.");
            }
            const ext = path.extname(part.filename).toLowerCase();
            if (!extensions.has(ext)) {
              part.file.resume();
              throw failure(415, "Formato audio non supportato.");
            }
            filename = path
              .basename(part.filename)
              .replace(/[\x00-\x1f\x7f]/g, "")
              .slice(0, 240);
            input = `source${ext}`;
            await pipeline(
              part.file,
              createWriteStream(path.join(directory, input), { flags: "wx" }),
            );
            if (part.file.truncated)
              throw failure(413, "Il file supera il limite di caricamento.");
            if (!(await stat(path.join(directory, input))).size)
              throw failure(400, "Il file audio è vuoto.");
          } else {
            if (
              part.fieldname !== "config" ||
              config !== undefined ||
              part.valueTruncated
            )
              throw failure(400, "Configurazione non valida.");
            try {
              config = JSON.parse(String(part.value));
            } catch {
              throw failure(400, "Configurazione JSON non valida.");
            }
          }
        }
        if (!input) throw failure(400, "Carica un file audio.");
        const parsed = configSchema.safeParse(config ?? {});
        if (!parsed.success)
          throw failure(
            400,
            parsed.error.issues
              .map((i) => `${i.path.join(".")}: ${i.message}`)
              .join("; "),
          );
        if (!options.allowLargeModel && parsed.data.backend !== "madmom" && parsed.data.checkpoint === "final0")
          throw failure(400, "Il modello final0 non è disponibile su questo server. Usa small0.");
        const now = new Date().toISOString();
        const job: Job = {
          id,
          filename,
          status: "queued",
          stage: "In coda",
          progress: 0,
          createdAt: now,
          updatedAt: now,
          config: parsed.data,
          attempts: 0,
        };
        try {
          uncertain = true;
          await store.enqueue(job, input, owner, ip);
          committed = true;
        } catch (error) {
          // An acknowledged-lost Redis reply must not delete an already queued source.
          const recovered = await store.get(id, owner);
          uncertain = false;
          if (!recovered) throw error;
          committed = true;
        }
        return reply.code(202).header("Location", `/api/jobs/${id}`).send(job);
      } finally {
        if (!committed && !uncertain) {
          await rm(directory, { recursive: true, force: true });
          await store.release(id, owner, ip);
        }
      }
    },
  );
  app.get<{ Params: { id: string } }>(
    "/api/jobs/:id",
    { schema: { params: idSchema } },
    async (request) => getJob(request.params.id, request),
  );
  app.get<{ Params: { id: string } }>(
    "/api/jobs/:id/events",
    { schema: { params: idSchema }, config: { rateLimit: false } },
    async (request, reply) => {
      await getJob(request.params.id, request);
      const owner = hash(session(request, options.secureCookies)!), ip = hash(request.ip), token = randomUUID();
      if (!await store.acquireEvents(token, owner, ip)) throw failure(429, "Troppe connessioni di aggiornamento aperte.");
      reply.hijack();
      const raw = reply.raw;
      raw.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "private, no-store, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      });
      let previous = "",
        busy = false,
        closed = false;
      const timer = setInterval(() => void send(), 1000);
      let refreshed = Date.now();
      const cleanup = () => {
        if (closed) return;
        closed = true;
        clearInterval(timer);
        streams.delete(stop);
        void store.releaseEvents(token, owner, ip).catch((error) => request.log.error(error));
      };
      const stop = () => { cleanup(); raw.end(); };
      streams.add(stop);
      raw.on("close", cleanup);
      raw.on("error", cleanup);
      async function send() {
        if (busy || closed) return;
        if (raw.destroyed || raw.writableEnded) { cleanup(); return; }
        busy = true;
        try {
          if (Date.now() - refreshed > 10_000) {
            if (!await store.renewEvents(token, owner, ip)) throw new Error("Event lease expired");
            refreshed = Date.now();
          }
          const job = await getJob(request.params.id, request);
          const json = JSON.stringify(job);
          if (raw.writableLength > 65536) throw new Error("Slow SSE client");
          if (json !== previous) {
            raw.write(`event: job\ndata: ${json}\n\n`);
            previous = json;
          } else raw.write(": keepalive\n\n");
          if (job.status === "completed" || job.status === "failed") {
            cleanup();
            raw.end();
          }
        } catch {
          cleanup();
          raw.end();
        } finally {
          busy = false;
        }
      }
      await send();
    },
  );
  app.get<{ Params: { id: string } }>(
    "/api/jobs/:id/result",
    { schema: { params: idSchema } },
    async (request) => {
      const job = await getJob(request.params.id, request);
      if (job.status !== "completed")
        throw failure(409, "Elaborazione non ancora completata.");
      return JSON.parse(
        await readFile(
          path.join(dataDir, job.id, "output", "result.json"),
          "utf8",
        ),
      );
    },
  );
  app.get<{ Params: { id: string } }>(
    "/api/jobs/:id/preview",
    { schema: { params: idSchema } },
    async (request) => {
      const job = await getJob(request.params.id, request);
      if (job.status !== "completed")
        throw failure(409, "Elaborazione non ancora completata.");
      return JSON.parse(
        await readFile(
          path.join(dataDir, job.id, "output", "preview.json"),
          "utf8",
        ),
      );
    },
  );
  app.get<{ Params: { id: string; artifact: string } }>(
    "/api/jobs/:id/files/:artifact",
    {
      schema: {
        params: {
          ...idSchema,
          required: ["id", "artifact"],
          properties: {
            ...idSchema.properties,
            artifact: { type: "string", enum: Object.keys(artifacts) },
          },
        },
      },
    },
    async (request, reply) => {
      const job = await getJob(request.params.id, request);
      if (job.status !== "completed")
        throw failure(409, "Elaborazione non ancora completata.");
      const [name, mime] = artifacts[request.params.artifact];
      const file = path.join(dataDir, job.id, "output", name);
      const size = (await stat(file)).size;
      reply.header("Accept-Ranges", "bytes").type(mime);
      // PCM and clicks can be streamed/searched without buffering the full WAV in memory.
      const inline =
        request.params.artifact === "pcm" ||
        request.params.artifact === "clicks" ||
        request.params.artifact === "debug";
      reply.header(
        "Content-Disposition",
        `${inline ? "inline" : "attachment"}; filename="${name}"`,
      );
      const range = request.headers.range;
      if (range) {
        const match = /^bytes=(\d*)-(\d*)$/.exec(range);
        let start = 0,
          end = size - 1;
        if (!match || (!match[1] && !match[2]))
          return reply
            .code(416)
            .header("Content-Range", `bytes */${size}`)
            .send();
        if (!match[1]) start = Math.max(0, size - Number(match[2]));
        else {
          start = Number(match[1]);
          if (match[2]) end = Math.min(end, Number(match[2]));
        }
        if (
          !Number.isSafeInteger(start) ||
          !Number.isSafeInteger(end) ||
          start < 0 ||
          start > end ||
          start >= size
        )
          return reply
            .code(416)
            .header("Content-Range", `bytes */${size}`)
            .send();
        return reply
          .code(206)
          .header("Content-Range", `bytes ${start}-${end}/${size}`)
          .header("Content-Length", end - start + 1)
          .send(createReadStream(file, { start, end }));
      }
      return reply.header("Content-Length", size).send(createReadStream(file));
    },
  );
  return app;
}
