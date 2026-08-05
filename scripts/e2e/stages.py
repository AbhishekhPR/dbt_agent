"""Fail-closed required-stage registry for the genuine GitHub E2E.

The previous driver reached a placeholder ``return 0`` after the environment
gate, so the workflow reported success for an E2E that never ran. This module
makes that impossible: success requires every stage to have been marked
complete by a real assertion, and marking is refused unless proof is supplied.

Writing an evidence file is explicitly NOT proof. ``complete()`` requires a
non-empty ``proof`` mapping describing what was actually observed, and
``assert_all_complete()`` raises unless every registered stage is marked.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_STAGES = (
    "environment_gate",
    "api_started",
    "worker_started",
    "tunnel_started",
    "webhook_preserved",
    "webhook_updated",
    "webhook_verified",
    "fixture_pr_created",
    "genuine_webhook_received",
    "postgres_review_verified",
    "targeted_request_verified",
    "waiting_publication_verified",
    "primary_snapshot_submitted",
    "duplicate_snapshot_verified",
    "conflicting_replay_verified",
    "recomputation_verified",
    "github_reconciliation_verified",
    "dashboard_verified",
    "variant_a_verified",
    "variant_b_verified",
    "variant_c_verified",
    "variant_d_verified",
    "variant_e_verified",
    "variant_f_verified",
    "variant_g_verified",
    "webhook_restored",
    "cleanup_verified",
)


class StageIncomplete(RuntimeError):
    """Raised when success is claimed while a required stage is unproven."""


class StageTracker:
    def __init__(self, out_path):
        self.out = Path(out_path)
        self.started_at = datetime.now(timezone.utc)
        self._stages = {name: {"complete": False, "proof": None, "at": None}
                        for name in REQUIRED_STAGES}
        self.current = None
        self.failure = None

    # -- marking ---------------------------------------------------------
    def begin(self, name: str) -> None:
        if name not in self._stages:
            raise KeyError(f"unknown stage: {name}")
        self.current = name
        self.write()

    def complete(self, name: str, proof: dict) -> None:
        """Mark a stage complete. Refuses to accept an empty proof."""
        if name not in self._stages:
            raise KeyError(f"unknown stage: {name}")
        if not isinstance(proof, dict) or not proof:
            raise StageIncomplete(
                f"stage {name} requires a non-empty proof of real execution")
        # A stage cannot be satisfied merely by having written a file.
        if set(proof) <= {"evidence_file", "evidence", "planned", "note"}:
            raise StageIncomplete(
                f"stage {name}: evidence generation alone is not execution")
        self._stages[name] = {
            "complete": True, "proof": proof,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.write()

    def fail(self, name: str, reason: str) -> None:
        self.failure = {"stage": name, "reason": str(reason)[:400]}
        self.write()

    # -- interrogation ---------------------------------------------------
    def incomplete(self) -> list[str]:
        return [n for n in REQUIRED_STAGES if not self._stages[n]["complete"]]

    def assert_all_complete(self) -> None:
        missing = self.incomplete()
        if missing:
            raise StageIncomplete(
                f"{len(missing)} required stage(s) never executed: "
                + ", ".join(missing))
        if self.failure:
            raise StageIncomplete(f"recorded failure: {self.failure}")

    def write(self) -> None:
        doc = {
            "started_at_utc": self.started_at.isoformat(timespec="seconds"),
            "written_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "current_stage": self.current,
            "failure": self.failure,
            "required_stage_count": len(REQUIRED_STAGES),
            "completed_stage_count": sum(
                1 for s in self._stages.values() if s["complete"]),
            "incomplete_stages": self.incomplete(),
            "all_complete": not self.incomplete() and self.failure is None,
            "stages": self._stages,
        }
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str)
                            + "\n", encoding="utf-8")
