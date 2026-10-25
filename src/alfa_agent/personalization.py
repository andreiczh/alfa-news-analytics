"""One persisted company/role preference; roles never grant data access."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from .identity import current_principal, role_for, PUBLIC_DEMO_COMPANIES
from .settings import utcnow

ROLE_NAMES = {
    "cfo": "Финансовый директор",
    "treasurer": "Казначей",
    "ceo": "Генеральный директор",
    "coo": "Операционный директор",
    "cco": "Коммерческий директор",
    "cmo": "Директор по маркетингу",
    "other": "Руководитель",
}


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: str = Field(min_length=1, max_length=100)
    role: str = Field(pattern="^(cfo|treasurer|ceo|coo|cco|cmo|other)$")
    job_title: str = Field(default="", max_length=120)


class Personalization:
    def __init__(self, service):
        self.service, self.store = service, service.store
        with self.store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS user_preferences(
                user_id TEXT PRIMARY KEY,company_id TEXT NOT NULL,role TEXT NOT NULL,
                job_title TEXT NOT NULL,updated_at TEXT NOT NULL)""")

    def get(self):
        principal = current_principal.get()
        if not principal:
            return {"company_id": None, "role": "cfo", "job_title": ROLE_NAMES["cfo"]}
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM user_preferences WHERE user_id=?", (principal.user_id,)
            ).fetchone()
        if (
            row
            and row["company_id"] not in PUBLIC_DEMO_COMPANIES
            and self.service.workspace.custom_exists(row["company_id"])
            and not self.service.workspace.owned(row["company_id"])
        ):
            with self.store.connection() as db:
                db.execute(
                    "UPDATE user_preferences SET company_id=?,updated_at=? WHERE user_id=?",
                    (principal.company_id, utcnow().isoformat(), principal.user_id),
                )
                row = db.execute(
                    "SELECT * FROM user_preferences WHERE user_id=?", (principal.user_id,)
                ).fetchone()
        return (
            dict(row)
            if row
            else {
                "company_id": principal.company_id,
                "role": role_for(principal.job_title),
                "job_title": principal.job_title,
            }
        )

    def save(self, payload):
        principal = current_principal.get()
        self.service.authorize_company(payload.company_id)
        title = payload.job_title.strip() if payload.role == "other" else ROLE_NAMES[payload.role]
        title = title or ROLE_NAMES[payload.role]
        if principal:
            with self.store.connection() as db:
                db.execute(
                    """INSERT INTO user_preferences VALUES(?,?,?,?,?) ON CONFLICT(user_id)
                    DO UPDATE SET company_id=excluded.company_id,role=excluded.role,
                    job_title=excluded.job_title,updated_at=excluded.updated_at""",
                    (principal.user_id, payload.company_id, payload.role, title, utcnow().isoformat()),
                )
        return {"company_id": payload.company_id, "role": payload.role, "job_title": title}


def marked_test_context(text):
    return bool(
        re.search(
            r"(?:^|[\n.!?:])\s*(?:(?:это|явно)\s+)?(?:синтетич\w*\s+(?:тестов\w*\s+)?(?:профил|данн|компани|сведен)|тестов\w*\s+профил)",
            text,
            re.I,
        )
    )


def is_bank_company(name, snapshot):
    """Use the declared industry as well as the name; a mention of a lender is no exposure."""
    features = {
        f["name"]: f
        for f in snapshot.get("profile", {}).get("features", [])
        if f.get("status") not in {"missing", "conflicting"}
    }
    industry = features.get("industry", {}).get("text_value") or ""
    field = snapshot.get("context", {}).get("fields", {}).get("industry", {})
    industry += " " + field.get("value", "")
    return bool(
        re.search(r"банк\b|bank\b", name, re.I)
        or re.search(r"\bбанк\b|банковск|денежн\w*\s+посреднич|кредитн\w*\s+организац", industry, re.I)
    )
