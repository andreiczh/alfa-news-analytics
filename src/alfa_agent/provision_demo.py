"""One-shot operator provisioning. No HTTP endpoint and no automatic startup hook.

Run ``python -m alfa_agent.provision_demo`` with ALFA_PROVISION_ACCOUNT containing
login, password_hash, full_name, company and job_title. Remove the variable after
successful provisioning. An absent configuration is a no-op for safe restarts.
"""

import json
import os

from .accounts import AuthError
from .service import Service
from .settings import Settings


def main():
    raw = os.environ.pop("ALFA_PROVISION_ACCOUNT", "")
    if not raw:
        return
    try:
        values = json.loads(raw)
        expected = {"login", "password_hash", "full_name", "company", "job_title"}
        if not isinstance(values, dict) or set(values) != expected:
            raise ValueError
        if any(not isinstance(value, str) or len(value) > 256 for value in values.values()):
            raise ValueError
    except (ValueError, TypeError):
        raise SystemExit("Invalid provisioning configuration") from None
    service = Service(Settings())
    try:
        service.accounts.provision_demo(
            values["login"],
            values["password_hash"],
            values["full_name"],
            values["company"],
            values["job_title"],
        )
    except (AuthError, ValueError):
        raise SystemExit("Demo provisioning refused; no existing account was changed") from None
    finally:
        service.corpus.close()
        service.store.close()
    print("Demo account provisioning completed")


if __name__ == "__main__":
    main()
