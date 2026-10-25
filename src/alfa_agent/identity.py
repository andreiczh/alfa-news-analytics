"""A demo employee profile is display context; permissions come from owned IDs."""

from contextvars import ContextVar
from dataclasses import asdict, dataclass
import re

from .storage import new_id
from .settings import utcnow


@dataclass(frozen=True)
class Principal:
    user_id: str
    company_id: str
    full_name: str
    company_name: str
    job_title: str
    role: str

    def public(self):
        return asdict(self)


current_principal = ContextVar("alfa_principal", default=None)
PUBLIC_DEMO_COMPANIES = frozenset({"company_b28d88c6688d4a128111"})
PUBLIC_DEMO_RUNS = frozenset({"run_04efacff8b8545c581eb", "run_bcf66bb4e3564e4f9970"})


def role_for(title):
    value = title.casefold()
    for role, needles in {
        "treasurer": ("казнач", "treasur"),
        "cfo": ("финанс", "cfo"),
        "ceo": ("генераль", "ceo", "собственник"),
        "coo": ("операци", "coo", "закуп"),
        "cco": ("коммерч", "cco", "продаж"),
        "cmo": ("маркет", "cmo", "marketing"),
    }.items():
        if any(part in value for part in needles):
            return role
    return "other"


def profile_values(full_name, company_name, job_title):
    values = [" ".join(str(x).split()) for x in (full_name, company_name, job_title)]
    name, company, title = values
    if not 3 <= len(name) <= 100 or len(name.split()) < 2:
        raise ValueError("Укажите имя и фамилию.")
    if not 2 <= len(company) <= 160:
        raise ValueError("Укажите название компании, от 2 до 160 символов.")
    if not 2 <= len(title) <= 120:
        raise ValueError("Укажите вашу должность.")
    if any(re.search(r"[\x00-\x1f]", x) for x in values):
        raise ValueError("Уберите служебные символы из полей.")
    return name, company, title


def create_principal(workspace, full_name, company_name, job_title):
    name, company, title = profile_values(full_name, company_name, job_title)
    uid = new_id("user")
    cid = workspace.create_company(company, owner_id=uid)["company_id"]
    principal = Principal(uid, cid, name, company, title, role_for(title))
    restore_principal(workspace, principal)
    return principal


def restore_principal(workspace, principal):
    # A valid signed session can restore only its own empty profile after a Free restart.
    # It cannot restore documents or join another workspace by a matching company name.
    with workspace.store.connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO custom_companies VALUES(?,?,?)",
            (principal.company_id, principal.company_name, utcnow().isoformat()),
        )
        db.execute("INSERT OR IGNORE INTO company_versions VALUES(?,1)", (principal.company_id,))
        db.execute(
            "INSERT OR IGNORE INTO company_access VALUES(?,?)", (principal.company_id, principal.user_id)
        )
        owner = db.execute(
            "SELECT user_id FROM company_access WHERE company_id=?", (principal.company_id,)
        ).fetchone()
        if owner[0] != principal.user_id:
            raise ValueError("session_company_mismatch")
        db.execute(
            "INSERT INTO employee_profiles VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name,job_title=excluded.job_title,role=excluded.role",
            (
                principal.user_id,
                principal.company_id,
                principal.full_name,
                principal.company_name,
                principal.job_title,
                principal.role,
                utcnow().isoformat(),
            ),
        )
