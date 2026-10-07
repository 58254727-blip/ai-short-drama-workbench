import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from workbench.domain import DomainError
from workbench.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite3")

    def test_scoped_shot_and_scene_reject_cross_episode_updates(self):
        a = self.store.create_project("甲")
        b = self.store.create_project("乙")
        ea = self.store.create_episode(a["id"], "一")
        eb = self.store.create_episode(b["id"], "一")
        scene = self.store.create_scene(ea["id"], {"title": "客厅", "purpose": "冲突", "location": "室内", "sequence": 1})
        shot = self.store.save_shot(ea["id"], {"scene_id": scene["id"], "order": 1, "dialogue": [{"speaker_id": "a", "text": "别走。"}]})
        self.assertEqual([ea], self.store.list_episodes(a["id"]))
        self.assertEqual([scene], self.store.list_scenes(ea["id"]))
        self.assertEqual([shot], self.store.list_shots(ea["id"]))
        with self.assertRaises(DomainError) as caught:
            self.store.save_shot(eb["id"], {"id": shot["id"], "action": "steal"}, shot["revision"])
        self.assertEqual(409, caught.exception.status)
        with self.assertRaises(DomainError):
            self.store.save_shot(eb["id"], {"scene_id": scene["id"], "order": 2})
        self.assertEqual(shot, self.store.get_shot(shot["id"]))
        self.assertEqual([], self.store.list_shots(eb["id"]))

    def test_revisions_and_script_are_preserved(self):
        p = self.store.create_project("甲")
        ep = self.store.create_episode(p["id"], "一")
        changed_ep = self.store.update_episode(ep["id"], {"script": "原句", "creative_notes": "克制"}, ep["revision"])
        self.assertEqual(2, changed_ep["revision"])
        shot = self.store.save_shot(ep["id"], {"order": 1, "action": "走"})
        changed = self.store.save_shot(ep["id"], {"id": shot["id"], "action": "停"}, shot["revision"])
        for action in (
            lambda: self.store.update_episode(ep["id"], {"script": "旧"}, ep["revision"]),
            lambda: self.store.save_shot(ep["id"], {"id": shot["id"], "action": "旧"}, shot["revision"]),
        ):
            with self.assertRaises(DomainError) as caught:
                action()
            self.assertEqual(409, caught.exception.status)
        self.assertEqual(changed_ep, self.store.get_episode(ep["id"]))
        self.assertEqual(changed, self.store.get_shot(shot["id"]))

    def test_export_restore_is_scoped_and_conflicts_are_atomic(self):
        a = self.store.create_project("甲")
        b = self.store.create_project("乙")
        ea = self.store.create_episode(a["id"], "一")
        self.store.create_episode(b["id"], "私有")
        self.store.save_shot(ea["id"], {"order": 1, "dialogue": [{"speaker_id": "a", "text": "原句"}]})
        bundle = self.store.export_project(a["id"])
        self.assertEqual(1, bundle["format_version"])
        self.assertNotIn("私有", str(bundle))
        self.assertNotIn(str(self.root), str(bundle))
        with self.assertRaises(DomainError) as caught:
            self.store.restore_project(bundle)
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(2, len(self.store.list_projects()))
        other = Store(self.root / "restore.sqlite3")
        self.assertEqual(a["id"], other.restore_project(bundle)["id"])
        self.assertEqual("原句", other.list_shots(ea["id"])[0]["dialogue"][0]["text"])
        broken = self.store.export_project(b["id"])
        broken["episodes"][0]["project_id"] = "wrong"
        with self.assertRaises(DomainError):
            other.restore_project(broken)
        self.assertEqual(1, len(other.list_projects()))

    def test_restore_rejects_invalid_duration_before_any_write(self):
        p = self.store.create_project("甲")
        ep = self.store.create_episode(p["id"], "一")
        self.store.save_shot(ep["id"], {"order": 1, "screen_duration_ms": 1000})
        bundle = self.store.export_project(p["id"])
        bundle["shots"][0]["screen_duration_ms"] = -1
        other = Store(self.root / "invalid.sqlite3")
        with self.assertRaises(DomainError):
            other.restore_project(bundle)
        self.assertEqual([], other.list_projects())

    def test_restore_rejects_non_utc_timestamp(self):
        p = self.store.create_project("甲")
        bundle = self.store.export_project(p["id"])
        bundle["project"]["created_at"] = "yesterday"
        other = Store(self.root / "invalid-time.sqlite3")
        with self.assertRaises(DomainError):
            other.restore_project(bundle)
        self.assertEqual([], other.list_projects())

    def test_export_reads_one_sqlite_snapshot_while_writer_commits(self):
        project = self.store.create_project("旧标题")
        episode = self.store.create_episode(project["id"], "一")
        self.store.update_episode(episode["id"], {"script": "旧稿"}, episode["revision"])
        with closing(sqlite3.connect(self.store.db_path)) as conn:
            self.assertEqual("wal", conn.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        read_started = threading.Event()
        writer_done = threading.Event()

        class PausingStore(Store):
            def _row(self, conn, table, identifier):
                row = super()._row(conn, table, identifier)
                if getattr(self, "pause_export", False) and table == "projects":
                    self.pause_export = False
                    read_started.set()
                    if not writer_done.wait(3):
                        raise AssertionError("writer did not commit while export was paused")
                return row

        def write_new_version():
            if not read_started.wait(3):
                writer_done.set()
                return
            try:
                with self.store.transaction() as conn:
                    conn.execute("UPDATE projects SET title='新标题' WHERE id=?", (project["id"],))
                    conn.execute("UPDATE episodes SET script='新稿' WHERE id=?", (episode["id"],))
            finally:
                writer_done.set()

        exporter = PausingStore(self.store.db_path)
        exporter.pause_export = True
        worker = threading.Thread(target=write_new_version)
        worker.start()
        try:
            bundle = exporter.export_project(project["id"])
        finally:
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual("新标题", self.store.get_project(project["id"])["title"])
        self.assertEqual("旧标题", bundle["project"]["title"])
        self.assertEqual("旧稿", bundle["episodes"][0]["script"])


if __name__ == "__main__":
    unittest.main()
