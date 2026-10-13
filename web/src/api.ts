import type { Config, Job } from "../../shared/config";

export async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error ?? "Impossibile contattare il server.");
  return data;
}
export function upload(
  file: File,
  config: Config,
  onProgress: (value: number) => void,
): Promise<Job> {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append("config", JSON.stringify(config));
    body.append("audio", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
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
