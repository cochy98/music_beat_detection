import { z } from "zod";

export const algorithms = [
  {
    id: "madmom",
    name: "madmom",
    description:
      "Rete ricorrente + DBN. Il punto di partenza per beat e misure.",
  },
  {
    id: "beat-this",
    name: "Beat This!",
    description:
      "Transformer. Rileva beat e inizi di misura con un modello neurale.",
  },
  {
    id: "ensemble",
    name: "Ensemble",
    description:
      "Beat This! con verifica madmom. Evidenzia i disaccordi tra i modelli.",
  },
] as const;

export const frameRates = [
  "24",
  "25",
  "30",
  "50",
  "60",
  "23.976",
  "29.97",
  "59.94",
  "24000/1001",
  "30000/1001",
  "60000/1001",
] as const;
const timecode = z
  .string()
  .regex(/^\d{2}:\d{2}:\d{2}[:;]\d{2}$/, "Usa HH:MM:SS:FF");
export const configSchema = z
  .object({
    backend: z.enum(["madmom", "beat-this", "ensemble"]).default("madmom"),
    markerType: z.enum(["beats", "downbeats"]).default("downbeats"),
    mode: z.enum(["grid", "attacks"]).default("grid"),
    beatsPerBar: z
      .string()
      .regex(/^(auto|(?:[2-9]|1[0-2])(?:,(?:[2-9]|1[0-2]))*)$/)
      .default("auto"),
    bpm: z.number().min(10).max(600).optional(),
    minBpm: z.number().min(10).max(599).default(40),
    maxBpm: z.number().min(11).max(600).default(240),
    transitionLambda: z.number().positive().max(1000).default(100),
    activationThreshold: z.number().min(0).max(0.99).default(0.05),
    checkpoint: z.enum(["final0", "small0"]).default("final0"),
    beatThisDbn: z.boolean().default(false),
    strict: z.boolean().default(false),
    minConfidence: z.number().min(0).max(1).default(0.65),
    maxSnapMs: z.number().min(0).max(150).default(50),
    refine: z.boolean().default(true),
    percussive: z.boolean().default(true),
    librosaCheck: z.boolean().default(true),
    silenceDb: z.number().min(-120).max(0).default(-60),
    fps: z.enum(frameRates).default("25"),
    dropFrame: z.boolean().default(false),
    timelineStart: timecode.default("01:00:00:00"),
    audioStart: timecode.optional(),
    sourceIn: z.number().min(0).max(86400).default(0),
    sourceOut: z.number().positive().max(86400).optional(),
    offsetMs: z.number().min(-60000).max(60000).default(0),
    debugStart: z.number().min(0).max(86400).default(0),
    debugDuration: z.number().positive().max(86400).optional(),
  })
  .strict()
  .superRefine((c, ctx) => {
    const issue = (path: string, message: string) =>
      ctx.addIssue({ code: "custom", path: [path], message });
    if (c.minBpm >= c.maxBpm)
      issue("maxBpm", "Il BPM massimo deve superare il minimo.");
    if (
      c.bpm !== undefined &&
      Math.max(c.minBpm, c.bpm * 0.85) >= Math.min(c.maxBpm, c.bpm * 1.15)
    )
      issue("bpm", "Il suggerimento BPM è fuori dall’intervallo.");
    if (c.sourceOut !== undefined && c.sourceOut <= c.sourceIn)
      issue("sourceOut", "La fine della clip deve seguire l’inizio.");
    const ntsc = ["29.97", "59.94", "30000/1001", "60000/1001"].includes(c.fps);
    if (c.dropFrame && !ntsc)
      issue("dropFrame", "Drop-frame disponibile solo a 29.97 o 59.94 FPS.");
    const nominal = Math.round(
      c.fps.includes("/")
        ? Number(c.fps.split("/")[0]) / Number(c.fps.split("/")[1])
        : Number(c.fps),
    );
    const toFrames = (value: string, path: string) => {
      if (value.includes(";") && !c.dropFrame)
        issue(path, "Il separatore ; richiede il formato drop-frame.");
      const [h, m, s, f] = value.split(/[:;]/).map(Number);
      if (h > 23 || m > 59 || s > 59 || f >= nominal)
        issue(path, "Timecode fuori intervallo per gli FPS selezionati.");
      const dropped = nominal === 60 ? 4 : 2;
      if (c.dropFrame && m % 10 !== 0 && s === 0 && f < dropped)
        issue(path, "Etichetta di fotogramma saltata nel formato drop-frame.");
      return ((h * 60 + m) * 60 + s) * nominal + f;
    };
    const start = toFrames(c.timelineStart, "timelineStart");
    if (c.audioStart && toFrames(c.audioStart, "audioStart") < start)
      issue(
        "audioStart",
        "La clip non può precedere l’origine della timeline.",
      );
  });

export type Config = z.infer<typeof configSchema>;
export const defaults: Config = configSchema.parse({});
export type JobStatus = "queued" | "running" | "completed" | "failed";
export interface Job {
  id: string;
  filename: string;
  status: JobStatus;
  stage: string;
  progress: number;
  createdAt: string;
  updatedAt: string;
  error?: string;
  config: Config;
  attempts: number;
}
export interface Marker {
  beat: number;
  bar: number | null;
  beat_position: number;
  is_downbeat: boolean;
  source_start_seconds: number;
  original_source_seconds: number;
  export_source_seconds: number;
  timeline_timecode: string;
  confidence: number;
  needs_review: boolean;
  quality_flags: string[];
}
export interface Report {
  audio: { duration_seconds: number; original_sample_rate: number };
  analysis: {
    estimated_bpm: number | null;
    beats_detected: number;
    bars_detected: number;
    markers_exported: number;
    markers_requiring_review: number;
    warnings: string[];
  };
  timeline: {
    source_in_seconds: number;
    source_out_seconds: number;
    fps: number;
  };
  markers: Marker[];
}
export interface Preview {
  duration: number;
  secondsPerPeak?: number;
  peaks: number[][];
}
