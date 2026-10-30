"""Exercise the published examples against a local fixture, never the live API."""

import base64
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX3sAAAAASUVORK5CYII="
)
MODELS = ("gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst")


class ExampleServer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers["Content-Type"].startswith("multipart/form-data"):
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw
            )
            body = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                body[name] = (
                    {"filename": part.get_filename(), "type": part.get_content_type(), "bytes": part.get_payload(decode=True)}
                    if part.get_filename() else part.get_payload(decode=True).decode("utf-8")
                )
        else:
            body = json.loads(raw)
        self.server.requests.append((self.path, self.headers, body))
        status = self.server.response_status
        payload = (
            {"created": 1793289600, "data": [{"url": self.server.image_url}]}
            if status == 200
            else {"error": {"message": "fixture unauthorized", "code": ""}}
        )
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.server.download_auth.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)


class DocumentationExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise RuntimeError("Node.js 20+ is required to validate the Node.js example")
        cls.scratch = tempfile.TemporaryDirectory(prefix="vizluma-doc-examples-")
        cls.scripts = Path(cls.scratch.name)
        for page, title in (
            ("guides/client-examples.mdx", "generate.py"),
            ("guides/client-examples.mdx", "generate.mjs"),
            ("guides/image-results.mdx", "save_images.py"),
            ("api-reference/images-edits.mdx", "edit-upload.sh"),
            ("api-reference/images-edits.mdx", "edit-url.sh"),
        ):
            source = (ROOT / page).read_text()
            match = re.search(r"^```\w+ " + re.escape(title) + r"\n(.*?)^```", source, re.M | re.S)
            if not match:
                raise RuntimeError(f"Published example missing: {title}")
            (cls.scripts / title).write_text(match[1])
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ExampleServer)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.server.image_url = cls.base + "/image.png"
        cls.server.requests = []
        cls.server.download_auth = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.scratch.cleanup()

    def setUp(self):
        self.server.requests.clear()
        self.server.download_auth.clear()
        self.server.response_status = 200
        self.work = tempfile.TemporaryDirectory(prefix="vizluma-doc-output-")
        self.addCleanup(self.work.cleanup)
        self.env = os.environ | {
            "VIZLUMA_API_KEY": "fixture-key-not-a-real-credential",
            "VIZLUMA_BASE_URL": self.base + "/v1/",
            "REFERENCE_IMAGE_URL": self.base + '/image.png?label="cat"&x=1',
        }
        (Path(self.work.name) / "input.png").write_bytes(PNG)

    def run_example(self, script, *args):
        runtime = "bash" if script.endswith(".sh") else "node" if script.endswith(".mjs") else sys.executable
        if script.endswith(".sh"):
            self.env["VIZLUMA_BASE_URL"] = self.base + "/v1"
        return subprocess.run(
            [runtime, str(self.scripts / script), *args],
            cwd=self.work.name, env=self.env, capture_output=True, text=True, timeout=15,
        )

    def test_clients_send_expected_contract_for_all_models(self):
        for script in ("generate.py", "generate.mjs"):
            for model in MODELS:
                with self.subTest(script=script, model=model):
                    self.env["VIZLUMA_MODEL"] = model
                    result = self.run_example(script)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    path, headers, body = self.server.requests[-1]
                    self.assertEqual(path, "/v1/images/generations")
                    self.assertEqual(headers["Authorization"], "Bearer " + self.env["VIZLUMA_API_KEY"])
                    self.assertEqual(headers["Content-Type"], "application/json")
                    self.assertEqual(body, {
                        "model": model,
                        "prompt": "一只小橘猫坐在纯白色背景上，简洁插画，无文字",
                        "size": "1024x1024", "n": 1, "response_format": "url",
                    })
                    payload = json.loads((Path(self.work.name) / "generation.json").read_text())
                    self.assertEqual(payload["data"][0]["url"], self.server.image_url)
                    self.assertNotIn(self.env["VIZLUMA_API_KEY"], result.stdout + result.stderr)
        self.assertEqual(len(self.server.requests), 6)

    def test_edit_examples_send_reference_url_or_png_file(self):
        for script in ("edit-url.sh", "edit-upload.sh"):
            with self.subTest(script=script):
                result = self.run_example(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                path, headers, body = self.server.requests[-1]
                self.assertEqual(path, "/v1/images/edits")
                self.assertEqual(headers["Authorization"], "Bearer fixture-key-not-a-real-credential")
                self.assertEqual(body["model"], "gpt-image-2")
                self.assertEqual(body["size"], "1024x1024")
                self.assertEqual(body["response_format"], "url")
                self.assertEqual(body["prompt"], "为参考图中的橘猫添加一条红色针织围巾，保持同一只猫、姿态和纯白背景，无文字")
                if script == "edit-upload.sh":
                    self.assertEqual(body["n"], "1")
                    self.assertEqual(body["image"], {"filename": "input.png", "type": "image/png", "bytes": PNG})
                    self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
                else:
                    self.assertEqual(body["n"], 1)
                    self.assertEqual(body["images"], [{"image_url": self.env["REFERENCE_IMAGE_URL"]}])
                payload = json.loads((Path(self.work.name) / "edit.json").read_text())
                self.assertEqual(payload["data"][0]["url"], self.server.image_url)
                self.assertNotIn(self.env["VIZLUMA_API_KEY"], result.stdout + result.stderr)
        self.assertEqual(len(self.server.requests), 2)

    def test_clients_do_not_retry_auth_errors_or_save_them_as_results(self):
        self.server.response_status = 401
        for script in ("generate.py", "generate.mjs"):
            with self.subTest(script=script):
                result = self.run_example(script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("401", result.stderr)
                self.assertFalse((Path(self.work.name) / "generation.json").exists())
                self.assertNotIn(self.env["VIZLUMA_API_KEY"], result.stdout + result.stderr)
        self.assertEqual(len(self.server.requests), 2)

    def test_placeholder_key_fails_without_sending_a_request(self):
        self.env["VIZLUMA_API_KEY"] = "YOUR_API_KEY"
        for script in ("generate.py", "generate.mjs"):
            result = self.run_example(script)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.server.requests, [])

    def test_saver_preserves_url_and_base64_bytes_without_forwarding_key(self):
        encoded = base64.b64encode(PNG).decode().rstrip("=")
        payload = {"data": [
            {"url": self.server.image_url},
            {"b64_json": "data:image/png;base64,\n" + encoded},
        ]}
        response = Path(self.work.name) / "generation.json"
        response.write_text(json.dumps(payload))
        result = self.run_example("save_images.py", str(response))
        self.assertEqual(result.returncode, 0, result.stderr)
        for i in (1, 2):
            self.assertEqual((Path(self.work.name) / f"image-{i}.png").read_bytes(), PNG)
        self.assertEqual(self.server.download_auth, [None])
        # Existing customer files must not be overwritten on a repeated download.
        repeated = self.run_example("save_images.py", str(response))
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual((Path(self.work.name) / "image-1.png").read_bytes(), PNG)

    def test_saver_rejects_error_payloads_and_non_images(self):
        for payload in (
            {"error": {"message": "fixture failed"}},
            {"data": []},
            {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
        ):
            with self.subTest(payload=payload):
                response = Path(self.work.name) / "generation.json"
                response.write_text(json.dumps(payload))
                result = self.run_example("save_images.py", str(response))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(Path(self.work.name).glob("image-*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
