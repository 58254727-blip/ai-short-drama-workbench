"""Persistent scheduling contracts exercised with a real SQLite database."""

import tempfile
import threading
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from workbench.domain import DomainError
from workbench.queue import Queue
from workbench.store import Store
from workbench.worker import Worker


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "queue.sqlite")
        self.project = self.store.create_project("甲")
        self.episode = self.store.create_episode(self.project["id"], "第一集")
        self.shot = self.store.save_shot(self.episode["id"], {})
        self.scope = {"project_id": self.project["id"], "episode_id": self.episode["id"], "shot_id": self.shot["id"]}
        self.queue = Queue(self.store)

    def enqueue(self, kind="h3", payload=None):
        return self.queue.enqueue(self.scope, kind, payload or {"plan_revision": 1, "strategy": "a", "seed": 1})

    def expire(self, job_id):
        with self.store.transaction() as conn:
            conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job_id))

    def gpu_started(self, job):
        self.queue.mark_submission_attempt(job["id"], job["claim_token"])
        self.queue.record_external(job["id"], "external-" + job["id"], job["claim_token"])
        started = datetime.now(timezone.utc).isoformat()
        self.queue.record_execution_started(job["id"], started, job["claim_token"])
        return started

    def test_claim_caps_gpu_at_one_across_instances(self):
        first = self.enqueue()
        second = self.enqueue()
        other = Queue(self.store)
        gate = threading.Barrier(3)
        results = []

        def claim(queue):
            gate.wait()
            results.append(queue.claim("gpu"))

        threads = [threading.Thread(target=claim, args=(q,)) for q in (self.queue, other)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len([job for job in results if job]))
        self.assertEqual("submitting", next(job for job in results if job)["state"])
        self.assertIsNone(self.queue.claim("gpu"))
        self.assertEqual({first["id"], second["id"]}, {j["id"] for j in self.queue.list_jobs()})

    def test_cpu_has_two_slots_and_does_not_wait_for_gpu_handler(self):
        gpu_entered = threading.Event()
        gpu_release = threading.Event()
        gpu = self.enqueue()
        cpu_jobs = [self.enqueue("text") for _ in range(3)]

        def gpu_handler(kind, job):
            self.gpu_started(job)
            gpu_entered.set()
            self.assertTrue(gpu_release.wait(5))
            return {"candidate": "local", "execution_finished_at": datetime.now(timezone.utc).isoformat()}

        gpu_worker = Worker(self.queue, "gpu", {"h3": gpu_handler})
        thread = threading.Thread(target=gpu_worker.run_once)
        thread.start()
        try:
            self.assertTrue(gpu_entered.wait(5))
            self.assertEqual("running", self.queue.get_job(gpu["id"])["state"])
            claimed = [self.queue.claim("cpu"), Queue(self.store).claim("cpu")]
            self.assertEqual({job["id"] for job in cpu_jobs[:2]}, {job["id"] for job in claimed})
            self.assertIsNone(self.queue.claim("cpu"))
            self.queue.finish(claimed[0]["id"], {"text": "ok"}, claimed[0]["claim_token"])
            self.assertIsNotNone(self.queue.claim("cpu"))
        finally:
            gpu_release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual("succeeded", self.queue.get_job(gpu["id"])["state"])

    def test_restart_preserves_queue_and_ambiguous_submission_requires_reconcile(self):
        queued = self.enqueue()
        ambiguous = self.enqueue()
        self.queue.claim("gpu")
        reopened = Queue(Store(self.store.db_path))
        self.assertEqual([], reopened.recover())
        self.expire(queued["id"])
        recovered = reopened.recover()
        self.assertEqual([queued["id"]], [j["id"] for j in recovered])
        self.assertEqual("needs_reconcile", reopened.get_job(queued["id"])["state"])
        self.assertIsNone(reopened.claim("gpu"))
        self.assertEqual("queued", reopened.get_job(ambiguous["id"])["state"])

    def test_external_id_survives_and_recovery_keeps_gpu_slot(self):
        job = self.enqueue()
        claimed = self.queue.claim("gpu")
        self.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.queue.record_external(job["id"], "remote-123", claimed["claim_token"])
        self.assertEqual("submitting", self.queue.get_job(job["id"])["state"])
        start = datetime.now(timezone.utc).isoformat()
        self.queue.record_execution_started(job["id"], start, claimed["claim_token"])
        self.assertEqual("running", self.queue.get_job(job["id"])["state"])
        reopened = Queue(Store(self.store.db_path))
        self.expire(job["id"])
        reopened.recover()
        self.assertEqual("needs_reconcile", reopened.get_job(job["id"])["state"])
        self.assertEqual("remote-123", reopened.get_job(job["id"])["external_id"])
        self.assertEqual("gpu_execution", reopened.get_job(job["id"])["phases"][-1]["phase"])
        self.assertIsNone(reopened.get_job(job["id"])["phases"][-1]["exited_at"])
        self.assertIsNone(reopened.claim("gpu"))
        reconciled = reopened.get_job(job["id"])
        reopened.record_execution_started(job["id"], start, reconciled["claim_token"])
        self.assertEqual("running", reopened.get_job(job["id"])["state"])
        self.assertEqual(1, len([phase for phase in reopened.get_job(job["id"])["phases"] if phase["phase"] == "gpu_execution"]))

    def test_reconciled_external_id_can_be_recorded_without_retransmitting(self):
        job = self.enqueue()
        self.queue.claim("gpu")
        self.expire(job["id"])
        self.queue.recover()
        reconciled = self.queue.get_job(job["id"])
        self.queue.record_external(job["id"], "found-existing", reconciled["claim_token"])
        self.assertEqual("found-existing", self.queue.get_job(job["id"])["external_id"])
        self.assertEqual("needs_reconcile", self.queue.get_job(job["id"])["state"])
        self.queue.record_execution_started(job["id"], datetime.now(timezone.utc).isoformat(), reconciled["claim_token"])
        self.assertEqual("running", self.queue.get_job(job["id"])["state"])
        self.queue.finish(job["id"], {"candidate": "found"}, reconciled["claim_token"], execution_finished_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual("succeeded", self.queue.get_job(job["id"])["state"])

    def test_retry_preserves_history_revision_classification_and_requires_new_plan_after_two_failures(self):
        first = self.enqueue()
        self.store.save_shot(self.episode["id"], {"id": self.shot["id"], "action": "new action"}, expected_revision=1)
        claimed = self.queue.claim("gpu")
        self.gpu_started(claimed)
        self.queue.fail(first["id"], "character", "脸不一致", claimed["claim_token"], execution_finished_at=datetime.now(timezone.utc).isoformat())
        second = self.queue.retry(first["id"], {"plan_revision": 1, "strategy": "a", "seed": 2})
        self.assertEqual(first["id"], second["retry_of_id"])
        self.assertEqual(first["source_revision"], second["source_revision"])
        self.assertEqual(first["source_snapshot"], second["source_snapshot"])
        self.assertEqual("", second["source_snapshot"]["shot"]["action"])
        self.assertEqual("character", second["retry_classification"])
        claimed = self.queue.claim("gpu")
        self.gpu_started(claimed)
        self.queue.fail(second["id"], "character", "脸仍不一致", claimed["claim_token"], execution_finished_at=datetime.now(timezone.utc).isoformat())
        with self.assertRaises(DomainError) as raised:
            self.queue.retry(second["id"], {"plan_revision": 1, "strategy": "a", "seed": 3})
        self.assertEqual("retry_plan_required", raised.exception.code)
        changed = self.queue.retry(second["id"], {"plan_revision": 2, "strategy": "b", "seed": 3})
        self.assertEqual(2, changed["payload"]["plan_revision"])
        self.assertEqual(2, len([j for j in self.queue.list_jobs() if j["state"] == "failed"]))
        claimed = self.queue.claim("gpu")
        self.gpu_started(claimed)
        self.queue.fail(changed["id"], "character", "仍需修改", claimed["claim_token"], execution_finished_at=datetime.now(timezone.utc).isoformat())
        with self.assertRaises(DomainError) as raised:
            self.queue.retry(changed["id"], {"plan_revision": 1, "strategy": "a", "seed": 9})
        self.assertEqual("retry_plan_required", raised.exception.code)

    def test_scope_validation_is_atomic_and_cancel_is_only_queued(self):
        other_project = self.store.create_project("乙")
        other_episode = self.store.create_episode(other_project["id"], "乙一")
        with self.assertRaises(DomainError) as raised:
            self.queue.enqueue({"project_id": self.project["id"], "episode_id": other_episode["id"]}, "text", {})
        self.assertEqual("ownership_conflict", raised.exception.code)
        self.assertEqual([], self.queue.list_jobs())
        queued = self.enqueue()
        self.queue.cancel_queued(queued["id"])
        self.assertEqual("cancelled", self.queue.get_job(queued["id"])["state"])
        running = self.enqueue()
        self.queue.claim("gpu")
        with self.assertRaises(DomainError):
            self.queue.cancel_queued(running["id"])

    def test_phase_timers_are_measured_and_worker_failure_is_recorded(self):
        job = self.enqueue("text")

        def broken(kind, item):
            raise ValueError("bad input")

        worker = Worker(self.queue, "cpu", {"text": broken})
        self.assertTrue(worker.run_once())
        result = self.queue.get_job(job["id"])
        self.assertEqual("failed", result["state"])
        self.assertEqual("ValueError", result["failure_code"])
        self.assertGreaterEqual(result["phases"][0]["duration_ms"], 0)
        self.assertIsNotNone(result["phases"][1]["exited_at"])

    def test_expired_claim_is_fenced_after_recovery_and_new_claim(self):
        job = self.enqueue("probe", {"plan_revision": 1, "strategy": "a", "idempotent_local": True})
        old = self.queue.claim("cpu")
        other = Queue(Store(self.store.db_path))
        self.assertEqual([], other.recover())
        self.expire(job["id"])
        other.recover()
        fresh = other.claim("cpu")
        self.assertEqual(job["id"], fresh["id"])
        self.assertNotEqual(old["claim_token"], fresh["claim_token"])
        for action in (
            lambda: self.queue.renew_lease(job["id"], old["claim_token"]),
            lambda: self.queue.finish(job["id"], {}, old["claim_token"]),
            lambda: self.queue.fail(job["id"], "old", "late", old["claim_token"]),
        ):
            with self.assertRaises(DomainError):
                action()
        self.assertEqual("running", other.get_job(job["id"])["state"])
        other.finish(job["id"], {"ok": True}, fresh["claim_token"])

    def test_gpu_cannot_report_success_before_external_execution_evidence(self):
        job = self.enqueue()
        claimed = self.queue.claim("gpu")
        with self.assertRaises(DomainError):
            self.queue.finish(job["id"], {"candidate": "fake"}, claimed["claim_token"], execution_finished_at=datetime.now(timezone.utc).isoformat())
        self.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        self.queue.record_external(job["id"], "external-1", claimed["claim_token"])
        with self.assertRaises(DomainError):
            self.queue.finish(job["id"], {"candidate": "fake"}, claimed["claim_token"], execution_finished_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual("submitting", self.queue.get_job(job["id"])["state"])

    def test_gpu_phase_uses_observed_provider_times_and_rejects_old_token(self):
        job = self.enqueue()
        old = self.queue.claim("gpu")
        self.expire(job["id"])
        self.queue.recover()
        current = self.queue.get_job(job["id"])
        with self.assertRaises(DomainError) as raised:
            self.queue.record_external(job["id"], "late-old", old["claim_token"])
        self.assertEqual("stale_claim", raised.exception.code)
        self.queue.record_external(job["id"], "observed-1", current["claim_token"])
        too_early = (datetime.fromisoformat(job["created_at"]) - timedelta(seconds=1)).isoformat()
        with self.assertRaises(DomainError):
            self.queue.record_execution_started(job["id"], too_early, current["claim_token"])
        started = datetime.now(timezone.utc).isoformat()
        self.queue.record_execution_started(job["id"], started, current["claim_token"])
        self.queue.record_execution_started(job["id"], started, current["claim_token"])
        with self.assertRaises(DomainError):
            self.queue.record_execution_started(job["id"], started, old["claim_token"])
        before_end = (datetime.fromisoformat(started) - timedelta(microseconds=1)).isoformat()
        with self.assertRaises(DomainError):
            self.queue.finish(job["id"], {}, current["claim_token"], execution_finished_at=before_end)
        finished = datetime.now(timezone.utc).isoformat()
        self.queue.finish(job["id"], {"candidate": "technical"}, current["claim_token"], execution_finished_at=finished)
        final = self.queue.get_job(job["id"])
        self.assertEqual("succeeded", final["state"])
        self.assertEqual(started, final["phases"][-1]["entered_at"])
        self.assertEqual(finished, final["phases"][-1]["exited_at"])

    def test_expired_cpu_text_requires_reconcile_but_local_probe_requeues(self):
        text_job = self.enqueue("text")
        probe_job = self.enqueue("probe", {"plan_revision": 1, "strategy": "a", "idempotent_local": True})
        first = self.queue.claim("cpu")
        second = self.queue.claim("cpu")
        self.expire(first["id"])
        self.expire(second["id"])
        self.queue.recover()
        self.assertEqual("needs_reconcile", self.queue.get_job(text_job["id"])["state"])
        self.assertEqual("queued", self.queue.get_job(probe_job["id"])["state"])
        interrupted = self.queue.get_job(probe_job["id"])["phases"][1]
        self.assertIsNone(interrupted["exited_at"])
        self.assertIsNone(interrupted["duration_ms"])
        self.assertIsNotNone(interrupted["interrupted_at"])

    def test_cpu_probe_without_idempotence_declaration_requires_reconcile(self):
        job = self.enqueue("probe")
        self.queue.claim("cpu")
        self.expire(job["id"])
        self.queue.recover()
        self.assertEqual("needs_reconcile", self.queue.get_job(job["id"])["state"])

    def test_retry_count_is_scoped_to_episode_revision_and_episode_job(self):
        first = self.queue.enqueue({"project_id": self.project["id"], "episode_id": self.episode["id"]}, "text", {"plan_revision": 1, "strategy": "a"})
        claimed = self.queue.claim("cpu")
        self.queue.fail(first["id"], "text", "bad", claimed["claim_token"])
        second = self.queue.retry(first["id"])
        claimed = self.queue.claim("cpu")
        self.queue.fail(second["id"], "text", "bad", claimed["claim_token"])
        other_project = self.store.create_project("乙")
        other_episode = self.store.create_episode(other_project["id"], "乙一")
        other_job = self.queue.enqueue({"project_id": other_project["id"], "episode_id": other_episode["id"]}, "text", {"plan_revision": 1, "strategy": "a"})
        claimed = self.queue.claim("cpu")
        self.queue.fail(other_job["id"], "text", "bad", claimed["claim_token"])
        other_retry = self.queue.retry(other_job["id"])
        self.assertEqual("queued", other_retry["state"])
        self.queue.cancel_queued(other_retry["id"])
        self.store.update_episode(self.episode["id"], {"script": "new source"}, expected_revision=1)
        newer = self.queue.enqueue({"project_id": self.project["id"], "episode_id": self.episode["id"]}, "text", {"plan_revision": 1, "strategy": "a"})
        claimed = self.queue.claim("cpu")
        self.queue.fail(newer["id"], "text", "bad", claimed["claim_token"])
        self.assertEqual("queued", self.queue.retry(newer["id"])["state"])

    def test_shot_retry_count_does_not_cross_episode_revision(self):
        first = self.enqueue("text")
        claimed = self.queue.claim("cpu")
        self.queue.fail(first["id"], "dialogue", "bad", claimed["claim_token"])
        second = self.queue.retry(first["id"])
        claimed = self.queue.claim("cpu")
        self.queue.fail(second["id"], "dialogue", "bad", claimed["claim_token"])
        self.store.update_episode(self.episode["id"], {"script": "changed"}, expected_revision=1)
        newer = self.enqueue("text")
        claimed = self.queue.claim("cpu")
        self.queue.fail(newer["id"], "dialogue", "bad", claimed["claim_token"])
        self.assertEqual("queued", self.queue.retry(newer["id"])["state"])

    def test_gpu_handler_error_after_start_stays_reserved_for_reconciliation(self):
        first = self.enqueue()
        self.enqueue()

        def broken(kind, job):
            self.gpu_started(job)
            raise RuntimeError("poll failed")

        self.assertTrue(Worker(self.queue, "gpu", {"h3": broken}).run_once())
        uncertain = self.queue.get_job(first["id"])
        self.assertEqual("needs_reconcile", uncertain["state"])
        self.assertEqual("RuntimeError", uncertain["failure_code"])
        self.assertIsNotNone(uncertain["claim_token"])
        self.assertIsNotNone(uncertain["lease_until"])
        self.assertIsNone(uncertain["phases"][-1]["exited_at"])
        self.assertIsNone(uncertain["phases"][-1]["duration_ms"])
        self.assertIsNone(self.queue.claim("gpu"))

    def test_gpu_missing_finish_event_stays_reserved(self):
        first = self.enqueue()
        self.enqueue()

        def missing_end(kind, job):
            self.gpu_started(job)
            return {"candidate": "not yet verified"}

        self.assertTrue(Worker(self.queue, "gpu", {"h3": missing_end}).run_once())
        uncertain = self.queue.get_job(first["id"])
        self.assertEqual("needs_reconcile", uncertain["state"])
        self.assertEqual("execution_unverified", uncertain["failure_code"])
        self.assertIsNone(uncertain["phases"][-1]["exited_at"])
        self.assertIsNone(self.queue.claim("gpu"))

    def test_gpu_ambiguous_submission_without_response_stays_reserved(self):
        first = self.enqueue()
        self.enqueue()

        def lost_response(kind, job):
            self.queue.mark_submission_attempt(job["id"], job["claim_token"])
            raise TimeoutError("submit response lost")

        self.assertTrue(Worker(self.queue, "gpu", {"h3": lost_response}).run_once())
        uncertain = self.queue.get_job(first["id"])
        self.assertEqual("needs_reconcile", uncertain["state"])
        self.assertIsNone(uncertain["external_id"])
        self.assertEqual("preparation", uncertain["phases"][-1]["phase"])
        self.assertIsNone(uncertain["phases"][-1]["exited_at"])
        self.assertIsNone(self.queue.claim("gpu"))

    def test_gpu_known_preflight_failure_releases_without_execution(self):
        first = self.enqueue()
        second = self.enqueue()
        claimed = self.queue.claim("gpu")
        self.queue.fail_preflight(first["id"], "missing_workflow", "not configured", claimed["claim_token"])
        self.assertEqual("failed", self.queue.get_job(first["id"])["state"])
        self.assertEqual("preparation", self.queue.get_job(first["id"])["phases"][-1]["phase"])
        self.assertEqual(second["id"], self.queue.claim("gpu")["id"])

    def test_worker_accepts_handler_recorded_preflight_failure(self):
        first = self.enqueue()

        def preflight(kind, job):
            self.queue.fail_preflight(job["id"], "missing_workflow", "not configured", job["claim_token"])
            return {}

        self.assertTrue(Worker(self.queue, "gpu", {"h3": preflight}).run_once())
        self.assertEqual("failed", self.queue.get_job(first["id"])["state"])

    def test_gpu_observed_failure_uses_provider_end_and_releases(self):
        first = self.enqueue()
        second = self.enqueue()
        claimed = self.queue.claim("gpu")
        self.gpu_started(claimed)
        ended = datetime.now(timezone.utc).isoformat()
        self.queue.fail(first["id"], "character", "face mismatch", claimed["claim_token"], execution_finished_at=ended)
        failed = self.queue.get_job(first["id"])
        self.assertEqual("failed", failed["state"])
        self.assertEqual(ended, failed["phases"][-1]["exited_at"])
        self.assertEqual(second["id"], self.queue.claim("gpu")["id"])

    def test_submitted_gpu_cannot_be_labeled_preflight_failure(self):
        job = self.enqueue()
        claimed = self.queue.claim("gpu")
        self.queue.mark_submission_attempt(job["id"], claimed["claim_token"])
        with self.assertRaises(DomainError):
            self.queue.fail_preflight(job["id"], "bad", "after submit", claimed["claim_token"])
        self.assertEqual("submitting", self.queue.get_job(job["id"])["state"])

    def test_provider_rejected_submission_releases_at_observed_time(self):
        first = self.enqueue()
        second = self.enqueue()
        claimed = self.queue.claim("gpu")
        self.queue.mark_submission_attempt(first["id"], claimed["claim_token"])
        rejected_at = datetime.now(timezone.utc).isoformat()
        self.queue.reject_submission(first["id"], "rejected", "provider declined", rejected_at, claimed["claim_token"])
        failed = self.queue.get_job(first["id"])
        self.assertEqual("failed", failed["state"])
        self.assertEqual(rejected_at, failed["phases"][-1]["exited_at"])
        self.assertEqual(second["id"], self.queue.claim("gpu")["id"])

    def test_queue_upgrades_prior_local_schema_without_losing_jobs(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite"
        legacy_store = Store(legacy_path)
        with legacy_store.connection() as conn:
            conn.executescript("""
                CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT, episode_id TEXT, shot_id TEXT,
                  kind TEXT, resource TEXT, state TEXT, payload TEXT, result TEXT, external_id TEXT,
                  retry_of_id TEXT, source_revision INTEGER, source_snapshot TEXT, retry_classification TEXT,
                  failure_code TEXT, failure_message TEXT, created_at TEXT, updated_at TEXT, lease_until TEXT);
                CREATE TABLE job_phases (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,
                  phase TEXT, entered_at TEXT, exited_at TEXT, duration_ms INTEGER);
            """)
        upgraded = Queue(legacy_store)
        project = legacy_store.create_project("old")
        episode = legacy_store.create_episode(project["id"], "old")
        job = upgraded.enqueue({"project_id": project["id"], "episode_id": episode["id"]}, "probe", {})
        self.assertIn("claim_token", upgraded.claim("cpu"))
        self.assertEqual(job["id"], upgraded.list_jobs()[0]["id"])

    def test_legacy_submitted_gpu_releases_only_after_provider_rejection_evidence(self):
        legacy_store = Store(Path(self.temp.name) / "legacy_gpu.sqlite")
        project = legacy_store.create_project("old")
        episode = legacy_store.create_episode(project["id"], "old")
        old_id = str(uuid4())
        created = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with legacy_store.connection() as conn:
            conn.executescript("""
                CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT, episode_id TEXT, shot_id TEXT,
                  kind TEXT, resource TEXT, state TEXT, payload TEXT, result TEXT, external_id TEXT,
                  retry_of_id TEXT, source_revision INTEGER, source_snapshot TEXT, retry_classification TEXT,
                  failure_code TEXT, failure_message TEXT, created_at TEXT, updated_at TEXT, lease_until TEXT);
                CREATE TABLE job_phases (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,
                  phase TEXT, entered_at TEXT, exited_at TEXT, duration_ms INTEGER);
            """)
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                old_id, project["id"], episode["id"], None, "h3", "gpu", "running", "{}", None,
                "legacy-provider-id", None, 1, json.dumps({"episode": episode, "shot": None}), None,
                None, None, created, created, expired,
            ))
            conn.execute("INSERT INTO job_phases(job_id,phase,entered_at) VALUES (?,?,?)", (old_id, "gpu_execution", created))
            conn.commit()
        queue = Queue(legacy_store)
        next_job = queue.enqueue({"project_id": project["id"], "episode_id": episode["id"]}, "h3", {})
        recovered = queue.recover()
        self.assertEqual([old_id], [job["id"] for job in recovered])
        self.assertIsNone(recovered[0]["submission_attempted_at"])
        self.assertEqual("needs_reconcile", recovered[0]["state"])
        queue.fail(old_id, "timeout", "no terminal evidence", recovered[0]["claim_token"])
        self.assertIsNone(queue.claim("gpu"))
        self.assertIsNone(queue.get_job(old_id)["phases"][-1]["exited_at"])
        token = queue.get_job(old_id)["claim_token"]
        rejected_at = datetime.now(timezone.utc).isoformat()
        queue.reject_submission(old_id, "rejected", "provider confirmed no execution", rejected_at, token)
        self.assertEqual("failed", queue.get_job(old_id)["state"])
        self.assertIsNone(queue.get_job(old_id)["phases"][-1]["exited_at"])
        self.assertIsNone(queue.get_job(old_id)["phases"][-1]["duration_ms"])
        self.assertEqual(rejected_at, queue.get_job(old_id)["phases"][-1]["interrupted_at"])
        self.assertEqual(next_job["id"], queue.claim("gpu")["id"])


if __name__ == "__main__":
    unittest.main()
