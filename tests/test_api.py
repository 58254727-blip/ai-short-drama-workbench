"""HTTP boundary tests against a real temporary SQLite store."""

import json
import io
import subprocess
import time
from datetime import datetime, timezone
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workbench.server import make_server
from workbench.domain import DomainError
from workbench import server as server_module
from workbench.archive import archive_project, restore_archive
from workbench.store import Store


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

    def expire_for_reconcile(self, job_id):
        with self.server.app.store.transaction() as conn:
            conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00+00:00", job_id))
        self.server.app.recover_expired()

    def selected_export_fixture(self):
        staging = Path(self.temp.name) / "staging"
        staging.mkdir(exist_ok=True)
        source = staging / "selected.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
                        "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True, capture_output=True, timeout=30)
        project = self.server.app.store.create_project("虚构")
        episode = self.server.app.store.create_episode(project["id"], "一")
        shot = self.server.app.store.save_shot(episode["id"], {"dialogue": [{"speaker_id": "甲", "text": "出发"}]})
        asset = self.server.app.store.import_asset(project["id"], source, "video", {"source": "synthetic"})
        shot = self.server.app.store.select_candidate(shot["id"], asset["id"], shot["revision"])
        self.server.app.timeline.save_timeline(episode["id"], [{"shot_id": shot["id"], "source_asset_id": asset["id"], "in_ms": 0, "out_ms": 800}], episode["revision"])
        return project, episode, shot, asset

    def test_queued_export_rejects_shot_revision_change_before_work(self):
        project, episode, shot, asset = self.selected_export_fixture()
        status, queued = self.call("POST", f"/api/projects/{project['id']}/episodes/{episode['id']}/export-jobs", {"subtitles": False, "width": 160, "height": 90})
        self.assertEqual(status, 201, queued)
        self.server.app.store.save_shot(episode["id"], {"id": shot["id"], "dialogue": [{"speaker_id": "甲", "text": "停下"}]}, shot["revision"])
        claimed = self.server.app.queue.claim("cpu")
        with self.assertRaises(DomainError) as caught:
            self.server.app._cpu_handlers()["export"](None, claimed)
        self.assertEqual(caught.exception.code, "revision_conflict")
        self.assertFalse((Path(self.temp.name) / "exports" / f"{queued['id']}.json").exists())

    def test_queued_export_discards_output_if_source_changes_before_report(self):
        project, episode, shot, asset = self.selected_export_fixture()
        _, queued = self.call("POST", f"/api/projects/{project['id']}/episodes/{episode['id']}/export-jobs", {"subtitles": False, "width": 160, "height": 90})
        claimed = self.server.app.queue.claim("cpu")
        real_export = server_module.export_episode
        def change_after_render(*args, **kwargs):
            result = real_export(*args, **kwargs)
            self.server.app.store.save_shot(episode["id"], {"id": shot["id"], "action": "改变"}, shot["revision"])
            return result
        with patch.object(server_module, "export_episode", side_effect=change_after_render):
            with self.assertRaises(DomainError) as caught:
                self.server.app._cpu_handlers()["export"](None, claimed)
        self.assertEqual(caught.exception.code, "revision_conflict")
        self.assertFalse((Path(self.temp.name) / "exports" / f"{queued['id']}.json").exists())
        self.assertFalse((Path(self.temp.name) / "exports" / f"{queued['id']}.mp4").exists())

    def test_queued_export_discards_output_if_binary_hash_changes_before_report(self):
        project, episode, shot, asset = self.selected_export_fixture()
        _, queued = self.call("POST", f"/api/projects/{project['id']}/episodes/{episode['id']}/export-jobs", {"subtitles": False, "width": 160, "height": 90})
        claimed = self.server.app.queue.claim("cpu")
        real_export = server_module.export_episode
        def alter_source_after_render(*args, **kwargs):
            result = real_export(*args, **kwargs)
            (Path(self.temp.name) / "assets" / asset["sha256"]).write_bytes(b"modified after render")
            return result
        with patch.object(server_module, "export_episode", side_effect=alter_source_after_render):
            with self.assertRaises(DomainError) as caught:
                self.server.app._cpu_handlers()["export"](None, claimed)
        self.assertEqual(caught.exception.code, "asset_hash_mismatch")
        self.assertFalse((Path(self.temp.name) / "exports" / f"{queued['id']}.json").exists())
        self.assertFalse((Path(self.temp.name) / "exports" / f"{queued['id']}.mp4").exists())

    def test_changed_asset_hash_is_never_served_by_media_get(self):
        project, episode, shot, asset = self.selected_export_fixture()
        (Path(self.temp.name) / "assets" / asset["sha256"]).write_bytes(b"changed")
        status, error = self.call("GET", f"/api/projects/{project['id']}/assets/{asset['id']}/media")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "asset_binary_missing")

    def test_cpu_text_manual_settlement_requires_local_recovered_claim(self):
        project = self.server.app.store.create_project("虚构")
        episode = self.server.app.store.create_episode(project["id"], "一")
        shot = self.server.app.store.save_shot(episode["id"], {})
        job = self.server.app.queue.enqueue({"project_id": project["id"], "episode_id": episode["id"], "shot_id": shot["id"]}, "text", {})
        self.server.app.queue.claim("cpu")
        route = f"/api/projects/{project['id']}/jobs/{job['id']}/manual-settlement"
        self.assertEqual(self.call("POST", route, {"acknowledged": True, "note": "检查"})[0], 409)
        self.expire_for_reconcile(job["id"])
        self.assertEqual(self.call("POST", route, {"acknowledged": False, "note": "检查"})[0], 400)
        status, settled = self.call("POST", route, {"acknowledged": True, "note": "用户确认外部状态未知"})
        self.assertEqual(status, 200, settled)
        self.assertEqual(settled["state"], "failed")
        self.assertEqual(settled["manual_settlement"]["note"], "用户确认外部状态未知")
        self.assertNotIn("claim_token", settled)

    def test_selected_video_audio_can_feed_fenced_fake_asr(self):
        project, episode, shot, asset = self.selected_export_fixture()
        self.server.app.config["asr"] = {"config_path": "fake-local-config"}
        route = f"/api/episodes/{episode['id']}/shots/{shot['id']}/jobs"
        with patch.object(self.server.app, "status", return_value={"asr": "configured"}):
            status, queued = self.call("POST", route, {"kind": "asr", "payload": {"source": "selected_video", "asset_id": asset["id"]}})
        self.assertEqual(status, 201, queued)
        job = self.server.app.queue.claim("cpu")
        with patch.object(self.server.app, "status", return_value={"asr": "configured"}), patch.object(server_module, "transcribe_offline", return_value=[{"text": "机器线索", "speaker_id": "unknown", "status": "machine_clue"}]) as fake:
            result = self.server.app._cpu_handlers()["asr"](None, job)
        self.assertEqual(fake.call_args.args[0], Path(self.temp.name) / "assets" / asset["sha256"])
        self.assertEqual(result["source_asset_id"], asset["id"])
        self.assertFalse(result["human_reviewed"])
        self.server.app.store.save_shot(episode["id"], {"id": shot["id"], "action": "修改"}, shot["revision"])
        with patch.object(self.server.app, "status", return_value={"asr": "configured"}), patch.object(server_module, "transcribe_offline") as fake:
            with self.assertRaises(DomainError):
                self.server.app._cpu_handlers()["asr"](None, job)
            fake.assert_not_called()

    def test_story_notes_fields_are_recordable_and_restored(self):
        project = self.server.app.store.create_project("虚构")
        episode = self.server.app.store.create_episode(project["id"], "一")
        first = self.server.app.store.create_scene(episode["id"], {"title": "前夜", "location": "屋内", "purpose": "等人", "sequence": 0})
        second = self.server.app.store.create_scene(episode["id"], {"title": "清晨", "location": "屋内", "purpose": "出发", "sequence": 1})
        notes = f"自由备注\n场景时间[{first['id']}]: 夜\n场景时间[{second['id']}]: 清晨\n续集期待: 门外是谁"
        self.call("PUT", f"/api/episodes/{episode['id']}", {"revision": 1, "creative_notes": notes})
        findings = self.call("GET", f"/api/episodes/{episode['id']}/story")[1]
        self.assertFalse(any(item["field"] == "next_expectation" for item in findings))
        self.assertTrue(any(item["field"] == "transition" and item["scene_id"] == second["id"] for item in findings))
        saved = Path(self.temp.name) / "story-notes.zip"
        archive_project(self.server.app.store, project["id"], saved)
        restored_root = Path(self.temp.name) / "restored-notes"
        restored = Store(restored_root / "db.sqlite", restored_root)
        restore_archive(restored, saved)
        self.assertEqual(restored.get_episode(episode["id"])["creative_notes"], notes)

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

    def test_static_script_css_html_and_favicon_have_browser_safe_content_types(self):
        expected = {"/app.js":"text/javascript", "/styles.css":"text/css", "/":"text/html", "/assets/rain-alley-demo.png":"image/png", "/assets/favicon.svg":"image/svg+xml", "/favicon.ico":"image/svg+xml"}
        with patch("mimetypes.guess_type", return_value=("text/plain", None)):
            for route, media_type in expected.items():
                with self.subTest(route=route), urlopen(self.base + route) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), media_type)
                    self.assertTrue(response.read())

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

    def test_scene_goal_edit_is_scoped_revisioned_and_persistent(self):
        _, left = self.call("POST", "/api/projects", {"title":"左"})
        _, right = self.call("POST", "/api/projects", {"title":"右"})
        _, episode = self.call("POST", f"/api/projects/{left['id']}/episodes", {"title":"一"})
        _, foreign = self.call("POST", f"/api/projects/{right['id']}/episodes", {"title":"二"})
        _, scene = self.call("POST", f"/api/episodes/{episode['id']}/scenes", {"title":"门前","purpose":"旧目标","location":"雨巷"})
        route = f"/api/episodes/{episode['id']}/scenes/{scene['id']}"
        status, edited = self.call("PUT", route, {"revision":1,"title":"门前","purpose":"找到屋内人","location":"雨巷"})
        self.assertEqual(status, 200)
        self.assertEqual(edited["purpose"], "找到屋内人")
        self.assertEqual(self.call("GET", f"/api/episodes/{episode['id']}/scenes")[1][0]["purpose"], "找到屋内人")
        status, conflict = self.call("PUT", route, {"revision":1,"purpose":"旧请求"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        status, foreign_error = self.call("PUT", f"/api/episodes/{foreign['id']}/scenes/{scene['id']}", {"revision":1,"purpose":"跨集"})
        self.assertEqual(status, 409)
        self.assertEqual(foreign_error["error"]["code"], "ownership_conflict")
        self.assertEqual(self.call("GET", f"/api/episodes/{episode['id']}/scenes")[1][0]["purpose"], "找到屋内人")

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

    def test_ascii_escaped_unicode_rights_header_preserves_exact_upload_record(self):
        _, project = self.call("POST", "/api/projects", {"title":"中文测试"})
        rights = {"source":"手动导入\n雨夜\"甲\"😀", "license":"自有或已获授权🐉", "status":"unreviewed"}
        encoded = json.dumps(rights, ensure_ascii=True, separators=(",", ":"))
        self.assertTrue(encoded.isascii())
        request = Request(self.base + f"/api/projects/{project['id']}/assets/upload", data=b"fictional document", method="POST",
                          headers={"X-Jingxu-Request":"1","X-Asset-Kind":"document","X-Asset-Rights":encoded})
        with urlopen(request) as response: asset = json.load(response)
        self.assertEqual(asset["rights"], rights)
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}/assets/{asset['id']}")[1]["rights"], rights)

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

    def test_shot_scoped_transcript_comparison_uses_selected_shot_dialogue(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, first = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"dialogue":[{"speaker_id":"甲","text":"第一句"}]})
        _, second = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"dialogue":[{"speaker_id":"乙","text":"第二句"}]})
        status, result = self.call("POST", f"/api/episodes/{episode['id']}/shots/{second['id']}/transcript-review", {"actual":[{"speaker_id":"乙","text":"第二句"}]})
        self.assertEqual(status, 200)
        self.assertEqual(result["expected"], [{"speaker_id":"乙","text":"第二句"}])
        self.assertEqual(result["findings"], [])
        self.assertNotEqual(result["expected"], first["dialogue"])

    def test_two_shot_manual_review_is_scoped_to_selected_source(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, first = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"一"})
        _, second = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"二"})
        staging = self.server.app.root / "staging"
        staging.mkdir(exist_ok=True)
        media = staging / "synthetic.mp4"
        media.write_bytes(b"\x00\x00\x00\x18ftypisomsynthetic")
        asset = self.server.app.store.import_asset(project["id"], media, "video", {"source":"fixture"})
        self.call("POST", f"/api/episodes/{episode['id']}/shots/{second['id']}/select", {"revision":1,"asset_id":asset["id"]})
        status, reviews = self.call("POST", f"/api/episodes/{episode['id']}/shots/{second['id']}/qc", {"asset_id":asset["id"],"verdict":"pass","note":"第二镜已看"})
        self.assertEqual(status, 200)
        self.assertEqual([row["shot_id"] for row in reviews], [second["id"]])
        self.assertEqual(self.call("POST", f"/api/episodes/{episode['id']}/shots/{first['id']}/qc", {"asset_id":asset["id"],"verdict":"pass","note":"错误镜头"})[0], 409)

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
        project, episode, shot, asset = self.selected_export_fixture()
        self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"]},"export",{"subtitles":False})
        self.server.app.store.update_episode(episode["id"], {"script":"新版"}, self.server.app.store.get_episode(episode["id"])["revision"])
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
        self.expire_for_reconcile(job["id"])
        time.sleep(0.01)
        stamp = datetime.now(timezone.utc).isoformat()
        png = (Path(__file__).parent.parent / "web" / "assets" / "rain-alley-demo.png").read_bytes()
        class FakeAdapter:
            def status(self, prompt_id):
                return {"state":"succeeded","started_at":stamp,"finished_at":stamp}
            def collect(self, prompt_id):
                return [{"filename":"frame.png","content":png}]
        gate = threading.Barrier(3)
        results, errors = [], []
        def attempt():
            gate.wait()
            try:
                results.append(self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), FakeAdapter()))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads: thread.start()
        gate.wait()
        for thread in threads: thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "invalid_state")
        result = results[0]
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(self.server.app.store.list_assets(project["id"])), 1)
        self.assertEqual(result["result"]["candidate_assets"][0]["rights"]["shot_id"], shot["id"])
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
        self.expire_for_reconcile(job["id"])
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

    def test_invalid_external_utc_proof_never_imports_candidate(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"镜"})
        job = self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"],"shot_id":shot["id"]},"h3",{"values":{}})
        claimed = self.server.app.queue.claim("gpu")
        self.server.app.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.server.app.queue.record_external(job["id"], "external-bad-time", claimed["claim_token"])
        self.server.app.queue.fail(job["id"], "uncertain", "等待核对", claimed["claim_token"])
        self.expire_for_reconcile(job["id"])
        png = (Path(__file__).parent.parent / "web" / "assets" / "rain-alley-demo.png").read_bytes()
        from workbench.domain import DomainError
        class BadProof:
            def status(self, prompt_id): return {"state":"succeeded","started_at":"2026-10-07T00:00:00+08:00","finished_at":"2026-10-07T00:01:00+08:00"}
            def collect(self, prompt_id): return [{"filename":"frame.png","content":png}]
        with self.assertRaises(DomainError): self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), BadProof())
        self.assertEqual(self.server.app.store.list_assets(project["id"]), [])
        self.assertEqual(self.server.app.queue.get_job(job["id"])["state"], "needs_reconcile")

    def test_foreign_live_reconcile_claim_is_not_borrowed_from_database(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"镜"})
        job = self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"],"shot_id":shot["id"]},"h3",{"values":{}})
        claimed = self.server.app.queue.claim("gpu")
        self.server.app.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.server.app.queue.record_external(job["id"], "external-live", claimed["claim_token"])
        self.server.app.queue.fail(job["id"], "uncertain", "等待核对", claimed["claim_token"])
        stamp = datetime.now(timezone.utc).isoformat()
        class Proof:
            def status(self, prompt_id): return {"state":"failed","started_at":stamp,"finished_at":stamp}
        from workbench.domain import DomainError
        with self.assertRaises(DomainError) as refused:
            self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), Proof())
        self.assertEqual(refused.exception.code, "lease_active")
        still = self.server.app.queue.get_job(job["id"])
        self.assertEqual(still["claim_token"], claimed["claim_token"])
        self.assertEqual(still["state"], "needs_reconcile")

    def test_paused_app_can_reconcile_after_lease_expiry_without_starting_workers(self):
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        _, shot = self.call("POST", f"/api/episodes/{episode['id']}/shots", {"story_job":"镜"})
        job = self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"],"shot_id":shot["id"]},"h3",{"values":{}})
        claimed = self.server.app.queue.claim("gpu")
        self.server.app.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.server.app.queue.record_external(job["id"], "external-paused", claimed["claim_token"])
        self.server.app.queue.fail(job["id"], "uncertain", "等待核对", claimed["claim_token"])
        with self.server.app.store.transaction() as conn:
            conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00+00:00", job["id"]))
        self.server.app.recover_expired()
        self.assertFalse(self.server.app.workers)
        self.assertNotEqual(self.server.app.queue.get_job(job["id"])["claim_token"], claimed["claim_token"])
        stamp = datetime.now(timezone.utc).isoformat()
        class Proof:
            def status(self, prompt_id): return {"state":"failed","started_at":stamp,"finished_at":stamp}
        resolved = self.server.app.resolve_known(self.server.app.queue.get_job(job["id"]), Proof())
        self.assertEqual(resolved["state"], "failed")

    def test_early_restart_only_requeues_expired_local_job_on_later_tick(self):
        from workbench.server import App
        _, project = self.call("POST", "/api/projects", {"title":"甲"})
        _, episode = self.call("POST", f"/api/projects/{project['id']}/episodes", {"title":"一"})
        job = self.server.app.queue.enqueue({"project_id":project["id"],"episode_id":episode["id"]},"probe",{"asset_id":"synthetic","idempotent_local":True})
        claimed = self.server.app.queue.claim("cpu")
        second = App(self.server.app.root)
        self.addCleanup(second.close)
        self.assertEqual(second.queue.get_job(job["id"])["claim_token"], claimed["claim_token"])
        self.assertEqual(second.queue.get_job(job["id"])["state"], "running")
        with self.server.app.store.transaction() as conn:
            conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", ("2000-01-01T00:00:00+00:00", job["id"]))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and second.queue.get_job(job["id"])["state"] != "queued":
            time.sleep(0.05)
        ready = second.queue.get_job(job["id"])
        self.assertEqual(ready["state"], "queued")
        self.assertIsNone(ready["claim_token"])
        self.assertFalse(second.workers)

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
