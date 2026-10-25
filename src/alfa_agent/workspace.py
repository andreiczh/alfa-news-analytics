"""Company-bound uploads, source-preserving profiles and immutable run snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from .case_data import CaseData
from .corpus import AccessDenied
from .domain import Citation, CompanyProfile, DocumentRef, Evidence, FeatureCandidate, TimeScope
from .settings import utcnow
from .storage import Conflict, NotFound, new_id
from .identity import current_principal, PUBLIC_DEMO_COMPANIES

MAX_FILE_BYTES = 15 * 1024 * 1024
EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md"}


class UploadError(ValueError):
    pass


class Workspace:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.case = CaseData(settings)
        self.upload_dir = settings.state_path / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS custom_companies (
              id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS company_versions (
              company_id TEXT PRIMARY KEY,version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS company_files (
              id TEXT PRIMARY KEY,company_id TEXT NOT NULL,name TEXT NOT NULL,
              extension TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,
              stored_name TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,
              document_json TEXT,chunks_json TEXT,features_json TEXT,evidence_json TEXT,
              issues_json TEXT NOT NULL DEFAULT '[]',UNIQUE(company_id,sha256));
            CREATE TABLE IF NOT EXISTS company_snapshots (
              id TEXT PRIMARY KEY,company_id TEXT NOT NULL,version INTEGER NOT NULL,
              as_of TEXT NOT NULL,payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS favorites (
              company_id TEXT NOT NULL,event_id TEXT NOT NULL,run_id TEXT,created_at TEXT NOT NULL,
              PRIMARY KEY(company_id,event_id));
            CREATE TABLE IF NOT EXISTS company_access(company_id TEXT PRIMARY KEY,user_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS company_members(company_id TEXT NOT NULL,user_id TEXT NOT NULL,
              permission TEXT NOT NULL,active INTEGER NOT NULL,joined_at TEXT NOT NULL,
              PRIMARY KEY(company_id,user_id));
            CREATE TABLE IF NOT EXISTS employee_profiles(user_id TEXT PRIMARY KEY,company_id TEXT NOT NULL,
              full_name TEXT NOT NULL,company_name TEXT NOT NULL,job_title TEXT NOT NULL,role TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS company_strategy(company_id TEXT PRIMARY KEY,
              revision INTEGER NOT NULL,fields_json TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS company_context(company_id TEXT PRIMARY KEY,text TEXT NOT NULL,website TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS user_favorites(user_id TEXT NOT NULL,company_id TEXT NOT NULL,event_id TEXT NOT NULL,run_id TEXT,created_at TEXT NOT NULL,PRIMARY KEY(user_id,company_id,event_id));
            CREATE INDEX IF NOT EXISTS files_company ON company_files(company_id,created_at);
            CREATE TABLE IF NOT EXISTS company_file_blobs(document_id TEXT PRIMARY KEY,
              content BLOB NOT NULL,sha256 TEXT NOT NULL);
            """)

    def custom_exists(self, company_id):
        with self.store.connection() as db:
            return bool(db.execute("SELECT 1 FROM custom_companies WHERE id=?", (company_id,)).fetchone())

    def list_custom(self):
        with self.store.connection() as db:
            values = [
                {"company_id": r["id"], "label": r["name"], "source": "company_upload", "synthetic": False}
                for r in db.execute("SELECT * FROM custom_companies ORDER BY created_at DESC")
            ]
        principal = current_principal.get()
        return (
            values
            if principal is None
            else [
                x for x in values if self.owned(x["company_id"]) or x["company_id"] in PUBLIC_DEMO_COMPANIES
            ]
        )

    def owned(self, company_id):
        principal = current_principal.get()
        if principal is None:
            return True
        with self.store.connection() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM company_members WHERE company_id=? AND user_id=? AND active=1 UNION SELECT 1 FROM company_access a WHERE a.company_id=? AND a.user_id=? AND NOT EXISTS(SELECT 1 FROM company_members m WHERE m.company_id=a.company_id AND m.user_id=a.user_id)",
                    (company_id, principal.user_id, company_id, principal.user_id),
                ).fetchone()
            )

    def require_writable(self, company_id):
        if not self.writable(company_id):
            raise AccessDenied("company_read_only")

    def writable(self, company_id):
        if not self.owned(company_id):
            return False
        principal = current_principal.get()
        if principal:
            with self.store.connection() as db:
                member = db.execute(
                    "SELECT permission FROM company_members WHERE company_id=? AND user_id=? AND active=1",
                    (company_id, principal.user_id),
                ).fetchone()
            if member and member[0] == "viewer":
                return False
        return True

    def context(self, company_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT text,website,updated_at FROM company_context WHERE company_id=?", (company_id,)
            ).fetchone()
        with self.store.connection() as db:
            strategy = db.execute(
                "SELECT * FROM company_strategy WHERE company_id=?", (company_id,)
            ).fetchone()
        fields = json.loads(strategy["fields_json"]) if strategy else {}
        from .personalization import marked_test_context

        synthetic = marked_test_context(row[0] if row else "") or any(
            f.get("synthetic") or marked_test_context(f["value"]) for f in fields.values()
        )
        turn_ids = list({f["turn_id"] for f in fields.values() if f.get("turn_id")})
        if turn_ids and not synthetic:
            with self.store.connection() as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_turns'"
                ).fetchone():
                    placeholders = ",".join("?" for _ in turn_ids)
                    origins = db.execute(
                        "SELECT input_json FROM context_turns WHERE company_id=? AND id IN ("
                        + placeholders
                        + ")",
                        [company_id, *turn_ids],
                    ).fetchall()
                    synthetic = any(
                        marked_test_context(json.loads(origin[0])["message"]) for origin in origins
                    )
        return {
            "synthetic": synthetic,
            "description": row[0] if row else "",
            "website": row[1] if row else "",
            "updated_at": strategy["updated_at"] if strategy else row[2] if row else None,
            "revision": strategy["revision"] if strategy else 0,
            "fields": fields,
        }

    def save_context(self, company_id, description, website):
        self.require_writable(company_id)
        if not self.custom_exists(company_id):
            raise AccessDenied("create_own_company_first")
        website = website.strip()
        if website and (not website.startswith("https://") or len(website) > 300):
            raise UploadError("website_requires_https")
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO company_context VALUES(?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET text=excluded.text,website=excluded.website,updated_at=excluded.updated_at",
                (company_id, description.strip(), website, utcnow().isoformat()),
            )
            db.execute(
                """INSERT INTO company_strategy VALUES(?,1,'{}',?) ON CONFLICT(company_id)
                DO UPDATE SET revision=company_strategy.revision+1,updated_at=excluded.updated_at""",
                (company_id, utcnow().isoformat()),
            )
            db.execute("UPDATE company_versions SET version=version+1 WHERE company_id=?", (company_id,))

    def create_company(self, name, owner_id=None):
        name = name.strip()
        if not 2 <= len(name) <= 160:
            raise UploadError("invalid_company_name")
        cid = new_id("company")
        with self.store.connection() as db:
            db.execute("INSERT INTO custom_companies VALUES(?,?,?)", (cid, name, utcnow().isoformat()))
            db.execute("INSERT INTO company_versions VALUES(?,1)", (cid,))
            principal = current_principal.get()
            owner_id = owner_id or (principal.user_id if principal else None)
            if owner_id:
                db.execute("INSERT INTO company_access VALUES(?,?)", (cid, owner_id))
                db.execute(
                    "INSERT INTO company_members VALUES(?,?,?,1,?)",
                    (cid, owner_id, "owner", utcnow().isoformat()),
                )
        return {"company_id": cid, "label": name, "source": "company_upload", "synthetic": False}

    def _custom_base(self, company_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM custom_companies WHERE id=?", (company_id,)).fetchone()
        if not row:
            raise NotFound("company_not_found")
        did, eid = company_id + "_onboarding", company_id + "_name_evidence"
        text = "Название, указанное пользователем: " + row["name"]
        doc = DocumentRef(
            document_id=did,
            source_kind="user_answer",
            title="Сведения при добавлении компании",
            publisher="Пользователь компании",
            published_at=None,
            publication_date_raw=None,
            available_at=row["created_at"],
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            tenant_company_id=company_id,
        )
        ev = Evidence(
            evidence_id=eid,
            document_id=did,
            object_kind="answer",
            locator="Название при добавлении компании",
            quote=text,
            extraction_method="user",
        )
        feature = FeatureCandidate(
            feature_id=company_id + "_name",
            name="company_name",
            text_value=row["name"],
            status="self_reported",
            period=TimeScope(),
            source_refs=[Citation(kind="evidence", ref_id=eid)],
        )
        profile = CompanyProfile(
            company_id=company_id,
            version=1,
            as_of=row["created_at"],
            features=[feature],
            unresolved_identity=False,
            source_snapshot_ids=[did],
        )
        context = self.context(company_id)
        parts = []
        if context["description"]:
            parts.append(("description", "Свободное описание", context["description"]))
        parts.extend((key, item["label"], item["value"]) for key, item in context["fields"].items())
        if context["website"]:
            parts.append(("website", "Сайт, указанный компанией", context["website"]))
        evidence, chunks, body = [ev.model_dump(mode="json")], [], [text]
        feature_names = {
            "description": "company_description",
            "industry": "industry",
            "products": "key_products",
            "geography": "markets",
            "competitors": "competitors",
            "business_model": "business_model",
            "goals": "strategy",
            "priorities": "information_priorities",
        }
        for key, label, value in parts:
            refs = []
            body.append(label + ": " + value)
            for offset in range(0, len(value), 1800):
                chunk = value[offset : offset + 1800]
                ref = company_id + "_context_" + key + "_" + str(offset)
                evidence.append(
                    Evidence(
                        evidence_id=ref,
                        document_id=did,
                        object_kind="answer",
                        locator=label,
                        quote=chunk,
                        extraction_method="user",
                    ).model_dump(mode="json")
                )
                chunks.append({"id": key + "_" + str(offset), "locator": label, "text": chunk, "page": None})
                refs.append(Citation(kind="evidence", ref_id=ref))
            if refs:
                profile.features.append(
                    FeatureCandidate(
                        feature_id=company_id + "_context_" + key,
                        name=feature_names.get(key, "context_" + key),
                        text_value=value,
                        status="self_reported",
                        period=TimeScope(),
                        source_refs=refs,
                    )
                )
        text = "\n".join(body)
        doc = doc.model_copy(update={"sha256": hashlib.sha256(text.encode()).hexdigest()})
        return {
            "profile": profile.model_dump(mode="json"),
            "documents": [doc.model_dump(mode="json")],
            "evidence": evidence,
            "document_text": {did: text},
            "document_chunks": {did: chunks},
            "metadata": {
                "source": "company_upload",
                "warnings": [
                    "Сведения и привязка документов сообщены пользователем; юридическая идентичность не проверена независимо."
                ],
            },
        }

    def snapshot(self, company_id, corpus, persist=False):
        if self.case.exists(company_id):
            base = self.case.build(company_id)
        elif self.custom_exists(company_id):
            base = self._custom_base(company_id)
        else:
            raw = corpus.source_bundle
            profile = next((p for p in raw["companies"] if p["company_id"] == company_id), None)
            if profile is None:
                raise AccessDenied("company_not_accessible")
            docs = [d for d in raw["documents"] if d.get("tenant_company_id") == company_id]
            ids = {d["document_id"] for d in docs}
            base = {
                "profile": profile,
                "documents": docs,
                "evidence": [e for e in raw["evidence"] if e["document_id"] in ids],
                "document_text": {k: v for k, v in raw["document_text"].items() if k in ids},
                "metadata": {
                    "source": "synthetic_demo",
                    "warnings": ["Демонстрационная компания и её исходные показатели синтетические."],
                },
            }
        # Deep copy: preview/snapshots must never mutate a source bundle or old run.
        base = json.loads(json.dumps(base, ensure_ascii=False))
        base.setdefault("document_chunks", {})
        base["context"] = self.context(company_id)
        profile = CompanyProfile.model_validate(base["profile"])
        candidates = defaultdict(list)
        for feature in profile.features:
            candidates[feature.name].append(feature)
        with self.store.connection() as db:
            files = db.execute(
                "SELECT * FROM company_files WHERE company_id=? AND status='READY' ORDER BY created_at,id",
                (company_id,),
            ).fetchall()
            version = db.execute(
                "SELECT version FROM company_versions WHERE company_id=?", (company_id,)
            ).fetchone()
        for file in files:
            doc = json.loads(file["document_json"])
            base["documents"].append(doc)
            base["evidence"].extend(json.loads(file["evidence_json"]))
            base["document_text"][doc["document_id"]] = "\n".join(
                c["text"] for c in json.loads(file["chunks_json"])
            )
            base["document_chunks"][doc["document_id"]] = json.loads(file["chunks_json"])
            profile.source_snapshot_ids.append(doc["document_id"])
            for raw in json.loads(file["features_json"]):
                feature = FeatureCandidate.model_validate(raw)
                if feature.name == "company_name":
                    feature = feature.model_copy(update={"name": "reported_name"})
                candidates[feature.name].append(feature)
        resolved, conflicts = [], []
        for name, items in candidates.items():
            usable = [f for f in items if f.status not in {"missing", "conflicting"}]
            has_conflict = any(f.status == "conflicting" for f in items)
            values = {self._value_key(f) for f in usable}
            if len(values) > 1 or has_conflict:
                refs = list({(c.kind, c.ref_id): c for f in items for c in f.source_refs}.values())
                resolved.append(
                    FeatureCandidate(
                        feature_id=company_id + "_resolved_" + name,
                        name=name,
                        status="conflicting",
                        period=TimeScope(),
                        source_refs=refs,
                        note="Разные значения/периоды не согласованы. Ни одно не выбрано автоматически.",
                    )
                )
                conflicts.append({"field": name, "candidates": [f.model_dump(mode="json") for f in items]})
            elif usable:
                first = usable[0].model_copy(deep=True)
                first.source_refs = list(
                    {(c.kind, c.ref_id): c for f in usable for c in f.source_refs}.values()
                )
                resolved.append(first)
            else:
                resolved.append(items[0])
        for name in (
            "rate_type",
            "floating_debt_rub",
            "capex_planned_rub",
            "inventory_days",
            "receivables_days",
            "credit_sales_share",
        ):
            if name not in candidates:
                resolved.append(
                    FeatureCandidate(
                        feature_id=company_id + "_missing_" + name,
                        name=name,
                        status="missing",
                        period=TimeScope(),
                        source_refs=[],
                    )
                )
        profile.features = resolved
        profile.version = version[0] if version else 1
        profile.as_of = utcnow()
        base["profile"] = profile.model_dump(mode="json")
        base["metadata"].update(
            {"document_count": len(files), "feature_conflicts": conflicts, "profile_version": profile.version}
        )
        if persist:
            sid = new_id("snapshot")
            with self.store.connection() as db:
                db.execute(
                    "INSERT INTO company_snapshots VALUES(?,?,?,?,?)",
                    (
                        sid,
                        company_id,
                        profile.version,
                        profile.as_of.isoformat(),
                        json.dumps(base, ensure_ascii=False),
                    ),
                )
            base["snapshot_id"] = sid
        return base

    @staticmethod
    def _value_key(feature):
        if feature.name == "industry" and feature.text_value:
            value = re.sub(r"^(?:компания|отрасль)\s*[:—-]?\s*", "", feature.text_value.casefold()).strip(
                " ."
            )
            if value in {"банк", "банковские услуги", "банковская деятельность", "денежное посредничество"}:
                return ("industry:bank", feature.boolean_value, None)
        if feature.numeric_value:
            n = feature.numeric_value
            return (
                str(n.value.normalize()) if n.value is not None else str(n.minimum),
                str(n.maximum),
                n.unit,
                feature.period.model_dump_json(),
            )
        return (feature.text_value, feature.boolean_value, feature.period.model_dump_json())

    def load_snapshot(self, snapshot_id, company_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT payload_json FROM company_snapshots WHERE id=? AND company_id=?",
                (snapshot_id, company_id),
            ).fetchone()
        if not row:
            raise AccessDenied("snapshot_not_accessible")
        return json.loads(row[0])

    def add_file(self, company_id, name, content):
        self.require_writable(company_id)
        name = Path(name.replace("\\", "/")).name[:180]
        ext = Path(name).suffix.lower()
        if ext not in EXTENSIONS:
            raise UploadError("file_type_not_supported")
        if not content or len(content) > MAX_FILE_BYTES:
            raise UploadError("file_size_limit")
        sha = hashlib.sha256(content).hexdigest()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT id FROM company_files WHERE company_id=? AND sha256=?", (company_id, sha)
            ).fetchone()
            if old:
                return self.file_info(company_id, old[0]), False
            quota = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM company_files WHERE company_id=?", (company_id,)
            ).fetchone()
            if quota[0] >= 20 or quota[1] + len(content) > 100 * 1024 * 1024:
                raise UploadError("company_upload_quota")
            total = db.execute("SELECT COALESCE(SUM(bytes),0) FROM company_files").fetchone()[0]
            if total + len(content) > 250 * 1024 * 1024:
                raise UploadError("pilot_storage_quota")
            if hasattr(self.store, "admission"):
                principal = current_principal.get()
                self.store.admission(db, principal.user_id if principal else "demo")
            did, jid = new_id("upload"), new_id("ingest")
            stored_name = did + ext
            path = self.upload_dir / stored_name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(content)
                now = utcnow().isoformat()
                db.execute(
                    "INSERT INTO company_files(id,company_id,name,extension,sha256,bytes,stored_name,created_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
                    (did, company_id, name, ext, sha, len(content), stored_name, now, "QUEUED"),
                )
                db.execute(
                    "INSERT INTO document_jobs(id,company_id,document_id,status,phase,created_at) VALUES(?,?,?,'QUEUED','parse',?)",
                    (jid, company_id, did, now),
                )
                db.execute("INSERT INTO company_file_blobs VALUES(?,?,?)", (did, content, sha))
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return self.file_info(company_id, did), True

    def file_path(self, row):
        """Raw originals live in the transactional store; disk is a verified local cache."""
        path = self.upload_dir / row["stored_name"]
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]:
            return path
        with self.store.connection() as db:
            blob = db.execute(
                "SELECT content,sha256 FROM company_file_blobs WHERE document_id=?", (row["id"],)
            ).fetchone()
        if (
            not blob
            or hashlib.sha256(bytes(blob[0])).hexdigest() != row["sha256"]
            or blob[1] != row["sha256"]
        ):
            raise UploadError("document_original_unavailable")
        temporary = path.with_suffix(path.suffix + "." + new_id("cache"))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(bytes(blob[0]))
        temporary.replace(path)
        return path

    def file_row(self, company_id, document_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM company_files WHERE id=? AND company_id=?", (document_id, company_id)
            ).fetchone()
        if not row:
            raise NotFound("document_not_found")
        return dict(row)

    def file_info(self, company_id, document_id):
        row = self.file_row(company_id, document_id)
        with self.store.connection() as db:
            job = db.execute(
                "SELECT * FROM document_jobs WHERE document_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            attempts = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id IN (SELECT id FROM document_jobs WHERE document_id=?)",
                (document_id,),
            ).fetchone()[0]
        progress = json.loads(job["progress_json"]) if job else {}
        return {
            "document_id": row["id"],
            "company_id": company_id,
            "name": row["name"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "created_at": row["created_at"],
            "status": "EXCLUDED" if row["status"] == "EXCLUDED" else job["status"] if job else row["status"],
            "phase": job["phase"] if job else None,
            "error_code": job["error_code"] if job else None,
            "next_eligible_at": job["next_eligible_at"] if job else None,
            "feature_count": len(json.loads(row["features_json"] or "[]")),
            "issues": json.loads(row["issues_json"]),
            "llm_calls": attempts,
            "progress": {
                "processed_packets": progress.get("processed_packets", 0),
                "total_packets": progress.get("total_packets"),
            },
        }

    def files(self, company_id):
        with self.store.connection() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM company_files WHERE company_id=? ORDER BY created_at DESC", (company_id,)
                )
            ]
        return [self.file_info(company_id, did) for did in ids]

    def claim_document(self):
        now = utcnow()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE document_jobs SET status='FAILED',error_code='interrupted_manual_retry' WHERE status='RUNNING' AND lease_until<?",
                (now.timestamp(),),
            )
            db.execute(
                "UPDATE document_jobs SET status='QUEUED' WHERE status='DEFERRED_BUDGET' AND next_eligible_at<=?",
                (now.isoformat(),),
            )
            if (
                db.execute("SELECT 1 FROM runs WHERE status='RUNNING'").fetchone()
                or db.execute("SELECT 1 FROM document_jobs WHERE status='RUNNING'").fetchone()
                or (
                    db.execute("SELECT 1 FROM sqlite_master WHERE name='research_jobs'").fetchone()
                    and db.execute("SELECT 1 FROM research_jobs WHERE status='RUNNING'").fetchone()
                )
            ):
                return None
            row = db.execute(
                "SELECT * FROM document_jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE document_jobs SET status='RUNNING',lease_until=? WHERE id=?",
                (now.timestamp() + self.settings.lease_seconds, row["id"]),
            )
            return dict(row)

    def update_job(self, job_id, phase, progress, status="RUNNING", error=None, retry_at=None):
        with self.store.connection() as db:
            db.execute(
                "UPDATE document_jobs SET phase=?,progress_json=?,status=?,error_code=?,next_eligible_at=?,lease_until=? WHERE id=?",
                (
                    phase,
                    json.dumps(progress, ensure_ascii=False),
                    status,
                    error,
                    retry_at,
                    utcnow().timestamp() + self.settings.lease_seconds,
                    job_id,
                ),
            )
            db.execute(
                "UPDATE company_files SET status=? WHERE id=(SELECT document_id FROM document_jobs WHERE id=?)",
                (status, job_id),
            )

    def finish_document(self, job, document, chunks, features, evidence, issues):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status FROM document_jobs WHERE id=?", (job["id"],)).fetchone()
            if not current or current[0] != "RUNNING":
                raise Conflict("document_job_changed")
            db.execute(
                "UPDATE company_files SET status='READY',document_json=?,chunks_json=?,features_json=?,evidence_json=?,issues_json=? WHERE id=?",
                (
                    document.model_dump_json(),
                    json.dumps(chunks, ensure_ascii=False),
                    json.dumps([f.model_dump(mode="json") for f in features], ensure_ascii=False),
                    json.dumps([e.model_dump(mode="json") for e in evidence], ensure_ascii=False),
                    json.dumps(issues, ensure_ascii=False),
                    job["document_id"],
                ),
            )
            db.execute(
                "INSERT INTO company_versions VALUES(?,2) ON CONFLICT(company_id) DO UPDATE SET version=company_versions.version+1",
                (job["company_id"],),
            )
            db.execute(
                "UPDATE document_jobs SET status='READY',phase='complete',error_code=NULL WHERE id=?",
                (job["id"],),
            )
            db.execute(
                "UPDATE attempts SET status='consumed' WHERE run_id=? AND status='received'", (job["id"],)
            )

    def retry_document(self, company_id, document_id):
        self.require_writable(company_id)
        self.file_row(company_id, document_id)
        info = self.file_info(company_id, document_id)
        if info["status"] not in {"FAILED", "READY"}:
            raise Conflict("document_cannot_retry_while_processing")
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO document_jobs(id,company_id,document_id,status,phase,created_at) VALUES(?,?,?,'QUEUED','parse',?)",
                (new_id("ingest"), company_id, document_id, utcnow().isoformat()),
            )
            db.execute("UPDATE company_files SET status='QUEUED' WHERE id=?", (document_id,))
        return self.file_info(company_id, document_id)

    def set_file_active(self, company_id, document_id, active):
        self.require_writable(company_id)
        row = self.file_row(company_id, document_id)
        allowed = "EXCLUDED" if active else "READY"
        target = "READY" if active else "EXCLUDED"
        if row["status"] == target:
            return self.file_info(company_id, document_id)
        if row["status"] != allowed:
            raise Conflict("only_ready_document_can_be_excluded_or_restored")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE company_files SET status=? WHERE id=? AND company_id=? AND status=?",
                (target, document_id, company_id, allowed),
            ).rowcount
            if changed != 1:
                raise Conflict("document_state_changed")
            db.execute(
                "INSERT INTO company_versions VALUES(?,2) ON CONFLICT(company_id) DO UPDATE SET version=company_versions.version+1",
                (company_id,),
            )
        return self.file_info(company_id, document_id)
