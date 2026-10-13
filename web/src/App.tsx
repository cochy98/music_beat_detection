import { useEffect, useRef, useState } from "react";
import {
  algorithms,
  configSchema,
  defaults,
  frameRates,
  type Config,
  type Job,
  type Preview,
  type Report,
} from "../../shared/config";
import { get, upload } from "./api";
import Timeline from "./Timeline";

type SavedJob = Pick<Job, "id" | "filename" | "createdAt">;
const loadHistory = (): SavedJob[] => {
  try {
    return JSON.parse(localStorage.getItem("beat-history") ?? "[]")
      .filter((j: SavedJob) => /^[0-9a-f-]{36}$/.test(j.id))
      .slice(0, 12);
  } catch {
    return [];
  }
};
export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [config, setConfig] = useState<Config>({ ...defaults });
  const [job, setJob] = useState<Job | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [history, setHistory] = useState<SavedJob[]>(loadHistory);
  const [activeId, setActiveId] = useState(() => loadHistory()[0]?.id ?? "");
  const [loading, setLoading] = useState(false);
  const [uploadPercent, setUploadPercent] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [connected, setConnected] = useState(true);
  const [dragging, setDragging] = useState(false);
  const [limit, setLimit] = useState(200 * 1024 * 1024);
  const [workerOnline, setWorkerOnline] = useState<boolean | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const active =
    loading || job?.status === "queued" || job?.status === "running";
  const set = <K extends keyof Config>(key: K, value: Config[K]) =>
    setConfig((c) => ({ ...c, [key]: value }));

  useEffect(() => {
    void get<{ maxUploadBytes: number }>("/api/config")
      .then((c) => setLimit(c.maxUploadBytes))
      .catch(() =>
        setError(
          "API non raggiungibile. Verifica che i servizi siano avviati.",
        ),
      );
    const check = () =>
      void get<{ workerOnline: boolean }>("/api/health")
        .then((h) => setWorkerOnline(h.workerOnline))
        .catch(() => setWorkerOnline(null));
    check();
    const timer = setInterval(check, 15000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!activeId) return;
    let alive = true,
      source: EventSource | undefined,
      fallback: ReturnType<typeof setInterval> | undefined;
    let resultLoaded = false,
      resultLoading = false;
    setJob(null);
    setReport(null);
    setPreview(null);
    setError("");
    setConnected(true);
    const receive = async (updated: Job) => {
      if (!alive) return;
      setJob(updated);
      if (updated.status === "failed") {
        source?.close();
        if (fallback) clearInterval(fallback);
        setError(updated.error ?? "Elaborazione non riuscita.");
      }
      if (updated.status === "completed" && !resultLoaded && !resultLoading) {
        source?.close();
        resultLoading = true;
        try {
          const [r, p] = await Promise.all([
            get<Report>(`/api/jobs/${activeId}/result`),
            get<Preview>(`/api/jobs/${activeId}/preview`),
          ]);
          if (alive) {
            setReport(r);
            setPreview(p);
            setNotice("Analisi completata. I tuoi marker sono pronti.");
            resultLoaded = true;
            if (fallback) clearInterval(fallback);
          }
        } catch (e) {
          if (alive) setError((e as Error).message);
        } finally {
          resultLoading = false;
        }
      }
    };
    const poll = () =>
      void get<Job>(`/api/jobs/${activeId}`)
        .then(receive)
        .catch((e) => {
          if (alive) setError(e.message);
        });
    // Polling also retries result loading and keeps the UI usable if SSE is interrupted.
    fallback = setInterval(poll, 5000);
    void get<Job>(`/api/jobs/${activeId}`)
      .then(async (j) => {
        if (alive) setConfig(j.config);
        await receive(j);
        if (!alive || j.status === "completed" || j.status === "failed") return;
        source = new EventSource(`/api/jobs/${activeId}/events`);
        source.addEventListener("job", (event) => {
          setConnected(true);
          void receive(JSON.parse((event as MessageEvent).data));
        });
        source.onopen = () => setConnected(true);
        source.onerror = () => {
          if (alive) setConnected(false);
        };
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
      source?.close();
      if (fallback) clearInterval(fallback);
    };
  }, [activeId]);
  useEffect(() => {
    document.title =
      job?.status === "running"
        ? "Analisi in corso · Beat Studio"
        : job?.status === "completed"
          ? "Marker pronti · Beat Studio"
          : "Beat Studio · Marker musicali";
  }, [job?.status]);
  useEffect(() => {
    if (notice) {
      const timer = setTimeout(() => setNotice(""), 6500);
      return () => clearTimeout(timer);
    }
  }, [notice]);

  const choose = (selected?: File) => {
    if (!selected) return;
    if (!/\.(mp3|wav|flac|ogg|m4a|aac|aiff|aif|opus)$/i.test(selected.name)) {
      setError("Scegli un file MP3, WAV, FLAC, OGG, M4A, AAC, AIFF o OPUS.");
      return;
    }
    if (!selected.size || selected.size > limit) {
      setError(
        `Carica un file non vuoto, fino a ${Math.round(limit / 1024 / 1024)} MB.`,
      );
      return;
    }
    setFile(selected);
    setError("");
  };
  const submit = async () => {
    if (!file) return;
    const parsed = configSchema.safeParse(config);
    if (!parsed.success) {
      setError(parsed.error.issues.map((i) => i.message).join(" "));
      return;
    }
    setLoading(true);
    setUploadPercent(0);
    setError("");
    try {
      const created = await upload(file, parsed.data, setUploadPercent);
      const recent = [
        {
          id: created.id,
          filename: created.filename,
          createdAt: created.createdAt,
        },
        ...history,
      ].slice(0, 12);
      setHistory(recent);
      try {
        localStorage.setItem("beat-history", JSON.stringify(recent));
      } catch {
        /* Storage can be disabled; the current job remains usable. */
      }
      setActiveId(created.id);
      setJob(created);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };
  const number = (
    key: keyof Config,
    label: string,
    min: number,
    max: number,
    step = 1,
    optional = false,
  ) => (
    <label className="field">
      {label}
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={(config[key] as number) ?? ""}
        placeholder={optional ? "Automatico" : ""}
        onChange={(e) =>
          set(
            key,
            (e.target.value === "" && optional
              ? undefined
              : Number(e.target.value)) as never,
          )
        }
      />
    </label>
  );
  const checkbox = (key: keyof Config, label: string) => (
    <label className="switch">
      <input
        type="checkbox"
        checked={Boolean(config[key])}
        onChange={(e) => set(key, e.target.checked as never)}
      />
      {label}
    </label>
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Beat Studio home">
          <span className="brand-mark">
            <i />
            <i />
            <i />
            <i />
            <i />
          </span>
          beat<span>studio</span>
        </a>
        <span className="topbar-caption">DALL’AUDIO AL MONTAGGIO</span>
        <span className="service">
          <i className={workerOnline === true ? "online" : "offline"} />
          {workerOnline === true
            ? "Motore pronto"
            : workerOnline === false
              ? "Motore offline"
              : "Connessione…"}
        </span>
      </header>
      <div className="workspace">
        <aside id="configuration" className="sidebar">
          <div className="sidebar-title">
            <span className="eyebrow">CONFIGURAZIONE</span>
            <span className="step-badge">01 — 03</span>
          </div>
          <fieldset disabled={Boolean(active)}>
            <div className="section-label">
              <span>01</span>
              <h2>Il tuo audio</h2>
            </div>
            <button
              type="button"
              className={`upload-zone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""}`}
              onClick={() => input.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                if (!active) choose(e.dataTransfer.files[0]);
              }}
            >
              <span className="upload-symbol">{file ? "♫" : "↥"}</span>
              <strong>{file?.name ?? "Trascina qui il tuo brano"}</strong>
              <span>
                {file
                  ? `${(file.size / 1024 / 1024).toFixed(1)} MB · clicca per cambiare`
                  : "oppure scegli un file"}
              </span>
              <small>
                MP3, WAV, FLAC e altri · max {Math.round(limit / 1024 / 1024)}{" "}
                MB
              </small>
            </button>
            <input
              ref={input}
              className="sr-only"
              type="file"
              accept=".mp3,.wav,.flac,.ogg,.m4a,.aac,.aiff,.aif,.opus"
              onChange={(e) => choose(e.target.files?.[0])}
              aria-label="Carica musica"
            />
            <div className="section-label">
              <span>02</span>
              <h2>Il rilevatore</h2>
            </div>
            <div className="algorithms">
              {algorithms.map((a) => (
                <label
                  className={`algorithm ${config.backend === a.id ? "selected" : ""}`}
                  key={a.id}
                >
                  <input
                    type="radio"
                    name="algorithm"
                    value={a.id}
                    checked={config.backend === a.id}
                    onChange={() => set("backend", a.id)}
                  />
                  <div>
                    <strong>
                      {a.name}
                      {a.id === "ensemble" && <small>CONFRONTO</small>}
                    </strong>
                    <p>{a.description}</p>
                  </div>
                  <span className="radio-dot" />
                </label>
              ))}
            </div>
            <div className="section-label">
              <span>03</span>
              <h2>I tuoi marker</h2>
            </div>
            <label className="field">
              Rileva
              <select
                value={config.markerType}
                onChange={(e) =>
                  set("markerType", e.target.value as Config["markerType"])
                }
              >
                <option value="downbeats">Inizi di misura</option>
                <option value="beats">Ogni beat</option>
              </select>
            </label>
            <div className="fields-grid">
              <label className="field">
                Metro
                <select
                  value={config.beatsPerBar}
                  onChange={(e) => set("beatsPerBar", e.target.value)}
                >
                  <option value="auto">Auto (3 o 4)</option>
                  {[2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((n) => (
                    <option key={n} value={n}>
                      {n} tempi
                    </option>
                  ))}
                  <option value="3,4">3 e 4</option>
                </select>
              </label>
              {number("bpm", "BPM suggerito", 10, 600, 1, true)}
            </div>
            <label className="field">
              Selezione
              <select
                value={config.mode}
                onChange={(e) => set("mode", e.target.value as Config["mode"])}
              >
                <option value="grid">
                  Griglia ritmica · include beat impliciti
                </option>
                <option value="attacks">Solo attacchi sonori confermati</option>
              </select>
            </label>
            {checkbox("strict", "Escludi i marker da verificare")}
            <details className="advanced">
              <summary>
                Parametri avanzati <span>＋</span>
              </summary>
              <div className="advanced-content">
                <h3>Rilevamento e qualità</h3>
                <div className="fields-grid">
                  {number("minBpm", "BPM minimo", 10, 599)}
                  {number("maxBpm", "BPM massimo", 11, 600)}
                  {number("transitionLambda", "Stabilità DBN", 1, 1000)}
                  {number(
                    "activationThreshold",
                    "Soglia attivazione",
                    0,
                    0.99,
                    0.01,
                  )}
                  {number("minConfidence", "Soglia qualità", 0, 1, 0.01)}
                  {number("maxSnapMs", "Snap massimo (ms)", 0, 150)}
                  {number("silenceDb", "Silenzio (dBFS)", -120, 0)}
                </div>
                {config.backend !== "madmom" && (
                  <>
                    <label className="field">
                      Checkpoint
                      <select
                        value={config.checkpoint}
                        onChange={(e) =>
                          set(
                            "checkpoint",
                            e.target.value as Config["checkpoint"],
                          )
                        }
                      >
                        <option value="final0">final0</option>
                        <option value="small0">small0 · più leggero</option>
                      </select>
                    </label>
                    {checkbox("beatThisDbn", "Decoder DBN per Beat This!")}
                  </>
                )}
                {checkbox("refine", "Affina le posizioni sugli attacchi")}
                {checkbox("percussive", "Analisi percussiva HPSS")}
                {checkbox("librosaCheck", "Verifica ausiliaria librosa")}
                <h3>Esportazione timeline</h3>
                <label className="field">
                  Fotogrammi al secondo
                  <select
                    value={config.fps}
                    onChange={(e) =>
                      set("fps", e.target.value as Config["fps"])
                    }
                  >
                    {frameRates.map((fps) => (
                      <option key={fps}>{fps}</option>
                    ))}
                  </select>
                </label>
                {checkbox("dropFrame", "Timecode drop-frame")}
                <label className="field">
                  Origine timeline
                  <input
                    value={config.timelineStart}
                    onChange={(e) => set("timelineStart", e.target.value)}
                    placeholder="01:00:00:00"
                  />
                </label>
                <label className="field">
                  Inizio clip (timecode)
                  <input
                    value={config.audioStart ?? ""}
                    onChange={(e) =>
                      set("audioStart", e.target.value || undefined)
                    }
                    placeholder="Come l’origine della timeline"
                  />
                </label>
                <div className="fields-grid">
                  {number("sourceIn", "Trim iniziale (s)", 0, 86400, 0.001)}
                  {number(
                    "sourceOut",
                    "Trim finale (s)",
                    0,
                    86400,
                    0.001,
                    true,
                  )}
                  {number("offsetMs", "Offset marker (ms)", -60000, 60000)}
                  {number("debugStart", "Inizio grafico (s)", 0, 86400, 0.1)}
                  {number(
                    "debugDuration",
                    "Durata grafico (s)",
                    0,
                    86400,
                    0.1,
                    true,
                  )}
                </div>
                <p className="hint">
                  La qualità è un punteggio euristico. Controlla i marker nel
                  contesto musicale prima del montaggio.
                </p>
              </div>
            </details>
          </fieldset>
          <button
            className="analyze-button"
            disabled={!file || Boolean(active)}
            onClick={() => void submit()}
          >
            {loading
              ? `Caricamento ${uploadPercent}%`
              : active
                ? "Elaborazione in corso"
                : "Rileva i marker"}
            <span>↗</span>
          </button>
          <p className="sidebar-footnote">
            L’analisi avviene sull’intero brano.
            <br />I trim si applicano ai marker esportati.
          </p>
        </aside>
        <main>
          <div className="page-heading">
            <div>
              <span className="eyebrow">IL TUO SPAZIO DI LAVORO</span>
              <h1>
                Trova il ritmo.
                <br />
                <span>Dai forma al montaggio.</span>
              </h1>
              <p>
                Dal primo beat all’ultimo taglio. Marker musicali, pronti per la
                tua timeline.
              </p>
              <a className="mobile-upload-link" href="#configuration">
                Carica e configura ↓
              </a>
            </div>
            <div className="heading-art" aria-hidden="true">
              {Array.from({ length: 17 }, (_, i) => (
                <i
                  key={i}
                  style={{ height: `${18 + Math.sin(i * 1.4) ** 2 * 68}px` }}
                />
              ))}
            </div>
          </div>
          {history.length > 0 && (
            <div className="history-row">
              <span>SESSIONI RECENTI</span>
              <select
                aria-label="Sessioni recenti"
                disabled={loading}
                value={activeId}
                onChange={(e) => setActiveId(e.target.value)}
              >
                {history.map((h) => (
                  <option key={h.id} value={h.id}>
                    {h.filename} ·{" "}
                    {new Date(h.createdAt).toLocaleString("it-IT", {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </option>
                ))}
              </select>
            </div>
          )}
          {error && (
            <div role="alert" className="error-banner">
              <span>!</span>
              <div>{error}</div>
              <button onClick={() => setError("")} aria-label="Chiudi errore">
                ×
              </button>
            </div>
          )}
          {workerOnline === false && (
            <p className="warning-note">
              Il worker non è connesso. I nuovi job rimarranno in coda fino al
              suo avvio.
            </p>
          )}
          {loading && (
            <div className="job-progress">
              <div>
                <span className="spinner" />
                <strong>Caricamento del brano</strong>
                <span>{uploadPercent}%</span>
              </div>
              <progress max="100" value={uploadPercent} />
            </div>
          )}
          {job && (job.status === "queued" || job.status === "running") && (
            <div className="job-progress" role="status">
              <div>
                <span className="spinner" />
                <strong>{job.stage}</strong>
                <span>
                  {job.status === "queued" ? "IN ATTESA" : "IN CORSO"}
                </span>
              </div>
              <progress max="100" value={job.progress} />
              <p>
                {job.filename} ·{" "}
                {connected
                  ? "Ti avvisiamo appena i marker sono pronti."
                  : "Riconnessione alle notifiche; aggiornamento periodico attivo."}
              </p>
              <small>
                Le fasi indicano l’avanzamento, non il tempo rimanente. Il primo
                uso di Beat This! scarica il modello.
              </small>
            </div>
          )}
          {report && preview && job ? (
            <>
              <div className="results-heading">
                <div>
                  <span className="eyebrow">ANALISI COMPLETATA</span>
                  <h2>{job.filename}</h2>
                </div>
                <a
                  className="download-button"
                  href={`/api/jobs/${job.id}/files/zip`}
                >
                  ↓ Scarica tutto <span>ZIP</span>
                </a>
              </div>
              <div className="stats">
                <div>
                  <span>MARKER ESPORTATI</span>
                  <strong>
                    {report.analysis.markers_exported}
                    <small>punti di montaggio</small>
                  </strong>
                </div>
                <div>
                  <span>TEMPO STIMATO</span>
                  <strong>
                    {report.analysis.estimated_bpm?.toFixed(1) ?? "—"}
                    <small>BPM</small>
                  </strong>
                </div>
                <div>
                  <span>DA VERIFICARE</span>
                  <strong className="amber">
                    {report.analysis.markers_requiring_review}
                    <small>marker</small>
                  </strong>
                </div>
              </div>
              <Timeline
                key={job.id}
                report={report}
                preview={preview}
                jobId={job.id}
              />
              {report.markers.length === 0 && (
                <p className="warning-note">
                  Nessun marker esportato. Prova ogni beat, allarga l’intervallo
                  BPM o disattiva il filtro rigoroso.
                </p>
              )}
              {report.analysis.warnings.length > 0 && (
                <details className="analysis-warnings">
                  <summary>
                    {report.analysis.warnings.length} avvisi del rilevatore
                  </summary>
                  {report.analysis.warnings.map((w, i) => (
                    <p key={i}>{w}</p>
                  ))}
                </details>
              )}
              <section className="exports">
                <div>
                  <span className="eyebrow">PORTA IL RITMO NEL TUO EDITOR</span>
                  <h3>I file del tuo progetto</h3>
                </div>
                <div className="export-links">
                  {[
                    ["edl", "Marker Resolve", "EDL"],
                    ["json", "Report completo", "JSON"],
                    ["clicks", "Audio con click", "WAV"],
                    ["pcm", "Audio PCM", "WAV"],
                    ["debug", "Grafico diagnostico", "PNG"],
                  ].map(([id, name, ext]) => (
                    <a
                      key={id}
                      href={`/api/jobs/${job.id}/files/${id}`}
                      download
                    >
                      <span>{ext}</span>
                      <strong>{name}</strong>
                      <i>↗</i>
                    </a>
                  ))}
                </div>
              </section>
            </>
          ) : (
            <section className="empty-state">
              <div className="empty-timeline" aria-hidden="true">
                <div className="empty-ruler">
                  <span>00:00</span>
                  <span>00:10</span>
                  <span>00:20</span>
                  <span>00:30</span>
                </div>
                <div className="empty-wave">
                  {Array.from({ length: 70 }, (_, i) => (
                    <i
                      key={i}
                      style={{
                        height: `${8 + Math.sin(i * 0.71) ** 2 * 40}px`,
                      }}
                    />
                  ))}
                </div>
                <div className="empty-markers">
                  <i />
                  <i />
                  <i />
                  <i />
                </div>
              </div>
              <span className="empty-icon">♫</span>
              <h2>Ogni brano ha i suoi punti di svolta.</h2>
              <p>
                Carica un audio e scegli il rilevatore.
                <br />
                Qui troverai la forma d’onda e i marker da esplorare.
              </p>
              <div className="empty-features">
                <span>
                  01 <strong>Carica</strong>
                </span>
                <span>
                  02 <strong>Rileva</strong>
                </span>
                <span>
                  03 <strong>Monta</strong>
                </span>
              </div>
            </section>
          )}
          <footer>
            BEAT STUDIO <span>Marker per DaVinci Resolve · Analisi CPU</span>
            <span>Ascolta. Verifica. Crea.</span>
          </footer>
        </main>
      </div>
      {notice && (
        <div className="toast" role="status">
          <span>✓</span>
          {notice}
          <button aria-label="Chiudi notifica" onClick={() => setNotice("")}>
            ×
          </button>
        </div>
      )}
    </div>
  );
}
