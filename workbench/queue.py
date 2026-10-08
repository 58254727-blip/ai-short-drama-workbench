"""SQLite backed, scoped task queue with durable resource reservations."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .domain import DomainError, require
from .assets import binary_available


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
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, lease_until TEXT,
 claim_token TEXT, execution_started_at TEXT, submission_attempted_at TEXT);
CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs(resource,state,created_at,id);
CREATE INDEX IF NOT EXISTS jobs_scope_idx ON jobs(project_id,episode_id,shot_id);
CREATE TABLE IF NOT EXISTS job_phases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
 phase TEXT NOT NULL, entered_at TEXT NOT NULL, exited_at TEXT, duration_ms INTEGER,
 interrupted_at TEXT);
CREATE INDEX IF NOT EXISTS job_phases_job_idx ON job_phases(job_id,id);
CREATE TABLE IF NOT EXISTS job_settlements (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id), acknowledgment TEXT NOT NULL,
 note TEXT NOT NULL, prior_failure_code TEXT, prior_failure_message TEXT,
 settled_at TEXT NOT NULL);
"""
SHELL_KEYS = {"command", "cmd", "shell", "argv", "executable", "subprocess", "script"}


def _now():
    return datetime.now(timezone.utc)


def _stamp(instant=None):
    return (instant or _now()).isoformat()


def _observed_utc(value, name):
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_timestamp", 400, f"{name} 必须是 UTC 时间") from None
    require(instant.tzinfo is not None and instant.utcoffset() == timedelta(0), "invalid_timestamp", 400, f"{name} 必须是 UTC 时间")
    require(instant <= _now(), "invalid_timestamp", 400, f"{name} 不得在未来")
    return instant


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
            for table, additions in (
                ("jobs", {"claim_token": "TEXT", "execution_started_at": "TEXT", "submission_attempted_at": "TEXT"}),
                ("job_phases", {"interrupted_at": "TEXT"}),
            ):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column, sql_type in additions.items():
                    if column not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def _row(self, conn, job_id):
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        require(row is not None, "not_found", 404, "任务不存在")
        return row

    def _job(self, conn, row):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["source_snapshot"] = json.loads(item["source_snapshot"])
        item["result"] = json.loads(item["result"]) if item["result"] is not None else None
        settlement = conn.execute("SELECT acknowledgment,note,prior_failure_code,prior_failure_message,settled_at FROM job_settlements WHERE job_id=?", (item["id"],)).fetchone()
        item["manual_settlement"] = dict(settlement) if settlement else None
        item["phases"] = [dict(phase) for phase in conn.execute(
            "SELECT phase,entered_at,exited_at,duration_ms,interrupted_at FROM job_phases WHERE job_id=? ORDER BY id", (item["id"],)
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

    def _export_state(self, conn, episode_id):
        meta = conn.execute("SELECT version FROM episode_timelines WHERE episode_id=?", (episode_id,)).fetchone()
        items = [dict(row) for row in conn.execute("SELECT * FROM timeline_items WHERE episode_id=? ORDER BY ordinal", (episode_id,))]
        shots = [dict(row) for row in conn.execute("SELECT id,revision,selected_candidate_id FROM shots WHERE episode_id=? ORDER BY id", (episode_id,))]
        assets = [dict(conn.execute("SELECT id,sha256,storage_key,size_bytes FROM assets WHERE id=?", (asset_id,)).fetchone())
                  for asset_id in sorted({item["source_asset_id"] for item in items})]
        caption_set = conn.execute("SELECT * FROM subtitle_sets WHERE episode_id=?", (episode_id,)).fetchone()
        cues = [dict(row) for row in conn.execute("SELECT * FROM subtitle_cues WHERE episode_id=? ORDER BY ordinal", (episode_id,))]
        qc = [dict(row) for row in conn.execute("SELECT * FROM manual_qc WHERE episode_id=? ORDER BY reviewed_at", (episode_id,))]
        return {"timeline_version": meta["version"] if meta else 0, "items": items, "shots": shots, "assets": assets,
                "caption_set": dict(caption_set) if caption_set else None, "cues": cues, "manual_qc": qc}

    def _checked_export_snapshot(self, conn, scope, payload):
        revision, snapshot = self._scope(conn, scope)
        state = self._export_state(conn, scope["episode_id"])
        shot_by_id = {shot["id"]: shot for shot in state["shots"]}
        require(bool(state["items"]) and all(shot_by_id.get(item["shot_id"], {}).get("selected_candidate_id") == item["source_asset_id"]
                for item in state["items"]), "timeline_missing", 409, "时间轴选片已变化")
        require(all(asset["storage_key"] == asset["sha256"] and binary_available(self.store.data_root, asset["sha256"])
                    for asset in state["assets"]), "asset_hash_mismatch", 409, "导出来源素材缺失或哈希变化")
        if payload.get("subtitles"):
            saved = state["caption_set"]
            current_shots = {item["shot_id"]: shot_by_id[item["shot_id"]]["revision"] for item in state["items"]}
            require(saved is not None and saved["timeline_version"] == state["timeline_version"]
                    and saved["shot_snapshot"] is not None and json.loads(saved["shot_snapshot"]) == current_shots
                    and bool(state["cues"]), "captions_not_ready", 409, "字幕已过期或未填写")
        snapshot["export"] = state
        return revision, snapshot

    def assert_export_source(self, job, conn=None):
        scope = {key: job[key] for key in ("project_id", "episode_id", "shot_id")}
        if conn is None:
            with self.store.connection() as owned:
                owned.execute("BEGIN")
                return self.assert_export_source(job, owned)
        _, current = self._checked_export_snapshot(conn, scope, job["payload"])
        require(current == job["source_snapshot"], "revision_conflict", 409, "排队后镜头、时间轴、字幕或校核已变化，请重新排队")
        return current["export"]

    def _phase(self, conn, job_id, name, when):
        open_phase = conn.execute("SELECT id,entered_at FROM job_phases WHERE job_id=? AND exited_at IS NULL AND interrupted_at IS NULL ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if open_phase:
            elapsed = max(0, round((when - datetime.fromisoformat(open_phase["entered_at"])).total_seconds() * 1000))
            conn.execute("UPDATE job_phases SET exited_at=?,duration_ms=? WHERE id=?", (_stamp(when), elapsed, open_phase["id"]))
        if name:
            conn.execute("INSERT INTO job_phases(job_id,phase,entered_at) VALUES (?,?,?)", (job_id, name, _stamp(when)))

    def _interrupt_phase(self, conn, job_id, when):
        conn.execute("UPDATE job_phases SET interrupted_at=? WHERE job_id=? AND exited_at IS NULL AND interrupted_at IS NULL", (_stamp(when), job_id))
        conn.execute("INSERT INTO job_phases(job_id,phase,entered_at) VALUES (?,?,?)", (job_id, "queued", _stamp(when)))

    def _insert(self, conn, scope, kind, payload, source_revision, source_snapshot, retry_of_id=None, retry_classification=None):
        when = _now()
        item = dict(id=str(uuid4()), project_id=scope["project_id"], episode_id=scope["episode_id"],
                    shot_id=scope.get("shot_id"), kind=kind, resource=KINDS[kind], state="queued",
                    payload=_json(payload), result=None, external_id=None, retry_of_id=retry_of_id,
                    source_revision=source_revision, source_snapshot=_json(source_snapshot), retry_classification=retry_classification,
                    failure_code=None, failure_message=None, created_at=_stamp(when), updated_at=_stamp(when), lease_until=None,
                    claim_token=None, execution_started_at=None, submission_attempted_at=None)
        conn.execute("INSERT INTO jobs VALUES (:id,:project_id,:episode_id,:shot_id,:kind,:resource,:state,:payload,:result,:external_id,:retry_of_id,:source_revision,:source_snapshot,:retry_classification,:failure_code,:failure_message,:created_at,:updated_at,:lease_until,:claim_token,:execution_started_at,:submission_attempted_at)", item)
        self._phase(conn, item["id"], "queued", when)
        return self._job(conn, self._row(conn, item["id"]))

    def enqueue(self, scope: dict, kind: str, payload: dict) -> dict:
        require(kind in KINDS, "invalid_kind", 400, "任务类型不支持")
        require(isinstance(payload, dict), "invalid_payload", 400, "任务内容必须是对象")
        _check_no_shell(payload)
        _json(payload)
        with self.store.transaction() as conn:
            revision, snapshot = self._checked_export_snapshot(conn, scope, payload) if kind == "export" else self._scope(conn, scope)
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
            token = str(uuid4())
            conn.execute("UPDATE jobs SET state=?,updated_at=?,lease_until=?,claim_token=? WHERE id=?", (state, _stamp(when), lease, token, row["id"]))
            self._phase(conn, row["id"], "preparation" if resource == "gpu" else CPU_PHASES[row["kind"]], when)
            return self._job(conn, self._row(conn, row["id"]))

    def _owned(self, row, claim_token):
        require(isinstance(claim_token, str) and row["claim_token"] == claim_token and row["lease_until"] is not None and
                datetime.fromisoformat(row["lease_until"]) > _now(), "stale_claim", 409, "任务领取已失效")

    def renew_lease(self, job_id: str, claim_token: str) -> None:
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["state"] in BUSY, "invalid_state", 409, "任务未运行或待核对")
            conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (_stamp(_now() + timedelta(seconds=60)), job_id))

    def validate_reconcile_proof(self, job_id: str, claim_token: str, started_at: str, finished_at: str) -> dict:
        """Read-only proof and ownership gate before collecting external outputs."""
        started = _observed_utc(started_at, "执行开始")
        finished = _observed_utc(finished_at, "执行结束")
        require(finished >= started, "invalid_timestamp", 400, "执行结束早于开始")
        with self.store.connection() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["resource"] == "gpu" and row["state"] == "needs_reconcile" and row["external_id"] is not None,
                    "invalid_state", 409, "任务没有可核对的外部执行")
            phase = conn.execute("SELECT entered_at FROM job_phases WHERE job_id=? AND exited_at IS NULL AND interrupted_at IS NULL ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
            require(phase is not None and started >= datetime.fromisoformat(phase["entered_at"]), "invalid_timestamp", 400, "执行开始早于任务领取")
            if row["execution_started_at"] is not None:
                require(datetime.fromisoformat(row["execution_started_at"]) == started, "execution_conflict", 409, "执行开始时间冲突")
            return self._job(conn, row)

    def mark_submission_attempt(self, job_id: str, claim_token: str) -> None:
        """Persist intent immediately before sending a provider request."""
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["resource"] == "gpu" and row["state"] == "submitting" and row["external_id"] is None,
                    "invalid_state", 409, "任务不在提交前状态")
            if row["submission_attempted_at"] is None:
                conn.execute("UPDATE jobs SET submission_attempted_at=?,updated_at=? WHERE id=?", (_stamp(), _stamp(), job_id))

    def record_external(self, job_id: str, external_id: str, claim_token: str) -> None:
        require(isinstance(external_id, str) and bool(external_id.strip()), "invalid_external_id", 400, "外部任务 ID 不能为空")
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["resource"] == "gpu" and row["state"] in ("submitting", "needs_reconcile"), "invalid_state", 409, "任务不在可核对状态")
            require(row["state"] == "needs_reconcile" or row["submission_attempted_at"] is not None,
                    "invalid_state", 409, "尚未记录提交尝试")
            if row["external_id"] == external_id:
                return
            require(row["external_id"] is None, "external_conflict", 409, "外部任务 ID 已记录")
            when = _now()
            conn.execute("UPDATE jobs SET external_id=?,updated_at=? WHERE id=?", (external_id, _stamp(when), job_id))

    def record_execution_started(self, job_id: str, started_at: str, claim_token: str) -> None:
        observed = _observed_utc(started_at, "执行开始")
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["resource"] == "gpu" and row["external_id"] is not None and row["state"] in ("submitting", "needs_reconcile", "running"), "invalid_state", 409, "GPU 任务尚无外部执行证据")
            if row["execution_started_at"] is not None:
                require(datetime.fromisoformat(row["execution_started_at"]) == observed, "execution_conflict", 409, "执行开始时间冲突")
                if row["state"] == "needs_reconcile":
                    conn.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (_stamp(), job_id))
                return
            opened = conn.execute("SELECT entered_at FROM job_phases WHERE job_id=? AND exited_at IS NULL AND interrupted_at IS NULL ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
            require(opened is not None and observed >= datetime.fromisoformat(opened["entered_at"]), "invalid_timestamp", 400, "执行开始早于领取")
            conn.execute("UPDATE jobs SET state='running',execution_started_at=?,updated_at=? WHERE id=?", (_stamp(observed), _stamp(), job_id))
            self._phase(conn, job_id, "gpu_execution", observed)

    def _terminal(self, job_id, state, claim_token, result=None, code=None, message=None, execution_finished_at=None):
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["state"] in BUSY, "invalid_state", 409, "任务无法完成")
            when = _now()
            if row["resource"] == "gpu":
                if state == "failed" and execution_finished_at is None:
                    conn.execute("UPDATE jobs SET state='needs_reconcile',failure_code=?,failure_message=?,updated_at=? WHERE id=?",
                                 (code, message, _stamp(), job_id))
                    return
                require(row["state"] == "running" and row["external_id"] is not None and row["execution_started_at"] is not None,
                        "execution_unverified", 409, "GPU 执行未核实完成")
                require(execution_finished_at is not None, "execution_unverified", 409, "缺少外部执行结束时间")
                when = _observed_utc(execution_finished_at, "执行结束")
                require(when >= datetime.fromisoformat(row["execution_started_at"]), "invalid_timestamp", 400, "执行结束早于开始")
            conn.execute("UPDATE jobs SET state=?,result=?,failure_code=?,failure_message=?,lease_until=NULL,claim_token=NULL,updated_at=? WHERE id=?",
                         (state, _json(result) if result is not None else None, code, message, _stamp(), job_id))
            self._phase(conn, job_id, None, when)

    def finish(self, job_id: str, result: dict, claim_token: str, *, execution_finished_at: str | None = None) -> None:
        require(isinstance(result, dict), "invalid_result", 400, "任务结果必须是对象")
        self._terminal(job_id, "succeeded", claim_token, result=result, execution_finished_at=execution_finished_at)

    def fail(self, job_id: str, code: str, message: str, claim_token: str, *, execution_finished_at: str | None = None) -> None:
        require(isinstance(code, str) and bool(code.strip()) and isinstance(message, str), "invalid_failure", 400, "失败分类或原因错误")
        self._terminal(job_id, "failed", claim_token, code=code, message=message, execution_finished_at=execution_finished_at)

    def fail_preflight(self, job_id: str, code: str, message: str, claim_token: str) -> None:
        """Use only before any provider submission attempt has been recorded."""
        require(isinstance(code, str) and bool(code.strip()) and isinstance(message, str), "invalid_failure", 400, "失败分类或原因错误")
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["resource"] == "gpu" and row["state"] == "submitting" and row["submission_attempted_at"] is None
                    and row["external_id"] is None and row["execution_started_at"] is None,
                    "invalid_state", 409, "已尝试提交，不能按预检失败释放 GPU")
            when = _now()
            conn.execute("UPDATE jobs SET state='failed',failure_code=?,failure_message=?,claim_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                         (code, message, _stamp(when), job_id))
            self._phase(conn, job_id, None, when)

    def reject_submission(self, job_id: str, code: str, message: str, rejected_at: str, claim_token: str) -> None:
        """Provider explicitly rejected submission without starting execution."""
        require(isinstance(code, str) and bool(code.strip()) and isinstance(message, str), "invalid_failure", 400, "失败分类或原因错误")
        observed = _observed_utc(rejected_at, "提交拒绝")
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            legacy_known_external = row["state"] == "needs_reconcile" and row["external_id"] is not None and row["submission_attempted_at"] is None
            require(row["resource"] == "gpu" and row["state"] in ("submitting", "needs_reconcile") and
                    (row["submission_attempted_at"] is not None or legacy_known_external) and row["execution_started_at"] is None,
                    "invalid_state", 409, "没有未执行的提交拒绝证据")
            lower_bound = row["submission_attempted_at"] or row["created_at"]
            require(observed >= datetime.fromisoformat(lower_bound), "invalid_timestamp", 400, "拒绝时间早于任务创建或提交")
            conn.execute("UPDATE jobs SET state='failed',failure_code=?,failure_message=?,claim_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                         (code, message, _stamp(), job_id))
            if legacy_known_external:
                conn.execute("UPDATE job_phases SET interrupted_at=? WHERE job_id=? AND exited_at IS NULL AND interrupted_at IS NULL",
                             (_stamp(observed), job_id))
            else:
                self._phase(conn, job_id, None, observed)

    def recover(self) -> list[dict]:
        """Call at startup; uncertain GPU submissions remain reserved for external reconciliation."""
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE state IN ('submitting','running','needs_reconcile') AND lease_until IS NOT NULL AND lease_until<=? ORDER BY created_at,id", (_stamp(),)).fetchall()
            result = []
            for row in rows:
                when = _now()
                idempotent_local = row["kind"] in ("probe", "asr", "export") and json.loads(row["payload"]).get("idempotent_local") is True
                if row["resource"] == "gpu" or row["external_id"] is not None or not idempotent_local:
                    state, phase = "needs_reconcile", None
                else:
                    state, phase = "queued", "queued"
                conn.execute("UPDATE jobs SET state=?,updated_at=?,lease_until=?,claim_token=? WHERE id=?",
                             (state, _stamp(when), _stamp(when + timedelta(seconds=60)) if state == "needs_reconcile" else None,
                              str(uuid4()) if state == "needs_reconcile" else None, row["id"]))
                if phase is not None:
                    self._interrupt_phase(conn, row["id"], when)
                result.append(self._job(conn, self._row(conn, row["id"])))
            return result

    def settle_uncertain_text(self, job_id: str, claim_token: str, acknowledged: bool, note: str) -> dict:
        """Operator-confirmed local failure only; no claim about the remote text request."""
        require(acknowledged is True and isinstance(note, str) and bool(note.strip()) and len(note) <= 2000,
                "manual_ack_required", 400, "须明确确认未知外部状态并记录本地收束说明")
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            self._owned(row, claim_token)
            require(row["state"] == "needs_reconcile" and row["resource"] == "cpu" and row["kind"] == "text"
                    and row["external_id"] is None, "invalid_state", 409, "仅能人工收束待核对的 CPU 文本任务")
            when = _now()
            conn.execute("INSERT INTO job_settlements VALUES (?,?,?,?,?,?)",
                         (job_id, "operator_confirmed_unknown", note.strip(), row["failure_code"], row["failure_message"], _stamp(when)))
            conn.execute("UPDATE jobs SET state='failed',failure_code='manual_unverified',failure_message=?,lease_until=NULL,claim_token=NULL,updated_at=? WHERE id=?",
                         ("用户确认外部状态未知，仅结束本地等待；未取消外部请求或核验执行结果", _stamp(when), job_id))
            self._phase(conn, job_id, None, when)
            return self._job(conn, self._row(conn, job_id))

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
            prior = conn.execute("SELECT payload,failure_code FROM jobs WHERE project_id=? AND episode_id=? AND shot_id IS ? AND kind=? AND source_revision=? AND source_snapshot=? AND state='failed'",
                                 (old["project_id"], old["episode_id"], old["shot_id"], old["kind"], old["source_revision"], old["source_snapshot"]))
            equivalent_failures = sum(1 for row in prior if row["failure_code"] == old["failure_code"] and
                                      json.loads(row["payload"]).get("plan_revision") == new_payload["plan_revision"] and
                                      json.loads(row["payload"]).get("strategy") == new_payload["strategy"])
            require(equivalent_failures < 2, "retry_plan_required", 409, "同类失败两次后必须改变方案")
            scope = {key: old[key] for key in ("project_id", "episode_id", "shot_id")}
            self._scope(conn, scope)
            return self._insert(conn, scope, old["kind"], new_payload, old["source_revision"], json.loads(old["source_snapshot"]), old["id"], old["failure_code"])
