"""Collection-plan regression against a manifest emitted by dbt itself.

Install the pinned optional runtime and run the mandatory focused proof with::

    python -m pip install -r requirements-direct-downstream-e2e.txt
    python -m unittest -v test_metadata_collection_plan_dbt_manifest.py

The ordinary suite may skip the generated-manifest case when that optional
runtime is absent; a focused run in the pinned environment is release evidence.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from agent.metadata_evidence.collection_plan import build_collection_plan


FIXTURE_PROJECT = Path(__file__).parent / "tests" / "fixtures" / "direct_downstream_dbt"
REQUIREMENTS_FILE = Path(__file__).parent / "requirements-direct-downstream-e2e.txt"


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError):
        return False


def _dbt_runtime_available() -> bool:
    core_available = _module_available("dbt.cli.main")
    sqlite_available = _module_available("dbt.adapters.sqlite")
    return core_available and sqlite_available


def _run_dbt_parse(project_dir: Path):
    dbt_executable = shutil.which("dbt", path=str(Path(sys.executable).parent))
    if dbt_executable is None:
        raise FileNotFoundError("dbt console entry point is unavailable")
    return subprocess.run(
        [
            dbt_executable,
            "parse",
            "--project-dir",
            str(project_dir),
            "--profiles-dir",
            str(project_dir),
            "--no-partial-parse",
            "--no-version-check",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


DBT_AVAILABLE = _dbt_runtime_available()


class DbtHarnessContractTests(unittest.TestCase):
    def test_requirements_pin_the_verified_dbt_runtime(self):
        pins = [
            line.strip()
            for line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(pins, ["dbt-core==1.11.12", "dbt-sqlite==1.10.0"])

    @mock.patch.object(importlib.util, "find_spec")
    def test_runtime_check_requires_the_sqlite_adapter(self, find_spec):
        find_spec.side_effect = lambda name: object() if name == "dbt.cli.main" else None
        self.assertFalse(_dbt_runtime_available())
        self.assertEqual(
            [call.args[0] for call in find_spec.call_args_list],
            ["dbt.cli.main", "dbt.adapters.sqlite"],
        )

    @mock.patch.object(subprocess, "run")
    @mock.patch.object(shutil, "which", return_value="dbt")
    def test_parse_subprocess_has_a_bounded_timeout(self, _which, run):
        _run_dbt_parse(Path("fixture-project"))
        self.assertEqual(run.call_args.kwargs["timeout"], 60)


@unittest.skipUnless(
    DBT_AVAILABLE,
    "dbt Core plus the sqlite adapter are optional; install "
    "requirements-direct-downstream-e2e.txt and run this focused test",
)
class GeneratedManifestDirectDownstreamTests(unittest.TestCase):
    def test_plan_contains_only_direct_downstream_models(self):
        with tempfile.TemporaryDirectory(prefix="relium-direct-downstream-") as temp_dir:
            project_dir = Path(temp_dir) / "project"
            shutil.copytree(FIXTURE_PROJECT, project_dir)
            result = _run_dbt_parse(project_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            manifest_path = project_dir / "target" / "manifest.json"
            self.assertTrue(manifest_path.is_file(), "dbt parse did not emit manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        project = "direct_downstream_fixture"
        source_id = f"source.{project}.raw.orders"
        staging_id = f"model.{project}.stg_orders"
        fact_id = f"model.{project}.fct_orders"
        revenue_id = f"model.{project}.rpt_revenue"
        customer_revenue_id = f"model.{project}.rpt_customer_revenue"
        transitive_id = f"model.{project}.rpt_executive_summary"
        exposure_id = f"exposure.{project}.revenue_dashboard"

        self.assertIn(source_id, manifest["sources"])
        self.assertEqual(manifest["nodes"][staging_id]["depends_on"]["nodes"], [source_id])
        self.assertEqual(manifest["nodes"][fact_id]["depends_on"]["nodes"], [staging_id])
        self.assertEqual(manifest["nodes"][revenue_id]["depends_on"]["nodes"], [fact_id])
        self.assertEqual(manifest["nodes"][customer_revenue_id]["depends_on"]["nodes"], [fact_id])
        self.assertEqual(manifest["nodes"][transitive_id]["depends_on"]["nodes"], [revenue_id])
        self.assertEqual(
            set(manifest["exposures"][exposure_id]["depends_on"]["nodes"]),
            {revenue_id, customer_revenue_id},
        )

        plan = build_collection_plan(
            base_manifest=manifest,
            head_manifest=manifest,
            changed_models=["fct_orders"],
        )

        self.assertEqual(plan.downstream_models, [customer_revenue_id, revenue_id])
        self.assertNotIn(transitive_id, plan.downstream_models)
        self.assertNotIn(exposure_id, plan.downstream_models)


if __name__ == "__main__":
    unittest.main()
