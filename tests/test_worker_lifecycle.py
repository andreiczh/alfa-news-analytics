"""An actual parent-death regression: no API calls and no abandoned worker."""

import os
import signal
import subprocess
import sys
import time


def test_worker_exits_after_its_parent_stops(service, tmp_path):
    marker = tmp_path / "worker-exited"
    pidfile = tmp_path / "child-pid"
    ready = tmp_path / "worker-ready"
    child_code = """import asyncio\nfrom pathlib import Path\nfrom alfa_agent.worker import work\nPath(MARKER_READY).write_text('ready')\ntry:\n asyncio.run(work())\nexcept asyncio.CancelledError:\n pass\nfinally:\n Path(MARKER_EXIT).write_text('stopped')\n""".replace(
        "MARKER_READY", repr(str(ready))
    ).replace("MARKER_EXIT", repr(str(marker)))
    parent_code = """import os,subprocess,sys,time\nfrom pathlib import Path\nchild=subprocess.Popen([sys.executable,'-c',CHILD_CODE],env={**os.environ,'ALFA_WORKER_PARENT_PID':str(os.getpid())})\nPath(PID_FILE).write_text(str(child.pid))\ntime.sleep(30)\n""".replace(
        "CHILD_CODE", repr(child_code)
    ).replace("PID_FILE", repr(str(pidfile)))
    env = {
        **os.environ,
        "ALFA_PROJECT_DIR": str(service.settings.root),
        "ALFA_CASE_DATA_DIR": str(tmp_path / "not-connected"),
    }
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists()
        child_pid = int(pidfile.read_text())
        parent.terminate()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.read_text() == "stopped"
        assert not service.store.attempts("anything")
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)
        if child_pid and not marker.exists():
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
