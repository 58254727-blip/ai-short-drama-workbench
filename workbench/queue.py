"""SQLite backed, scoped task queue with durable resource reservations."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .domain import DomainError, require


KINDS = {"h3": "gpu", "text": "cpu", "asr": "cpu", "probe": "cpu", "export": "cpu"}
CPU_PHASES = {"text": "preparation", "probe": "preparation", "asr": "sound_check", "export": "postproduction"}
LIMITS = {"gpu": 1, "cpu": 2}
BUSY = ("submitting", "running", "needs_reconcile")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 episode_id TEXT NOT NULL REFERENCES episodes(id), shot_id TEXT REFERENCES shots(id),
 kind TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL,
 payload TEXT NOT NULL, result TEXT, external_id TEXT, retry_of_id TEXT REFERENCES jobs(id),
 source_revision INTEGER NOT NULL, source_snapshot TEXT NOT NULL, retry_classification TEXT,
 failure_code TEXT, failure_message TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, lease_until TEXT);
CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs(resource,state,created_at,id);
CREATE INDEX IF NOT EXISTS jobs_scope_idx ON jobs(project_id,episode_id,shot_id);
CREATE TABLE IF NOT EXISTS job_phases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
 phase TEXT NOT NULL, entered_at TEXT NOT NULL, exited_at TEXT, duration_ms INTEGER);
CREATE INDEX IF NOT EXISTS job_phases_job_idx ON job_phases(job_id,id);
"""
SHELL_KEYS = {"command", "cmd", "shell", "argv", "executable", "subprocess", "script"}


def _now():
    return datetime.now(timezone.utc)


def _stamp(instant=None):
    return (instant or _now()).isoformat()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        raise DomainError("invalid_payload", 400, "任务内容必须是有效 JSON") from None


def _check_no_shell(value):
    if isinstance(value, dict):
        require(not (SHELL_KEYS & {str(key).lower() for key in value}), "invalid_payload", 400, "任务内容不可包含任意命令")
        for nested in value.values():
            _check_no_shell(nested)
    elif isinstance(value, list):
        for nested in value:
            _check_no_shell(nested)


class Queue:
    def __init__(self, store):
        self.store = store
        with store.transaction() as conn:
            conn.executescript(SCHEMA)

    def _row(self, conn, job_id):
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        require(row is not None, "not_found", 404, "任务不存在")
        return row

    def _job(self, conn, row):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["source_snapshot"] = json.loads(item["source_snapshot"])
        item["result"] = json.loads(item["result"]) if item["result"] is not None else None
        item["phases"] = [dict(phase) for phase in conn.execute(
            "SELECT phase,entered_at,exited_at,duration_ms FROM job_phases WHERE job_id=? ORDER BY id", (item["id"],)
        )]
        return item

    def _scope(self, conn, scope):
        require(isinstance(scope, dict) and set(scope) <= {"project_id", "episode_id", "shot_id"}, "invalid_scope", 400, "任务范围错误")
        project_id, episode_id = scope.get("project_id"), scope.get("episode_id")
        require(isinstance(project_id, str) and isinstance(episode_id, str), "invalid_scope", 400, "任务须指定作品和分集")
        project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        require(project is not None and episode is not None, "not_found", 404, "作品或分集不存在")
        require(episode["project_id"] == project_id, "ownership_conflict", 409, "分集不属于作品")
        shot_id = scope.get("shot_id")
        if shot_id is not None:
            shot = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
            require(shot is not None, "not_found", 404, "镜头不存在")
            require(shot["episode_id"] == episode_id, "ownership_conflict", 409, "镜头不属于分集")
            shot_snapshot = dict(shot)
            shot_snapshot["dialogue"] = json.loads(shot_snapshot["dialogue"])
            shot_snapshot["asset_version_ids"] = json.loads(shot_snapshot["asset_version_ids"])
            return shot["revision"], {"episode": dict(episode), "shot": shot_snapshot}
        return episode["revision"], {"episode": dict(episode), "shot": None}

    def _phase(self, conn, job_id, name, when):
        open_phase = conn.execute("SELECT id,entered_at FROM job_phases WHERE job_id=? AND exited_at IS NULL ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if open_phase:
            elapsed = max(0, round((when - datetime.fromisoformat(open_phase["entered_at"])).total_seconds() * 1000))
            conn.execute("UPDATE job_phases SET exited_at=?,duration_ms=? WHERE id=?", (_stamp(when), elapsed, open_phase["id"]))
        if name:
            conn.execute("INSERT INTO job_phases(job_id,phase,entered_at) VALUES (?,?,?)", (job_id, name, _stamp(when)))

    def _insert(self, conn, scope, kind, payload, source_revision, source_snapshot, retry_of_id=None, retry_classification=None):
        when = _now()
        item = dict(id=str(uuid4()), project_id=scope["project_id"], episode_id=scope["episode_id"],
                    shot_id=scope.get("shot_id"), kind=kind, resource=KINDS[kind], state="queued",
                    payload=_json(payload), result=None, external_id=None, retry_of_id=retry_of_id,
                    source_revision=source_revision, source_snapshot=_json(source_snapshot), retry_classification=retry_classification,
                    failure_code=None, failure_message=None, created_at=_stamp(when), updated_at=_stamp(when), lease_until=None)
        conn.execute("INSERT INTO jobs VALUES (:id,:project_id,:episode_id,:shot_id,:kind,:resource,:state,:payload,:result,:external_id,:retry_of_id,:source_revision,:source_snapshot,:retry_classification,:failure_code,:failure_message,:created_at,:updated_at,:lease_until)", item)
        self._phase(conn, item["id"], "queued", when)
        return self._job(conn, self._row(conn, item["id"]))

    def enqueue(self, scope: dict, kind: str, payload: dict) -> dict:
        require(kind in KINDS, "invalid_kind", 400, "任务类型不支持")
        require(isinstance(payload, dict), "invalid_payload", 400, "任务内容必须是对象")
        _check_no_shell(payload)
        _json(payload)
        with self.store.transaction() as conn:
            revision, snapshot = self._scope(conn, scope)
            return self._insert(conn, scope, kind, payload, revision, snapshot)

    def claim(self, resource: str) -> dict | None:
        require(resource in LIMITS, "invalid_resource", 400, "计算资源不支持")
        with self.store.transaction() as conn:
            busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE resource=? AND state IN (?,?,?)", (resource, *BUSY)).fetchone()[0]
            if busy >= LIMITS[resource]:
                return None
            row = conn.execute("SELECT * FROM jobs WHERE resource=? AND state='queued' ORDER BY created_at,id LIMIT 1", (resource,)).fetchone()
            if row is None:
                return None
            when = _now()
            state = "submitting" if resource == "gpu" else "running"
            lease = _stamp(when + timedelta(seconds=60))
            conn.execute("UPDATE jobs SET state=?,updated_at=?,lease_until=? WHERE id=?", (state, _stamp(when), lease, row["id"]))
            self._phase(conn, row["id"], "preparation" if resource == "gpu" else CPU_PHASES[row["kind"]], when)
            return self._job(conn, self._row(conn, row["id"]))

    def renew_lease(self, job_id: str) -> None:
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            require(row["state"] in ("submitting", "running"), "invalid_state", 409, "任务未运行")
            conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (_stamp(_now() + timedelta(seconds=60)), job_id))

    def record_external(self, job_id: str, external_id: str) -> None:
        require(isinstance(external_id, str) and bool(external_id.strip()), "invalid_external_id", 400, "外部任务 ID 不能为空")
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            require(row["resource"] == "gpu" and row["state"] in ("submitting", "needs_reconcile") and row["external_id"] is None, "invalid_state", 409, "任务不在可核对状态")
            when = _now()
            conn.execute("UPDATE jobs SET external_id=?,state='running',updated_at=? WHERE id=?", (external_id, _stamp(when), job_id))
            self._phase(conn, job_id, "gpu_execution", when)

    def _terminal(self, job_id, state, result=None, code=None, message=None):
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            require(row["state"] in BUSY, "invalid_state", 409, "任务无法完成")
            when = _now()
            conn.execute("UPDATE jobs SET state=?,result=?,failure_code=?,failure_message=?,lease_until=NULL,updated_at=? WHERE id=?",
                         (state, _json(result) if result is not None else None, code, message, _stamp(when), job_id))
            self._phase(conn, job_id, None, when)

    def finish(self, job_id: str, result: dict) -> None:
        require(isinstance(result, dict), "invalid_result", 400, "任务结果必须是对象")
        self._terminal(job_id, "succeeded", result=result)

    def fail(self, job_id: str, code: str, message: str) -> None:
        require(isinstance(code, str) and bool(code.strip()) and isinstance(message, str), "invalid_failure", 400, "失败分类或原因错误")
        self._terminal(job_id, "failed", code=code, message=message)

    def recover(self) -> list[dict]:
        """Call at startup; uncertain GPU submissions remain reserved for external reconciliation."""
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE state IN ('submitting','running') ORDER BY created_at,id").fetchall()
            result = []
            for row in rows:
                when = _now()
                if row["resource"] == "gpu" or row["external_id"] is not None:
                    state, phase = "needs_reconcile", "needs_reconcile"
                else:
                    state, phase = "queued", "queued"
                conn.execute("UPDATE jobs SET state=?,updated_at=?,lease_until=NULL WHERE id=?", (state, _stamp(when), row["id"]))
                self._phase(conn, row["id"], phase, when)
                result.append(self._job(conn, self._row(conn, row["id"])))
            return result

    def get_job(self, job_id: str) -> dict:
        with self.store.connection() as conn:
            return self._job(conn, self._row(conn, job_id))

    def list_jobs(self, project_id=None) -> list[dict]:
        with self.store.connection() as conn:
            if project_id is not None:
                require(conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is not None, "not_found", 404, "作品不存在")
            rows = conn.execute("SELECT * FROM jobs WHERE (? IS NULL OR project_id=?) ORDER BY created_at,id", (project_id, project_id))
            return [self._job(conn, row) for row in rows]

    def cancel_queued(self, job_id: str) -> dict:
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            require(row["state"] == "queued", "invalid_state", 409, "只能取消尚未提交的任务")
            when = _now()
            conn.execute("UPDATE jobs SET state='cancelled',updated_at=? WHERE id=?", (_stamp(when), job_id))
            self._phase(conn, job_id, None, when)
            return self._job(conn, self._row(conn, job_id))

    def retry(self, job_id: str, payload: dict | None = None) -> dict:
        with self.store.transaction() as conn:
            old = self._row(conn, job_id)
            require(old["state"] == "failed", "invalid_state", 409, "只能重试失败任务")
            old_payload = json.loads(old["payload"])
            new_payload = old_payload if payload is None else payload
            require(isinstance(new_payload, dict), "invalid_payload", 400, "任务内容必须是对象")
            _check_no_shell(new_payload)
            _json(new_payload)
            require(new_payload.get("plan_revision") is not None and new_payload.get("strategy") is not None, "invalid_payload", 400, "重试须记录方案版本和策略")
            prior = conn.execute("SELECT payload,failure_code FROM jobs WHERE shot_id IS ? AND kind=? AND source_revision=? AND state='failed'",
                                 (old["shot_id"], old["kind"], old["source_revision"]))
            equivalent_failures = sum(1 for row in prior if row["failure_code"] == old["failure_code"] and
                                      json.loads(row["payload"]).get("plan_revision") == new_payload["plan_revision"] and
                                      json.loads(row["payload"]).get("strategy") == new_payload["strategy"])
            require(equivalent_failures < 2, "retry_plan_required", 409, "同类失败两次后必须改变方案")
            scope = {key: old[key] for key in ("project_id", "episode_id", "shot_id")}
            self._scope(conn, scope)
            return self._insert(conn, scope, old["kind"], new_payload, old["source_revision"], json.loads(old["source_snapshot"]), old["id"], old["failure_code"])
