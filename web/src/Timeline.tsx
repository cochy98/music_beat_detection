import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Marker, Preview, Report } from "../../shared/config";

export const clock = (time: number) =>
  `${Math.floor(time / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(time % 60)
    .toString()
    .padStart(2, "0")}.${Math.floor((time % 1) * 1000)
    .toString()
    .padStart(3, "0")}`;

export default function Timeline({
  report,
  preview,
  jobId,
}: {
  report: Report;
  preview: Preview;
  jobId: string;
}) {
  const audio = useRef<HTMLAudioElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [scrollLeft, setScrollLeft] = useState(0);
  const [width, setWidth] = useState(800);
  const [selected, setSelected] = useState<Marker | null>(null);
  const [clicks, setClicks] = useState(false);
  const [error, setError] = useState("");
  const duration = preview.duration;
  const fullWidth = width * zoom;
  const previousViewport = useRef({ width, fullWidth });

  useEffect(() => {
    const observer = new ResizeObserver((entries) =>
      setWidth(entries[0].contentRect.width),
    );
    if (scroll.current) observer.observe(scroll.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      if (audio.current) setCurrent(audio.current.currentTime);
      raf = requestAnimationFrame(tick);
    };
    if (playing) raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);
  useLayoutEffect(() => {
    const viewport = scroll.current;
    if (!viewport || width <= 0 || duration <= 0) return;

    const resized =
      previousViewport.current.width !== width ||
      previousViewport.current.fullWidth !== fullWidth;
    previousViewport.current = { width, fullWidth };

    const playhead = (current / duration) * fullWidth;
    const maxScroll = Math.max(0, fullWidth - width);
    const clamp = (left: number) => Math.max(0, Math.min(maxScroll, left));
    let left = clamp(viewport.scrollLeft);

    if (resized) {
      // Keep the playback position in view when zoom or viewport size changes.
      left = clamp(playhead - width / 2);
    } else if (
      playing &&
      (playhead < left || playhead > left + width * 0.85)
    ) {
      // Advance in pages so the cursor moves across the waveform during play.
      left = clamp(playhead - width * 0.2);
    }

    if (viewport.scrollLeft !== left) viewport.scrollLeft = left;
    // Sync drawing before paint instead of waiting for the native scroll event.
    setScrollLeft(viewport.scrollLeft);
  }, [current, playing, width, fullWidth, duration]);
  useEffect(() => {
    const c = canvas.current;
    if (!c || !width || !duration) return;
    const ratio = window.devicePixelRatio || 1;
    c.width = width * ratio;
    c.height = 252 * ratio;
    const ctx = c.getContext("2d")!;
    ctx.scale(ratio, ratio);
    ctx.fillStyle = "#151d19";
    ctx.fillRect(0, 0, width, 252);
    const x = (t: number) => (t / duration) * fullWidth - scrollLeft;
    const secondsVisible = duration / zoom;
    const desired = secondsVisible / Math.max(1, width / 100);
    const steps = [
      0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600,
    ];
    const step = steps.find((s) => s >= desired) ?? 3600;
    const first =
      Math.floor(((scrollLeft / fullWidth) * duration) / step) * step;
    ctx.font = "11px ui-monospace, monospace";
    for (let t = first; x(t) < width; t += step) {
      ctx.strokeStyle = "#2a352e";
      ctx.beginPath();
      ctx.moveTo(x(t), 28);
      ctx.lineTo(x(t), 252);
      ctx.stroke();
      ctx.fillStyle = "#8b9c91";
      ctx.fillText(clock(t).slice(0, step < 1 ? 9 : 5), x(t) + 6, 18);
    }
    ctx.fillStyle = "#1d2923";
    ctx.fillRect(0, 43, width, 129);
    ctx.strokeStyle = "#436c59";
    ctx.beginPath();
    ctx.moveTo(0, 107);
    ctx.lineTo(width, 107);
    ctx.stroke();
    ctx.fillStyle = "#8fcaa9";
    const bucket = preview.secondsPerPeak
      ? (fullWidth * preview.secondsPerPeak) / duration
      : fullWidth / preview.peaks.length;
    const from = Math.max(0, Math.floor(scrollLeft / bucket));
    const to = Math.min(
      preview.peaks.length,
      Math.ceil((scrollLeft + width) / bucket),
    );
    for (let i = from; i < to; i++) {
      const [min, max] = preview.peaks[i];
      ctx.fillRect(
        i * bucket - scrollLeft,
        107 - Math.min(1, max) * 53,
        Math.max(1, bucket - 0.7),
        Math.max(1, (Math.min(1, max) - Math.max(-1, min)) * 53),
      );
    }
    const start = x(report.timeline.source_in_seconds),
      end = x(report.timeline.source_out_seconds);
    ctx.fillStyle = "#00000070";
    if (start > 0) ctx.fillRect(0, 43, Math.min(width, start), 193);
    if (end < width) ctx.fillRect(Math.max(0, end), 43, width - end, 193);
    for (const marker of report.markers) {
      const px = x(marker.export_source_seconds);
      if (px < -10 || px > width + 10) continue;
      ctx.strokeStyle = marker.needs_review ? "#edc16f" : "#8fcaa9";
      ctx.lineWidth = marker === selected ? 2 : 1;
      ctx.beginPath();
      ctx.moveTo(px, 44);
      ctx.lineTo(px, 172);
      ctx.stroke();
      ctx.fillStyle = marker === selected ? "#fff" : ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(px, 197);
      ctx.lineTo(px - 5, 190);
      ctx.lineTo(px - 5, 181);
      ctx.lineTo(px + 5, 181);
      ctx.lineTo(px + 5, 190);
      ctx.closePath();
      ctx.fill();
      if (zoom > 2 || report.markers.length < 40) {
        ctx.fillStyle = marker.needs_review ? "#edc16f" : "#8fcaa9";
        ctx.fillText(
          marker.is_downbeat ? `M${marker.bar}` : `${marker.beat}`,
          px + 4,
          215,
        );
      }
    }
    const playhead = x(current);
    ctx.strokeStyle = "#f4f5e9";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(playhead, 27);
    ctx.lineTo(playhead, 242);
    ctx.stroke();
    ctx.fillStyle = "#f4f5e9";
    ctx.beginPath();
    ctx.moveTo(playhead - 5, 27);
    ctx.lineTo(playhead + 5, 27);
    ctx.lineTo(playhead, 34);
    ctx.closePath();
    ctx.fill();
  }, [
    report,
    preview,
    width,
    duration,
    fullWidth,
    scrollLeft,
    current,
    selected,
    zoom,
  ]);

  const seek = (time: number) => {
    if (audio.current)
      audio.current.currentTime = Math.max(0, Math.min(duration, time));
    setCurrent(time);
  };
  const select = (marker: Marker) => {
    setSelected(marker);
    seek(marker.export_source_seconds);
    if (scroll.current)
      scroll.current.scrollLeft = Math.max(
        0,
        (marker.export_source_seconds / duration) * fullWidth - width / 2,
      );
  };
  const changeAudio = (value: boolean) => {
    setPlaying(false);
    setClicks(value);
    const player = audio.current!;
    const position = player.currentTime;
    player.pause();
    player.src = `/api/jobs/${jobId}/files/${value ? "clicks" : "pcm"}`;
    player.load();
    player.addEventListener(
      "loadedmetadata",
      () => {
        player.currentTime = position;
      },
      { once: true },
    );
  };
  const toggle = async () => {
    if (!audio.current) return;
    if (playing) audio.current.pause();
    else
      try {
        await audio.current.play();
        setError("");
      } catch {
        setError("Riproduzione non disponibile. Puoi scaricare il WAV.");
      }
  };
  const next = (direction: number) => {
    const markers = report.markers;
    const found =
      direction > 0
        ? markers.find((m) => m.export_source_seconds > current + 0.01)
        : [...markers]
            .reverse()
            .find((m) => m.export_source_seconds < current - 0.01);
    if (found) select(found);
  };

  return (
    <section className="timeline-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">ANTEPRIMA</span>
          <h2>Il ritmo, sulla timeline.</h2>
        </div>
        <span className="tag">
          {report.timeline.fps.toFixed(3).replace(/\.0+$/, "")} FPS
        </span>
      </div>
      <div className="transport">
        <div className="transport-main">
          <button
            className="icon-button"
            title="Marker precedente"
            aria-label="Marker precedente"
            onClick={() => next(-1)}
          >
            ⏮
          </button>
          <button
            className="play-button"
            aria-label={playing ? "Pausa" : "Riproduci"}
            onClick={() => void toggle()}
          >
            {playing ? "Ⅱ" : "▶"}
          </button>
          <button
            className="icon-button"
            title="Marker successivo"
            aria-label="Marker successivo"
            onClick={() => next(1)}
          >
            ⏭
          </button>
          <span className="time-display">
            {clock(current)} <span>/ {clock(duration)}</span>
          </span>
        </div>
        <div className="transport-options">
          <label className="switch">
            <input
              type="checkbox"
              checked={clicks}
              onChange={(e) => changeAudio(e.target.checked)}
            />
            Ascolta con click
          </label>
          <label className="zoom">
            Zoom{" "}
            <input
              type="range"
              min="1"
              max="16"
              step="1"
              value={zoom}
              onChange={(e) => setZoom(Number(e.target.value))}
            />
            {zoom}×
          </label>
        </div>
      </div>
      <audio
        ref={audio}
        src={`/api/jobs/${jobId}/files/pcm`}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(e) => setCurrent(e.currentTarget.currentTime)}
        onError={() =>
          setError(
            "Impossibile riprodurre questo audio. Scarica il WAV per ascoltarlo.",
          )
        }
      />
      <div className="editor">
        <div className="track-labels">
          <span>SORGENTE</span>
          <div>
            ♫<strong>Audio PCM</strong>
            <small>44.1 kHz</small>
          </div>
          <div>
            ⚑<strong>Marker</strong>
            <small>{report.markers.length} punti</small>
          </div>
        </div>
        <div
          ref={scroll}
          className="timeline-scroll"
          onScroll={(e) => setScrollLeft(e.currentTarget.scrollLeft)}
        >
          <div style={{ width: fullWidth, height: 252 }}>
            <canvas
              ref={canvas}
              style={{ width, height: 252, position: "sticky", left: 0 }}
              aria-label="Forma d’onda e marker. Clicca per spostare il cursore; usa la lista sotto per selezionare un marker."
              onClick={(e) => {
                const x =
                  e.clientX - e.currentTarget.getBoundingClientRect().left;
                const time = ((x + scrollLeft) / fullWidth) * duration;
                const marker = report.markers.reduce<Marker | null>(
                  (best, m) =>
                    !best ||
                    Math.abs(m.export_source_seconds - time) <
                      Math.abs(best.export_source_seconds - time)
                      ? m
                      : best,
                  null,
                );
                if (
                  marker &&
                  (Math.abs(marker.export_source_seconds - time) / duration) *
                    fullWidth <
                    8
                )
                  select(marker);
                else {
                  setSelected(null);
                  seek(time);
                }
              }}
            />
          </div>
        </div>
      </div>
      <div className="timeline-footer">
        <div className="legend">
          <span>
            <i />
            Confermato
          </span>
          <span>
            <i className="review" />
            Da verificare
          </span>
        </div>
        <span>
          Tempi del sorgente · clicca per ascoltare · scorri per esplorare
        </span>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <div className="marker-inspector">
        <div>
          <span className="eyebrow">DETTAGLIO MARKER</span>
          <h3>
            {selected
              ? `${selected.is_downbeat ? "Misura" : "Beat"} ${selected.is_downbeat ? selected.bar : selected.beat}`
              : "Seleziona un punto sulla timeline"}
          </h3>
        </div>
        {selected && (
          <>
            <dl>
              <div>
                <dt>Sorgente</dt>
                <dd>{clock(selected.export_source_seconds)}</dd>
              </div>
              <div>
                <dt>Timecode export</dt>
                <dd>{selected.timeline_timecode}</dd>
              </div>
              <div>
                <dt>Qualità euristica</dt>
                <dd>{Math.round(selected.confidence * 100)} / 100</dd>
              </div>
            </dl>
            <span className={`tag ${selected.needs_review ? "warning" : ""}`}>
              {selected.needs_review ? "Da verificare" : "Confermato"}
            </span>
          </>
        )}
      </div>
      {selected?.quality_flags.length ? (
        <p className="quality-note">
          {selected.quality_flags
            .map(
              (flag) =>
                ({
                  silence_or_very_low_energy: "Silenzio o energia molto bassa",
                  ambiguous_attacks: "Attacchi ambigui",
                  no_nearby_attack: "Nessun attacco vicino",
                  weak_model_activation: "Risposta debole del modello",
                  irregular_local_pulse: "Pulsazione locale irregolare",
                  neural_trackers_disagree: "Disaccordo tra i modelli",
                  refinement_rejected_by_rhythm:
                    "Correzione incompatibile con il ritmo",
                })[flag] ?? flag,
            )
            .join(" · ")}
        </p>
      ) : null}
      <details className="marker-list">
        <summary>Esplora tutti i {report.markers.length} marker</summary>
        <div>
          {report.markers.map((m) => (
            <button
              key={m.beat}
              className={m.needs_review ? "review" : ""}
              onClick={() => select(m)}
            >
              {m.is_downbeat ? `M${m.bar}` : `B${m.beat}`}{" "}
              <span>{clock(m.export_source_seconds)}</span>
            </button>
          ))}
        </div>
      </details>
    </section>
  );
}
