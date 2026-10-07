"""Provider boundary tests use a local HTTP server, never a real model."""

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from workbench.domain import DomainError
from workbench.store import Store
from workbench.queue import Queue
from workbench.adapters.comfy import ComfyAdapter
from workbench.adapters.text import TextAdapter
from workbench.workflows import bind_workflow, make_h3_handler
from workbench.worker import Worker


START = 1710000000000
END = START + 2000
INFO = {"ImageNode": {"input": {"required": {"width": ["INT", {"min": 64, "max": 2048}],
                                            "height": ["INT", {"min": 64, "max": 2048}],
                                            "frames": ["INT", {"min": 1, "max": 120}]}}}}
WORKFLOW = {"1": {"class_type": "ImageNode", "inputs": {"width": 512, "height": 512, "frames": 24}}}
BINDINGS = {"width": {"node": "1", "input": "width"}, "height": {"node": "1", "input": "height"},
            "frames": {"node": "1", "input": "frames"}}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.calls.append(("GET", self.path))
        if self.path.startswith("/view?"):
            self.reply(200, b"fake-video", "application/octet-stream")
            return
        if self.path.startswith("/history/"):
            prompt_id = self.path.split("/")[-1]
            history = self.server.history.get(prompt_id, {})
            if history and self.server.dynamic_history:
                now = int(time.time() * 1000)
                for message in history["status"]["messages"]:
                    message[1]["timestamp"] = now
            self.json(200, {prompt_id: history} if history else {})
            return
        if self.path == "/queue":
            self.json(200, self.server.queue)
            return
        if self.path == "/object_info":
            self.json(200, self.server.info)
            return
        self.json(404, {})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.server.calls.append(("POST", self.path, body))
        if self.path == "/prompt":
            if self.server.slow:
                time.sleep(0.2)
            if self.server.prompt_raw is not None:
                self.reply(self.server.prompt_status, self.server.prompt_raw, "text/plain")
            else:
                self.json(self.server.prompt_status, self.server.prompt_reply)
        elif self.path == "/upload/image":
            self.json(200, {"name": "uploaded.png", "subfolder": "", "type": "input"})
        elif self.path == "/v1/chat/completions":
            self.json(self.server.text_status, self.server.text_reply)
        else:
            self.json(404, {})

    def json(self, status, value):
        self.reply(status, json.dumps(value).encode(), "application/json")

    def reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.server.calls = []
        self.server.queue = {"queue_running": [], "queue_pending": []}
        self.server.info = INFO
        self.server.prompt_status = 200
        self.server.prompt_reply = {"prompt_id": "mine", "number": 0, "node_errors": {}}
        self.server.prompt_raw = None
        self.server.text_status = 200
        self.server.text_reply = {"choices": [{"message": {"role": "assistant", "content": '{"idea":"new"}'}}]}
        self.server.slow = False
        self.server.dynamic_history = False
        self.server.history = {"mine": {"status": {"status_str": "success", "completed": True,
            "messages": [["execution_start", {"prompt_id": "mine", "timestamp": START}],
                         ["execution_success", {"prompt_id": "mine", "timestamp": END}]]},
            "outputs": {"2": {"gifs": [{"filename": "shot.mp4", "subfolder": "", "type": "output"}]}}}}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_busy_queue_prevents_submission(self):
        self.server.queue["queue_running"] = [[0, "someone-else", {}, {}, []]]
        with self.assertRaises(DomainError) as caught:
            ComfyAdapter(self.url, 1).submit(WORKFLOW, "client")
        self.assertEqual(caught.exception.code, "provider_busy")
        self.assertFalse(any(call[0] == "POST" for call in self.server.calls))

    def test_binding_checks_node_required_inputs_and_ranges(self):
        actual = bind_workflow(WORKFLOW, BINDINGS, {"width": 1024, "height": 576, "frames": 48}, INFO)
        self.assertEqual(actual["1"]["inputs"], {"width": 1024, "height": 576, "frames": 48})
        self.assertEqual(WORKFLOW["1"]["inputs"]["width"], 512)
        for workflow, values in [({"1": {"class_type": "Missing", "inputs": {}}}, {}),
                                 (WORKFLOW, {"width": 4096})]:
            with self.assertRaises(DomainError):
                bind_workflow(workflow, BINDINGS, values, INFO)

    def test_http_rejection_and_ambiguous_submit_are_distinct(self):
        adapter = ComfyAdapter(self.url, 0.05)
        self.server.prompt_status = 401
        with self.assertRaises(DomainError) as caught:
            adapter.submit(WORKFLOW, "client")
        self.assertEqual(caught.exception.code, "provider_rejected")
        self.server.prompt_status = 200
        self.server.slow = True
        with self.assertRaises(DomainError) as caught:
            adapter.submit(WORKFLOW, "client")
        self.assertEqual(caught.exception.code, "submission_uncertain")

    def test_malformed_success_reply_is_uncertain(self):
        self.server.prompt_raw = b"not json"
        with self.assertRaises(DomainError) as caught:
            ComfyAdapter(self.url, 1).submit(WORKFLOW, "client")
        self.assertEqual(caught.exception.code, "submission_uncertain")
        self.assertEqual(len([call for call in self.server.calls if call[0] == "POST"]), 1)

    def test_upload_uses_bytes_not_server_filesystem_path(self):
        name = ComfyAdapter(self.url, 1).upload_image(b"image bytes", "frame.png")
        self.assertEqual(name, "uploaded.png")
        upload = [call for call in self.server.calls if call[0] == "POST" and call[1] == "/upload/image"]
        self.assertEqual(len(upload), 1)
        self.assertIn(b"image bytes", upload[0][2])
        with self.assertRaises(DomainError):
            ComfyAdapter(self.url, 1).upload_image(b"image bytes", "../bad.png")
        with self.assertRaises(DomainError):
            ComfyAdapter(self.url, 1).upload_image(b"image bytes", 'bad"name.png')

    def test_first_frame_import_is_scoped_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite", root / "data")
            first = store.create_project("Fiction A")
            other = store.create_project("Fiction B")
            ep = store.create_episode(first["id"], "Episode A")
            shot = store.save_shot(ep["id"], {})
            staging = store.data_root / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            source = staging / "frame.png"
            source.write_bytes(b"image bytes")
            foreign = store.import_asset(other["id"], source, "image", {})
            queue = Queue(store)
            job = queue.enqueue({"project_id": first["id"], "episode_id": ep["id"], "shot_id": shot["id"]},
                                "h3", {"values": {}, "first_frame_asset_id": foreign["id"]})
            workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}}}
            info = {"LoadImage": {"input": {"required": {"image": [["placeholder.png"]]}}}}
            self.server.info = info
            handler = make_h3_handler(queue, store, ComfyAdapter(self.url, 1), workflow,
                                      {"first_frame": {"node": "1", "input": "image"}},
                                      first_frame_binding="first_frame")
            Worker(queue, "gpu", {"h3": handler}).run_once()
            self.assertEqual(queue.get_job(job["id"])["state"], "failed")
            self.assertFalse(any(call[0] == "POST" for call in self.server.calls))

    def test_history_is_scoped_and_collects_binary(self):
        adapter = ComfyAdapter(self.url, 1)
        state = adapter.status("mine")
        self.assertEqual(state["started_at"], datetime.fromtimestamp(START / 1000, timezone.utc).isoformat())
        self.assertEqual(state["finished_at"], datetime.fromtimestamp(END / 1000, timezone.utc).isoformat())
        self.assertEqual(adapter.collect("mine")[0]["content"], b"fake-video")
        self.assertFalse(any("someone-else" in str(call) for call in self.server.calls))
        self.server.history["mine"]["outputs"]["2"]["gifs"][0]["filename"] = "../escape.mp4"
        with self.assertRaises(DomainError):
            adapter.collect("mine")

    def test_execution_error_never_becomes_success(self):
        self.server.history["mine"]["status"] = {"status_str": "error", "completed": False,
            "messages": [["execution_start", {"prompt_id": "mine", "timestamp": START}],
                         ["execution_error", {"prompt_id": "mine", "timestamp": END}]]}
        state = ComfyAdapter(self.url, 1).status("mine")
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["finished_at"], datetime.fromtimestamp(END / 1000, timezone.utc).isoformat())
        with self.assertRaises(DomainError):
            ComfyAdapter(self.url, 1).collect("mine")

    def test_handler_persists_provider_id_and_scoped_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite", root / "data")
            first = store.create_project("Fiction A")
            other = store.create_project("Fiction B")
            ep = store.create_episode(first["id"], "Episode A")
            other_ep = store.create_episode(other["id"], "Episode B")
            shot = store.save_shot(ep["id"], {})
            queue = Queue(store)
            job = queue.enqueue({"project_id": first["id"], "episode_id": ep["id"], "shot_id": shot["id"]},
                                "h3", {"values": {"width": 1024}})
            claimed = queue.claim("gpu")
            self.server.dynamic_history = True
            handler = make_h3_handler(queue, store, ComfyAdapter(self.url, 1), WORKFLOW, BINDINGS)
            result = handler("h3", claimed)
            self.assertEqual(queue.get_job(job["id"])["external_id"], "mine")
            self.assertEqual(queue.get_job(job["id"])["state"], "running")
            self.assertEqual(result["candidate_assets"][0]["project_id"], first["id"])
            self.assertEqual(result["candidate_assets"][0]["rights"]["episode_id"], ep["id"])
            self.assertEqual(store.list_assets(other["id"]), [])
            self.assertIsNone(store.get_shot(shot["id"])["selected_candidate_id"])
            self.assertNotEqual(other_ep["id"], ep["id"])

    def test_text_unconfigured_and_structured_suggestion(self):
        with self.assertRaises(DomainError) as caught:
            TextAdapter().generate([{"role": "user", "content": "idea"}], None)
        self.assertEqual(caught.exception.code, "text_unconfigured")
        response = TextAdapter(self.url + "/v1/chat/completions", "fiction-model").generate(
            [{"role": "user", "content": "idea"}], {"type": "object"})
        self.assertEqual(response["suggestion"], {"idea": "new"})
        self.assertEqual(json.loads([call for call in self.server.calls if call[0] == "POST"][-1][2])["model"], "fiction-model")

    def test_worker_records_observed_failure_without_success(self):
        self.server.dynamic_history = True
        self.server.history["mine"]["status"] = {"status_str": "error", "completed": False,
            "messages": [["execution_start", {"prompt_id": "mine", "timestamp": START}],
                         ["execution_error", {"prompt_id": "mine", "timestamp": END}]]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite", root / "data")
            project = store.create_project("Fiction")
            episode = store.create_episode(project["id"], "Episode")
            shot = store.save_shot(episode["id"], {})
            queue = Queue(store)
            job = queue.enqueue({"project_id": project["id"], "episode_id": episode["id"], "shot_id": shot["id"]},
                                "h3", {"values": {"width": 1024}})
            handler = make_h3_handler(queue, store, ComfyAdapter(self.url, 1), WORKFLOW, BINDINGS)
            self.assertTrue(Worker(queue, "gpu", {"h3": handler}).run_once())
            saved = queue.get_job(job["id"])
            self.assertEqual(saved["state"], "failed")
            self.assertEqual(saved["failure_code"], "provider_execution_failed")
            self.assertEqual(saved["external_id"], "mine")
            self.assertEqual(store.list_assets(project["id"]), [])

    def test_worker_keeps_uncertain_submission_reserved(self):
        self.server.slow = True
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite", root / "data")
            project = store.create_project("Fiction")
            episode = store.create_episode(project["id"], "Episode")
            shot = store.save_shot(episode["id"], {})
            queue = Queue(store)
            job = queue.enqueue({"project_id": project["id"], "episode_id": episode["id"], "shot_id": shot["id"]},
                                "h3", {"values": {}})
            handler = make_h3_handler(queue, store, ComfyAdapter(self.url, 0.05), WORKFLOW, BINDINGS)
            self.assertTrue(Worker(queue, "gpu", {"h3": handler}).run_once())
            saved = queue.get_job(job["id"])
            self.assertEqual(saved["state"], "needs_reconcile")
            self.assertIsNotNone(saved["submission_attempted_at"])
            self.assertIsNone(saved["external_id"])
            self.assertIsNone(queue.claim("gpu"))
            self.assertEqual(len([call for call in self.server.calls if call[0] == "POST" and call[1] == "/prompt"]), 1)

    def test_worker_releases_busy_preflight_without_post(self):
        self.server.queue["queue_pending"] = [[1, "other", {}, {}, []]]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite", root / "data")
            project = store.create_project("Fiction")
            episode = store.create_episode(project["id"], "Episode")
            shot = store.save_shot(episode["id"], {})
            queue = Queue(store)
            job = queue.enqueue({"project_id": project["id"], "episode_id": episode["id"], "shot_id": shot["id"]},
                                "h3", {"values": {}})
            handler = make_h3_handler(queue, store, ComfyAdapter(self.url, 1), WORKFLOW, BINDINGS)
            Worker(queue, "gpu", {"h3": handler}).run_once()
            self.assertEqual(queue.get_job(job["id"])["state"], "failed")
            self.assertIsNone(queue.get_job(job["id"])["submission_attempted_at"])
            self.assertFalse(any(call[0] == "POST" for call in self.server.calls))

    def test_worker_releases_explicit_http_rejection(self):
        self.server.prompt_status = 401
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite", root / "data")
            project = store.create_project("Fiction")
            episode = store.create_episode(project["id"], "Episode")
            shot = store.save_shot(episode["id"], {})
            queue = Queue(store)
            job = queue.enqueue({"project_id": project["id"], "episode_id": episode["id"], "shot_id": shot["id"]},
                                "h3", {"values": {}})
            handler = make_h3_handler(queue, store, ComfyAdapter(self.url, 1), WORKFLOW, BINDINGS)
            Worker(queue, "gpu", {"h3": handler}).run_once()
            saved = queue.get_job(job["id"])
            self.assertEqual(saved["state"], "failed")
            self.assertEqual(saved["failure_code"], "provider_rejected")
            self.assertIsNotNone(saved["submission_attempted_at"])


if __name__ == "__main__":
    unittest.main()
