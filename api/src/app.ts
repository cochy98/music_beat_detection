import Fastify from "fastify";
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
}) {
  const { store, dataDir } = options;
  const maxUploadBytes = options.maxUploadBytes ?? 200 * 1024 * 1024;
  const app = Fastify({
    logger: options.logger ?? true,
    requestTimeout: 300_000,
    bodyLimit: 65536,
  });
  await app.register(multipart, {
    limits: {
      fileSize: maxUploadBytes,
      files: 1,
      fields: 1,
      parts: 2,
      fieldSize: 16384,
    },
  });
  await app.register(rateLimit, { max: 120, timeWindow: "1 minute" });
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
    };
  });
  app.get("/api/config", async () => ({
    algorithms,
    defaults,
    maxUploadBytes,
  }));
  const getJob = async (id: string) => {
    const job = await store.get(id);
    if (!job) throw failure(404, "Job non trovato.");
    return job;
  };
  app.post(
    "/api/jobs",
    { config: { rateLimit: { max: 10, timeWindow: "1 minute" } } },
    async (request, reply) => {
      const id = randomUUID();
      const directory = path.join(dataDir, id);
      await mkdir(directory, { recursive: true });
      let filename = "",
        input = "",
        config: unknown;
      let committed = false;
      try {
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
        await store.enqueue(job, input);
        committed = true;
        return reply.code(202).header("Location", `/api/jobs/${id}`).send(job);
      } finally {
        if (!committed) await rm(directory, { recursive: true, force: true });
      }
    },
  );
  app.get<{ Params: { id: string } }>(
    "/api/jobs/:id",
    { schema: { params: idSchema } },
    async (request) => getJob(request.params.id),
  );
  app.get<{ Params: { id: string } }>(
    "/api/jobs/:id/events",
    { schema: { params: idSchema }, config: { rateLimit: false } },
    async (request, reply) => {
      await getJob(request.params.id);
      reply.hijack();
      const raw = reply.raw;
      raw.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      });
      let previous = "",
        busy = false,
        closed = false;
      const timer = setInterval(() => void send(), 1000);
      const cleanup = () => {
        closed = true;
        clearInterval(timer);
      };
      raw.on("close", cleanup);
      async function send() {
        if (busy || closed) return;
        busy = true;
        try {
          const job = await getJob(request.params.id);
          const json = JSON.stringify(job);
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
      const job = await getJob(request.params.id);
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
      const job = await getJob(request.params.id);
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
      const job = await getJob(request.params.id);
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
