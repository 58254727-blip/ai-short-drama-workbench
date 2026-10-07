"""Known persistent postproduction records included in portable archives."""

import sqlite3

from .domain import DomainError, require
from .review import ManualReviewService
from .subtitles import SubtitleService
from .timeline import TimelineService

EXT_TABLES = ("episode_timelines", "timeline_items", "subtitle_sets", "subtitle_cues", "manual_qc")
EXT_COLUMNS = {
    "episode_timelines": ("episode_id", "version"),
    "timeline_items": ("episode_id", "ordinal", "shot_id", "source_asset_id", "in_ms", "out_ms", "speech_start_ms", "speech_end_ms"),
    "subtitle_sets": ("episode_id", "timeline_version"),
    "subtitle_cues": ("id", "episode_id", "ordinal", "start_ms", "end_ms", "text", "speaker_id", "source_asset_id"),
    "manual_qc": ("episode_id", "shot_id", "asset_id", "shot_revision", "timeline_version", "verdict", "note", "reviewed_at"),
}


def init_extensions(store):
    TimelineService(store)
    SubtitleService(store)
    ManualReviewService(store)


def collect_extensions(store, project_id):
    init_extensions(store)
    with store.connection() as conn:
        episode_ids = [row["id"] for row in conn.execute("SELECT id FROM episodes WHERE project_id=?", (project_id,))]
        result = {}
        for table in EXT_TABLES:
            rows = []
            for episode_id in episode_ids:
                rows.extend(dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE episode_id=?", (episode_id,)))
            result[table] = rows
        return result


def validate_extensions(trial_store, extension, project_id):
    require(isinstance(extension, dict) and set(extension) == set(EXT_TABLES), "invalid_archive", 400, "后期记录结构无效")
    init_extensions(trial_store)
    with trial_store.transaction() as conn:
        for table in EXT_TABLES:
            rows = extension[table]
            require(isinstance(rows, list), "invalid_archive", 400, "后期记录必须为列表")
            columns = EXT_COLUMNS[table]
            for row in rows:
                require(isinstance(row, dict) and set(row) == set(columns), "invalid_archive", 400, "后期记录字段无效")
                episode = conn.execute("SELECT project_id FROM episodes WHERE id=?", (row["episode_id"],)).fetchone()
                require(episode and episode["project_id"] == project_id, "invalid_archive", 400, "后期记录跨作品")
                if table == "episode_timelines":
                    require(type(row["version"]) is int and row["version"] >= 1, "invalid_archive", 400, "时间轴版本无效")
                if table == "subtitle_sets":
                    require(type(row["timeline_version"]) is int and row["timeline_version"] >= 1 and conn.execute("SELECT 1 FROM episode_timelines WHERE episode_id=?", (row["episode_id"],)).fetchone(), "invalid_archive", 400, "字幕版本无效")
                if table == "timeline_items":
                    shot = conn.execute("SELECT episode_id,selected_candidate_id FROM shots WHERE id=?", (row["shot_id"],)).fetchone()
                    asset = conn.execute("SELECT project_id,kind FROM assets WHERE id=?", (row["source_asset_id"],)).fetchone()
                    require(shot and shot["episode_id"] == row["episode_id"] and asset and asset["project_id"] == project_id and asset["kind"] == "video", "invalid_archive", 400, "时间轴来源无效")
                    require(type(row["ordinal"]) is int and row["ordinal"] >= 0 and type(row["in_ms"]) is int and type(row["out_ms"]) is int and 0 <= row["in_ms"] < row["out_ms"], "invalid_archive", 400, "时间轴切点无效")
                    speech = (row["speech_start_ms"], row["speech_end_ms"])
                    require(speech == (None, None) or all(type(value) is int for value in speech) and 0 <= speech[0] < speech[1], "invalid_archive", 400, "对白范围无效")
                if table == "subtitle_cues":
                    asset = conn.execute("SELECT project_id FROM assets WHERE id=?", (row["source_asset_id"],)).fetchone()
                    require(asset and asset["project_id"] == project_id and type(row["ordinal"]) is int and row["ordinal"] >= 0 and type(row["start_ms"]) is int and type(row["end_ms"]) is int and 0 <= row["start_ms"] < row["end_ms"] and isinstance(row["text"], str) and isinstance(row["speaker_id"], str), "invalid_archive", 400, "字幕来源或时间无效")
                    saved = conn.execute("SELECT timeline_version FROM subtitle_sets WHERE episode_id=?", (row["episode_id"],)).fetchone()
                    current = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (row["episode_id"],)).fetchone()
                    require(saved is not None, "invalid_archive", 400, "字幕集元数据缺失")
                    if current and saved["timeline_version"] == current["version"]:
                        offset = 0
                        covered = False
                        for segment in conn.execute("SELECT source_asset_id,in_ms,out_ms FROM timeline_items WHERE episode_id=? ORDER BY ordinal", (row["episode_id"],)):
                            end = offset + segment["out_ms"] - segment["in_ms"]
                            covered = covered or offset <= row["start_ms"] < row["end_ms"] <= end and segment["source_asset_id"] == row["source_asset_id"]
                            offset = end
                        require(covered, "invalid_archive", 400, "当前字幕不属于覆盖的时间轴片段")
                if table == "manual_qc":
                    shot = conn.execute("SELECT episode_id,selected_candidate_id FROM shots WHERE id=?", (row["shot_id"],)).fetchone()
                    asset = conn.execute("SELECT project_id,kind FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
                    require(shot and shot["episode_id"] == row["episode_id"] and asset and asset["project_id"] == project_id and asset["kind"] == "video" and type(row["shot_revision"]) is int and row["shot_revision"] >= 1 and type(row["timeline_version"]) is int and row["timeline_version"] >= 0 and row["verdict"] in ("pass", "revise", "reject") and isinstance(row["note"], str) and isinstance(row["reviewed_at"], str), "invalid_archive", 400, "人工校核来源无效")
                try:
                    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [row[column] for column in columns])
                except (sqlite3.IntegrityError, sqlite3.ProgrammingError, ValueError, TypeError):
                    raise DomainError("invalid_archive", 400, "后期记录关系或数据无效") from None
    with trial_store.connection() as conn:
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in EXT_TABLES}
