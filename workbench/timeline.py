"""Durable selected source cuts in exported-episode order."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .domain import DomainError, require
from .media import _path, _speech_guard, probe


def _now():
    return datetime.now(timezone.utc).isoformat()


def _init(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS episode_timelines (episode_id TEXT PRIMARY KEY REFERENCES episodes(id), version INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS timeline_items (episode_id TEXT NOT NULL REFERENCES episodes(id), ordinal INTEGER NOT NULL, shot_id TEXT NOT NULL REFERENCES shots(id), source_asset_id TEXT NOT NULL REFERENCES assets(id), in_ms INTEGER NOT NULL, out_ms INTEGER NOT NULL, speech_start_ms INTEGER, speech_end_ms INTEGER, PRIMARY KEY (episode_id,ordinal))")


def _asset_path(root, row):
    require(row["storage_key"] == row["sha256"], "asset_hash_mismatch", 409, "素材哈希记录不一致")
    return _path(Path(root) / "assets" / row["storage_key"], root)


def _validated_item(store, conn, episode, item):
    require(isinstance(item, dict), "invalid_timeline", 400, "时间轴项必须是对象")
    try:
        shot_id, asset_id, start, end = (item[k] for k in ("shot_id", "source_asset_id", "in_ms", "out_ms"))
    except KeyError as error:
        raise DomainError("invalid_timeline", 400, f"时间轴缺少 {error}") from error
    require(type(start) is int and type(end) is int and 0 <= start < end, "invalid_cut", 400, "时间轴切点无效")
    shot = store._row(conn, "shots", shot_id)
    asset = store._row(conn, "assets", asset_id)
    require(shot["episode_id"] == episode["id"] and shot["selected_candidate_id"] == asset_id and asset["project_id"] == episode["project_id"] and asset["kind"] == "video", "ownership_conflict", 409, "镜头与已选素材不匹配")
    info = probe(_asset_path(store.data_root, asset), data_root=store.data_root)
    require(info["sha256"] == asset["sha256"], "asset_hash_mismatch", 409, "素材二进制哈希变化")
    require(info["has_audio"], "audio_missing", 422, "选片缺少音轨")
    require(end <= info["duration_ms"], "invalid_cut", 400, "切点超出实际源时长")
    speech_start = item.get("speech_start_ms")
    speech_end = item.get("speech_end_ms")
    require((speech_start is None) == (speech_end is None), "invalid_speech_range", 400, "对白范围必须同时提供起止")
    if speech_start is not None:
        _speech_guard(start, end, [(speech_start, speech_end)])
        require(speech_end <= info["duration_ms"], "invalid_speech_range", 400, "对白范围超出素材")
    return {"shot_id": shot_id, "source_asset_id": asset_id, "in_ms": start, "out_ms": end, "speech_start_ms": speech_start, "speech_end_ms": speech_end}


def _items(conn, episode_id):
    return [dict(row) for row in conn.execute("SELECT shot_id,source_asset_id,in_ms,out_ms,speech_start_ms,speech_end_ms FROM timeline_items WHERE episode_id=? ORDER BY ordinal", (episode_id,))]


class TimelineService:
    def __init__(self, store):
        self.store = store
        with store.connection() as conn:
            _init(conn)
            conn.commit()

    def get_timeline(self, episode_id: str) -> dict:
        with self.store.connection() as conn:
            episode = self.store._row(conn, "episodes", episode_id)
            meta = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
            items = _items(conn, episode_id)
            valid = all(conn.execute("SELECT 1 FROM shots WHERE id=? AND episode_id=? AND selected_candidate_id=?", (item["shot_id"], episode_id, item["source_asset_id"])).fetchone() for item in items)
            return {"episode_id": episode_id, "revision": episode["revision"], "version": meta["version"] if meta else 0, "items": items, "status": "ready" if items and valid else "needs_selection"}

    def save_timeline(self, episode_id: str, items: list[dict], revision: int) -> dict:
        require(isinstance(items, list) and bool(items), "timeline_missing", 400, "请先添加实际已选片段")
        with self.store.transaction() as conn:
            _init(conn)
            episode = self.store._row(conn, "episodes", episode_id)
            require(episode["revision"] == revision, "revision_conflict", 409, "分集版本已变化")
            checked = [_validated_item(self.store, conn, episode, item) for item in items]
            meta = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
            version = (meta["version"] if meta else 0) + 1
            conn.execute("INSERT INTO episode_timelines (episode_id,version) VALUES (?,?) ON CONFLICT(episode_id) DO UPDATE SET version=excluded.version", (episode_id, version))
            conn.execute("DELETE FROM timeline_items WHERE episode_id=?", (episode_id,))
            for index, item in enumerate(checked):
                conn.execute("INSERT INTO timeline_items VALUES (?,?,?,?,?,?,?,?)", (episode_id, index, item["shot_id"], item["source_asset_id"], item["in_ms"], item["out_ms"], item["speech_start_ms"], item["speech_end_ms"]))
            conn.execute("UPDATE episodes SET revision=revision+1,updated_at=? WHERE id=?", (_now(), episode_id))
        return self.get_timeline(episode_id)
