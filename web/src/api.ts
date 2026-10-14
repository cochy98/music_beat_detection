import type { Config, Job } from "../../shared/config";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
let initialization: Promise<void> | undefined;
export function ensureSession(): Promise<void> {
  return initialization ??= fetch("/api/session", { method: "POST", headers: { "X-Beat-Studio": "1" } })
    .then(async (r) => { if (!r.ok) throw new ApiError((await r.json()).error ?? "Sessione non disponibile.", r.status); })
    .catch((e) => { initialization = undefined; throw e; });
}
export async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  let data;
  try { data = await response.json(); }
  catch { throw new ApiError("Servizio temporaneamente non disponibile. Riprova tra poco.", response.status); }
  if (!response.ok)
    throw new ApiError(data.error ?? "Impossibile contattare il server.", response.status);
  return data;
}
export async function upload(
  file: File,
  config: Config,
  onProgress: (value: number) => void,
): Promise<Job> {
  await ensureSession();
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append("config", JSON.stringify(config));
    body.append("audio", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.setRequestHeader("X-Beat-Studio", "1");
    xhr.timeout = 300_000;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable)
        onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onerror = () =>
      reject(new Error("Connessione interrotta durante il caricamento."));
    xhr.ontimeout = () =>
      reject(
        new Error("Caricamento scaduto. Riprova con un file più piccolo."),
      );
    xhr.onload = () => {
      try {
        const result = JSON.parse(xhr.responseText);
        if (xhr.status !== 202)
          reject(new Error(result.error ?? "Caricamento non riuscito."));
        else resolve(result);
      } catch {
        reject(new Error("Risposta del server non valida."));
      }
    };
    xhr.send(body);
  });
}
