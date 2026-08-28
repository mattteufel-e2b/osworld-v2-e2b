"""Unit tests for the standalone E2B resource profiler (no network).

The profiler lives in the repo-root `tools/` dir (deliberately outside the
vended `src/env_registry` package so it stays copy-portable per bench). Load it
by path so the tests do not depend on it being importable as a package.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "profile_e2b_resources.py"
_spec = importlib.util.spec_from_file_location("profile_e2b_resources", _TOOL)
assert _spec and _spec.loader
prof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prof)

_GIB = 1024**3
_MIB = 1024**2

REAL_ID = "iak7xhxt2x1wmyusfum70"
REAL_ID2 = "icevwsa6wvjch8811k7yg"


# --- is_sandbox_id / harvesting ------------------------------------------------


def test_is_sandbox_id_shape():
    assert prof.is_sandbox_id(REAL_ID)
    assert prof.is_sandbox_id(REAL_ID2)
    # A 3-digit task id (as seen in real evidence) is NOT a sandbox id.
    assert not prof.is_sandbox_id("003")
    assert not prof.is_sandbox_id("")
    assert not prof.is_sandbox_id(None)
    assert not prof.is_sandbox_id(123)
    assert not prof.is_sandbox_id("HAS-UPPER-AND-DASH-xxxx")
    assert not prof.is_sandbox_id("has_underscore_soshort")  # underscore not alnum


def test_harvest_from_nested_sandbox_object_not_task_id():
    """Mirrors a real rung-3 record: task id '003' + nested sandbox.id."""
    record = {
        "id": "003",
        "domain": "gimp",
        "sandbox": {"generation": 2, "id": REAL_ID, "restricted_ingress": True},
        "score": 0.0,
    }
    assert prof.harvest_sandbox_ids(record) == [REAL_ID]


def test_harvest_top_level_sandbox_id():
    assert prof.harvest_sandbox_ids({"sandbox_id": REAL_ID2, "template": "x"}) == [REAL_ID2]


def test_harvest_dedups_and_orders():
    obj = [
        {"sandbox": {"id": REAL_ID}},
        {"sandbox_id": REAL_ID2},
        {"sandbox": {"id": REAL_ID}},  # dup
    ]
    assert prof.harvest_sandbox_ids(obj) == [REAL_ID, REAL_ID2]


def test_load_ids_from_evidence_json_and_jsonl(tmp_path):
    single = tmp_path / "smoke.json"
    single.write_text(json.dumps({"sandbox_id": REAL_ID}))
    lines = tmp_path / "validate.jsonl"
    lines.write_text(
        json.dumps({"id": "003", "sandbox": {"id": REAL_ID}}) + "\n"  # dup of smoke id
        + json.dumps({"id": "004", "sandbox": {"id": REAL_ID2}}) + "\n"
    )
    ids = prof.load_ids_from_evidence([tmp_path])
    assert ids == [REAL_ID, REAL_ID2]


def test_load_ids_ignores_missing_path(capsys):
    assert prof.load_ids_from_evidence(["/no/such/path/xyz"]) == []


# --- summarize -----------------------------------------------------------------


def _sample(pct, mem_used, disk_used, ts, cpu_count=2, mem_total=4 * _GIB, disk_total=10 * _GIB):
    return {
        "cpu_count": cpu_count,
        "cpu_used_pct": pct,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_cache": 0,
        "disk_used": disk_used,
        "disk_total": disk_total,
        "timestamp": ts,
    }


def test_summarize_empty():
    assert prof.summarize([]) == {"sample_count": 0}


def test_summarize_peaks_means_and_duration():
    samples = [
        _sample(10.0, 1 * _GIB, 2 * _GIB, "2026-08-26T19:16:00+00:00"),
        _sample(50.0, 3 * _GIB, 2 * _GIB, "2026-08-26T19:16:10+00:00"),
        _sample(30.0, 2 * _GIB, 4 * _GIB, "2026-08-26T19:16:20+00:00"),
    ]
    s = prof.summarize(samples)
    assert s["sample_count"] == 3
    assert s["duration_seconds"] == 20.0
    assert s["cpu_count"] == 2
    assert s["cpu_used_pct_of_allocation_peak"] == 50.0
    assert s["cpu_used_pct_of_allocation_mean"] == 30.0
    # 50% of a 2-core allocation = 1.0 core in use.
    assert s["cpu_cores_used_peak"] == 1.0
    assert s["mem_used_bytes_peak"] == 3 * _GIB
    assert s["mem_used_gib_peak"] == 3.0
    assert s["disk_used_gib_peak"] == 4.0
    # No 3-in-a-row over 90%, memory peak 3/4 GiB < 90%, disk 4/10 < 90%.
    assert s["cpu_saturated"] is False
    assert s["mem_pressure"] is False
    assert s["disk_pressure"] is False


def test_summarize_cpu_saturation_needs_three_consecutive():
    over = _sample(95.0, _GIB, _GIB, "2026-08-26T19:16:00+00:00")
    under = _sample(10.0, _GIB, _GIB, "2026-08-26T19:16:00+00:00")
    # 95,95,10,95 -> longest run is 2, not saturated
    assert prof.summarize([over, over, under, over])["cpu_saturated"] is False
    # 95,95,95 -> saturated
    assert prof.summarize([over, over, over])["cpu_saturated"] is True


def test_summarize_memory_and_disk_pressure():
    hot = _sample(10.0, int(3.8 * _GIB), int(9.5 * _GIB), "2026-08-26T19:16:00+00:00")
    s = prof.summarize([hot])
    assert s["mem_pressure"] is True  # 3.8/4.0 > 90%
    assert s["disk_pressure"] is True  # 9.5/10 > 90%


# --- recommend -----------------------------------------------------------------


def test_recommend_ram_uses_plus_1gib_when_larger():
    # peak 2 GiB: 1.25x = 2.5 GiB, peak+1GiB = 3 GiB -> pick 3 GiB = 3072 MiB
    agg = {"mem_used_bytes_peak": 2 * _GIB, "disk_used_gib_peak": 1, "disk_total_gib": 10,
           "cpu_count": 2}
    rec = prof.recommend(agg)
    assert rec["recommended_memory_mib"] == 3072
    assert rec["recommended_memory_gib"] == 3.0


def test_recommend_ram_uses_1_25x_when_larger_and_rounds_to_128():
    # peak 8 GiB: 1.25x = 10 GiB, peak+1 = 9 GiB -> pick 10 GiB = 10240 MiB (128-aligned)
    agg = {"mem_used_bytes_peak": 8 * _GIB}
    rec = prof.recommend(agg)
    assert rec["recommended_memory_mib"] == 10240


def test_recommend_ram_rounds_up_to_128_boundary():
    # peak 1000 MiB -> +1GiB = 2024 MiB -> ceil to 128 => 2048
    agg = {"mem_used_bytes_peak": 1000 * _MIB}
    rec = prof.recommend(agg)
    assert rec["recommended_memory_mib"] % 128 == 0
    assert rec["recommended_memory_mib"] == 2048


# --- aggregate + report --------------------------------------------------------


def test_aggregate_takes_worst_case_across_sandboxes():
    ts = "2026-08-26T19:16:00+00:00"
    per = [
        {"summary": prof.summarize([_sample(20.0, 2 * _GIB, 3 * _GIB, ts)])},
        {"summary": prof.summarize([_sample(60.0, 3 * _GIB, 5 * _GIB, ts)])},
    ]
    agg = prof.aggregate_peaks(per)
    assert agg["profiled_sandbox_count"] == 2
    assert agg["cpu_used_pct_of_allocation_peak"] == 60.0
    assert agg["mem_used_gib_peak"] == 3.0
    assert agg["disk_used_gib_peak"] == 5.0


def test_aggregate_empty():
    assert prof.aggregate_peaks([])["profiled_sandbox_count"] == 0
    assert prof.aggregate_peaks([{"summary": {"sample_count": 0}}])["profiled_sandbox_count"] == 0


def test_build_report_complete_when_all_profiled_no_flags():
    per = [
        {
            "sandbox_id": REAL_ID,
            "summary": prof.summarize(
                [_sample(20.0, 2 * _GIB, 3 * _GIB, "2026-08-26T19:16:00+00:00")]
            ),
        }
    ]
    report = prof.build_report(per, "2026-08-26T00:00:00+00:00", [REAL_ID])
    assert report["resource_requirements_complete"] is True
    assert report["schema"] == "e2b-resource-requirements/v1"
    assert "recommendation" in report
    assert report["sandboxes_without_metrics"] == []


def test_build_report_incomplete_when_metrics_missing():
    per = [{"sandbox_id": REAL_ID, "summary": {"sample_count": 0}}]
    report = prof.build_report(per, "2026-08-26T00:00:00+00:00", [REAL_ID])
    assert report["resource_requirements_complete"] is False
    assert report["sandboxes_without_metrics"] == [REAL_ID]
    assert "recommendation" not in report


def test_build_report_incomplete_when_flag_tripped():
    hot = _sample(95.0, _GIB, _GIB, "2026-08-26T19:16:00+00:00")
    per = [{"sandbox_id": REAL_ID, "summary": prof.summarize([hot, hot, hot])}]
    report = prof.build_report(per, "2026-08-26T00:00:00+00:00", [REAL_ID])
    assert "cpu_saturated" in report["flags"]
    assert report["resource_requirements_complete"] is False


def test_build_report_incomplete_when_fewer_profiled_than_requested():
    per = [
        {
            "sandbox_id": REAL_ID,
            "summary": prof.summarize(
                [_sample(20.0, _GIB, _GIB, "2026-08-26T19:16:00+00:00")]
            ),
        }
    ]
    report = prof.build_report(per, "2026-08-26T00:00:00+00:00", [REAL_ID, REAL_ID2])
    assert report["resource_requirements_complete"] is False
