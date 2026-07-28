from pathlib import Path
import unittest


DEMO_DIR = Path(__file__).parent / "demo" / "memory_aware"


class MemoryAwareDemoTests(unittest.TestCase):
    def test_memory_aware_demo_files_exist(self):
        self.assertTrue((DEMO_DIR / "README.md").exists())
        self.assertTrue((DEMO_DIR / "run_demo.ps1").exists())
        self.assertTrue((DEMO_DIR / "run_demo.sh").exists())

    def test_scripts_run_full_memory_loop_without_duplicating_manifests(self):
        for script_name in ["run_demo.ps1", "run_demo.sh"]:
            script = (DEMO_DIR / script_name).read_text(encoding="utf-8")

            self.assertIn("demo/history_aware/manifest_previous.json", script)
            self.assertIn("demo/history_aware/manifest_current.json", script)
            self.assertIn("init-baseline", script)
            self.assertIn("review-deployment", script)
            self.assertIn("backtest-deployment", script)
            self.assertIn("record-outcome", script)
            self.assertIn("outcome-summary", script)
            self.assertIn("fixed_before_merge", script)
            self.assertIn("refunds-risk-review-repeat", script)
            self.assertIn("demo/memory_aware/.relium/deployment_outcomes.json", script)

        self.assertFalse((DEMO_DIR / "manifest_previous.json").exists())
        self.assertFalse((DEMO_DIR / "manifest_current.json").exists())

    def test_readme_explains_product_story(self):
        readme = (DEMO_DIR / "README.md").read_text(encoding="utf-8").lower()

        self.assertIn("trusted baseline", readme)
        self.assertIn("semantic revenue change", readme)
        self.assertIn("backtest", readme)
        self.assertIn("outcome recording", readme)
        self.assertIn("outcome memory", readme)
        self.assertIn("deployment outcome memory", readme)


if __name__ == "__main__":
    unittest.main()
