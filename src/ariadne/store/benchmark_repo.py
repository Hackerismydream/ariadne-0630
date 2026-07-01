"""BenchmarkRun persistence methods."""

from __future__ import annotations

import json

from ariadne.models import BenchmarkRun

from .base import _new_id, _now_iso


class BenchmarkRepo:

    def create_benchmark_run(
        self,
        suite_name: str,
        case_name: str,
        issue_id: str,
        runtime_policy: dict | None = None,
        artifact_dir: str = "",
        status: str = "running",
    ) -> BenchmarkRun:
        run_id = _new_id("bench")
        started_at = _now_iso()
        self._conn.execute(
            """INSERT INTO benchmark_run
               (id, suite_name, case_name, issue_id, runtime_policy_json, status,
                started_at, artifact_dir)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                suite_name,
                case_name,
                issue_id,
                json.dumps(runtime_policy or {}),
                status,
                started_at,
                artifact_dir,
            ),
        )
        self._conn.commit()
        run = self.get_benchmark_run(run_id)
        if run is None:
            raise KeyError(f"benchmark run not found: {run_id}")
        return run

    def complete_benchmark_run(
        self,
        benchmark_run_id: str,
        status: str,
        summary: dict,
        metrics: dict,
    ) -> BenchmarkRun:
        self._conn.execute(
            """UPDATE benchmark_run
               SET status = ?, completed_at = ?, summary_json = ?,
                   metrics_json = ?
               WHERE id = ?""",
            (
                status,
                _now_iso(),
                json.dumps(summary),
                json.dumps(metrics),
                benchmark_run_id,
            ),
        )
        self._conn.commit()
        run = self.get_benchmark_run(benchmark_run_id)
        if run is None:
            raise KeyError(f"benchmark run not found: {benchmark_run_id}")
        return run

    def get_benchmark_run(self, benchmark_run_id: str) -> BenchmarkRun | None:
        row = self._conn.execute(
            "SELECT * FROM benchmark_run WHERE id = ?", (benchmark_run_id,)
        ).fetchone()
        return self.row_to(BenchmarkRun, row) if row else None

    def list_benchmark_runs(self) -> list[BenchmarkRun]:
        rows = self._conn.execute(
            "SELECT * FROM benchmark_run ORDER BY started_at DESC, id DESC"
        ).fetchall()
        return [self.row_to(BenchmarkRun, r) for r in rows]
