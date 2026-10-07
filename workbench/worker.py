"""Local worker for registered task handlers; no external service assumptions."""

import threading

from .domain import DomainError, require
from .queue import KINDS, LIMITS


class ProviderFailure(DomainError):
    """Observed provider outcome used by Worker to preserve GPU fencing."""

    def __init__(self, code: str, message: str, outcome: str, observed_at: str | None = None):
        super().__init__(code, 502, message)
        require(outcome in ("preflight", "rejected", "executed_failure", "uncertain"),
                "invalid_outcome", 500, "外部结果类别无效")
        self.outcome = outcome
        self.observed_at = observed_at


class Worker:
    def __init__(self, queue, resource: str, handlers: dict):
        require(resource in LIMITS, "invalid_resource", 400, "计算资源不支持")
        require(isinstance(handlers, dict) and all(kind in KINDS and KINDS[kind] == resource and callable(fn)
                                                   for kind, fn in handlers.items()), "invalid_handler", 400, "任务处理器无效")
        self.queue = queue
        self.resource = resource
        self.handlers = handlers
        self._stop = threading.Event()

    def run_once(self) -> bool:
        job = self.queue.claim(self.resource)
        if job is None:
            return False
        done = threading.Event()

        def heartbeat():
            while not done.wait(20):
                try:
                    self.queue.renew_lease(job["id"], job["claim_token"])
                except DomainError:
                    return

        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        try:
            handler = self.handlers.get(job["kind"])
            if handler is None:
                raise DomainError("handler_missing", 503, "任务处理器未配置")
            result = handler(job["kind"], job)
            require(isinstance(result, dict), "invalid_result", 500, "任务处理器结果必须是对象")
            self.queue.finish(job["id"], result, job["claim_token"],
                              execution_finished_at=result.get("execution_finished_at") if self.resource == "gpu" else None)
        except Exception as error:
            code = error.code if isinstance(error, DomainError) else type(error).__name__
            try:
                if self.resource == "gpu" and isinstance(error, ProviderFailure):
                    if error.outcome == "preflight":
                        self.queue.fail_preflight(job["id"], code, str(error), job["claim_token"])
                    elif error.outcome == "rejected" and error.observed_at:
                        self.queue.reject_submission(job["id"], code, str(error), error.observed_at, job["claim_token"])
                    elif error.outcome == "executed_failure" and error.observed_at:
                        self.queue.fail(job["id"], code, str(error), job["claim_token"],
                                        execution_finished_at=error.observed_at)
                    else:
                        self.queue.fail(job["id"], code, str(error), job["claim_token"])
                else:
                    self.queue.fail(job["id"], code, str(error), job["claim_token"])
            except DomainError as stale:
                if stale.code not in ("stale_claim", "invalid_timestamp", "invalid_state", "execution_unverified"):
                    raise
                if stale.code != "stale_claim":
                    self.queue.fail(job["id"], code, str(error), job["claim_token"])
        finally:
            done.set()
            pulse.join()
        return True

    def run_forever(self, idle_seconds: float = 0.5) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(idle_seconds)

    def stop(self) -> None:
        """Stop claiming new work; let the active handler finish naturally."""
        self._stop.set()
