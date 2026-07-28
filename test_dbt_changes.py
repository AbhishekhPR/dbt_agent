import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent.dbt_changes import (
    load_changed_models_from_manifest,
    load_changed_models_from_paths,
)


class DbtChangesTests(unittest.TestCase):
    def test_in_memory_manifest_maps_changed_file_without_mutation(self):
        manifest = _manifest()
        original = copy.deepcopy(manifest)

        changed = load_changed_models_from_manifest(
            manifest=manifest,
            changed_files=["analytics/models/marts/fct_revenue.sql"],
        )

        self.assertEqual(changed, ["fct_revenue"])
        self.assertEqual(manifest, original)

    def test_path_loader_delegates_to_same_mapping_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

            changed = load_changed_models_from_paths(
                manifest_path=str(manifest_path),
                changed_files=["models/staging/stg_orders.sql"],
            )

        self.assertEqual(changed, ["stg_orders"])

    def test_changed_files_are_deduplicated_in_first_seen_order(self):
        changed = load_changed_models_from_manifest(
            manifest=_manifest(),
            changed_files=[
                "models/marts/fct_revenue.sql",
                "models/staging/stg_orders.sql",
                "models/marts/fct_revenue.sql",
            ],
        )

        self.assertEqual(changed, ["fct_revenue", "stg_orders"])


def _manifest():
    return {
        "nodes": {
            "model.analytics.fct_revenue": {
                "resource_type": "model",
                "name": "fct_revenue",
                "unique_id": "model.analytics.fct_revenue",
                "original_file_path": "models/marts/fct_revenue.sql",
            },
            "model.analytics.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "unique_id": "model.analytics.stg_orders",
                "original_file_path": "models/staging/stg_orders.sql",
            },
        }
    }


if __name__ == "__main__":
    unittest.main()
