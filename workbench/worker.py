"""Local worker for registered task handlers; no external service assumptions."""

import threading

from .domain import DomainError, require
from .queue import KINDS, LIMITS


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
                    self.queue.renew_lease(job["id"])
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
            self.queue.finish(job["id"], result)
        except Exception as error:
            code = error.code if isinstance(error, DomainError) else type(error).__name__
            self.queue.fail(job["id"], code, str(error))
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
