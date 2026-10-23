from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import hashlib
import json
import os
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request, File, Form, UploadFile, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .corpus import AccessDenied
from .engine import Engine
from .service import Answer, CreateRun, Service
from .settings import PACKAGE, Settings, read_key, utcnow
from .state import RuntimeState
from .domain import CompanyProfile
from .storage import Conflict, NotFound, new_id
from .workspace import UploadError, MAX_FILE_BYTES
from .access import COOKIE, SESSION_SECONDS, issue_session, session_principal, signing_key
from .identity import current_principal, create_principal, restore_principal, PUBLIC_DEMO_RUNS
from .research import ResearchRequest, ResearchAnswer


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: str = Field(pattern="^(useful|irrelevant|error)$")
    comment: str = Field(default="", max_length=500)


class NewCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=160)


class Favorite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, max_length=100)


class CompanyContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=5000)
    website: str = Field(default="", max_length=300)


class ResearchFavorite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    saved: bool


def create_app(settings=None):
    settings = settings or Settings()
    access_password = os.environ.get("ALFA_ACCESS_PASSWORD", "").strip()
    if settings.public_deployment and len(access_password) < 16:
        raise ValueError("public_deployment_requires_strong_access_password")
    session_key = signing_key(settings, access_password) if access_password else ""

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(settings)
        yield
        app.state.service.corpus.close()

    app = FastAPI(title="Пульс бизнеса — прототип", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.token = (
        hmac.new(session_key.encode(), b"alfa-page-csrf-v3", hashlib.sha256).hexdigest()
        if access_password
        else secrets.token_urlsafe(32)
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        public_path = request.url.path in {"/api/health", "/auth/login"} or request.url.path.startswith(
            "/static/"
        )
        principal = (
            session_principal(request.cookies.get(COOKIE, ""), session_key) if access_password else None
        )
        if access_password and not public_path and principal is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "team_login_required"}, status_code=401)
            return RedirectResponse("/auth/login", status_code=303)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "origin_not_allowed"}, status_code=403)
        if request.url.path == "/auth/login" and request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdigit() or int(length) > 8192:
                return JSONResponse({"detail": "login_body_limit"}, status_code=413)
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            token = request.headers.get("x-app-token", "")
            if not hmac.compare_digest(token, app.state.token):
                return JSONResponse({"detail": "local_session_required"}, status_code=401)
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "origin_not_allowed"}, status_code=403)
            if request.method in {"POST", "PUT"} and not request.url.path.endswith("/documents"):
                length = request.headers.get("content-length", "0")
                if not length.isdigit() or int(length) > 65536:
                    return JSONResponse({"detail": "request_size_limit"}, status_code=413)
            if request.method == "POST" and request.url.path.endswith("/documents"):
                length = request.headers.get("content-length", "")
                if not length.isdigit():
                    return JSONResponse({"detail": "content_length_required"}, status_code=411)
                if int(length) > MAX_FILE_BYTES + 65536:
                    return JSONResponse({"detail": "file_size_limit"}, status_code=413)
        marker = current_principal.set(principal)
        try:
            if principal and not public_path:
                workspace = app.state.service.workspace
                with workspace.store.connection() as db:
                    known = db.execute(
                        "SELECT 1 FROM employee_profiles WHERE user_id=?", (principal.user_id,)
                    ).fetchone()
                if not known:
                    restore_principal(workspace, principal)
            response = await call_next(request)
        finally:
            current_principal.reset(marker)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(AccessDenied)
    async def denied(request, error):
        return JSONResponse({"detail": "access_denied"}, status_code=403)

    @app.exception_handler(NotFound)
    async def missing(request, error):
        return JSONResponse({"detail": "not_found"}, status_code=404)

    @app.exception_handler(UploadError)
    async def upload_error(request, error):
        return JSONResponse(
            {"detail": str(error)},
            status_code=413 if str(error) in {"file_size_limit", "company_upload_quota"} else 422,
        )

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return (PACKAGE / "static/index.html").read_text().replace("{{APP_TOKEN}}", app.state.token)

    @app.get("/auth/login", response_class=HTMLResponse)
    async def login_page():
        if not access_password:
            return RedirectResponse("/", status_code=303)
        return (PACKAGE / "static/login.html").read_text().replace("{{ERROR}}", "")

    @app.post("/auth/login")
    async def login(request: Request):
        if not access_password:
            return RedirectResponse("/", status_code=303)
        form = await request.form(max_fields=5, max_files=0, max_part_size=4096)
        supplied = str(form.get("password", ""))
        if not hmac.compare_digest(supplied.encode(), access_password.encode()):
            html = (
                (PACKAGE / "static/login.html").read_text().replace("{{ERROR}}", "Неверный пароль доступа.")
            )
            return HTMLResponse(html, status_code=401)
        try:
            principal = create_principal(
                app.state.service.workspace,
                form.get("full_name", ""),
                form.get("company_name", ""),
                form.get("job_title", ""),
            )
        except ValueError as error:
            html = (PACKAGE / "static/login.html").read_text().replace("{{ERROR}}", str(error))
            return HTMLResponse(html, status_code=422)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            issue_session(session_key, principal),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=settings.public_deployment,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/auth/logout")
    async def logout():
        response = RedirectResponse("/auth/login", status_code=303)
        response.delete_cookie(
            COOKIE, path="/", secure=settings.public_deployment, httponly=True, samesite="strict"
        )
        return response

    @app.get("/api/me")
    async def me():
        principal = current_principal.get()
        return {
            "authenticated": bool(principal),
            **(
                principal.public()
                if principal
                else {
                    "full_name": "Участник команды",
                    "company_id": None,
                    "company_name": "",
                    "role": "cfo",
                    "job_title": "Финансовый директор",
                }
            ),
        }

    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    @app.get("/api/health")
    async def health():
        worker = getattr(app.state, "worker_process", None)
        if worker is not None and worker.poll() is not None:
            return JSONResponse({"status": "worker_unavailable"}, status_code=503)
        return {
            "status": "ok",
            "version": "0.3.0",
            "mode": "live",
            "model": settings.model,
            "research_model": settings.research_model,
            "key_configured": bool(read_key(settings.root)),
            "demo_only": True,
            "company_data": "case_csv_and_company_documents",
            "team_login": bool(access_password),
            "budget_rub_shared": 500,
            "budget_reset": "03:00 МСК",
        }

    @app.get("/api/companies")
    async def companies(q: str = Query(default="", max_length=100)):
        service = app.state.service
        cases = service.workspace.case.search(q, limit=20)["items"]
        custom = [x for x in service.workspace.list_custom() if q.casefold() in x["label"].casefold()]
        demos = [
            {
                "company_id": p.company_id,
                "label": next(f.text_value for f in p.features if f.name == "company_name"),
                "synthetic": True,
                "source": "synthetic_demo",
            }
            for p in (CompanyProfile.model_validate(x) for x in service.corpus.source_bundle["companies"])
            if p.company_id in service.allowed_companies
        ]
        demos = [
            x
            for x in demos
            if q.casefold() in x["label"].casefold() or q.casefold() in x["company_id"].casefold()
        ]
        items = custom + demos + cases
        for item in items:
            item["writable"] = service.workspace.owned(item["company_id"])
        return items

    @app.get("/api/workspace")
    async def workspace():
        service = app.state.service
        return {
            "case": service.workspace.case.summary(),
            "custom_company_count": len(service.workspace.list_custom()),
            "upload_limits": {
                "file_mb": 15,
                "files_per_company": 20,
                "text_characters": 100000,
                "pdf_pages": 40,
                "scanned_pdf_pages": 8,
                "extensions": ["pdf", "docx", "pptx", "xlsx", "csv", "txt", "md"],
            },
            "mode": "hosted_prototype" if settings.public_deployment else "local_prototype",
            "hosting": {
                "storage": os.environ.get("ALFA_STORAGE_MODE", "local"),
                "notice": "Демо: новые файлы и история сбрасываются после простоя или перезапуска. Скачайте важные результаты."
                if os.environ.get("ALFA_STORAGE_MODE") == "ephemeral_demo"
                else None,
            },
            "bank": {
                "document_count": len(service.research.bank.documents),
                "page_count": sum(d["page_count"] for d in service.research.bank.documents),
                "selected_claims": len(service.corpus.claims),
                "review_status": "pending_human_review",
                "scope": "9 материалов кейса, поиск по OCR 64 страниц. Полная ручная разметка тезисов не завершена.",
            },
        }

    @app.post("/api/companies", status_code=201)
    async def create_company(payload: NewCompany):
        return app.state.service.workspace.create_company(payload.name)

    @app.get("/api/companies/{company_id}/profile")
    async def profile(company_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        snapshot = service.workspace.snapshot(company_id, service.corpus)
        return {
            **{k: v for k, v in snapshot.items() if k not in {"document_text", "document_chunks"}},
            "writable": service.workspace.owned(company_id),
            "context": service.workspace.context(company_id),
        }

    @app.put("/api/companies/{company_id}/context")
    async def company_context(company_id: str, payload: CompanyContext):
        service = app.state.service
        service.authorize_company(company_id)
        service.workspace.save_context(company_id, payload.description, payload.website)
        return {"saved": True}

    @app.get("/api/companies/{company_id}/connections")
    async def connections(company_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        if not service.workspace.case.exists(company_id):
            return {
                "company_id": company_id,
                "available": False,
                "warnings": [
                    "В CSV кейса нет платёжного среза для добавленной или демонстрационной компании."
                ],
            }
        return {"available": True, **service.workspace.case.connections(company_id)}

    @app.get("/api/companies/{company_id}/documents")
    async def documents(company_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        return service.workspace.files(company_id)

    @app.post("/api/companies/{company_id}/documents", status_code=202)
    async def upload(company_id: str, bound_company_id: str = Form(...), file: UploadFile = File(...)):
        service = app.state.service
        service.authorize_company(company_id)
        try:
            if bound_company_id != company_id:
                raise HTTPException(422, "document_company_binding_mismatch")
            content = await file.read(MAX_FILE_BYTES + 1)
            document, created = service.workspace.add_file(company_id, file.filename or "file", content)
            return {"document": document, "created": created}
        finally:
            await file.close()

    @app.get("/api/companies/{company_id}/documents/{document_id}/download")
    async def download(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        row = service.workspace.file_row(company_id, document_id)
        return FileResponse(
            service.workspace.upload_dir / row["stored_name"],
            filename=row["name"],
            media_type="application/octet-stream",
            content_disposition_type="attachment",
        )

    @app.post("/api/companies/{company_id}/documents/{document_id}/retry", status_code=202)
    async def retry_document(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        return service.workspace.retry_document(company_id, document_id)

    @app.post("/api/companies/{company_id}/documents/{document_id}/exclude")
    async def exclude_document(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        return service.workspace.set_file_active(company_id, document_id, False)

    @app.post("/api/companies/{company_id}/documents/{document_id}/restore")
    async def restore_document(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        return service.workspace.set_file_active(company_id, document_id, True)

    @app.get("/api/library")
    async def library():
        corpus = app.state.service.corpus
        return {
            "bank_documents": app.state.service.research.bank.summary(),
            "documents": [
                d.model_dump(mode="json") for d in corpus.documents.values() if d.tenant_company_id is None
            ],
            "claims": [c.model_dump(mode="json") for c in corpus.claims.values()],
            "events": [e.model_dump(mode="json") for e in corpus.events.values()],
            "evidence": [
                e.model_dump(mode="json")
                for e in corpus.evidence.values()
                if corpus.documents[e.document_id].tenant_company_id is None
            ],
        }

    @app.get("/api/companies/{company_id}/favorites")
    async def favorites(company_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        with service.store.connection() as db:
            if current_principal.get():
                return [
                    dict(r)
                    for r in db.execute(
                        "SELECT event_id,run_id,created_at FROM user_favorites WHERE user_id=? AND company_id=? ORDER BY created_at DESC",
                        (service.tenant, company_id),
                    )
                ]
            return [
                dict(r)
                for r in db.execute(
                    "SELECT event_id,run_id,created_at FROM favorites WHERE company_id=? ORDER BY created_at DESC",
                    (company_id,),
                )
            ]

    @app.put("/api/companies/{company_id}/favorites/{event_id}")
    async def save_favorite(company_id: str, event_id: str, payload: Favorite):
        service = app.state.service
        service.authorize_company(company_id)
        if event_id not in service.corpus.events:
            raise NotFound("event_not_found")
        if payload.run_id and service.get(payload.run_id)["company_id"] != company_id:
            raise AccessDenied("run_company_mismatch")
        with service.store.connection() as db:
            if current_principal.get():
                db.execute(
                    "INSERT INTO user_favorites VALUES(?,?,?,?,?) ON CONFLICT(user_id,company_id,event_id) DO UPDATE SET run_id=excluded.run_id",
                    (service.tenant, company_id, event_id, payload.run_id, utcnow().isoformat()),
                )
                return {"saved": True}
            db.execute(
                "INSERT INTO favorites VALUES(?,?,?,?) ON CONFLICT(company_id,event_id) DO UPDATE SET run_id=excluded.run_id",
                (company_id, event_id, payload.run_id, utcnow().isoformat()),
            )
        return {"saved": True}

    @app.delete("/api/companies/{company_id}/favorites/{event_id}")
    async def remove_favorite(company_id: str, event_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        with service.store.connection() as db:
            if current_principal.get():
                db.execute(
                    "DELETE FROM user_favorites WHERE user_id=? AND company_id=? AND event_id=?",
                    (service.tenant, company_id, event_id),
                )
                return {"saved": False}
            db.execute("DELETE FROM favorites WHERE company_id=? AND event_id=?", (company_id, event_id))
        return {"saved": False}

    @app.get("/api/runs")
    async def history(company_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        with service.store.connection() as db:
            rows = db.execute(
                "SELECT id,status,created_at FROM runs WHERE company=? AND (tenant=? OR id IN (?,?)) AND json_extract(input_json,'$.profile_snapshot_id') IS NOT NULL ORDER BY created_at DESC LIMIT 15",
                (company_id, service.tenant, *sorted(PUBLIC_DEMO_RUNS)),
            ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/runs", status_code=202)
    async def start(payload: CreateRun, idempotency_key: str = Header(min_length=1, max_length=100)):
        return app.state.service.create(payload, idempotency_key)

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str):
        return app.state.service.get(run_id)

    @app.post("/api/runs/{run_id}/answers", status_code=202)
    async def answer(run_id: str, payload: Answer):
        return app.state.service.answer(run_id, payload)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        service = app.state.service
        row = service.run_row(run_id)
        if row["tenant"] != service.tenant:
            raise AccessDenied("read_only_demo")
        service.store.cancel(run_id, service.tenant)
        return service.get(run_id)

    @app.get("/api/runs/{run_id}/sources")
    async def sources(run_id: str):
        service = app.state.service
        service.get(run_id)
        row = service.run_row(run_id)
        state = RuntimeState.model_validate(row["state"])
        view = Engine(settings, service.store, service.corpus, None).view(state, row["input"])
        return {
            "documents": [d.model_dump(mode="json") for d in view.documents],
            "evidence": [e.model_dump(mode="json") for e in view.evidence],
            "profile": view.profile.model_dump(mode="json") if view.profile else None,
            "calculations": [c.model_dump(mode="json") for c in view.calculations],
        }

    @app.post("/api/runs/{run_id}/feedback", status_code=201)
    async def feedback(run_id: str, payload: Feedback):
        service = app.state.service
        service.get(run_id)
        with service.store.connection() as db:
            db.execute(
                "INSERT INTO feedback VALUES(?,?,?,?)",
                (new_id("feedback"), run_id, utcnow().isoformat(), payload.model_dump_json()),
            )
        return {"saved": True}

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: str):
        payload = app.state.service.get(run_id)
        if payload["status"] not in {"SUCCEEDED", "PARTIAL"}:
            raise HTTPException(409, "verified_result_not_ready")
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="insight.json"'},
        )

    @app.post("/api/research", status_code=202)
    async def start_research(
        payload: ResearchRequest, idempotency_key: str = Header(min_length=1, max_length=100)
    ):
        try:
            return app.state.service.research.create(payload, idempotency_key)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/research")
    async def research_history(company_id: str):
        return app.state.service.research.history(company_id)

    @app.get("/api/research/{run_id}")
    async def research_result(run_id: str):
        return app.state.service.research.get(run_id)

    @app.post("/api/research/{run_id}/answers", status_code=202)
    async def research_answer(run_id: str, payload: ResearchAnswer):
        return app.state.service.research.answer(run_id, payload)

    @app.post("/api/research/{run_id}/cancel")
    async def cancel_research(run_id: str):
        return app.state.service.research.cancel(run_id)

    @app.put("/api/research/{run_id}/favorite")
    async def favorite_research(run_id: str, payload: ResearchFavorite):
        app.state.service.research.set_favorite(run_id, payload.saved)
        return {"saved": payload.saved}

    @app.post("/api/research/{run_id}/feedback", status_code=201)
    async def research_feedback(run_id: str, payload: Feedback):
        app.state.service.research.get(run_id)
        with app.state.service.store.connection() as db:
            db.execute(
                "INSERT INTO feedback VALUES(?,?,?,?)",
                (new_id("feedback"), run_id, utcnow().isoformat(), payload.model_dump_json()),
            )
        return {"saved": True}

    @app.get("/api/research/{run_id}/export")
    async def export_research(run_id: str):
        payload = app.state.service.research.get(run_id)
        if not payload["result"] or not payload["verification"]["passed"]:
            raise HTTPException(409, "verified_result_not_ready")
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="research.json"'},
        )

    return app


app = create_app()
