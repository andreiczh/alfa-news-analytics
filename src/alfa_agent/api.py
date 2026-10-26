from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import hashlib
import json
import os
import secrets
import time
import logging
from html import escape
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request, File, Form, UploadFile, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask

from .corpus import AccessDenied
from .engine import Engine
from .service import Answer, CreateRun, Service
from .settings import PACKAGE, Settings, read_key, utcnow
from .state import RuntimeState
from .domain import CompanyProfile
from .storage import Conflict, NotFound, new_id
from .workspace import UploadError, MAX_FILE_BYTES
from .access import COOKIE, SESSION_SECONDS, issue_session, session_principal, signing_key, session_nonce
from .accounts import AuthError, AuthLimited
from .operations import HttpMetrics
from .identity import current_principal, create_principal, restore_principal, PUBLIC_DEMO_RUNS
from .research import ResearchRequest, ResearchAnswer
from .periods import period_intent, NEWS_DAYS, PLANNING_DAYS
from .personalization import PreferencesUpdate, ROLE_NAMES
from .company_dialogue import ContextMessage, ContextConfirm
from . import __version__


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


class NewsRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = Field(default=None, pattern="^(cfo|treasurer|ceo|coo|cco|cmo|other)$")
    force: bool = False


class NewsFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    news_id: str = Field(pattern=r"^news_[a-f0-9]{24}$")
    rating: str = Field(pattern="^(relevant|irrelevant)$")


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class TeamInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permission: str = Field(default="member", pattern="^(member|viewer)$")


class JoinWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=32, max_length=100)


class UserReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str = Field(pattern="^(accepted|rejected)$")
    comment: str = Field(min_length=20, max_length=1000)
    sources_checked: bool
    assumptions_checked: bool


def create_app(settings=None):
    settings = settings or Settings()
    access_password = os.environ.get("ALFA_ACCESS_PASSWORD", "").strip()
    if settings.public_deployment and len(access_password) < 16:
        raise ValueError("public_deployment_requires_strong_access_password")
    if os.environ.get("ALFA_REQUIRE_DURABLE_STORAGE") == "1" and not settings.database_url:
        raise ValueError("durable_database_is_required")
    session_key = signing_key(settings, access_password) if access_password else ""
    accounts_mode = bool(access_password and os.environ.get("ALFA_AUTH_MODE") == "accounts")
    operator_token = os.environ.get("ALFA_OPERATOR_TOKEN", "")

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(settings)
        yield
        app.state.service.corpus.close()
        app.state.service.store.close()

    app = FastAPI(title="Пульс бизнеса — прототип", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.token = (
        hmac.new(session_key.encode(), b"alfa-page-csrf-v3", hashlib.sha256).hexdigest()
        if access_password
        else secrets.token_urlsafe(32)
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.state.metrics = HttpMetrics()

    def page_token(cookie):
        if not accounts_mode:
            return app.state.token
        nonce = session_nonce(cookie, session_key) or ""
        return hmac.new(
            session_key.encode(), ("alfa-csrf-account:" + nonce).encode(), hashlib.sha256
        ).hexdigest()

    def account_page(register=False, error="", status=200):
        name = "account-register.html" if register else "account-login.html"
        return HTMLResponse(
            (PACKAGE / "static" / name).read_text().replace("{{ERROR}}", escape(error)), status_code=status
        )

    def signed_in(principal, sid=None):
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            issue_session(session_key, principal, sid),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=settings.public_deployment,
            samesite="strict",
            path="/",
        )
        return response

    async def observe_http(request: Request, call_next):
        started, request_id = time.monotonic(), secrets.token_hex(8)
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            elapsed = time.monotonic() - started
            app.state.metrics.observe(request.method, route, status, elapsed)
            if status >= 500:
                logging.getLogger("alfa.operations").error(
                    json.dumps(
                        {"event": "http_error", "request_id": request_id, "route": route, "status": status}
                    )
                )

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        operator = (
            request.url.path == "/ops/backup"
            and request.method == "POST"
            and len(operator_token) >= 32
            and hmac.compare_digest(
                request.headers.get("x-operator-token", "").encode(), operator_token.encode()
            )
        )
        public_path = (
            request.url.path in {"/api/health", "/auth/login", "/auth/register"}
            or request.url.path.startswith("/static/")
            or operator
        )
        principal = (
            session_principal(request.cookies.get(COOKIE, ""), session_key) if access_password else None
        )
        if accounts_mode:
            principal = (
                None
                if public_path
                else await run_in_threadpool(
                    app.state.service.accounts.authenticate, request.cookies.get(COOKIE, ""), session_key
                )
            )
        if access_password and not public_path and principal is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "team_login_required"}, status_code=401)
            return RedirectResponse("/auth/login", status_code=303)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "origin_not_allowed"}, status_code=403)
        if request.url.path in {"/auth/login", "/auth/register"} and request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdigit() or int(length) > 8192:
                return JSONResponse({"detail": "login_body_limit"}, status_code=413)
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            token = request.headers.get("x-app-token", "")
            if not hmac.compare_digest(token, page_token(request.cookies.get(COOKIE, ""))):
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
            if principal and not public_path and not accounts_mode:
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
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if settings.public_deployment:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    app.middleware("http")(observe_http)

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

    @app.exception_handler(AuthError)
    async def auth_error(request, error):
        return JSONResponse(
            {"detail": str(error)},
            status_code=429 if isinstance(error, AuthLimited) else 400,
            headers={"Retry-After": "900"} if isinstance(error, AuthLimited) else None,
        )

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request):
        return (
            (PACKAGE / "static/index.html")
            .read_text()
            .replace("{{APP_TOKEN}}", page_token(request.cookies.get(COOKIE, "")))
        )

    @app.get("/auth/login", response_class=HTMLResponse)
    def login_page():
        if not access_password:
            return RedirectResponse("/", status_code=303)
        if accounts_mode:
            return account_page()
        return (PACKAGE / "static/login.html").read_text().replace("{{ERROR}}", "")

    @app.post("/auth/login")
    async def login(request: Request):
        if not access_password:
            return RedirectResponse("/", status_code=303)
        form = await request.form(max_fields=5, max_files=0, max_part_size=4096)
        supplied = str(form.get("password", ""))
        if accounts_mode:

            def authenticate():
                service = app.state.service
                service.accounts.limit(
                    "login_ip", request.client.host if request.client else "unknown", 100, 900
                )
                service.accounts.limit(
                    "login_account", str(form.get("login", "")).strip().casefold(), 10, 900
                )
                return service.accounts.login(str(form.get("login", "")), supplied, with_session=True)

            try:
                principal, sid = await run_in_threadpool(authenticate)
            except AuthError as error:
                return account_page(error=str(error), status=429 if isinstance(error, AuthLimited) else 401)
            return signed_in(principal, sid)
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
        return signed_in(principal)

    @app.get("/auth/register", response_class=HTMLResponse)
    def register_page():
        return (
            account_page(register=True) if accounts_mode else RedirectResponse("/auth/login", status_code=303)
        )

    @app.post("/auth/register")
    async def register(request: Request):
        if not accounts_mode:
            raise HTTPException(404)
        form = await request.form(max_fields=8, max_files=0, max_part_size=4096)

        def create():
            accounts = app.state.service.accounts
            accounts.limit("registration_ip", request.client.host if request.client else "unknown", 10, 3600)
            if not hmac.compare_digest(
                str(form.get("invite_password", "")).encode(), access_password.encode()
            ):
                raise AuthError("Неверный код доступа к пилоту.")
            principal = accounts.register(
                str(form.get("login", "")),
                str(form.get("password", "")),
                form.get("full_name", ""),
                form.get("company_name", ""),
                form.get("job_title", ""),
            )
            return principal, accounts.new_session(principal)

        try:
            principal, sid = await run_in_threadpool(create)
        except ValueError as error:
            return account_page(
                register=True, error=str(error), status=429 if isinstance(error, AuthLimited) else 422
            )
        return signed_in(principal, sid)

    @app.post("/auth/logout")
    async def logout(request: Request):
        if accounts_mode:
            await run_in_threadpool(
                app.state.service.accounts.logout, request.cookies.get(COOKIE, ""), session_key
            )
        response = RedirectResponse("/auth/login", status_code=303)
        response.delete_cookie(
            COOKIE, path="/", secure=settings.public_deployment, httponly=True, samesite="strict"
        )
        return response

    @app.get("/api/me")
    def me():
        principal = current_principal.get()
        prefs = app.state.service.personalization.get()
        return {
            "authenticated": bool(principal),
            "auth_mode": "accounts" if accounts_mode else "demo",
            **(
                principal.public()
                if principal
                else {
                    "full_name": "Участник команды",
                    "company_id": None,
                    "company_name": "",
                    "job_title": prefs["job_title"],
                    "role": prefs["role"],
                }
            ),
            "selected_company_id": prefs["company_id"],
            "role": prefs["role"],
            "job_title": prefs["job_title"],
            "role_names": ROLE_NAMES,
            "workspace_permission": app.state.service.accounts.permission(prefs["company_id"])
            if principal and prefs["company_id"]
            else None,
        }

    @app.put("/api/account/password")
    async def change_password(payload: PasswordChange):
        if not accounts_mode:
            raise HTTPException(403, "account_login_required")
        await run_in_threadpool(
            app.state.service.accounts.change_password, payload.current_password, payload.new_password
        )
        return {"changed": True, "login_required": True}

    @app.get("/api/companies/{company_id}/team")
    def team(company_id: str):
        return {"members": app.state.service.accounts.team(company_id)}

    @app.post("/api/companies/{company_id}/team/invites")
    def invite(company_id: str, payload: TeamInvite):
        return app.state.service.accounts.invite(company_id, payload.permission)

    @app.post("/api/account/join")
    def join(payload: JoinWorkspace):
        if not accounts_mode:
            raise HTTPException(403, "account_login_required")
        return app.state.service.accounts.join(payload.code)

    @app.delete("/api/companies/{company_id}/team/{user_id}")
    def revoke(company_id: str, user_id: str):
        return app.state.service.accounts.revoke(company_id, user_id)

    @app.put("/api/me/preferences")
    def preferences(payload: PreferencesUpdate):
        return app.state.service.personalization.save(payload)

    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    @app.get("/api/health")
    async def health():
        worker = getattr(app.state, "worker_process", None)
        if worker is not None and worker.poll() is not None:
            return JSONResponse({"status": "worker_unavailable"}, status_code=503)
        readiness = await run_in_threadpool(
            app.state.service.operations.readiness, os.environ.get("ALFA_REQUIRE_WORKER") == "1"
        )
        if not readiness["ready"]:
            return JSONResponse({"status": "not_ready", **readiness}, status_code=503)
        return {
            "status": "ok",
            "version": __version__,
            "runtime_commit": os.environ.get("RENDER_GIT_COMMIT") or None,
            "analysis_policy_version": __import__(
                "alfa_agent.analysis_policy", fromlist=["POLICY_VERSION"]
            ).POLICY_VERSION,
            "mode": "live",
            "model": settings.model,
            "research_model": settings.research_model,
            "key_configured": bool(read_key(settings.root)),
            "demo_only": True,
            "company_data": "case_csv_and_company_documents",
            "team_login": bool(access_password),
            "auth_mode": "accounts" if accounts_mode else "demo",
            "storage": app.state.service.store.backend,
            "storage_expires_at": os.environ.get("ALFA_STORAGE_EXPIRES_AT") or None,
            "readiness": readiness,
            "budget_rub_shared": 500,
            "budget_reset": "03:00 МСК",
        }

    @app.get("/api/operations")
    def operations():
        return {
            "readiness": app.state.service.operations.readiness(True),
            "http": app.state.metrics.snapshot(),
            "storage": app.state.service.store.backend,
            "storage_expires_at": os.environ.get("ALFA_STORAGE_EXPIRES_AT") or None,
            "limits": {
                "model_calls_per_research": settings.max_calls,
                "tools_per_research": settings.max_tools,
                "research_seconds": settings.active_seconds,
                "instance_count": 1,
            },
            "scope": "Техническое состояние пилота; качество финансовых выводов этими метриками не измеряется.",
        }

    @app.post("/ops/backup")
    async def operator_backup(request: Request):
        if len(operator_token) < 32 or not hmac.compare_digest(
            request.headers.get("x-operator-token", "").encode(), operator_token.encode()
        ):
            raise HTTPException(403, "operator_required")
        from .operations import backup

        path = settings.state_path / "private-backups" / (new_id("backup") + ".jsonl.gz")
        await run_in_threadpool(backup, app.state.service.store, path)
        return FileResponse(
            path,
            media_type="application/gzip",
            filename="alfa-runtime-backup.jsonl.gz",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.get("/api/companies")
    def companies(q: str = Query(default="", max_length=100)):
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
        selected = service.personalization.get()["company_id"]
        if (
            not q
            and selected
            and not any(x["company_id"] == selected for x in items)
            and service.workspace.case.exists(selected)
        ):
            items.extend(
                x
                for x in service.workspace.case.search(selected, limit=5)["items"]
                if x["company_id"] == selected
            )
        for item in items:
            item["writable"] = service.workspace.writable(item["company_id"])
        return items

    @app.get("/api/workspace")
    def workspace():
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
                "storage": service.store.backend,
                "expires_at": os.environ.get("ALFA_STORAGE_EXPIRES_AT") or None,
                "notice": (
                    "Файлы и история сохраняются при перезапуске. Бесплатное хранилище действует до "
                    + (
                        os.environ["ALFA_STORAGE_EXPIRES_AT"][:10]
                        if os.environ.get("ALFA_STORAGE_EXPIRES_AT")
                        else "окончания пилота"
                    )
                    + ". Скачивайте важные результаты."
                )
                if service.store.backend == "postgresql"
                else "Демо: новые файлы и история сбрасываются после простоя или перезапуска. Скачайте важные результаты."
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
    def create_company(payload: NewCompany):
        return app.state.service.workspace.create_company(payload.name)

    @app.get("/api/companies/{company_id}/profile")
    def profile(company_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        snapshot = service.workspace.snapshot(company_id, service.corpus)
        return {
            **{k: v for k, v in snapshot.items() if k not in {"document_text", "document_chunks"}},
            "writable": service.workspace.writable(company_id),
            "context": service.workspace.context(company_id),
        }

    @app.get("/api/companies/{company_id}/news")
    def company_news(
        company_id: str,
        role: str | None = Query(default=None, pattern="^(cfo|treasurer|ceo|coo|cco|cmo|other)$"),
        days: int = Query(default=7, ge=1, le=90),
    ):
        return app.state.service.news.get(
            company_id, role or app.state.service.personalization.get()["role"], days
        )

    @app.post("/api/companies/{company_id}/news/refresh", status_code=202)
    def refresh_news(company_id: str, payload: NewsRefresh):
        return app.state.service.news.refresh(
            company_id, payload.role or app.state.service.personalization.get()["role"], payload.force
        )

    @app.get("/api/companies/{company_id}/search")
    def workspace_search(
        company_id: str,
        q: str = Query(min_length=2, max_length=500),
        scope: str = Query(default="all", pattern="^(all|news|company)$"),
        days: int = Query(default=7, ge=1, le=90),
    ):
        return app.state.service.workspace_search.search(company_id, q, scope=scope, days=days)

    @app.get("/api/companies/{company_id}/news/saved")
    def saved_news(
        company_id: str,
        limit: int = Query(default=25, ge=1, le=50),
        offset: int = Query(default=0, ge=0, le=10000),
    ):
        return app.state.service.news.saved_items(company_id, limit=limit, offset=offset)

    @app.put("/api/companies/{company_id}/news/{news_id}/saved")
    def save_news(company_id: str, news_id: str, payload: ResearchFavorite):
        app.state.service.news.save_item(company_id, news_id, payload.saved)
        return {"saved": payload.saved}

    @app.post("/api/companies/{company_id}/news/feedback", status_code=201)
    def news_feedback(company_id: str, payload: NewsFeedback):
        app.state.service.news.feedback(company_id, payload.news_id, payload.rating)
        return {"saved": True}

    @app.put("/api/companies/{company_id}/context")
    def company_context(company_id: str, payload: CompanyContext):
        service = app.state.service
        service.authorize_company(company_id)
        service.workspace.save_context(company_id, payload.description, payload.website)
        return {"saved": True}

    @app.get("/api/companies/{company_id}/context/dialogue")
    def context_history(company_id: str):
        return app.state.service.context_dialogue.history(company_id)

    @app.post("/api/companies/{company_id}/context/dialogue", status_code=202)
    def context_message(
        company_id: str,
        payload: ContextMessage,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=100),
    ):
        return app.state.service.context_dialogue.create(company_id, payload, idempotency_key)

    @app.post("/api/companies/{company_id}/context/dialogue/{turn_id}/confirm")
    def confirm_context(company_id: str, turn_id: str, payload: ContextConfirm):
        return app.state.service.context_dialogue.confirm(company_id, turn_id, payload)

    @app.post("/api/companies/{company_id}/context/dialogue/{turn_id}/cancel")
    def cancel_context(company_id: str, turn_id: str):
        return app.state.service.context_dialogue.cancel(company_id, turn_id)

    @app.put("/api/companies/{company_id}/context/fields")
    def edit_context(company_id: str, payload: ContextConfirm):
        return app.state.service.context_dialogue.save_fields(company_id, payload)

    @app.put("/api/companies/{company_id}")
    def rename_company(company_id: str, payload: NewCompany):
        service = app.state.service
        service.authorize_company(company_id)
        service.workspace.require_writable(company_id)
        if not service.workspace.custom_exists(company_id):
            raise AccessDenied("create_own_company_first")
        with service.store.connection() as db:
            db.execute("UPDATE custom_companies SET name=? WHERE id=?", (payload.name.strip(), company_id))
            db.execute("UPDATE company_versions SET version=version+1 WHERE company_id=?", (company_id,))
        return {"company_id": company_id, "label": payload.name.strip()}

    @app.get("/api/companies/{company_id}/connections")
    def connections(company_id: str):
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
    def documents(company_id: str):
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
    def download(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        row = service.workspace.file_row(company_id, document_id)
        return FileResponse(
            service.workspace.file_path(row),
            filename=row["name"],
            media_type="application/octet-stream",
            content_disposition_type="attachment",
        )

    @app.post("/api/companies/{company_id}/documents/{document_id}/retry", status_code=202)
    def retry_document(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        return service.workspace.retry_document(company_id, document_id)

    @app.post("/api/companies/{company_id}/documents/{document_id}/exclude")
    def exclude_document(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        return service.workspace.set_file_active(company_id, document_id, False)

    @app.post("/api/companies/{company_id}/documents/{document_id}/restore")
    def restore_document(company_id: str, document_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        return service.workspace.set_file_active(company_id, document_id, True)

    @app.get("/api/library")
    def library(company_id: str | None = None):
        service = app.state.service
        corpus = service.corpus
        prefs = service.personalization.get()
        selected = company_id or prefs["company_id"]
        documents = service.research.bank.summary()
        if selected:
            service.authorize_company(selected)
            documents = service.research.bank.recommend(
                service.workspace.snapshot(selected, corpus), prefs["role"]
            )
        return {
            "company_id": selected,
            "role": prefs["role"],
            "bank_documents": documents,
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
    def favorites(company_id: str):
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
    def save_favorite(company_id: str, event_id: str, payload: Favorite):
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
    def remove_favorite(company_id: str, event_id: str):
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
    def history(company_id: str):
        service = app.state.service
        service.authorize_company(company_id)
        with service.store.connection() as db:
            rows = db.execute(
                "SELECT id,status,created_at FROM runs WHERE company=? AND (tenant=? OR id IN (?,?)) AND json_extract(input_json,'$.profile_snapshot_id') IS NOT NULL ORDER BY created_at DESC LIMIT 15",
                (company_id, service.tenant, *sorted(PUBLIC_DEMO_RUNS)),
            ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/runs", status_code=202)
    def start(payload: CreateRun, idempotency_key: str = Header(min_length=1, max_length=100)):
        return app.state.service.create(payload, idempotency_key)

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return app.state.service.get(run_id)

    @app.post("/api/runs/{run_id}/answers", status_code=202)
    def answer(run_id: str, payload: Answer):
        return app.state.service.answer(run_id, payload)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        service = app.state.service
        row = service.run_row(run_id)
        if row["tenant"] != service.tenant:
            raise AccessDenied("read_only_demo")
        service.store.cancel(run_id, service.tenant)
        return service.get(run_id)

    @app.get("/api/runs/{run_id}/sources")
    def sources(run_id: str):
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
    def feedback(run_id: str, payload: Feedback):
        service = app.state.service
        service.get(run_id)
        with service.store.connection() as db:
            db.execute(
                "INSERT INTO feedback VALUES(?,?,?,?)",
                (new_id("feedback"), run_id, utcnow().isoformat(), payload.model_dump_json()),
            )
        return {"saved": True}

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str):
        payload = app.state.service.get(run_id)
        if payload["status"] not in {"SUCCEEDED", "PARTIAL"}:
            raise HTTPException(409, "verified_result_not_ready")
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="insight.json"'},
        )

    @app.get("/api/research/periods")
    def research_periods(
        q: str = Query(min_length=2, max_length=1500),
        news_days: int = Query(default=7, ge=1, le=90),
        planning_days: int = Query(default=30, ge=1, le=365),
    ):
        if news_days not in NEWS_DAYS or planning_days not in PLANNING_DAYS:
            raise HTTPException(422, "unsupported_research_period")
        return period_intent(q, news_days, planning_days)

    @app.post("/api/research", status_code=202)
    def start_research(payload: ResearchRequest, idempotency_key: str = Header(min_length=1, max_length=100)):
        try:
            return app.state.service.research.create(payload, idempotency_key)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/research")
    def research_history(
        company_id: str,
        saved_only: bool = False,
        limit: int = Query(default=25, ge=1, le=50),
        offset: int = Query(default=0, ge=0, le=10000),
    ):
        return app.state.service.research.history(
            company_id, saved_only=saved_only, limit=limit, offset=offset
        )

    @app.get("/api/research/{run_id}")
    def research_result(run_id: str):
        return app.state.service.research.get(run_id)

    @app.post("/api/research/{run_id}/answers", status_code=202)
    def research_answer(run_id: str, payload: ResearchAnswer):
        return app.state.service.research.answer(run_id, payload)

    @app.post("/api/research/{run_id}/cancel")
    def cancel_research(run_id: str):
        return app.state.service.research.cancel(run_id)

    @app.put("/api/research/{run_id}/favorite")
    def favorite_research(run_id: str, payload: ResearchFavorite):
        app.state.service.research.set_favorite(run_id, payload.saved)
        return {"saved": payload.saved}

    @app.post("/api/research/{run_id}/feedback", status_code=201)
    def research_feedback(run_id: str, payload: Feedback):
        app.state.service.research.get(run_id)
        with app.state.service.store.connection() as db:
            db.execute(
                "INSERT INTO feedback VALUES(?,?,?,?)",
                (new_id("feedback"), run_id, utcnow().isoformat(), payload.model_dump_json()),
            )
        return {"saved": True}

    @app.post("/api/research/{run_id}/review")
    def review_research(run_id: str, payload: UserReview):
        if not accounts_mode:
            raise HTTPException(403, "account_login_required")
        if payload.verdict == "accepted" and not (payload.sources_checked and payload.assumptions_checked):
            raise HTTPException(422, "review_sources_and_assumptions_first")
        return app.state.service.research.review(run_id, payload.verdict, payload.comment)

    @app.get("/api/research/{run_id}/export")
    def export_research(run_id: str):
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
