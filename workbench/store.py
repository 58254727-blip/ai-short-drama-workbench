"""SQLite owned records, revisioned edits and scoped project snapshots."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .assets import KINDS, binary_available, import_binary
from .domain import DomainError, require


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return str(uuid4())


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object(value, name):
    require(isinstance(value, dict), "invalid_payload", 400, f"{name} 必须是对象")
    return value


def _title(value):
    require(isinstance(value, str) and bool(value.strip()), "invalid_title", 400, "标题不能为空")
    return value.strip()


def _int(value, name):
    require(type(value) is int and value >= 0, "invalid_payload", 400, f"{name} 必须为非负整数")
    return value


def _text(value, name):
    require(isinstance(value, str), "invalid_payload", 400, f"{name} 必须为文本")
    return value


def _dialogue(value):
    require(isinstance(value, list), "invalid_payload", 400, "dialogue 必须是列表")
    for item in value:
        require(isinstance(item, dict) and isinstance(item.get("speaker_id"), str) and isinstance(item.get("text"), str), "invalid_payload", 400, "对白须包含 speaker_id 和 text")
    return value


def _uuid(value):
    try:
        require(isinstance(value, str) and str(UUID(value)) == value, "invalid_bundle", 400, "备份 ID 不是 UUID")
    except ValueError:
        raise DomainError("invalid_bundle", 400, "备份 ID 不是 UUID") from None
    return value


def _timestamp(value):
    try:
        require(isinstance(value, str), "invalid_bundle", 400, "备份时间错误")
        parsed = datetime.fromisoformat(value)
        require(parsed.utcoffset() == timezone.utc.utcoffset(None), "invalid_bundle", 400, "备份时间必须为 UTC")
    except ValueError:
        raise DomainError("invalid_bundle", 400, "备份时间错误") from None
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL, script TEXT NOT NULL, creative_notes TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scenes (id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id), title TEXT NOT NULL, purpose TEXT NOT NULL, location TEXT NOT NULL, sequence INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL, rights TEXT NOT NULL, sha256 TEXT NOT NULL, storage_key TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shots (id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id), scene_id TEXT REFERENCES scenes(id), order_index INTEGER NOT NULL, revision INTEGER NOT NULL, story_job TEXT NOT NULL, start_state TEXT NOT NULL, action TEXT NOT NULL, end_state TEXT NOT NULL, transition TEXT NOT NULL, dialogue TEXT NOT NULL, screen_duration_ms INTEGER NOT NULL, generation_duration_ms INTEGER NOT NULL, asset_version_ids TEXT NOT NULL, selected_candidate_id TEXT REFERENCES assets(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS episodes_project_idx ON episodes(project_id);
CREATE INDEX IF NOT EXISTS scenes_episode_idx ON scenes(episode_id);
CREATE INDEX IF NOT EXISTS shots_episode_idx ON shots(episode_id);
CREATE INDEX IF NOT EXISTS assets_project_idx ON assets(project_id);
"""


class Store:
    def __init__(self, db_path: Path, data_root: Path | None = None):
        self.db_path = Path(db_path)
        self.data_root = Path(data_root) if data_root is not None else self.db_path.parent
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        """Short lived, per-call connection; callers own any transaction."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """Atomic write boundary suitable for a later persistent queue."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _row(self, conn, table, identifier):
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        require(row is not None, "not_found", 404, f"{table} 记录不存在")
        return row

    def _project_of_episode(self, conn, episode_id):
        return self._row(conn, "episodes", episode_id)["project_id"]

    def _shot(self, row):
        item = dict(row)
        item["order"] = item.pop("order_index")
        item["dialogue"] = json.loads(item["dialogue"])
        item["asset_version_ids"] = json.loads(item["asset_version_ids"])
        return item

    def _asset(self, row):
        item = dict(row)
        item["rights"] = json.loads(item["rights"])
        item["binary_available"] = binary_available(self.data_root, item["storage_key"])
        return item

    def create_project(self, title: str) -> dict:
        item = {"id": _id(), "title": _title(title), "created_at": _now()}
        with self.transaction() as conn:
            conn.execute("INSERT INTO projects VALUES (:id,:title,:created_at)", item)
        return item

    def list_projects(self) -> list:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY created_at,id")]

    def get_project(self, project_id: str) -> dict:
        with self.connection() as conn:
            return dict(self._row(conn, "projects", project_id))

    def create_episode(self, project_id: str, title: str) -> dict:
        stamp = _now()
        item = {"id": _id(), "project_id": project_id, "title": _title(title), "script": "", "creative_notes": "", "revision": 1, "created_at": stamp, "updated_at": stamp}
        with self.transaction() as conn:
            self._row(conn, "projects", project_id)
            conn.execute("INSERT INTO episodes VALUES (:id,:project_id,:title,:script,:creative_notes,:revision,:created_at,:updated_at)", item)
        return item

    def list_episodes(self, project_id: str) -> list:
        with self.connection() as conn:
            self._row(conn, "projects", project_id)
            return [dict(row) for row in conn.execute("SELECT * FROM episodes WHERE project_id=? ORDER BY created_at,id", (project_id,))]

    def get_episode(self, episode_id: str) -> dict:
        with self.connection() as conn:
            return dict(self._row(conn, "episodes", episode_id))

    def update_episode(self, episode_id: str, payload: dict, expected_revision: int) -> dict:
        payload = _object(payload, "episode")
        with self.transaction() as conn:
            item = dict(self._row(conn, "episodes", episode_id))
            require(item["revision"] == expected_revision, "revision_conflict", 409, "分集版本已变化")
            require(set(payload) <= {"title", "script", "creative_notes"}, "invalid_payload", 400, "分集字段不受支持")
            for key, value in payload.items():
                item[key] = _title(value) if key == "title" else _text(value, key)
            item["revision"] += 1
            item["updated_at"] = _now()
            conn.execute("UPDATE episodes SET title=:title,script=:script,creative_notes=:creative_notes,revision=:revision,updated_at=:updated_at WHERE id=:id", item)
        return item

    def create_scene(self, episode_id: str, payload: dict) -> dict:
        payload = _object(payload, "scene")
        require(set(payload) <= {"title", "purpose", "location", "sequence"}, "invalid_payload", 400, "场景字段不受支持")
        item = {"id": _id(), "episode_id": episode_id, "title": _title(payload.get("title", "未命名场景")), "purpose": _text(payload.get("purpose", ""), "purpose"), "location": _text(payload.get("location", ""), "location"), "sequence": _int(payload.get("sequence", 0), "sequence"), "created_at": _now()}
        with self.transaction() as conn:
            self._row(conn, "episodes", episode_id)
            conn.execute("INSERT INTO scenes VALUES (:id,:episode_id,:title,:purpose,:location,:sequence,:created_at)", item)
        return item

    def list_scenes(self, episode_id: str) -> list:
        with self.connection() as conn:
            self._row(conn, "episodes", episode_id)
            return [dict(row) for row in conn.execute("SELECT * FROM scenes WHERE episode_id=? ORDER BY sequence,id", (episode_id,))]

    def update_scene(self, episode_id: str, scene_id: str, payload: dict, expected_episode_revision: int) -> dict:
        payload = _object(payload, "scene")
        require(set(payload) <= {"title", "purpose", "location"}, "invalid_payload", 400, "场景字段不受支持")
        with self.transaction() as conn:
            episode = dict(self._row(conn, "episodes", episode_id))
            scene = dict(self._row(conn, "scenes", scene_id))
            require(scene["episode_id"] == episode_id, "ownership_conflict", 409, "场景不属于当前分集")
            require(episode["revision"] == expected_episode_revision, "revision_conflict", 409, "分集版本已变化")
            for key, value in payload.items():
                scene[key] = _title(value) if key == "title" else _text(value, key)
            conn.execute("UPDATE scenes SET title=:title,purpose=:purpose,location=:location WHERE id=:id", scene)
            conn.execute("UPDATE episodes SET revision=revision+1,updated_at=? WHERE id=?", (_now(), episode_id))
        return {**scene, "episode_revision": episode["revision"] + 1}

    def _validate_shot_links(self, conn, item):
        project_id = self._project_of_episode(conn, item["episode_id"])
        if item["scene_id"] is not None:
            require(self._row(conn, "scenes", item["scene_id"])["episode_id"] == item["episode_id"], "ownership_conflict", 409, "场景不属于当前分集")
        for asset_id in item["asset_version_ids"]:
            require(self._row(conn, "assets", asset_id)["project_id"] == project_id, "ownership_conflict", 409, "素材不属于当前作品")
        if item["selected_candidate_id"] is not None:
            asset = self._row(conn, "assets", item["selected_candidate_id"])
            require(asset["project_id"] == project_id and asset["kind"] == "video", "ownership_conflict", 409, "候选视频不属于当前作品")

    def save_shot(self, episode_id: str, payload: dict, expected_revision: int | None = None) -> dict:
        payload = _object(payload, "shot")
        mutable = {"scene_id", "order", "story_job", "start_state", "action", "end_state", "transition", "dialogue", "screen_duration_ms", "generation_duration_ms", "asset_version_ids"}
        require(set(payload) <= mutable | {"id", "episode_id", "revision", "selected_candidate_id", "created_at", "updated_at"}, "invalid_payload", 400, "镜头字段不受支持")
        with self.transaction() as conn:
            self._row(conn, "episodes", episode_id)
            old = None
            if payload.get("id"):
                old = self._shot(self._row(conn, "shots", payload["id"]))
                require(old["episode_id"] == episode_id, "ownership_conflict", 409, "镜头不属于当前分集")
                require(expected_revision == old["revision"], "revision_conflict", 409, "镜头版本已变化")
                item = old.copy()
            else:
                require(expected_revision is None, "revision_conflict", 409, "新镜头不能指定旧版本")
                stamp = _now()
                item = {"id": _id(), "episode_id": episode_id, "scene_id": None, "order": 0, "revision": 1, "story_job": "", "start_state": "", "action": "", "end_state": "", "transition": "", "dialogue": [], "screen_duration_ms": 0, "generation_duration_ms": 0, "asset_version_ids": [], "selected_candidate_id": None, "created_at": stamp, "updated_at": stamp}
            require(payload.get("episode_id", episode_id) == episode_id, "ownership_conflict", 409, "镜头分集不可更改")
            require(payload.get("selected_candidate_id", item["selected_candidate_id"]) == item["selected_candidate_id"], "invalid_payload", 400, "请使用候选选择接口")
            for key in mutable & payload.keys():
                value = payload[key]
                if key in {"order", "screen_duration_ms", "generation_duration_ms"}:
                    value = _int(value, key)
                elif key == "dialogue":
                    value = _dialogue(value)
                elif key == "asset_version_ids":
                    require(isinstance(value, list) and len(value) == len(set(value)) and all(isinstance(v, str) for v in value), "invalid_payload", 400, "asset_version_ids 格式错误")
                elif key == "scene_id":
                    require(value is None or isinstance(value, str), "invalid_payload", 400, "scene_id 格式错误")
                else:
                    value = _text(value, key)
                item[key] = value
            self._validate_shot_links(conn, item)
            if old:
                item["revision"] += 1
                item["updated_at"] = _now()
            record = {**item, "order_index": item["order"], "dialogue": _json(item["dialogue"]), "asset_version_ids": _json(item["asset_version_ids"])}
            if old:
                conn.execute("""UPDATE shots SET scene_id=:scene_id,order_index=:order_index,revision=:revision,story_job=:story_job,start_state=:start_state,action=:action,end_state=:end_state,transition=:transition,dialogue=:dialogue,screen_duration_ms=:screen_duration_ms,generation_duration_ms=:generation_duration_ms,asset_version_ids=:asset_version_ids,updated_at=:updated_at WHERE id=:id""", record)
            else:
                conn.execute("""INSERT INTO shots VALUES (:id,:episode_id,:scene_id,:order_index,:revision,:story_job,:start_state,:action,:end_state,:transition,:dialogue,:screen_duration_ms,:generation_duration_ms,:asset_version_ids,:selected_candidate_id,:created_at,:updated_at)""", record)
        return item

    def get_shot(self, shot_id: str) -> dict:
        with self.connection() as conn:
            return self._shot(self._row(conn, "shots", shot_id))

    def list_shots(self, episode_id: str) -> list:
        with self.connection() as conn:
            self._row(conn, "episodes", episode_id)
            return [self._shot(row) for row in conn.execute("SELECT * FROM shots WHERE episode_id=? ORDER BY order_index,id", (episode_id,))]

    def import_asset(self, project_id: str, path: Path, kind: str, rights: dict) -> dict:
        require(kind in KINDS, "invalid_kind", 400, "素材类型不支持")
        rights = _object(rights, "rights")
        try:
            encoded_rights = _json(rights)
        except (TypeError, ValueError):
            raise DomainError("invalid_rights", 400, "素材权利记录必须可序列化为 JSON") from None
        with self.transaction() as conn:
            self._row(conn, "projects", project_id)
            sha, storage_key, size = import_binary(self.data_root, Path(path))
            item = {"id": _id(), "project_id": project_id, "kind": kind, "rights": rights.copy(), "sha256": sha, "storage_key": storage_key, "size_bytes": size, "created_at": _now(), "binary_available": True}
            record = {**item, "rights": encoded_rights}
            conn.execute("INSERT INTO assets VALUES (:id,:project_id,:kind,:rights,:sha256,:storage_key,:size_bytes,:created_at)", record)
        return item

    def get_asset(self, asset_id: str) -> dict:
        with self.connection() as conn:
            return self._asset(self._row(conn, "assets", asset_id))

    def list_assets(self, project_id: str) -> list:
        with self.connection() as conn:
            self._row(conn, "projects", project_id)
            return [self._asset(row) for row in conn.execute("SELECT * FROM assets WHERE project_id=? ORDER BY created_at,id", (project_id,))]

    def select_candidate(self, shot_id: str, asset_id: str, expected_revision: int) -> dict:
        with self.transaction() as conn:
            item = self._shot(self._row(conn, "shots", shot_id))
            require(item["revision"] == expected_revision, "revision_conflict", 409, "镜头版本已变化")
            asset = self._row(conn, "assets", asset_id)
            require(asset["project_id"] == self._project_of_episode(conn, item["episode_id"]) and asset["kind"] == "video", "ownership_conflict", 409, "候选视频不属于当前作品")
            item["selected_candidate_id"] = asset_id
            item["revision"] += 1
            item["updated_at"] = _now()
            conn.execute("UPDATE shots SET selected_candidate_id=?,revision=?,updated_at=? WHERE id=?", (asset_id, item["revision"], item["updated_at"], shot_id))
        return item

    def export_project(self, project_id: str) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                project = dict(self._row(conn, "projects", project_id))
                episodes = [dict(r) for r in conn.execute("SELECT * FROM episodes WHERE project_id=? ORDER BY id", (project_id,))]
                scenes = [dict(r) for r in conn.execute("SELECT scenes.* FROM scenes JOIN episodes ON episodes.id=scenes.episode_id WHERE episodes.project_id=? ORDER BY scenes.id", (project_id,))]
                shots = [self._shot(r) for r in conn.execute("SELECT shots.* FROM shots JOIN episodes ON episodes.id=shots.episode_id WHERE episodes.project_id=? ORDER BY shots.id", (project_id,))]
                assets = []
                for row in conn.execute("SELECT * FROM assets WHERE project_id=? ORDER BY id", (project_id,)):
                    asset = self._asset(row)
                    asset.pop("binary_available")
                    asset.pop("storage_key")
                    asset["binary_status"] = "external_binary_required"
                    assets.append(asset)
                return {"format_version": 1, "project": project, "episodes": episodes, "scenes": scenes, "shots": shots, "assets": assets}
            finally:
                conn.rollback()

    def restore_project(self, bundle: dict) -> dict:
        bundle = _object(bundle, "bundle")
        require(bundle.get("format_version") == 1, "invalid_bundle", 400, "备份格式版本不支持")
        try:
            project = bundle["project"]
            episodes, scenes, shots, assets = (bundle[key] for key in ("episodes", "scenes", "shots", "assets"))
            require(isinstance(project, dict) and all(isinstance(value, list) for value in (episodes, scenes, shots, assets)), "invalid_bundle", 400, "备份结构错误")
            pid = _uuid(project["id"])
            _title(project["title"])
            _timestamp(project["created_at"])
            ep_ids = {_uuid(e["id"]) for e in episodes}
            scene_owner = {_uuid(s["id"]): s["episode_id"] for s in scenes}
            asset_ids = {_uuid(a["id"]) for a in assets}
            require(len(ep_ids) == len(episodes) and len(scene_owner) == len(scenes) and len(asset_ids) == len(assets), "invalid_bundle", 400, "备份存在重复 ID")
            require(all(e["project_id"] == pid for e in episodes), "invalid_bundle", 400, "分集归属错误")
            require(all(s["episode_id"] in ep_ids for s in scenes), "invalid_bundle", 400, "场景归属错误")
            require(all(a["project_id"] == pid and a["kind"] in KINDS and a["binary_status"] == "external_binary_required" for a in assets), "invalid_bundle", 400, "素材归属或二进制状态错误")
            for episode in episodes:
                _title(episode["title"])
                _text(episode["script"], "script")
                _text(episode["creative_notes"], "creative_notes")
                require(type(episode["revision"]) is int and episode["revision"] >= 1, "invalid_bundle", 400, "分集版本错误")
                _timestamp(episode["created_at"])
                _timestamp(episode["updated_at"])
            for scene in scenes:
                _title(scene["title"])
                _text(scene["purpose"], "purpose")
                _text(scene["location"], "location")
                _int(scene["sequence"], "sequence")
                _timestamp(scene["created_at"])
            asset_kind = {}
            for asset in assets:
                _object(asset["rights"], "rights")
                require(isinstance(asset["sha256"], str) and len(asset["sha256"]) == 64 and all(c in "0123456789abcdef" for c in asset["sha256"]), "invalid_bundle", 400, "素材哈希错误")
                _int(asset["size_bytes"], "size_bytes")
                _timestamp(asset["created_at"])
                asset_kind[asset["id"]] = asset["kind"]
            for shot in shots:
                _uuid(shot["id"])
                require(shot["episode_id"] in ep_ids and (shot["scene_id"] is None or scene_owner.get(shot["scene_id"]) == shot["episode_id"]), "invalid_bundle", 400, "镜头归属错误")
                require(isinstance(shot["asset_version_ids"], list) and all(isinstance(v, str) for v in shot["asset_version_ids"]), "invalid_bundle", 400, "镜头素材列表错误")
                require(set(shot["asset_version_ids"]) <= asset_ids and (shot["selected_candidate_id"] is None or asset_kind.get(shot["selected_candidate_id"]) == "video"), "invalid_bundle", 400, "镜头素材归属错误")
                _int(shot["order"], "order")
                _int(shot["screen_duration_ms"], "screen_duration_ms")
                _int(shot["generation_duration_ms"], "generation_duration_ms")
                require(type(shot["revision"]) is int and shot["revision"] >= 1, "invalid_bundle", 400, "镜头版本错误")
                for key in ("story_job", "start_state", "action", "end_state", "transition"):
                    _text(shot[key], key)
                _timestamp(shot["created_at"])
                _timestamp(shot["updated_at"])
                _dialogue(shot["dialogue"])
            require(len({s["id"] for s in shots}) == len(shots), "invalid_bundle", 400, "镜头 ID 重复")
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError("invalid_bundle", 400, "备份结构错误") from error
        with self.transaction() as conn:
            for table, ids in (("projects", [pid]), ("episodes", ep_ids), ("scenes", scene_owner), ("shots", [s["id"] for s in shots]), ("assets", asset_ids)):
                for identifier in ids:
                    require(conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (identifier,)).fetchone() is None, "restore_conflict", 409, "恢复 ID 已存在")
            conn.execute("INSERT INTO projects VALUES (:id,:title,:created_at)", project)
            for episode in episodes:
                conn.execute("INSERT INTO episodes VALUES (:id,:project_id,:title,:script,:creative_notes,:revision,:created_at,:updated_at)", episode)
            for scene in scenes:
                conn.execute("INSERT INTO scenes VALUES (:id,:episode_id,:title,:purpose,:location,:sequence,:created_at)", scene)
            for asset in assets:
                record = {**asset, "rights": _json(asset["rights"]), "storage_key": ""}
                conn.execute("INSERT INTO assets VALUES (:id,:project_id,:kind,:rights,:sha256,:storage_key,:size_bytes,:created_at)", record)
            for shot in shots:
                record = {**shot, "order_index": shot["order"], "dialogue": _json(shot["dialogue"]), "asset_version_ids": _json(shot["asset_version_ids"])}
                conn.execute("INSERT INTO shots VALUES (:id,:episode_id,:scene_id,:order_index,:revision,:story_job,:start_state,:action,:end_state,:transition,:dialogue,:screen_duration_ms,:generation_duration_ms,:asset_version_ids,:selected_candidate_id,:created_at,:updated_at)", record)
        return project.copy()
