"""One-to-one timing evaluation against human annotations in source seconds."""

import argparse
import json
import math
from pathlib import Path
import sys

import numpy as np


def match_events(reference, estimated, tolerance):
    """Maximize matched events, then minimize total timing error (monotone DP).

    Each reference and detection can be used at most once. This avoids optimistic
    nearest-neighbor metrics when a detector produces several markers per beat.
    """
    reference, estimated = np.sort(reference), np.sort(estimated)
    n, m = len(reference), len(estimated)
    if n * m > 25_000_000:
        raise ValueError("Evaluation too large; split annotations into sections (max 25 million pairs).")
    choices = np.zeros((n + 1, m + 1), dtype=np.uint8)
    counts = np.zeros(m + 1, dtype=np.int32)
    costs = np.zeros(m + 1)
    for i in range(1, n + 1):
        next_counts = np.zeros(m + 1, dtype=np.int32)
        next_costs = np.zeros(m + 1)
        for j in range(1, m + 1):
            best = (int(counts[j]), -costs[j], 1)
            best = max(best, (int(next_counts[j - 1]), -next_costs[j - 1], 2))
            difference = abs(float(estimated[j - 1] - reference[i - 1]))
            if difference <= tolerance + 1e-12:
                best = max(best, (int(counts[j - 1]) + 1, -(costs[j - 1] + difference), 3))
            next_counts[j], next_costs[j], choices[i, j] = best[0], -best[1], best[2]
        counts, costs = next_counts, next_costs
    pairs = []
    i, j = n, m
    while i and j:
        choice = choices[i, j]
        if choice == 3:
            pairs.append((float(reference[i - 1]), float(estimated[j - 1])))
            i, j = i - 1, j - 1
        elif choice == 1:
            i -= 1
        else:
            j -= 1
    return list(reversed(pairs))


def evaluate(reference, estimated, tolerance):
    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("Tolerance must be finite and positive.")
    for values in (reference, estimated):
        if not np.all(np.isfinite(values)) or np.any(np.asarray(values) < 0):
            raise ValueError("Annotations and estimates must be finite, non-negative seconds.")
    pairs = match_events(reference, estimated, tolerance)
    count = len(pairs)
    precision = count / len(estimated) if len(estimated) else 0.0
    recall = count / len(reference) if len(reference) else 0.0
    errors = np.array([estimate - truth for truth, estimate in pairs]) * 1000
    return {
        "reference_events": len(reference), "estimated_events": len(estimated), "matched_events": count,
        "false_positives": len(estimated) - count, "missed_events": len(reference) - count,
        "precision": precision, "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "tolerance_ms": tolerance * 1000,
        "mean_absolute_error_ms": float(np.mean(np.abs(errors))) if count else None,
        "p95_absolute_error_ms": float(np.percentile(np.abs(errors), 95)) if count else None,
        "signed_bias_ms": float(np.mean(errors)) if count else None,
        "timing_statistics_scope": "Matched events only; assess false positives and misses alongside timing error.",
    }


def read_reference(path, kind):
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            key = "bars" if kind == "downbeats" else kind
            data = data[key]
        return np.asarray([item["source_start_seconds"] if isinstance(item, dict) else float(item) for item in data])
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            values.append(float(line))
    return np.asarray(values)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare detection JSON with human source-time annotations.")
    parser.add_argument("detection")
    parser.add_argument("reference", help="TXT: one time in seconds per line, or JSON list")
    parser.add_argument("--kind", choices=("beats", "downbeats", "markers"), default="downbeats")
    parser.add_argument("--stage", choices=("original", "refined", "exported"), help="Default: refined, or exported for markers")
    parser.add_argument("--tolerance-ms", type=float, default=20)
    parser.add_argument("--output", help="Optional metrics JSON")
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.detection).read_text(encoding="utf-8"))
        stage = args.stage or ("exported" if args.kind == "markers" else "refined")
        key = "bars" if args.kind == "downbeats" else args.kind
        items = report[key]
        field = {"original": "original_source_seconds", "refined": "source_start_seconds", "exported": "export_source_seconds"}[stage]
        if stage == "exported":
            items = [item for item in items if item.get("export_status") == "exported"]
        estimated = np.array([item[field] for item in items])
        reference = read_reference(args.reference, args.kind)
        if stage == "exported":
            timeline = report["timeline"]
            reference = reference[(reference >= timeline.get("source_in_seconds", 0)) &
                                  (reference < timeline.get("source_out_seconds", report["audio"]["duration_seconds"]))]
        metrics = evaluate(reference, estimated, args.tolerance_ms / 1000)
        metrics.update(kind=args.kind, stage=stage, detection=args.detection, reference=args.reference)
        text = json.dumps(metrics, indent=2, allow_nan=False) + "\n"
        if args.output:
            path = Path(args.output)
            if path.resolve() in (Path(args.detection).resolve(), Path(args.reference).resolve()):
                raise ValueError("Metrics output cannot overwrite detection or reference files.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        print(text, end="")
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
