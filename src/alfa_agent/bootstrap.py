"""Install the reviewed initial state once; never overwrite an existing workspace."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil


def prepare_state(settings):
    target = settings.state_path
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    seed = settings.root / "deploy/seed"
    manifest = seed / "manifest.json"
    if not manifest.is_file():
        return
    with (target / "bootstrap.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if settings.db_path.exists():
            return
        files = json.loads(manifest.read_text())["files"]
        for relative, digest in files.items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or relative not in {
                "agent.sqlite3", *[str(Path("uploads") / p.name) for p in (seed / "uploads").glob("*")]
            }:
                raise ValueError("invalid_seed_path")
            if hashlib.sha256((seed / path).read_bytes()).hexdigest() != digest:
                raise ValueError("seed_integrity_error")
        # Publish the database last so a failed first boot can safely be retried.
        for relative in sorted(files, key=lambda name: name == "agent.sqlite3"):
            source, destination = seed / relative, target / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(destination.name + ".bootstrap")
            shutil.copyfile(source, temporary)
            temporary.chmod(0o600)
            os.replace(temporary, destination)
