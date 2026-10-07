"""HTTP boundary tests against a real temporary SQLite store."""

import json
import io
import subprocess
import time
from datetime import datetime, timezone
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workbench.server import make_server


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_server("127.0.0.1", 0, Path(self.temp.name))
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.app.stop_workers)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, method, route, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + route, data=data, method=method,
                          headers={"Content-Type": "application/json", "X-Jingxu-Request": "1", **(headers or {})})
        try:
            with urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_manual_edits_persist_without_model_configuration(self):
        _, project = self.call("POST", "/api/projects", {"title": "虚构作品"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title": "第一集"})
        status, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job": "敲门", "screen_duration_ms": 4000})
        self.assertEqual(status, 201)
        self.assertEqual(shot["revision"], 1)
        status, changed = self.call("PUT", f"/api/episodes/{episode['id']}/shots/{shot['id']}", {"revision": 1, "action": "雨中停步"})
        self.assertEqual(status, 200)
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.call("GET", f"/api/episodes/{episode['id']}/shots")[1][0]["action"], "雨中停步")
        findings = self.call("GET", f"/api/episodes/{episode['id']}/story")[1]
        self.assertTrue(any(item["shot_id"] == shot["id"] and item["field"] == "creative_notes" for item in findings))
        self.assertEqual(self.call("GET", "/api/settings/status")[1]["video"], "unconfigured")

    def test_host_origin_and_custom_header_reject_cross_site(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"X-Jingxu-Request": ""}):
            status, error = self.call("POST", "/api/projects", {"title": "禁止"}, headers)
            self.assertEqual(status, 403)
            self.assertIn("code", error["error"])
        self.assertEqual(self.call("GET", "/api/projects")[1], [])

    def test_invalid_fields_and_cross_project_scope_are_atomic(self):
        _, left = self.call("POST", "/api/projects", {"title": "左"})
        _, right = self.call("POST", "/api/projects", {"title": "右"})
        _, episode = self.call("POST", f"/api/projects/{left['id']}/episodes", {"title": "一"})
        status, error = self.call("POST", f"/api/projects/{right['id']}/episodes/{episode['id']}/shots", {"story_job": "bad"})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "ownership_conflict")
        status, error = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"unknown": 1})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_payload")
        status, error = self.call("PUT", f"/api/episodes/{episode['id']}", {"revision":True,"script":"不应保存"})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_revision")
        self.assertEqual(self.call("GET", f"/api/episodes/{episode['id']}/shots")[1], [])

    def test_episode_revision_and_manual_qc_survive_reopen(self):
        _, project = self.call("POST", "/api/projects", {"title": "甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title": "一"})
        status, edited = self.call("PUT", f"/api/episodes/{episode['id']}", {"revision": 1, "script": "雨停了"})
        self.assertEqual(status, 200)
        self.assertEqual(edited["script"], "雨停了")
        self.assertEqual(self.call("GET", f"/api/episodes/{episode['id']}")[1]["revision"], 2)
        status, error = self.call("PUT", f"/api/episodes/{episode['id']}", {"revision": 1, "script": "旧稿"})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "revision_conflict")

    def test_upload_is_scoped_and_media_is_not_arbitrary_path(self):
        _, left = self.call("POST", "/api/projects", {"title": "甲"})
        _, right = self.call("POST", "/api/projects", {"title": "乙"})
        request = Request(self.base + f"/api/projects/{left['id']}/assets/upload", data=b"synthetic image", method="POST",
                          headers={"X-Jingxu-Request": "1", "X-Asset-Kind": "document", "X-Asset-Rights": json.dumps({"source": "fictional test"})})
        with urlopen(request) as response:
            asset = json.load(response)
        self.assertEqual(self.call("GET", f"/api/projects/{right['id']}/assets/{asset['id']}")[0], 409)
        self.assertEqual(self.call("GET", "/api/../workbench/store.py")[0], 400)
        bad = Request(self.base + f"/api/projects/{left['id']}/assets/upload", data=b"not-video", method="POST",
                      headers={"X-Jingxu-Request":"1","X-Asset-Kind":"video","X-Asset-Rights":json.dumps({"source":"bad"})})
        with self.assertRaises(HTTPError) as failed: urlopen(bad)
        self.assertEqual(failed.exception.code, 422)
        self.assertEqual(len(self.call("GET", f"/api/projects/{left['id']}/assets")[1]), 1)

    def test_settings_never_return_operator_secret(self):
        root = Path(self.temp.name)
        (root / "operator-config.json").write_text(json.dumps({"text": {"endpoint": "http://127.0.0.1:9999", "model": "x", "secret": "TOP_SECRET"}}))
        self.server.app.config = self.server.app._config()
        status = self.call("GET", "/api/settings/status")[1]
        self.assertEqual(status["text"], "configured")
        self.assertNotIn("TOP_SECRET", json.dumps(status))
        self.server.app.config["asr"] = {"config_path": str(root / "missing-asr.json")}
        self.assertEqual(self.call("GET", "/api/settings/status")[1]["asr"], "config_missing")

    def test_text_proposal_requires_explicit_adoption_and_matching_revision(self):
        _, project = self.call("POST", "/api/projects", {"title": "甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title": "一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job": "原稿"})
        job = self.server.app.queue.enqueue({"project_id":project["id"], "episode_id":episode["id"], "shot_id":shot["id"]}, "text", {})
        public_job = self.call("GET", f"/api/projects/{project['id']}/jobs/{job['id']}")[1]
        self.assertNotIn("claim_token", public_job)
        self.assertNotIn("source_snapshot", public_job)
        claimed = self.server.app.queue.claim("cpu")
        self.server.app.queue.finish(job["id"], {"suggestion":"改稿", "status":"pending_adoption"}, claimed["claim_token"])
        self.assertEqual(self.call("GET", f"/api/episodes/{episode['id']}/shots/{shot['id']}")[1]["story_job"], "原稿")
        status, adopted = self.call("POST", f"/api/projects/{project['id']}/jobs/{job['id']}/adopt", {"revision":shot["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(adopted["story_job"], "改稿")
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/jobs/{job['id']}/adopt", {"revision":shot["revision"]})[0], 409)

    def test_manual_transcript_comparison_does_not_claim_asr(self):
        _, project = self.call("POST", "/api/projects", {"title": "甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title": "一"})
        status, result = self.call("POST", f"/api/episodes/{episode['id']}/transcript-review", {"expected":[{"speaker_id":"甲","text":"你好"}],"actual":[{"speaker_id":"甲","text":"您好"}]})
        self.assertEqual(status, 200)
        self.assertFalse(result["human_reviewed"])
        self.assertTrue(result["findings"])

    def test_unconfigured_asr_does_not_queue_and_foreign_probe_is_rejected(self):
        _, left = self.call("POST", "/api/projects", {"title": "甲"})
        _, right = self.call("POST", "/api/projects", {"title": "乙"})
        _, episode = self.call("POST", f"/api/projects/{left['id']}/episodes", {"title": "一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job": "镜"})
        status, error = self.call("POST", f"/api/episodes/{episode['id']}/shots/{shot['id']}/jobs", {"kind":"asr","payload":{"asset_id":"does-not-exist"}})
        self.assertEqual(status, 503)
        self.assertEqual(error["error"]["code"], "asr_unconfigured")
        media = Path(self.temp.name) / "other.bin"
        media.write_bytes(b"image binary")
        request = Request(self.base + f"/api/projects/{right['id']}/assets/upload", data=media.read_bytes(), method="POST",
                          headers={"X-Jingxu-Request":"1","X-Asset-Kind":"document","X-Asset-Rights":json.dumps({"source":"test"})})
        with urlopen(request) as response: asset = json.load(response)
        status, error = self.call("POST", f"/api/episodes/{episode['id']}/shots/{shot['id']}/jobs", {"kind":"probe","payload":{"asset_id":asset['id']}})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "ownership_conflict")
        self.assertEqual(self.call("GET", f"/api/projects/{left['id']}/jobs")[1], [])

    def test_workers_only_start_after_explicit_same_origin_action(self):
        self.assertFalse(self.call("GET", "/api/settings/status")[1]["workers_active"])
        status, started = self.call("POST", "/api/queue/start", {})
        self.assertEqual(status, 200)
        self.assertTrue(started["workers_active"])
        self.assertEqual(started["gpu_workers"], 0)

    def test_export_job_requires_real_selected_timeline(self):
        _, project = self.call("POST", "/api/projects", {"title": "甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title": "一"})
        status, error = self.call("POST", f"/api/projects/{project['id']}/episodes/{episode['id']}/export-jobs", {"subtitles":False})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "timeline_missing")

    def test_export_worker_rejects_edited_episode_after_queueing(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"]},"export",{"subtitles":False})
        self.server.app.store.update_episode(episode["id"], {"script":"新版"}, 1)
        claimed = self.server.app.queue.claim("cpu")
        from workbench.domain import DomainError
        with self.assertRaises(DomainError) as stale:
            self.server.app._cpu_handlers()["export"]("export", claimed)
        self.assertEqual(stale.exception.code, "revision_conflict")

    def test_unknown_external_job_stays_manual_pending_during_reconcile(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"镜"})
        job = self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"],"shot_id":shot["id"]},"h3",{"values":{}})
        claimed = self.server.app.queue.claim("gpu")
        self.server.app.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.server.app.queue.fail(job["id"], "network_uncertain", "无法确认外部响应", claimed["claim_token"])
        status, result = self.call("GET", f"/api/projects/{project['id']}/jobs/{job['id']}/reconcile")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "manual_pending")
        self.assertEqual(self.server.app.queue.get_job(job["id"])["state"], "needs_reconcile")

    def test_known_external_success_is_collected_once_with_proof(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"镜"})
        job = self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"],"shot_id":shot["id"]},"h3",{"values":{}})
        claimed = self.server.app.queue.claim("gpu")
        self.server.app.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.server.app.queue.record_external(job["id"], "external-1", claimed["claim_token"])
        self.server.app.queue.fail(job["id"], "network_uncertain", "等待核对", claimed["claim_token"])
        time.sleep(0.01)
        stamp = datetime.now(timezone.utc).isoformat()
        png = (Path(__file__).parent.parent / "web" / "assets" / "rain-alley-demo.png").read_bytes()
        class FakeAdapter:
            def status(self, prompt_id):
                return {"state":"succeeded","started_at":stamp,"finished_at":stamp}
            def collect(self, prompt_id):
                return [{"filename":"frame.png","content":png}]
        result = self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), FakeAdapter())
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(self.server.app.store.list_assets(project["id"])), 1)
        self.assertEqual(result["result"]["candidate_assets"][0]["rights"]["shot_id"], shot["id"])
        with self.assertRaises(Exception):
            self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), FakeAdapter())
        self.assertEqual(len(self.server.app.store.list_assets(project["id"])), 1)

    def test_known_external_failure_needs_complete_timestamps(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"镜"})
        job = self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"],"shot_id":shot["id"]},"h3",{"values":{}})
        claimed = self.server.app.queue.claim("gpu")
        self.server.app.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.server.app.queue.record_external(job["id"], "external-2", claimed["claim_token"])
        self.server.app.queue.fail(job["id"], "network_uncertain", "等待核对", claimed["claim_token"])
        time.sleep(0.01)
        stamp = datetime.now(timezone.utc).isoformat()
        class FakeAdapter:
            def __init__(self, complete): self.complete = complete
            def status(self, prompt_id): return {"state":"failed","started_at":stamp,"finished_at":stamp if self.complete else None}
        from workbench.domain import DomainError
        with self.assertRaises(DomainError) as missing:
            self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), FakeAdapter(False))
        self.assertEqual(missing.exception.code, "evidence_missing")
        self.assertEqual(self.server.app.queue.get_job(job["id"])["state"], "needs_reconcile")
        resolved = self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), FakeAdapter(True))
        self.assertEqual(resolved["state"], "failed")

    def test_h3_queue_binds_current_shot_and_native_dialogue_only_when_configured(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"寻找失踪者","generation_duration_ms":6000,"dialogue":[{"speaker_id":"访客","text":"有人在吗"}]})
        self.server.app.config["video"] = {"endpoint":"http://127.0.0.1:8188","workflow":{"1":{"class_type":"Fake","inputs":{}}},"bindings":{"prompt":{},"voice":{},"seconds":{}},"input_map":{"prompt":"story_job","voice":"dialogue_text","seconds":"generation_duration_seconds"}}
        status, queued = self.call("POST", f"/api/episodes/{episode['id']}/shots/{shot['id']}/jobs", {"kind":"h3","payload":{"plan_revision":1,"strategy":"configured"}})
        self.assertEqual(status, 201, queued)
        stored = self.server.app.queue.get_job(queued["id"])
        self.assertEqual(stored["payload"]["values"], {"prompt":"寻找失踪者","voice":"访客：有人在吗","seconds":6})
        self.assertNotIn("values", queued["payload"])

    def test_real_video_timeline_cues_export_and_archive(self):
        source = Path(self.temp.name) / "source.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True, timeout=30)
        _, project = self.call("POST", "/api/projects", {"title": "虚构成片"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title": "一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job": "门前", "dialogue":[{"speaker_id":"访客","text":"有人吗"}]})
        request = Request(self.base + f"/api/projects/{project['id']}/assets/upload", data=source.read_bytes(), method="POST", headers={"X-Jingxu-Request":"1","X-Asset-Kind":"video","X-Asset-Rights":json.dumps({"source":"upload","license":"synthetic"})})
        with urlopen(request) as response: asset = json.load(response)
        status, info = self.call("GET", f"/api/projects/{project['id']}/assets/{asset['id']}/probe")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(info["duration_ms"], 800)
        _, selected = self.call("POST", f"/api/episodes/{episode['id']}/shots/{shot['id']}/select", {"revision":1,"asset_id":asset['id']})
        self.assertEqual(selected["selected_candidate_id"], asset["id"])
        status, timeline = self.call("PUT", f"/api/episodes/{episode['id']}/timeline", {"revision":1,"items":[{"shot_id":shot['id'],"source_asset_id":asset['id'],"in_ms":0,"out_ms":800}]})
        self.assertEqual(status, 200)
        status, cues = self.call("PUT", f"/api/episodes/{episode['id']}/cues", {"revision":2,"cues":[{"start_ms":0,"end_ms":500,"text":"有人吗","speaker_id":"访客","source_asset_id":asset['id']}]})
        self.assertEqual(status, 200)
        self.assertEqual(cues["status"], "ready")
        status, queued = self.call("POST", f"/api/projects/{project['id']}/episodes/{episode['id']}/export-jobs", {"subtitles":True,"width":160,"height":90,"fps":10})
        self.assertEqual(status, 201, queued)
        self.assertEqual(queued["state"], "queued")
        self.call("POST", "/api/queue/start", {})
        for _ in range(100):
            _, job = self.call("GET", f"/api/projects/{project['id']}/jobs/{queued['id']}")
            if job["state"] in ("succeeded", "failed"): break
            time.sleep(0.1)
        self.assertEqual(job["state"], "succeeded", job)
        report = job["result"]
        self.assertEqual(report["id"], queued["id"])
        self.assertEqual(status, 201, report)
        self.assertTrue(report["subtitle_included"])
        with urlopen(self.base + report["video_url"]) as response: self.assertGreater(len(response.read()), 1000)
        with urlopen(Request(self.base + report["video_url"], headers={"Range":"bytes=0-15"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 16)
        status, archive = self.call("POST", f"/api/projects/{project['id']}/archive", {})
        self.assertEqual(status, 201)
        with urlopen(self.base + archive["url"]) as response: self.assertTrue(response.read(4).startswith(b"PK"))
        _, other = self.call("POST", "/api/projects", {"title":"其他作品"})
        cross_scope = archive["url"].replace(project["id"], other["id"])
        self.assertEqual(self.call("GET", cross_scope)[0], 409)
        status, no_caption_job = self.call("POST", f"/api/projects/{project['id']}/episodes/{episode['id']}/export-jobs", {"subtitles":False,"width":160,"height":90,"fps":10})
        self.assertEqual(status, 201)
        for _ in range(100):
            _, completed = self.call("GET", f"/api/projects/{project['id']}/jobs/{no_caption_job['id']}")
            if completed["state"] in ("succeeded", "failed"): break
            time.sleep(0.1)
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertTrue(completed["result"]["missing_caption_flag"])
        self.assertIsNone(completed["result"]["srt_url"])


if __name__ == "__main__":
    unittest.main()
