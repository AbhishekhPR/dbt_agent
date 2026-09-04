"""Clean-host acceptance for the exact dashboard collector bundle.

Set RELIUM_COLLECTOR_BUNDLE to the ZIP extracted from the production image and
RELIUM_TEST_WAREHOUSE_DSN to a disposable PostgreSQL database. The test uses no
repository code after launch: it unpacks the downloaded artifact into an empty
directory, verifies it, installs it into a new virtual environment, and invokes
the installed console script against a local API boundary and real PostgreSQL.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
import venv
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUNDLE = os.environ.get("RELIUM_COLLECTOR_BUNDLE")
WAREHOUSE_DSN = os.environ.get("RELIUM_TEST_WAREHOUSE_DSN")


class _CollectorApi(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.received.append((self.path, body))
        if self.path == "/api/collectors":
            payload = {"collector": body}
        elif self.path == "/api/collectors/verification":
            payload = {"status": "verified"}
        else:
            self.send_error(404)
            return
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(BUNDLE and WAREHOUSE_DSN,
                     "set RELIUM_COLLECTOR_BUNDLE and RELIUM_TEST_WAREHOUSE_DSN")
class CleanMachineCollectorBundleTests(unittest.TestCase):
    def test_download_unzip_verify_install_and_collect_test(self):
        bundle = Path(BUNDLE).resolve()
        self.assertTrue(bundle.is_file(), bundle)
        self.assertEqual(bundle.name, "relium-collector-0.1.0.zip")

        with tempfile.TemporaryDirectory() as empty_host:
            root = Path(empty_host)
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["SHA256SUMS", "relium-0.1.0-py3-none-any.whl"],
                )
                archive.extractall(root)

            wheel = root / "relium-0.1.0-py3-none-any.whl"
            manifest = (root / "SHA256SUMS").read_text(
                encoding="utf-8").strip().split()
            self.assertEqual(manifest[1], wheel.name)
            self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(),
                             manifest[0])
            with zipfile.ZipFile(wheel) as archive:
                wheel_names = archive.namelist()
                entry_points = archive.read(
                    "relium-0.1.0.dist-info/entry_points.txt"
                ).decode("utf-8")
            self.assertIn("relium = agent.cli:cli", entry_points)
            self.assertNotIn("relium/__main__.py", wheel_names)
            self.assertNotIn("agent/__main__.py", wheel_names)

            environment = root / ".venv"
            venv.EnvBuilder(with_pip=True, clear=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt"
                                    else "bin/python")
            relium = environment / ("Scripts/relium.exe" if os.name == "nt"
                                    else "bin/relium")
            subprocess.run(
                [str(python), "-m", "pip", "install", "--disable-pip-version-check",
                 str(wheel)],
                cwd=root, check=True, capture_output=True, text=True,
            )
            self.assertTrue(relium.is_file(), relium)

            _CollectorApi.received = []
            server = ThreadingHTTPServer(("127.0.0.1", 0), _CollectorApi)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            env = os.environ.copy()
            env.update({
                "RELIUM_API_URL": f"http://127.0.0.1:{server.server_port}",
                "RELIUM_API_TOKEN": "rlm_clean_machine.acceptance-secret",
                "RELIUM_WAREHOUSE_DSN": WAREHOUSE_DSN,
                "RELIUM_ENVIRONMENT": "production",
                "RELIUM_COLLECTOR_ID": "clean-machine-collector",
            })
            result = subprocess.run(
                [str(relium), "collect", "--test"],
                cwd=root, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("collector verified warehouse connectivity", result.stdout)
            self.assertEqual(
                [path for path, _ in _CollectorApi.received],
                ["/api/collectors", "/api/collectors/verification"],
            )
            outbound_bodies = json.dumps([body for _, body in _CollectorApi.received])
            self.assertNotIn(WAREHOUSE_DSN, outbound_bodies)
            self.assertNotIn("postgresql://", outbound_bodies)


if __name__ == "__main__":
    unittest.main()
