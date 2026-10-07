import hashlib
import tempfile
import unittest
from pathlib import Path

from workbench.domain import DomainError
from workbench.store import Store


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "staging").mkdir()
        self.store = Store(self.root / "db.sqlite3", self.root)
        self.project = self.store.create_project("甲")
        ep = self.store.create_episode(self.project["id"], "一")
        self.shot = self.store.save_shot(ep["id"], {"order": 1})

    def source(self, name="source.bin", body=b"video"):
        path = self.root / "staging" / name
        path.write_bytes(body)
        return path

    def test_import_deduplicates_binary_and_preserves_selection(self):
        first = self.store.import_asset(self.project["id"], self.source(), "video", {"license": "owned"})
        selected = self.store.select_candidate(self.shot["id"], first["id"], self.shot["revision"])
        second = self.store.import_asset(self.project["id"], self.source("copy.bin"), "video", {})
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["storage_key"], second["storage_key"])
        self.assertEqual(hashlib.sha256(b"video").hexdigest(), first["sha256"])
        self.assertEqual(1, len(list((self.root / "assets").iterdir())))
        self.assertEqual(first["id"], self.store.get_shot(self.shot["id"])["selected_candidate_id"])
        self.assertEqual(selected["revision"], self.store.get_shot(self.shot["id"])["revision"])

    def test_external_missing_symlink_and_foreign_asset_rejected(self):
        outside = self.root / "external.bin"
        outside.write_bytes(b"bad")
        for path in (outside, self.root / "staging" / "missing"):
            with self.assertRaises(DomainError):
                self.store.import_asset(self.project["id"], path, "video", {})
        link = self.root / "staging" / "link.bin"
        try:
            link.symlink_to(outside)
        except OSError:
            pass
        else:
            with self.assertRaises(DomainError):
                self.store.import_asset(self.project["id"], link, "video", {})
        other = self.store.create_project("乙")
        foreign = self.store.import_asset(other["id"], self.source(), "image", {})
        with self.assertRaises(DomainError):
            self.store.select_candidate(self.shot["id"], foreign["id"], self.shot["revision"])
        with self.assertRaises(DomainError):
            self.store.save_shot(self.shot["episode_id"], {"id": self.shot["id"], "asset_version_ids": [foreign["id"]]}, self.shot["revision"])
        self.assertEqual([], self.store.get_shot(self.shot["id"])["asset_version_ids"])

    def test_staging_directory_link_cannot_escape_data_root(self):
        external = self.root / "external"
        external.mkdir()
        (external / "clip.bin").write_bytes(b"outside")
        linked_root = self.root / "linked"
        linked_root.mkdir()
        try:
            (linked_root / "staging").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks unavailable")
        linked = Store(linked_root / "db.sqlite3", linked_root)
        project = linked.create_project("乙")
        with self.assertRaises(DomainError):
            linked.import_asset(project["id"], linked_root / "staging" / "clip.bin", "video", {})
        self.assertEqual([], linked.list_assets(project["id"]))

    def test_backup_declares_missing_binary_and_stale_selection_conflicts(self):
        asset = self.store.import_asset(self.project["id"], self.source(), "video", {})
        bundle = self.store.export_project(self.project["id"])
        self.assertEqual("external_binary_required", bundle["assets"][0]["binary_status"])
        other = Store(self.root / "restore.sqlite3", self.root)
        other.restore_project(bundle)
        self.assertFalse(other.get_asset(asset["id"])["binary_available"])
        self.store.select_candidate(self.shot["id"], asset["id"], self.shot["revision"])
        with self.assertRaises(DomainError) as caught:
            self.store.select_candidate(self.shot["id"], asset["id"], self.shot["revision"])
        self.assertEqual(409, caught.exception.status)

    def test_restore_rejects_nonvideo_selected_candidate(self):
        image = self.store.import_asset(self.project["id"], self.source(), "image", {})
        bundle = self.store.export_project(self.project["id"])
        bundle["shots"][0]["selected_candidate_id"] = image["id"]
        other = Store(self.root / "invalid.sqlite3", self.root)
        with self.assertRaises(DomainError):
            other.restore_project(bundle)
        self.assertEqual([], other.list_projects())


if __name__ == "__main__":
    unittest.main()
