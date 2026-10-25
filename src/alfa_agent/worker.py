import asyncio
import fcntl
import os
import hashlib
import logging
from starlette.concurrency import run_in_threadpool

from .engine import Engine
from .gateway import LiveGateway
from .service import Service
from .settings import Settings
from .ingestion import DocumentProcessor
from .research import ResearchEngine
from .company_dialogue import ContextEngine
from .storage import new_id


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
    database_lock, lock_key = None, None
    if service.store.pool:
        database_lock = service.store.pool.getconn()
        lock_key = int(
            hashlib.sha256(("alfa-worker:" + settings.database_schema).encode()).hexdigest()[:15], 16
        )
        acquired = database_lock.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,)).fetchone()[0]
        database_lock.commit()
        if not acquired:
            service.store.pool.putconn(database_lock)
            service.corpus.close()
            service.store.close()
            if monitor:
                monitor.cancel()
            lock.close()
            return None
    worker_id = new_id("worker")

    async def heartbeat():
        while True:
            try:
                await run_in_threadpool(service.operations.heartbeat, worker_id)
            except Exception:
                logging.getLogger("alfa.operations").error("worker_heartbeat_failed")
            await asyncio.sleep(5)

    await run_in_threadpool(service.operations.heartbeat, worker_id)
    heartbeat_task = asyncio.create_task(heartbeat())
    gateway = LiveGateway(settings, service.store)
    engine = Engine(settings, service.store, service.corpus, gateway)
    processor = DocumentProcessor(service.workspace, gateway)
    research = ResearchEngine(service.research, gateway)
    context = ContextEngine(service.context_dialogue, gateway)
    # Public IO has no model calls and must not block the paid research queue.
    news_task = asyncio.create_task(service.news.work_loop())
    try:
        while True:
            job = await context.once()
            if not job:
                job = await processor.once()
            if not job:
                job = await research.once()
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
        news_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(news_task, return_exceptions=True)
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        if monitor:
            monitor.cancel()
        await gateway.close()
        service.corpus.close()
        if database_lock:
            database_lock.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            database_lock.commit()
            service.store.pool.putconn(database_lock)
        service.store.close()
        lock.close()


if __name__ == "__main__":
    asyncio.run(work())
