"""Human editable subtitle cues aligned to persisted selected source cuts."""

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .domain import DomainError, require
from .assets import _matches_hash
from .timeline import _asset_path, _init as _timeline_init, _items


def _now():
    return datetime.now(timezone.utc).isoformat()


def _init(conn):
    _timeline_init(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS subtitle_sets (episode_id TEXT PRIMARY KEY REFERENCES episodes(id), timeline_version INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS subtitle_cues (id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id), ordinal INTEGER NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL, speaker_id TEXT NOT NULL, source_asset_id TEXT NOT NULL REFERENCES assets(id))")


def _cues(conn, episode_id):
    return [dict(row) for row in conn.execute("SELECT id,start_ms,end_ms,text,speaker_id,source_asset_id FROM subtitle_cues WHERE episode_id=? ORDER BY ordinal", (episode_id,))]


def _segments(items):
    offset = 0
    for item in items:
        end = offset + item["out_ms"] - item["in_ms"]
        yield offset, end, item["source_asset_id"]
        offset = end


class SubtitleService:
    def __init__(self, store):
        self.store = store
        with store.connection() as conn:
            _init(conn)
            conn.commit()

    def get_cues(self, episode_id: str) -> dict:
        with self.store.connection() as conn:
            episode = self.store._row(conn, "episodes", episode_id)
            saved = conn.execute("SELECT timeline_version FROM subtitle_sets WHERE episode_id=?", (episode_id,)).fetchone()
            timeline = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
            items = _items(conn, episode_id)
            selected = all(conn.execute("SELECT 1 FROM shots WHERE id=? AND episode_id=? AND selected_candidate_id=?", (item["shot_id"], episode_id, item["source_asset_id"])).fetchone() for item in items)
            status = "needs_entry" if saved is None else "ready" if timeline and saved["timeline_version"] == timeline["version"] and selected else "needs_realign"
            return {"episode_id": episode_id, "revision": episode["revision"], "status": status, "human_reviewed": False, "cues": _cues(conn, episode_id)}

    def save_cues(self, episode_id: str, cues: list[dict], revision: int) -> dict:
        require(isinstance(cues, list), "invalid_cues", 400, "字幕必须是列表")
        with self.store.transaction() as conn:
            _init(conn)
            episode = self.store._row(conn, "episodes", episode_id)
            require(episode["revision"] == revision, "revision_conflict", 409, "分集版本已变化")
            timeline = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
            items = _items(conn, episode_id)
            require(timeline is not None and bool(items), "timeline_missing", 400, "字幕需要实际已选时间轴")
            for item in items:
                require(conn.execute("SELECT 1 FROM shots WHERE id=? AND episode_id=? AND selected_candidate_id=?", (item["shot_id"], episode_id, item["source_asset_id"])).fetchone(), "timeline_stale", 409, "选片已变化，请重新对齐时间轴")
                asset = self.store._row(conn, "assets", item["source_asset_id"])
                require(_matches_hash(_asset_path(self.store.data_root, asset), asset["sha256"]), "asset_hash_mismatch", 409, "素材文件哈希变化")
            speakers = {line["speaker_id"] for shot in conn.execute("SELECT shots.dialogue FROM shots JOIN timeline_items ON shots.id=timeline_items.shot_id WHERE timeline_items.episode_id=?", (episode_id,)) for line in json.loads(shot["dialogue"])}
            segments = list(_segments(items))
            previous_end = -1
            existing_ids = {row["id"] for row in conn.execute("SELECT id FROM subtitle_cues WHERE episode_id=?", (episode_id,))}
            checked = []
            for index, cue in enumerate(cues):
                require(isinstance(cue, dict) and all(k in cue for k in ("start_ms", "end_ms", "text", "speaker_id", "source_asset_id")), "invalid_cue", 400, "字幕缺少必要字段")
                start, end = cue["start_ms"], cue["end_ms"]
                require(type(start) is int and type(end) is int and 0 <= start < end, "invalid_cue", 400, "字幕起止时间无效")
                require(start >= previous_end, "cue_overlap", 400, "字幕重叠或顺序错误")
                previous_end = end
                require(end <= segments[-1][1], "cue_out_of_range", 400, "字幕超出整集实际片段时长")
                require(isinstance(cue["text"], str) and bool(cue["text"].strip()), "invalid_cue", 400, "字幕文本不能为空")
                require(cue["speaker_id"] in speakers, "unknown_speaker", 400, "字幕说话人未见于已选镜头对白")
                require(any(left <= start and end <= right and cue["source_asset_id"] == asset for left, right, asset in segments), "cue_source_mismatch", 409, "字幕来源与覆盖片段不匹配")
                cue_id = cue.get("id") or str(uuid4())
                require(cue.get("id") is None or cue_id in existing_ids, "invalid_cue_id", 400, "字幕 ID 不属于当前分集")
                require(cue_id not in {entry[0] for entry in checked}, "invalid_cue_id", 400, "字幕 ID 重复")
                checked.append((cue_id, episode_id, index, start, end, cue["text"], cue["speaker_id"], cue["source_asset_id"]))
            conn.execute("DELETE FROM subtitle_cues WHERE episode_id=?", (episode_id,))
            for row in checked:
                conn.execute("INSERT INTO subtitle_cues VALUES (?,?,?,?,?,?,?,?)", row)
            conn.execute("INSERT INTO subtitle_sets VALUES (?,?) ON CONFLICT(episode_id) DO UPDATE SET timeline_version=excluded.timeline_version", (episode_id, timeline["version"]))
            conn.execute("UPDATE episodes SET revision=revision+1,updated_at=? WHERE id=?", (_now(), episode_id))
        return self.get_cues(episode_id)


def _stamp(milliseconds):
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, milli = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milli:03d}"


def write_srt(cues: list[dict], path: Path) -> None:
    lines = []
    for index, cue in enumerate(cues, 1):
        start, end = cue["start_ms"], cue["end_ms"]
        require(type(start) is int and type(end) is int and 0 <= start < end, "invalid_cue", 400, "字幕时间无效")
        require(isinstance(cue["text"], str), "invalid_cue", 400, "字幕文本无效")
        lines.append(f"{index}\n{_stamp(start)} --> {_stamp(end)}\n{html.escape(cue['text'], quote=False)}\n")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
