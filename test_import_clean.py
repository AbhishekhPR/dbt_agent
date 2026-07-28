import contextlib
import importlib
import io
import sys
import unittest


class ImportCleanTests(unittest.TestCase):
    def test_demo_test_modules_do_not_print_at_import_time(self):
        for module_name in ["test_client", "test_diagnose"]:
            sys.modules.pop(module_name, None)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                importlib.import_module(module_name)

            self.assertEqual(output.getvalue(), "", module_name)


if __name__ == "__main__":
    unittest.main()
