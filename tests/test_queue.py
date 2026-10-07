"""Persistent scheduling contracts exercised with a real SQLite database."""

import tempfile
import threading
import unittest
from pathlib import Path

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
            gpu_entered.set()
            self.assertTrue(gpu_release.wait(5))
            return {"candidate": "local"}

        gpu_worker = Worker(self.queue, "gpu", {"h3": gpu_handler})
        thread = threading.Thread(target=gpu_worker.run_once)
        thread.start()
        try:
            self.assertTrue(gpu_entered.wait(5))
            self.assertEqual("submitting", self.queue.get_job(gpu["id"])["state"])
            claimed = [self.queue.claim("cpu"), Queue(self.store).claim("cpu")]
            self.assertEqual({job["id"] for job in cpu_jobs[:2]}, {job["id"] for job in claimed})
            self.assertIsNone(self.queue.claim("cpu"))
            self.queue.finish(claimed[0]["id"], {"text": "ok"})
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
        recovered = reopened.recover()
        self.assertEqual([queued["id"]], [j["id"] for j in recovered])
        self.assertEqual("needs_reconcile", reopened.get_job(queued["id"])["state"])
        self.assertIsNone(reopened.claim("gpu"))
        self.assertEqual("queued", reopened.get_job(ambiguous["id"])["state"])

    def test_external_id_survives_and_recovery_keeps_gpu_slot(self):
        job = self.enqueue()
        self.queue.claim("gpu")
        self.queue.record_external(job["id"], "remote-123")
        self.assertEqual("running", self.queue.get_job(job["id"])["state"])
        reopened = Queue(Store(self.store.db_path))
        reopened.recover()
        self.assertEqual("needs_reconcile", reopened.get_job(job["id"])["state"])
        self.assertEqual("remote-123", reopened.get_job(job["id"])["external_id"])
        self.assertIsNone(reopened.claim("gpu"))

    def test_reconciled_external_id_can_be_recorded_without_retransmitting(self):
        job = self.enqueue()
        self.queue.claim("gpu")
        self.queue.recover()
        self.queue.record_external(job["id"], "found-existing")
        self.assertEqual("found-existing", self.queue.get_job(job["id"])["external_id"])
        self.assertEqual("running", self.queue.get_job(job["id"])["state"])
        self.queue.finish(job["id"], {"candidate": "found"})
        self.assertEqual("succeeded", self.queue.get_job(job["id"])["state"])

    def test_retry_preserves_history_revision_classification_and_requires_new_plan_after_two_failures(self):
        first = self.enqueue()
        self.store.save_shot(self.episode["id"], {"id": self.shot["id"], "action": "new action"}, expected_revision=1)
        self.queue.claim("gpu")
        self.queue.fail(first["id"], "character", "脸不一致")
        second = self.queue.retry(first["id"], {"plan_revision": 1, "strategy": "a", "seed": 2})
        self.assertEqual(first["id"], second["retry_of_id"])
        self.assertEqual(first["source_revision"], second["source_revision"])
        self.assertEqual(first["source_snapshot"], second["source_snapshot"])
        self.assertEqual("", second["source_snapshot"]["shot"]["action"])
        self.assertEqual("character", second["retry_classification"])
        self.queue.claim("gpu")
        self.queue.fail(second["id"], "character", "脸仍不一致")
        with self.assertRaises(DomainError) as raised:
            self.queue.retry(second["id"], {"plan_revision": 1, "strategy": "a", "seed": 3})
        self.assertEqual("retry_plan_required", raised.exception.code)
        changed = self.queue.retry(second["id"], {"plan_revision": 2, "strategy": "b", "seed": 3})
        self.assertEqual(2, changed["payload"]["plan_revision"])
        self.assertEqual(2, len([j for j in self.queue.list_jobs() if j["state"] == "failed"]))
        self.queue.claim("gpu")
        self.queue.fail(changed["id"], "character", "仍需修改")
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


if __name__ == "__main__":
    unittest.main()
