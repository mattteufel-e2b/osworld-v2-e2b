#!/usr/bin/env python3
"""Standalone E2B workload resource profiler.

Publish real CPU / RAM / DISK requirements for running a benchmark on E2B by
querying E2B's own sandbox metrics REST API for sandboxes a run already created.

Design goals (deliberate, read before editing):

* **Portable and standalone.** This file imports only the Python stdlib and the
  `e2b` SDK. It imports *nothing* from any benchmark implementation (no relay,
  provider, harness, or `env_registry` package). Each benchmark will become its
  own repo; this script is meant to be copied in verbatim and run against any of
  them. Keep it a single file with no repo-internal imports.

* **Zero perturbation, zero code in the vended environment.** Resource numbers
  come from `Sandbox.get_metrics(sandbox_id=...)` — E2B's authoritative
  server-side accounting — not from an in-guest sampler that would compete with
  the workload (a desktop GUI bench in particular) for the very CPU/RAM being
  measured. Nothing is injected into the sandbox. The profiler runs *after* the
  work, keyed on sandbox IDs the verification ladder already records in its
  evidence JSON.

* **Metrics are fetched post-mortem by ID.** `get_metrics` hits
  `GET /sandboxes/{id}/metrics` and works for a sandbox that has already been
  killed, within E2B's retention window. Run this soon after the ladder so the
  window has not expired.

Input modes (combine freely):

    --sandbox-id ID            explicit sandbox id (repeatable)
    --from-evidence PATH       harvest ids from a run's evidence JSON/JSONL
                               (file or directory; recursed). Portable across
                               benches: they all record `sandbox_id` or a
                               `sandbox` object carrying `id`/`sandbox_id`.
    --metadata KEY=VALUE       select live sandboxes by E2B metadata (repeatable;
                               all must match). Only sees still-running sandboxes.

Output: a `resource-requirements` report (JSON to --out, human summary to stderr)
with per-sandbox and aggregate CPU/RAM/DISK peak+mean, saturation flags, and a
recommended RAM floor using the same margin doctrine the container lane uses
(`skills/optimize-env-performance`): max(1.25 x peak, peak + 1 GiB), rounded up
to 128 MiB and to an even MiB. Disk is reported as a tier-capacity compatibility
constraint, not a template knob.

Exit status: 0 if every requested sandbox yielded metrics and nothing tripped a
saturation flag; non-zero otherwise, so a verification rung can gate on it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

_GIB = 1024**3
_MIB = 1024**2

# E2B sandbox ids are long lowercase alphanumeric strings (e.g.
# "iak7xhxt2x1wmyusfum70"). This shape check discriminates a real sandbox id
# from unrelated fields such as a 3-digit task id, so evidence harvesting never
# turns a task number into a bogus metrics query.
_SANDBOX_ID_MIN_LEN = 16


def is_sandbox_id(value: Any) -> bool:
    """True for a value shaped like an E2B sandbox id."""
    return (
        isinstance(value, str)
        and len(value) >= _SANDBOX_ID_MIN_LEN
        and value.isascii()
        and value.islower()
        and value.isalnum()
    )


def harvest_sandbox_ids(obj: Any) -> list[str]:
    """Recursively collect E2B sandbox ids from a parsed evidence object.

    Only reads sandbox-bearing keys — a top-level/nested ``sandbox_id`` string,
    or a ``sandbox`` mapping's ``id``/``sandbox_id`` — and shape-validates each,
    so a bare task ``id`` (e.g. ``"003"``) is never mistaken for a sandbox id.
    Order-preserving, deduplicated.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if is_sandbox_id(value) and value not in seen:
            seen.add(value)
            found.append(value)

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "sandbox_id":
                    add(value)
                elif key == "sandbox":
                    if isinstance(value, Mapping):
                        add(value.get("id"))
                        add(value.get("sandbox_id"))
                    else:
                        add(value)
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(obj)
    return found


def _iter_evidence_files(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(
                sorted(p for p in path.rglob("*") if p.suffix in {".json", ".jsonl"})
            )
        elif path.is_file():
            files.append(path)
        else:
            print(
                f"[profile] warning: evidence path not found: {path}", file=sys.stderr
            )
    return files


def _parse_evidence_text(text: str) -> list[Any]:
    """Parse a file as a single JSON document, or as JSON Lines if that fails."""
    text = text.strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    docs: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return docs


def load_ids_from_evidence(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Harvest deduplicated sandbox ids from evidence files/directories."""
    found: list[str] = []
    seen: set[str] = set()
    for file in _iter_evidence_files(paths):
        for doc in _parse_evidence_text(file.read_text()):
            for sandbox_id in harvest_sandbox_ids(doc):
                if sandbox_id not in seen:
                    seen.add(sandbox_id)
                    found.append(sandbox_id)
    return found


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _epoch(timestamp: Any) -> float | None:
    """Normalize a metrics timestamp (datetime | ISO str | epoch number) to epoch seconds."""
    if isinstance(timestamp, _dt.datetime):
        return timestamp.timestamp()
    if isinstance(timestamp, (int, float)):
        return float(timestamp)
    if isinstance(timestamp, str):
        try:
            return _dt.datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None
    return None


def _max_consecutive_over(values: Sequence[float], threshold: float) -> int:
    """Longest run of consecutive samples strictly above ``threshold``."""
    best = run = 0
    for value in values:
        run = run + 1 if value > threshold else 0
        best = max(best, run)
    return best


# Three consecutive samples above 90% of the CPU allocation is the container
# lane's persistent-saturation rule (skills/optimize-env-performance). Reused
# here so a saturated CPU is flagged, not silently published as adequate.
_SATURATION_PCT = 90.0
_SATURATION_RUN = 3


def summarize(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Reduce a sandbox's metrics time series to peak/mean/flags.

    Each sample is a mapping with keys: cpu_count, cpu_used_pct, mem_used,
    mem_total, mem_cache, disk_used, disk_total, timestamp. Memory/disk values
    are bytes. Returns ``sample_count == 0`` when there is nothing to summarize.
    """
    if not samples:
        return {"sample_count": 0}

    def col(key: str) -> list[float]:
        return [float(s.get(key) or 0) for s in samples]

    cpu_pct = col("cpu_used_pct")
    cpu_counts = [int(s.get("cpu_count") or 0) for s in samples]
    mem_used = col("mem_used")
    mem_total = col("mem_total")
    mem_cache = col("mem_cache")
    disk_used = col("disk_used")
    disk_total = col("disk_total")

    epochs = sorted(
        e for e in (_epoch(s.get("timestamp")) for s in samples) if e is not None
    )
    duration = (epochs[-1] - epochs[0]) if len(epochs) >= 2 else 0.0

    cpu_count = max(cpu_counts) if cpu_counts else 0
    cpu_pct_peak = max(cpu_pct)
    mem_used_peak = max(mem_used)
    mem_total_max = max(mem_total)
    disk_used_peak = max(disk_used)
    disk_total_max = max(disk_total)

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    cpu_saturated = _max_consecutive_over(cpu_pct, _SATURATION_PCT) >= _SATURATION_RUN
    mem_pressure = (
        mem_total_max > 0 and mem_used_peak > _SATURATION_PCT / 100 * mem_total_max
    )
    disk_pressure = (
        disk_total_max > 0 and disk_used_peak > _SATURATION_PCT / 100 * disk_total_max
    )

    return {
        "sample_count": len(samples),
        "duration_seconds": round(duration, 1),
        "cpu_count": cpu_count,
        # ASSUMPTION: E2B's cpu_used_pct is a 0-100 percentage of the sandbox's
        # *total* CPU allocation (the SDK pairs it with a separate cpu_count).
        # Under that reading the 90%-saturation rule applies to cpu_used_pct
        # directly, and cores actually in use = pct/100 * cpu_count. If a future
        # E2B revision reports per-core-summed percent instead, revisit this
        # single line and the flag threshold.
        "cpu_used_pct_of_allocation_peak": round(cpu_pct_peak, 2),
        "cpu_used_pct_of_allocation_mean": round(mean(cpu_pct), 2),
        "cpu_cores_used_peak": round(cpu_pct_peak / 100.0 * cpu_count, 3),
        "mem_used_bytes_peak": int(mem_used_peak),
        "mem_used_gib_peak": round(mem_used_peak / _GIB, 3),
        "mem_used_gib_mean": round(mean(mem_used) / _GIB, 3),
        "mem_cache_bytes_peak": int(max(mem_cache)) if mem_cache else 0,
        "mem_total_bytes": int(mem_total_max),
        "mem_total_gib": round(mem_total_max / _GIB, 3),
        "disk_used_bytes_peak": int(disk_used_peak),
        "disk_used_gib_peak": round(disk_used_peak / _GIB, 3),
        "disk_used_gib_mean": round(mean(disk_used) / _GIB, 3),
        "disk_total_bytes": int(disk_total_max),
        "disk_total_gib": round(disk_total_max / _GIB, 3),
        "cpu_saturated": cpu_saturated,
        "mem_pressure": mem_pressure,
        "disk_pressure": disk_pressure,
    }


def _round_up_ram_mib(byte_peak: float) -> int:
    """Container-lane RAM doctrine: max(1.25 x peak, peak + 1 GiB), rounded up to
    128 MiB and to an even MiB. Returns whole MiB."""
    margin = max(byte_peak * 1.25, byte_peak + _GIB)
    mib = _ceil_div(int(margin), _MIB)  # ceil to whole MiB
    mib = _ceil_div(mib, 128) * 128  # ceil to 128 MiB boundary (already even)
    return mib


def recommend(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    """Turn aggregate peaks into a publishable resource recommendation."""
    mem_peak = float(aggregate.get("mem_used_bytes_peak") or 0)
    ram_mib = _round_up_ram_mib(mem_peak)
    disk_peak_gib = float(aggregate.get("disk_used_gib_peak") or 0)
    disk_total_gib = float(aggregate.get("disk_total_gib") or 0)
    cpu_count = int(aggregate.get("cpu_count") or 0)
    return {
        "recommended_memory_mib": ram_mib,
        "recommended_memory_gib": round(ram_mib / 1024, 3),
        "memory_basis": "max(1.25 x measured peak, peak + 1 GiB), rounded up to 128 MiB",
        "observed_cpu_count": cpu_count,
        "cpu_note": (
            "Keep the tested allocation unless CPU saturated. "
            "cpu_saturated=" + str(bool(aggregate.get("cpu_saturated")))
        ),
        # Disk on E2B is an account-tier compatibility constraint, not a
        # per-template build knob, so this is a compatibility floor, not a
        # request. Report peak used against tier capacity.
        "disk_peak_used_gib": round(disk_peak_gib, 3),
        "disk_tier_capacity_gib": round(disk_total_gib, 3),
        "disk_note": (
            "Disk is an E2B account-tier capacity constraint, not a template "
            "knob: the workload's peak used must fit the tier's disk_total."
        ),
    }


def aggregate_peaks(per_sandbox: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine per-sandbox summaries into worst-case peaks across the run."""
    profiled = [
        s["summary"] for s in per_sandbox if s.get("summary", {}).get("sample_count")
    ]
    if not profiled:
        return {"profiled_sandbox_count": 0}

    def peak(key: str) -> float:
        return max(float(s.get(key) or 0) for s in profiled)

    def any_flag(key: str) -> bool:
        return any(bool(s.get(key)) for s in profiled)

    return {
        "profiled_sandbox_count": len(profiled),
        "cpu_count": int(peak("cpu_count")),
        "cpu_used_pct_of_allocation_peak": round(
            peak("cpu_used_pct_of_allocation_peak"), 2
        ),
        "cpu_cores_used_peak": round(peak("cpu_cores_used_peak"), 3),
        "mem_used_bytes_peak": int(peak("mem_used_bytes_peak")),
        "mem_used_gib_peak": round(peak("mem_used_bytes_peak") / _GIB, 3),
        "mem_total_bytes": int(peak("mem_total_bytes")),
        "mem_total_gib": round(peak("mem_total_bytes") / _GIB, 3),
        "disk_used_bytes_peak": int(peak("disk_used_bytes_peak")),
        "disk_used_gib_peak": round(peak("disk_used_bytes_peak") / _GIB, 3),
        "disk_total_bytes": int(peak("disk_total_bytes")),
        "disk_total_gib": round(peak("disk_total_bytes") / _GIB, 3),
        "cpu_saturated": any_flag("cpu_saturated"),
        "mem_pressure": any_flag("mem_pressure"),
        "disk_pressure": any_flag("disk_pressure"),
    }


def build_report(
    per_sandbox: Sequence[Mapping[str, Any]],
    generated_at: str,
    requested_ids: Sequence[str],
) -> dict[str, Any]:
    aggregate = aggregate_peaks(per_sandbox)
    profiled = int(aggregate.get("profiled_sandbox_count") or 0)
    missing = [
        s["sandbox_id"]
        for s in per_sandbox
        if not s.get("summary", {}).get("sample_count")
    ]
    flags = [
        name
        for name in ("cpu_saturated", "mem_pressure", "disk_pressure")
        if aggregate.get(name)
    ]
    complete = profiled == len(requested_ids) and not missing and not flags
    report: dict[str, Any] = {
        "schema": "e2b-resource-requirements/v1",
        "generated_at": generated_at,
        "requested_sandbox_count": len(requested_ids),
        "profiled_sandbox_count": profiled,
        "sandboxes_without_metrics": missing,
        "aggregate": aggregate,
        "flags": flags,
        "resource_requirements_complete": complete,
        "per_sandbox": list(per_sandbox),
    }
    if profiled:
        report["recommendation"] = recommend(aggregate)
    return report


# --- network boundary (only function that talks to E2B) ------------------------


def fetch_metrics(
    sandbox_id: str,
    start: _dt.datetime | None = None,
    end: _dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch a sandbox's metrics time series from E2B by id (post-mortem safe).

    Returns a list of plain dicts. On a not-found/expired sandbox returns [].
    """
    from e2b import Sandbox  # imported lazily so pure functions/tests need no key

    try:
        raw = Sandbox.get_metrics(sandbox_id=sandbox_id, start=start, end=end)
    except Exception as exc:  # noqa: BLE001 - surface, don't abort the whole run
        name = type(exc).__name__
        if "NotFound" in name:
            print(
                f"[profile] no metrics for {sandbox_id} (not found / retention expired)",
                file=sys.stderr,
            )
            return []
        print(
            f"[profile] error fetching metrics for {sandbox_id}: {name}: {exc}",
            file=sys.stderr,
        )
        return []

    samples: list[dict[str, Any]] = []
    for metric in raw:
        samples.append(
            {
                "cpu_count": getattr(metric, "cpu_count", 0),
                "cpu_used_pct": getattr(metric, "cpu_used_pct", 0.0),
                "mem_used": getattr(metric, "mem_used", 0),
                "mem_total": getattr(metric, "mem_total", 0),
                "mem_cache": getattr(metric, "mem_cache", 0),
                "disk_used": getattr(metric, "disk_used", 0),
                "disk_total": getattr(metric, "disk_total", 0),
                "timestamp": _iso(getattr(metric, "timestamp", None)),
            }
        )
    return samples


def _iso(timestamp: Any) -> Any:
    if isinstance(timestamp, _dt.datetime):
        return timestamp.isoformat()
    return timestamp


def list_ids_by_metadata(selectors: Mapping[str, str]) -> list[str]:
    """Select currently-running sandboxes whose metadata matches all selectors."""
    from e2b import Sandbox

    ids: list[str] = []
    try:
        listed = Sandbox.list()
    except Exception as exc:  # noqa: BLE001
        print(f"[profile] could not list sandboxes: {exc}", file=sys.stderr)
        return ids
    items = getattr(listed, "sandboxes", listed)
    for item in items:
        metadata = getattr(item, "metadata", None) or {}
        if all(metadata.get(key) == value for key, value in selectors.items()):
            sandbox_id = getattr(item, "sandbox_id", None) or getattr(item, "id", None)
            if is_sandbox_id(sandbox_id):
                ids.append(sandbox_id)
    return ids


# --- CLI -----------------------------------------------------------------------


def _human_summary(report: Mapping[str, Any]) -> str:
    lines = [
        f"E2B resource requirements ({report['profiled_sandbox_count']}"
        f"/{report['requested_sandbox_count']} sandboxes profiled)",
    ]
    agg = report.get("aggregate", {})
    if agg.get("profiled_sandbox_count"):
        lines += [
            f"  CPU:  {agg['cpu_used_pct_of_allocation_peak']}% peak of {agg['cpu_count']} core(s) "
            f"(~{agg['cpu_cores_used_peak']} cores)   saturated={agg['cpu_saturated']}",
            f"  RAM:  {agg['mem_used_gib_peak']} GiB peak / {agg['mem_total_gib']} GiB total"
            f"   pressure={agg['mem_pressure']}",
            f"  DISK: {agg['disk_used_gib_peak']} GiB peak / {agg['disk_total_gib']} GiB tier"
            f"   pressure={agg['disk_pressure']}",
        ]
    rec = report.get("recommendation")
    if rec:
        lines.append(
            f"  -> recommend memory {rec['recommended_memory_mib']} MiB "
            f"({rec['recommended_memory_gib']} GiB); disk peak "
            f"{rec['disk_peak_used_gib']} GiB must fit tier capacity."
        )
    if report.get("sandboxes_without_metrics"):
        lines.append(
            f"  WARNING no metrics for: {', '.join(report['sandboxes_without_metrics'])}"
        )
    if report.get("flags"):
        lines.append(f"  WARNING saturation flags: {', '.join(report['flags'])}")
    lines.append(
        f"  resource_requirements_complete={report['resource_requirements_complete']}"
    )
    return "\n".join(lines)


def _parse_metadata(pairs: Sequence[str]) -> dict[str, str]:
    selectors: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--metadata expects KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        selectors[key] = value
    return selectors


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Profile E2B workload resource use (CPU/RAM/DISK) from sandbox metrics.",
    )
    parser.add_argument(
        "--sandbox-id", action="append", default=[], help="explicit sandbox id"
    )
    parser.add_argument(
        "--from-evidence",
        action="append",
        default=[],
        help="evidence JSON/JSONL file or directory to harvest sandbox ids from",
    )
    parser.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="KEY=VALUE metadata selector for live sandboxes (repeatable, all must match)",
    )
    parser.add_argument("--out", help="write the JSON report to this path")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="exit 0 even if some sandboxes had no metrics or a flag tripped",
    )
    args = parser.parse_args(argv)

    ids: list[str] = []
    seen: set[str] = set()

    def add_ids(candidates: Iterable[str]) -> None:
        for candidate in candidates:
            if is_sandbox_id(candidate) and candidate not in seen:
                seen.add(candidate)
                ids.append(candidate)

    add_ids(args.sandbox_id)
    if args.from_evidence:
        add_ids(load_ids_from_evidence(args.from_evidence))
    if args.metadata:
        add_ids(list_ids_by_metadata(_parse_metadata(args.metadata)))

    if not ids:
        print(
            "[profile] no sandbox ids resolved from --sandbox-id/--from-evidence/--metadata",
            file=sys.stderr,
        )
        return 2

    per_sandbox: list[dict[str, Any]] = []
    for sandbox_id in ids:
        samples = fetch_metrics(sandbox_id)
        per_sandbox.append(
            {
                "sandbox_id": sandbox_id,
                "summary": summarize(samples),
                "samples": samples,
            }
        )

    report = build_report(per_sandbox, _now_iso(), ids)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"[profile] wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    print(_human_summary(report), file=sys.stderr)

    if args.allow_missing:
        return 0
    return 0 if report["resource_requirements_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
