"""Exercise the published examples against a local fixture, never the live API."""

import base64
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import io
import contextlib
from unittest.mock import patch
import urllib.error
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
        if status == 200 and self.headers.get("Prefer") == "respond-async":
            payload = {"id": "img_example", "object": "image.task", "status": "QUEUED"}
            status = 202
        elif status == 200 and self.path == "/v1/videos":
            payload = {"id": "video_fixture", "object": "video", "status": "queued"}
            status = 202
        else:
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
        if self.path.startswith("/v1/images/tasks"):
            self.server.image_task_queries.append((self.path, self.headers.get("Authorization")))
            task = {"id": "img_example", "object": "image.task", "status": "SUCCESS",
                    "response_code": 200, "result": {"data": [{"url": self.server.image_url}]}}
            body = {"object": "list", "data": [task], "total": 1} if "?" in self.path else task
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/v1/videos/"):
            self.server.video_requests.append((self.path, self.headers.get("Authorization")))
            content = self.path.endswith("/content")
            payload = b"fixture-mp4-not-real-media" if content else json.dumps({"id": "video_fixture", "status": "completed", "duration": 5}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4" if content else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
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
            ("api-reference/chat-completions.mdx", "chat.sh"),
            ("api-reference/responses.mdx", "responses.sh"),
            ("api-reference/videos.mdx", "video-create.sh"),
            ("api-reference/videos.mdx", "video-status.sh"),
            ("api-reference/videos.mdx", "video-download.sh"),
            ("api-reference/image-tasks.mdx", "async-create.sh"),
            ("api-reference/image-tasks.mdx", "async-status.sh"),
            ("api-reference/image-tasks.mdx", "async-list.sh"),
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
        cls.server.video_requests = []
        cls.server.image_task_queries = []
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
        self.server.video_requests.clear()
        self.server.image_task_queries.clear()
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

    def test_text_examples_send_expected_contract(self):
        expected = {
            "chat.sh": ("/v1/chat/completions", {
                "model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                "reasoning_effort": "low", "max_completion_tokens": 128, "stream": False,
            }),
            "responses.sh": ("/v1/responses", {
                "model": "gpt-6.1-sol", "input": "Reply with exactly OK.",
                "reasoning": {"effort": "low"}, "max_output_tokens": 128, "store": False, "stream": False,
            }),
        }
        for script, (expected_path, expected_body) in expected.items():
            with self.subTest(script=script):
                result = self.run_example(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                path, headers, body = self.server.requests[-1]
                self.assertEqual(path, expected_path)
                self.assertEqual(body, expected_body)
                self.assertEqual(headers["Authorization"], "Bearer " + self.env["VIZLUMA_API_KEY"])
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertNotIn(self.env["VIZLUMA_API_KEY"], result.stdout + result.stderr)
        self.assertEqual(len(self.server.requests), 2)

    def test_video_examples_create_once_then_query_and_download(self):
        created = self.run_example("video-create.sh")
        self.assertEqual(created.returncode, 0, created.stderr)
        task = json.loads((Path(self.work.name) / "video-task.json").read_text())
        self.env["VIDEO_ID"] = task["id"]
        path, headers, body = self.server.requests[-1]
        self.assertEqual(path, "/v1/videos")
        self.assertEqual(body, {
            "model": "seedance-2.5-480p",
            "prompt": "A cat walking slowly on a sunny beach, gentle waves in the background",
            "duration": 5, "ratio": "16:9", "resolution": "480p",
        })
        self.assertEqual(headers["Authorization"], "Bearer fixture-key-not-a-real-credential")
        status = self.run_example("video-status.sh")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["status"], "completed")
        downloaded = self.run_example("video-download.sh")
        self.assertEqual(downloaded.returncode, 0, downloaded.stderr)
        self.assertEqual((Path(self.work.name) / "video.mp4").read_bytes(), b"fixture-mp4-not-real-media")
        self.assertEqual(self.server.video_requests, [
            ("/v1/videos/video_fixture", "Bearer fixture-key-not-a-real-credential"),
            ("/v1/videos/video_fixture/content", "Bearer fixture-key-not-a-real-credential"),
        ])
        self.assertEqual(len(self.server.requests), 1, "polling and download must not create more videos")
        for result in (created, status, downloaded):
            self.assertNotIn(self.env["VIZLUMA_API_KEY"], result.stdout + result.stderr)

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

    def test_async_image_submission_query_and_list_examples(self):
        for script in ("async-create.sh", "async-status.sh", "async-list.sh"):
            result = self.run_example(script)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.requests), 1)
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1/images/generations")
        self.assertEqual(headers["Prefer"], "respond-async")
        self.assertEqual(headers["Idempotency-Key"], "your-unique-business-request-id")
        self.assertEqual(headers["Authorization"], "Bearer " + self.env["VIZLUMA_API_KEY"])
        self.assertEqual(body["n"], 1)
        self.assertEqual(body["model"], "gpt-image-2")
        self.assertEqual(json.loads((Path(self.work.name) / "image-task.json").read_text())["status"], "QUEUED")
        detail = json.loads((Path(self.work.name) / "image-task-status.json").read_text())
        self.assertEqual(detail["result"]["data"][0]["url"], self.server.image_url)
        listing = json.loads((Path(self.work.name) / "image-task-list.json").read_text())
        self.assertEqual(listing["total"], 1)
        self.assertEqual([query[0] for query in self.server.image_task_queries], [
            "/v1/images/tasks/img_example", "/v1/images/tasks?p=1&page_size=20"])
        self.assertTrue(all(auth == "Bearer " + self.env["VIZLUMA_API_KEY"] for _, auth in self.server.image_task_queries))

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
            {"url": "data:image/png;base64," + encoded},
        ]}
        response = Path(self.work.name) / "generation.json"
        response.write_text(json.dumps(payload))
        result = self.run_example("save_images.py", str(response))
        self.assertEqual(result.returncode, 0, result.stderr)
        for i in (1, 2, 3):
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


class AsyncWorkflowExamples(unittest.TestCase):
    """Execute the exact published workflow; use transport fixtures without sockets."""

    def run_workflow(self, statuses, resume=False, query_error=False):
        source = (ROOT / "guides/async-images.mdx").read_text()
        match = re.search(r"^```python async-image.py\n(.*?)^```", source, re.M | re.S)
        self.assertIsNotNone(match)
        requests = []
        env = {"VIZLUMA_API_KEY": "fixture-key-not-real", "IDEMPOTENCY_KEY": "order-fixture",
               "VIZLUMA_BASE_URL": "https://fixture.invalid/v1/"}
        if resume:
            env["IMAGE_TASK_ID"] = "img_fixture"
        states = iter(statuses)

        def respond(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 60)
            if request.get_method() == "POST":
                self.assertEqual(request.full_url, "https://fixture.invalid/v1/images/generations")
                self.assertEqual(request.get_header("Prefer"), "respond-async")
                self.assertEqual(request.get_header("Idempotency-key"), "order-fixture")
                self.assertEqual(json.loads(request.data)["n"], 1)
                body = {"id": "img_fixture", "status": "QUEUED"}
            else:
                self.assertEqual(request.full_url, "https://fixture.invalid/v1/images/tasks/img_fixture")
                if query_error:
                    raise urllib.error.HTTPError(request.full_url, 503, "fixture unavailable", {}, None)
                body = {"id": "img_fixture", "status": next(states),
                        "result": {"data": [{"url": "https://fixture.invalid/image.png"}]}}
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-key-not-real")
            return io.BytesIO(json.dumps(body).encode())

        with tempfile.TemporaryDirectory() as work:
            previous = Path.cwd()
            try:
                os.chdir(work)
                with patch.dict(os.environ, env, clear=True), \
                     patch("urllib.request.urlopen", side_effect=respond), \
                     patch("time.sleep") as sleep, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    failure = None
                    try:
                        exec(compile(match[1], "async-image.py", "exec"), {"__name__": "__main__"})
                    except SystemExit as error:
                        failure = str(error)
                    files = {p.name: json.loads(p.read_text()) for p in Path(work).glob("*.json")}
                    return requests, files, failure, sleep.call_count, output.getvalue()
            finally:
                os.chdir(previous)

    def test_create_once_poll_then_save_and_resume_without_creating(self):
        requests, files, error, waits, output = self.run_workflow(["IN_PROGRESS", "SUCCESS"])
        self.assertIsNone(error)
        self.assertEqual([r.get_method() for r in requests], ["POST", "GET", "GET"])
        self.assertEqual(files["image-task.json"]["id"], "img_fixture")
        self.assertEqual(files["image-result.json"]["data"][0]["url"], "https://fixture.invalid/image.png")
        self.assertEqual(waits, 1)
        self.assertNotIn("fixture-key-not-real", output)
        requests, files, error, _, _ = self.run_workflow(["SUCCESS"], resume=True)
        self.assertIsNone(error)
        self.assertEqual([r.get_method() for r in requests], ["GET"])
        self.assertNotIn("image-task.json", files)
        self.assertIn("image-result.json", files)

    def test_failure_unknown_and_unrecognized_states_never_resubmit(self):
        for status in ("FAILURE", "UNKNOWN", "unexpected"):
            with self.subTest(status=status):
                requests, files, error, waits, _ = self.run_workflow([status])
                self.assertIsNotNone(error)
                self.assertIn(status, error)
                self.assertEqual([r.get_method() for r in requests], ["POST", "GET"])
                self.assertEqual(files["image-task.json"]["id"], "img_fixture")
                self.assertNotIn("image-result.json", files)
                self.assertEqual(waits, 0)

    def test_query_transport_error_preserves_id_and_never_recreates(self):
        requests, files, error, waits, _ = self.run_workflow([], query_error=True)
        self.assertIn("503", error)
        self.assertEqual(files["image-task.json"]["id"], "img_fixture")
        self.assertNotIn("image-result.json", files)
        self.assertEqual([r.get_method() for r in requests], ["POST", "GET"])
        self.assertEqual(waits, 0)


class TextModelExamples(unittest.TestCase):
    """Validate the published shell invocation without contacting a model."""

    def test_new_model_shell_examples_preserve_path_headers_and_json(self):
        examples = (
            ("models/gpt-text.mdx", "gpt-text.sh", "/chat/completions", "gpt-6.1-terra", "max_completion_tokens", False),
            ("models/claude.mdx", "claude-chat.sh", "/chat/completions", "claude-sonnet-5-5", "max_tokens", False),
            ("api-reference/messages.mdx", "claude-messages.sh", "/messages", "claude-sonnet-5-5", "max_tokens", True),
        )
        with tempfile.TemporaryDirectory(prefix="vizluma-text-docs-") as work:
            folder = Path(work)
            capture = folder / "arguments.json"
            curl = folder / "curl"
            curl.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                            "open(os.environ['VIZLUMA_CAPTURE'],'w').write(json.dumps(sys.argv[1:]))\n")
            curl.chmod(0o700)
            env = os.environ | {
                "PATH": work + os.pathsep + os.environ["PATH"],
                "VIZLUMA_API_KEY": "fixture-not-a-real-key",
                "VIZLUMA_BASE_URL": "https://fixture.invalid/v1/",
                "VIZLUMA_CAPTURE": str(capture),
            }
            for page, title, endpoint, model, token_field, native in examples:
                with self.subTest(page=page):
                    match = re.search(r"^```bash " + re.escape(title) + r"\n(.*?)^```",
                                      (ROOT / page).read_text(), re.M | re.S)
                    self.assertIsNotNone(match)
                    result = subprocess.run(["bash", "-c", match[1]], cwd=work, env=env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    args = json.loads(capture.read_text())
                    self.assertIn("https://fixture.invalid/v1" + endpoint, args)
                    self.assertIn("x-api-key: fixture-not-a-real-key" if native else
                                  "Authorization: Bearer fixture-not-a-real-key", args)
                    if native:
                        self.assertIn("anthropic-version: 2023-06-01", args)
                    body = json.loads(args[args.index("-d") + 1])
                    self.assertEqual(body, {
                        "model": model, token_field: 128, "stream": False,
                        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                    })
                    self.assertNotIn(env["VIZLUMA_API_KEY"], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
