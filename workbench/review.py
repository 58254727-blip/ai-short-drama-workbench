"""Explicit human media review, versioned separately from machine checks."""

from datetime import datetime, timezone

from .domain import require


def init_review(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS manual_qc (episode_id TEXT NOT NULL REFERENCES episodes(id), shot_id TEXT NOT NULL REFERENCES shots(id), asset_id TEXT NOT NULL REFERENCES assets(id), shot_revision INTEGER NOT NULL, timeline_version INTEGER NOT NULL, verdict TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL, PRIMARY KEY(episode_id,shot_id,asset_id))")


class ManualReviewService:
    def __init__(self, store):
        self.store = store
        with store.connection() as conn:
            init_review(conn)
            conn.commit()

    def list(self, episode_id):
        with self.store.connection() as conn:
            episode = self.store._row(conn, "episodes", episode_id)
            version_row = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
            version = version_row["version"] if version_row else 0
            rows = [dict(row) for row in conn.execute("SELECT * FROM manual_qc WHERE episode_id=? ORDER BY reviewed_at", (episode_id,))]
            for row in rows:
                shot = self.store._row(conn, "shots", row["shot_id"])
                row["current"] = bool(shot["selected_candidate_id"] == row["asset_id"] and shot["revision"] == row["shot_revision"] and version == row["timeline_version"])
            return rows

    def save(self, episode_id, shot_id, asset_id, verdict, note):
        require(verdict in ("pass", "revise", "reject") and isinstance(note, str), "invalid_review", 400, "人工校核记录无效")
        with self.store.transaction() as conn:
            init_review(conn)
            episode = self.store._row(conn, "episodes", episode_id)
            shot = self.store._row(conn, "shots", shot_id)
            asset = self.store._row(conn, "assets", asset_id)
            require(shot["episode_id"] == episode_id and shot["selected_candidate_id"] == asset_id and asset["project_id"] == episode["project_id"] and asset["kind"] == "video", "ownership_conflict", 409, "只能校核当前分集选片")
            version_row = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
            version = version_row["version"] if version_row else 0
            stamp = datetime.now(timezone.utc).isoformat()
            conn.execute("INSERT INTO manual_qc VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(episode_id,shot_id,asset_id) DO UPDATE SET shot_revision=excluded.shot_revision,timeline_version=excluded.timeline_version,verdict=excluded.verdict,note=excluded.note,reviewed_at=excluded.reviewed_at", (episode_id, shot_id, asset_id, shot["revision"], version, verdict, note, stamp))
        return self.list(episode_id)
