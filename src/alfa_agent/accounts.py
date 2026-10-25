"""Individual pilot accounts, revocable sessions and explicit workspace invitations.

Names and professional roles are self-reported. They never prove employment or
grant permissions. An invite is issued by a workspace owner, not inferred by name.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from .access import SESSION_SECONDS, session_nonce
from .corpus import AccessDenied
from .identity import Principal, profile_values, role_for, current_principal
from .settings import utcnow
from .storage import Conflict, new_id


class AuthError(ValueError):
    pass


class AuthLimited(AuthError):
    pass


def password_hash(password, salt=None):
    if not 12 <= len(password) <= 256:
        raise AuthError("Пароль должен содержать от 12 до 256 символов.")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
    return "pbkdf2_sha256$600000$" + salt + "$" + digest


def password_matches(password, encoded):
    try:
        algorithm, iterations, salt, digest = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or iterations != "600000" or len(password) > 256:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


_DUMMY_PASSWORD = password_hash("not-a-real-account-password")


class Accounts:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.dummy_password = _DUMMY_PASSWORD
        with self.store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,login TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0,
              auth_version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS account_sessions(id_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,
              auth_version INTEGER NOT NULL,expires_at REAL NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sessions_user ON account_sessions(user_id);
            CREATE TABLE IF NOT EXISTS auth_limits(key TEXT PRIMARY KEY,window_start REAL NOT NULL,count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS company_invites(id_hash TEXT PRIMARY KEY,company_id TEXT NOT NULL,
              permission TEXT NOT NULL,created_by TEXT NOT NULL,expires_at REAL NOT NULL,
              used_by TEXT,used_at TEXT,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS security_events(id TEXT PRIMARY KEY,user_id TEXT,
              event TEXT NOT NULL,company_id TEXT,created_at TEXT NOT NULL);
            """)

    def event(self, db, user_id, event, company_id=None):
        db.execute(
            "INSERT INTO security_events VALUES(?,?,?,?,?)",
            (new_id("security"), user_id, event, company_id, utcnow().isoformat()),
        )

    def limit(self, namespace, identity, maximum, seconds):
        key = hashlib.sha256((namespace + ":" + identity).encode()).hexdigest()
        now = time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM auth_limits WHERE key=?", (key,)).fetchone()
            count = row["count"] + 1 if row and row["window_start"] > now - seconds else 1
            start = row["window_start"] if row and row["window_start"] > now - seconds else now
            db.execute(
                "INSERT INTO auth_limits VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,count=excluded.count",
                (key, start, count),
            )
            db.execute("DELETE FROM auth_limits WHERE window_start<?", (now - 86400,))
        if count > maximum:
            raise AuthLimited("Слишком много попыток. Повторите позднее.")

    def register(self, login, password, full_name, company, job_title):
        login = login.strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", login):
            raise AuthError("Логин: от 3 до 64 латинских букв, цифр, точек, дефисов или подчёркиваний.")
        encoded = password_hash(password)
        name, company, title = profile_values(full_name, company, job_title)
        user_id, company_id = new_id("user"), new_id("company")
        principal = Principal(user_id, company_id, name, company, title, role_for(title))
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM accounts WHERE login=?", (login,)).fetchone():
                raise AuthError("Этот логин недоступен.")
            db.execute(
                "INSERT INTO accounts(id,login,password_hash,created_at) VALUES(?,?,?,?)",
                (user_id, login, encoded, utcnow().isoformat()),
            )
            now = utcnow().isoformat()
            db.execute("INSERT INTO custom_companies VALUES(?,?,?)", (company_id, company, now))
            db.execute("INSERT INTO company_versions VALUES(?,1)", (company_id,))
            db.execute("INSERT INTO company_access VALUES(?,?)", (company_id, user_id))
            db.execute("INSERT INTO company_members VALUES(?,?,?,1,?)", (company_id, user_id, "owner", now))
            db.execute(
                "INSERT INTO employee_profiles VALUES(?,?,?,?,?,?,?)",
                (user_id, company_id, name, company, title, principal.role, now),
            )
            self.event(db, user_id, "account_registered", principal.company_id)
        return principal

    def login(self, login, password):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM accounts WHERE login=?", (login.strip().casefold(),)).fetchone()
        valid = password_matches(password, row["password_hash"] if row else self.dummy_password)
        if not row or row["disabled"] or not valid:
            raise AuthError("Неверный логин или пароль.")
        with self.store.connection() as db:
            profile = db.execute("SELECT * FROM employee_profiles WHERE user_id=?", (row["id"],)).fetchone()
            if not profile:
                raise AuthError("Рабочее пространство недоступно. Обратитесь к администратору.")
            self.event(db, row["id"], "login")
        return Principal(**{key: profile[key] for key in Principal.__dataclass_fields__})

    def new_session(self, principal):
        sid = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (principal.user_id,)).fetchone()
            if not account or account["disabled"]:
                raise AuthError("Учётная запись недоступна.")
            db.execute("DELETE FROM account_sessions WHERE expires_at<?", (time.time(),))
            db.execute(
                "INSERT INTO account_sessions VALUES(?,?,?,?,?)",
                (
                    hashlib.sha256(sid.encode()).hexdigest(),
                    principal.user_id,
                    account["auth_version"],
                    time.time() + SESSION_SECONDS,
                    utcnow().isoformat(),
                ),
            )
        return sid

    def authenticate(self, cookie, key):
        sid = session_nonce(cookie, key)
        if not sid:
            return None
        with self.store.connection() as db:
            row = db.execute(
                """SELECT p.* FROM account_sessions s JOIN accounts a ON a.id=s.user_id
                JOIN employee_profiles p ON p.user_id=a.id
                WHERE s.id_hash=? AND s.expires_at>? AND a.disabled=0 AND s.auth_version=a.auth_version""",
                (hashlib.sha256(sid.encode()).hexdigest(), time.time()),
            ).fetchone()
        return Principal(**{k: row[k] for k in Principal.__dataclass_fields__}) if row else None

    def logout(self, cookie, key):
        sid = session_nonce(cookie, key)
        if sid:
            with self.store.connection() as db:
                db.execute(
                    "DELETE FROM account_sessions WHERE id_hash=?",
                    (hashlib.sha256(sid.encode()).hexdigest(),),
                )

    def change_password(self, old, new):
        principal = current_principal.get()
        with self.store.connection() as db:
            row = db.execute("SELECT password_hash FROM accounts WHERE id=?", (principal.user_id,)).fetchone()
        if not row or not password_matches(old, row[0]):
            raise AuthError("Неверный текущий пароль.")
        encoded = password_hash(new)
        with self.store.connection() as db:
            db.execute(
                "UPDATE accounts SET password_hash=?,auth_version=auth_version+1 WHERE id=?",
                (encoded, principal.user_id),
            )
            db.execute("DELETE FROM account_sessions WHERE user_id=?", (principal.user_id,))
            self.event(db, principal.user_id, "password_changed_sessions_revoked")

    def permission(self, company_id):
        principal = current_principal.get()
        if not principal:
            return None
        with self.store.connection() as db:
            row = db.execute(
                "SELECT permission FROM company_members WHERE company_id=? AND user_id=? AND active=1",
                (company_id, principal.user_id),
            ).fetchone()
        return row[0] if row else None

    def team(self, company_id):
        self.service.authorize_company(company_id)
        if self.permission(company_id) not in {"owner", "admin"}:
            raise AccessDenied("team_admin_required")
        with self.store.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    """SELECT m.user_id,m.permission,m.active,p.full_name,p.job_title
                FROM company_members m JOIN employee_profiles p ON p.user_id=m.user_id
                WHERE m.company_id=? ORDER BY m.joined_at""",
                    (company_id,),
                )
            ]

    def invite(self, company_id, permission):
        if self.permission(company_id) not in {"owner", "admin"} or permission not in {"member", "viewer"}:
            raise AccessDenied("team_admin_required")
        token = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO company_invites VALUES(?,?,?,?,?,NULL,NULL,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    company_id,
                    permission,
                    current_principal.get().user_id,
                    time.time() + 48 * 3600,
                    utcnow().isoformat(),
                ),
            )
            self.event(db, current_principal.get().user_id, "workspace_invite_created", company_id)
        return {"code": token, "expires_in_hours": 48, "permission": permission}

    def join(self, code):
        principal = current_principal.get()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            invite = db.execute(
                "SELECT * FROM company_invites WHERE id_hash=? AND expires_at>? AND used_by IS NULL",
                (hashlib.sha256(code.encode()).hexdigest(), time.time()),
            ).fetchone()
            if not invite:
                raise AuthError("Приглашение недействительно или уже использовано.")
            if db.execute(
                "SELECT 1 FROM company_members WHERE company_id=? AND user_id=? AND active=1",
                (invite["company_id"], principal.user_id),
            ).fetchone():
                raise Conflict("already_workspace_member")
            db.execute(
                "INSERT INTO company_members VALUES(?,?,?,?,?) ON CONFLICT(company_id,user_id) DO UPDATE SET permission=excluded.permission,active=1,joined_at=excluded.joined_at",
                (invite["company_id"], principal.user_id, invite["permission"], 1, utcnow().isoformat()),
            )
            db.execute(
                "UPDATE company_invites SET used_by=?,used_at=? WHERE id_hash=?",
                (principal.user_id, utcnow().isoformat(), invite["id_hash"]),
            )
            self.event(db, principal.user_id, "workspace_joined", invite["company_id"])
        return {"company_id": invite["company_id"], "permission": invite["permission"]}

    def revoke(self, company_id, user_id):
        if self.permission(company_id) not in {"owner", "admin"}:
            raise AccessDenied("team_admin_required")
        with self.store.connection() as db:
            target = db.execute(
                "SELECT permission FROM company_members WHERE company_id=? AND user_id=?",
                (company_id, user_id),
            ).fetchone()
            if not target or target[0] in {"owner", "admin"} or user_id == current_principal.get().user_id:
                raise AccessDenied("cannot_revoke_workspace_owner")
            db.execute(
                "UPDATE company_members SET active=0 WHERE company_id=? AND user_id=?", (company_id, user_id)
            )
            self.event(db, current_principal.get().user_id, "workspace_member_revoked", company_id)
        return {"revoked": True}
