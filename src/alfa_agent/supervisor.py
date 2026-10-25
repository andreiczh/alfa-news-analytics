"""Restart the worker after a crash; uncertain paid calls still require manual retry."""

from collections import deque
import logging
import os
import subprocess
import sys
import threading
import time


class WorkerSupervisor:
    def __init__(self):
        self.stopping = threading.Event()
        self.restarts = 0
        self._start()
        self.thread = threading.Thread(target=self._watch, name="alfa-worker-supervisor", daemon=True)
        self.thread.start()

    def _start(self):
        self.child = subprocess.Popen(
            [sys.executable, "-m", "alfa_agent", "worker"],
            env={**os.environ, "ALFA_WORKER_PARENT_PID": str(os.getpid())},
        )

    def _watch(self):
        attempts = deque()
        while not self.stopping.wait(1):
            if self.child.poll() is None:
                continue
            now = time.monotonic()
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
            if len(attempts) >= 5:
                continue
            attempts.append(now)
            self.restarts += 1
            logging.getLogger("alfa.operations").warning("worker_restarted_after_exit")
            self._start()

    def poll(self):
        return self.child.poll()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=2)
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=3)
