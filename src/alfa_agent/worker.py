import asyncio
import fcntl
import os

from .engine import Engine
from .gateway import LiveGateway
from .service import Service
from .settings import Settings
from .ingestion import DocumentProcessor


async def work(once=False, settings=None):
    settings = settings or Settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    lock = (settings.db_path.parent / "worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    main_task = asyncio.current_task()
    owner = os.environ.get("ALFA_WORKER_PARENT_PID")

    async def watch_parent():
        while True:
            if owner and os.getppid() != int(owner):
                main_task.cancel()
                return
            await asyncio.sleep(0.5)

    monitor = asyncio.create_task(watch_parent()) if owner else None
    service = Service(settings)
    gateway = LiveGateway(settings, service.store)
    engine = Engine(settings, service.store, service.corpus, gateway)
    processor = DocumentProcessor(service.workspace, gateway)
    try:
        while True:
            job = await processor.once()
            if not job:
                job = await engine.once()
            if once:
                return job
            if not job:
                await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        if not owner or os.getppid() == int(owner):
            raise
    finally:
        if monitor:
            monitor.cancel()
        await gateway.close()
        service.corpus.close()
        lock.close()


if __name__ == "__main__":
    asyncio.run(work())
