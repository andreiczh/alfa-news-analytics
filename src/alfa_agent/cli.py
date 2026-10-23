from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .engine import Engine
from .gateway import LiveGateway
from .reporting import write_report
from .service import CreateRun, Service
from .settings import Settings, read_key
from .storage import new_id


async def run_once(args, settings):
    service = Service(settings)
    item = service.create(
        CreateRun(company_id=args.company, scenario_delta_pp=args.delta, horizon_days=args.days),
        new_id("cli"),
    )
    gateway = LiveGateway(settings, service.store)
    try:
        engine = Engine(settings, service.store, service.corpus, gateway)
        deadline = time.monotonic() + settings.active_seconds * 2
        while time.monotonic() < deadline:
            status = service.get(item["run_id"])["status"]
            if status not in {"QUEUED", "RUNNING"}:
                break
            if not await engine.once(item["run_id"]):
                await asyncio.sleep(0.5)
        result = service.get(item["run_id"])
        if result["result"]:
            path = Path(args.output)
            write_report(result, path)
            path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in ("run_id", "status", "error_code", "llm_calls", "tool_calls", "replans")
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if result["question"]:
            print(result["question"]["question"])
        return 0 if result["result"] else 2
    finally:
        await gateway.close()
        service.corpus.close()


def main():
    parser = argparse.ArgumentParser(description="Собственный агент новостной аналитики")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="UI + отдельный worker")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    serve.add_argument("--host", default=None)
    commands.add_parser("worker")
    commands.add_parser("build-case-index", help="Подготовить неизменяемый индекс CSV при сборке")
    run = commands.add_parser("run", help="Один живой прогон и HTML-результат")
    run.add_argument("--company", default="demo_floating")
    run.add_argument("--delta", default="-1")
    run.add_argument("--days", type=int, default=90)
    run.add_argument("--output", default="reports/live-insight.html")
    commands.add_parser("status", help="Только наличие конфигурации, без секрета")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "build-case-index":
        from .case_data import CaseData

        folder = settings.root / "deploy/case-index"
        indexed = CaseData(Settings(root=settings.root, state_dir=folder), prefer_packaged=False)
        if not indexed.available:
            raise SystemExit("Source CSVs are required to build the case index")
        with indexed.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
        print(json.dumps({"case_index": "ready", "rows": indexed.summary()["row_counts"]}))
    elif args.command == "status":
        print(
            json.dumps(
                {
                    "key_configured": bool(read_key(settings.root)),
                    "model": settings.model,
                    "db": str(settings.db_path),
                }
            )
        )
    elif args.command == "worker":
        from .worker import work

        asyncio.run(work(settings=settings))
    elif args.command == "run":
        raise SystemExit(asyncio.run(run_once(args, settings)))
    else:
        import uvicorn
        from .bootstrap import prepare_state

        prepare_state(settings)
        if settings.public_deployment and not os.environ.get("ALFA_ACCESS_PASSWORD", "").strip():
            raise SystemExit("ALFA_ACCESS_PASSWORD is required for a public deployment")

        child = subprocess.Popen(
            [sys.executable, "-m", "alfa_agent", "worker"],
            env={**os.environ, "ALFA_WORKER_PARENT_PID": str(os.getpid())},
        )
        try:
            from .api import create_app

            host = args.host or ("0.0.0.0" if settings.public_deployment else "127.0.0.1")
            app = create_app(settings)
            app.state.worker_process = child
            uvicorn.run(app, host=host, port=args.port, access_log=False)
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
