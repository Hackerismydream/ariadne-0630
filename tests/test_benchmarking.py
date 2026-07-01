from __future__ import annotations

import sqlite3

from ariadne.benchmarking import (
    aggregate_run,
    read_json,
    run_artifact_spine,
    run_control_plane_concurrency,
    run_real_backend_patch,
    run_state_machine_recovery,
    run_squad_routing,
    run_trace_replay,
)


def test_artifact_spine_exports_product_facts_and_hashes(tmp_path):
    artifact_dir = tmp_path / "artifact"

    metrics = run_artifact_spine(artifact_dir)

    assert metrics["status"] == "passed"
    assert (artifact_dir / "case_manifest.json").exists()
    assert (artifact_dir / "metrics.json").exists()
    assert (artifact_dir / "product_facts" / "task.csv").exists()
    assert (artifact_dir / "product_facts" / "runtime_lease.jsonl").exists()
    assert (artifact_dir / "product_facts" / "ariadne.db").exists()
    assert "metrics.json" in (artifact_dir / "hashes.txt").read_text()

    conn = sqlite3.connect(artifact_dir / "product_facts" / "ariadne.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1
    finally:
        conn.close()


def test_control_plane_concurrency_reports_zero_duplicate_claims(tmp_path):
    artifact_dir = tmp_path / "control"

    metrics = run_control_plane_concurrency(artifact_dir, tasks=12, workers=3)

    assert metrics["status"] == "passed"
    assert metrics["claimed_taskrun_count"] == 12
    assert metrics["duplicate_claim_count"] == 0
    assert metrics["lost_taskrun_count"] == 0
    assert metrics["claim_latency_ms_p95"] >= 0
    assert (artifact_dir / "claims.csv").exists()
    assert (artifact_dir / "sql_invariants.json").exists()


def test_state_squad_trace_and_aggregate_metrics_are_separate(tmp_path):
    run_artifact_spine(tmp_path / "artifact")
    run_state_machine_recovery(tmp_path / "state")
    run_squad_routing(tmp_path / "squad")
    run_trace_replay(tmp_path / "trace")
    run_real_backend_patch(tmp_path / "real", provider="synthetic")
    quality_dir = tmp_path / "quality"
    quality_dir.mkdir()
    (quality_dir / "pytest.xml").write_text('<testsuite tests="5" failures="1" errors="0" />')
    (quality_dir / "ruff.log").write_text("All checks passed!\n")

    aggregate = aggregate_run(tmp_path)

    assert aggregate["status"] == "passed"
    assert aggregate["suite_count"] == 5
    assert aggregate["dry_run_success_count"] == 1
    assert aggregate["synthetic_success_count"] == 4
    assert aggregate["real_backend_success_count"] == 0
    assert aggregate["deterministic_route_accuracy"] == 1.0
    assert aggregate["local_pytest_total"] == 5
    assert aggregate["local_pytest_passed"] == 4
    assert aggregate["ruff_passed"] is True
    assert aggregate["ci_pass_rate"] is None
    assert aggregate["failure_distribution"]["policy"] == 1
    assert (tmp_path / "aggregate" / "resume_metrics.json").exists()


def test_synthetic_real_backend_smoke_requires_patch_and_tests(tmp_path):
    artifact_dir = tmp_path / "real"

    metrics = run_real_backend_patch(artifact_dir, provider="synthetic")

    assert metrics["status"] == "passed"
    assert metrics["synthetic_attempt_count"] == 4
    assert metrics["synthetic_success_count"] == 4
    assert metrics["patch_generated_count"] == 4
    assert metrics["expected_files_matched_count"] == 4
    assert metrics["test_pass_count"] == 4
    assert metrics["dry_run_in_denominator"] is False

    audit = read_json(artifact_dir / "case-0" / "worktree_audit.json")
    assert audit["worktree_created"] is True
    assert audit["original_repo_clean_after"] is True
    assert (artifact_dir / "case-0" / "command_cwd.txt").read_text() == audit["execution_repo_path"]
