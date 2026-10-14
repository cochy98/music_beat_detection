import { createHash, randomBytes } from "node:crypto";
import type { FastifyRequest } from "fastify";
export const cookieName = (secure = false) => secure ? "__Host-beat_session" : "beat_session";
export const newSession = () => randomBytes(32).toString("hex");
export const hash = (value: string) => createHash("sha256").update(value).digest("hex");
export function session(request: FastifyRequest, secure = false) {
  const name = cookieName(secure);
  const value = request.headers.cookie?.split(";").map((v) => v.trim()).find((v) => v.startsWith(`${name}=`))?.slice(name.length + 1);
  return value && /^[a-f0-9]{64}$/.test(value) ? value : null;
}
