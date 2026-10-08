"""Known persistent postproduction records included in portable archives."""

import json
import sqlite3

from .domain import DomainError, require
from .review import ManualReviewService
from .subtitles import SubtitleService, _valid_srt_text
from .timeline import TimelineService, _validated_item
from .media import decode_check

EXT_TABLES = ("episode_timelines", "timeline_items", "subtitle_sets", "subtitle_cues", "manual_qc")
EXT_COLUMNS = {
    "episode_timelines": ("episode_id", "version"),
    "timeline_items": ("episode_id", "ordinal", "shot_id", "source_asset_id", "in_ms", "out_ms", "speech_start_ms", "speech_end_ms"),
    "subtitle_sets": ("episode_id", "timeline_version", "shot_snapshot"),
    "subtitle_cues": ("id", "episode_id", "ordinal", "start_ms", "end_ms", "text", "speaker_id", "source_asset_id"),
    "manual_qc": ("episode_id", "shot_id", "asset_id", "shot_revision", "timeline_version", "verdict", "note", "reviewed_at"),
}


def init_extensions(store):
    TimelineService(store)
    SubtitleService(store)
    ManualReviewService(store)


def collect_extensions(store, project_id, conn=None):
    if conn is None:
        init_extensions(store)
        with store.connection() as owned:
            owned.execute("BEGIN")
            try:
                return collect_extensions(store, project_id, owned)
            finally:
                owned.rollback()
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
                if table == "subtitle_sets" and isinstance(row, dict) and set(row) == {"episode_id", "timeline_version"}:
                    row = {**row, "shot_snapshot": None}
                require(isinstance(row, dict) and set(row) == set(columns), "invalid_archive", 400, "后期记录字段无效")
                episode = conn.execute("SELECT project_id FROM episodes WHERE id=?", (row["episode_id"],)).fetchone()
                require(episode and episode["project_id"] == project_id, "invalid_archive", 400, "后期记录跨作品")
                if table == "episode_timelines":
                    require(type(row["version"]) is int and row["version"] >= 1, "invalid_archive", 400, "时间轴版本无效")
                if table == "subtitle_sets":
                    require(type(row["timeline_version"]) is int and row["timeline_version"] >= 1 and
                            (row["shot_snapshot"] is None or isinstance(row["shot_snapshot"], str)) and
                            conn.execute("SELECT 1 FROM episode_timelines WHERE episode_id=?", (row["episode_id"],)).fetchone(), "invalid_archive", 400, "字幕版本无效")
                    if row["shot_snapshot"] is not None:
                        try: recorded_shots = json.loads(row["shot_snapshot"])
                        except ValueError: raise DomainError("invalid_archive", 400, "字幕镜头版本记录无效") from None
                        require(isinstance(recorded_shots, dict) and all(isinstance(k, str) and type(v) is int and v >= 1 for k, v in recorded_shots.items()),
                                "invalid_archive", 400, "字幕镜头版本记录无效")
                if table == "timeline_items":
                    shot = conn.execute("SELECT episode_id,selected_candidate_id FROM shots WHERE id=?", (row["shot_id"],)).fetchone()
                    asset = conn.execute("SELECT project_id,kind FROM assets WHERE id=?", (row["source_asset_id"],)).fetchone()
                    require(shot and shot["episode_id"] == row["episode_id"] and asset and asset["project_id"] == project_id and asset["kind"] == "video", "invalid_archive", 400, "时间轴来源无效")
                    require(type(row["ordinal"]) is int and row["ordinal"] >= 0 and type(row["in_ms"]) is int and type(row["out_ms"]) is int and 0 <= row["in_ms"] < row["out_ms"], "invalid_archive", 400, "时间轴切点无效")
                    speech = (row["speech_start_ms"], row["speech_end_ms"])
                    require(speech == (None, None) or all(type(value) is int for value in speech) and 0 <= speech[0] < speech[1], "invalid_archive", 400, "对白范围无效")
                if table == "subtitle_cues":
                    asset = conn.execute("SELECT project_id FROM assets WHERE id=?", (row["source_asset_id"],)).fetchone()
                    require(asset and asset["project_id"] == project_id and type(row["ordinal"]) is int and row["ordinal"] >= 0 and type(row["start_ms"]) is int and type(row["end_ms"]) is int and 0 <= row["start_ms"] < row["end_ms"] and isinstance(row["text"], str) and bool(row["text"].strip()) and isinstance(row["speaker_id"], str) and bool(row["speaker_id"].strip()), "invalid_archive", 400, "字幕来源或时间无效")
                    _valid_srt_text(row["text"])
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
        for saved in conn.execute("SELECT episode_id,timeline_version,shot_snapshot FROM subtitle_sets"):
            episode_id = saved["episode_id"]
            timeline = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
            require(timeline and saved["timeline_version"] <= timeline["version"], "invalid_archive", 400, "字幕引用的时间轴版本无效")
            items = [dict(row) for row in conn.execute("SELECT shot_id,source_asset_id,in_ms,out_ms FROM timeline_items WHERE episode_id=? ORDER BY ordinal", (episode_id,))]
            selection_current = all(conn.execute("SELECT 1 FROM shots WHERE id=? AND selected_candidate_id=?", (item["shot_id"], item["source_asset_id"])).fetchone() for item in items)
            shot_revisions = {item["shot_id"]: conn.execute("SELECT revision FROM shots WHERE id=?", (item["shot_id"],)).fetchone()["revision"] for item in items}
            claimed_ready = saved["timeline_version"] == timeline["version"] and bool(items) and selection_current and saved["shot_snapshot"] is not None and json.loads(saved["shot_snapshot"]) == shot_revisions
            segments = []
            offset = 0
            for item in items:
                right = offset + item["out_ms"] - item["in_ms"]
                segments.append((offset, right, item["shot_id"], item["source_asset_id"]))
                offset = right
            previous_end = -1
            cues = [dict(row) for row in conn.execute("SELECT ordinal,start_ms,end_ms,speaker_id,source_asset_id FROM subtitle_cues WHERE episode_id=? ORDER BY ordinal", (episode_id,))]
            for index, cue in enumerate(cues):
                require(cue["ordinal"] == index and cue["start_ms"] >= previous_end, "invalid_archive", 400, "字幕顺序或重叠无效")
                previous_end = cue["end_ms"]
                if claimed_ready:
                    covering = next((segment for segment in segments if segment[0] <= cue["start_ms"] and cue["end_ms"] <= segment[1] and segment[3] == cue["source_asset_id"]), None)
                    require(covering is not None, "invalid_archive", 400, "当前字幕不属于覆盖的时间轴片段")
                    dialogue = json.loads(conn.execute("SELECT dialogue FROM shots WHERE id=?", (covering[2],)).fetchone()["dialogue"])
                    require(cue["speaker_id"] in {line["speaker_id"] for line in dialogue}, "invalid_archive", 400, "字幕说话人不属于覆盖的镜头")
        checked_media = set()
        for meta in conn.execute("SELECT episode_id FROM episode_timelines"):
            episode = trial_store._row(conn, "episodes", meta["episode_id"])
            items = [dict(row) for row in conn.execute("SELECT * FROM timeline_items WHERE episode_id=? ORDER BY ordinal", (episode["id"],))]
            current = all(conn.execute("SELECT 1 FROM shots WHERE id=? AND selected_candidate_id=?", (item["shot_id"], item["source_asset_id"])).fetchone() for item in items)
            if not current:
                continue
            for item in items:
                checked = _validated_item(trial_store, conn, episode, item)
                if checked["source_asset_id"] not in checked_media:
                    asset = trial_store._row(conn, "assets", checked["source_asset_id"])
                    decoded = decode_check(trial_store.data_root / "assets" / asset["sha256"], data_root=trial_store.data_root)
                    require(decoded["decoded"], "invalid_media", 422, "恢复的当前选片无法完整解码")
                    checked_media.add(checked["source_asset_id"])
    with trial_store.connection() as conn:
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in EXT_TABLES}
