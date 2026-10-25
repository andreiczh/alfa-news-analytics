from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PACKAGE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ALFA_PROJECT_DIR", PACKAGE.parents[1])).resolve()
BASE_URL = "https://changellenge.up.railway.app/v1"
MODELS = frozenset(
    {
        "google/gemma-4-26b-a4b-it",
        "z-ai/glm-5.3-flash",
        "qwen/qwen3.6-35b-a3b",
        "deepseek/deepseek-v4.1-flash",
        "qwen/qwen3-vl-30b-a3b-instruct",
        "moonshotai/kimi-k2.6",
    }
)
MOSCOW = ZoneInfo("Europe/Moscow")


def utcnow():
    from datetime import timezone

    return datetime.now(timezone.utc)


def next_reset(now: datetime) -> datetime:
    local = now.astimezone(MOSCOW)
    candidate = local.replace(hour=3, minute=0, second=0, microsecond=0)
    return candidate if candidate > local else candidate + timedelta(days=1)


def read_key(root: Path = ROOT) -> str:
    key = os.environ.get("ALFA_LLM_API_KEY", "").strip()
    if key:
        return key
    path = root / ".env.local"
    if path.is_file():
        for line in path.read_text().splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "ALFA_LLM_API_KEY":
                return value.strip().strip('"').strip("'")
    return ""


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    state_dir: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["ALFA_STATE_DIR"]).resolve() if os.environ.get("ALFA_STATE_DIR") else None
        )
    )
    model: str = os.environ.get("ALFA_LLM_MODEL", "qwen/qwen3.6-35b-a3b")
    database_url: str = field(default_factory=lambda: os.environ.get("ALFA_DATABASE_URL", ""), repr=False)
    database_schema: str = field(default_factory=lambda: os.environ.get("ALFA_DATABASE_SCHEMA", "public"))
    research_model: str = os.environ.get("ALFA_RESEARCH_MODEL", "z-ai/glm-5.3-flash")
    disable_reasoning: bool = True
    max_calls: int = 12
    max_tools: int = 8
    max_replans: int = 2
    active_seconds: int = 300
    timeout_seconds: int = 90
    lease_seconds: int = 120

    @property
    def state_path(self):
        return self.state_dir or self.root / "var"

    @property
    def public_deployment(self):
        return os.environ.get("RENDER") == "true" or os.environ.get("ALFA_PUBLIC_DEPLOYMENT") == "1"

    @property
    def allowed_hosts(self):
        hosts = ["127.0.0.1", "localhost", "testserver"]
        external = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
        configured = os.environ.get("ALFA_ALLOWED_HOSTS", "")
        for host in [external, *configured.split(",")]:
            host = host.strip().lower()
            if not host:
                continue
            if not all(c.isalnum() or c in ".-" for c in host) or host.startswith("."):
                raise ValueError("invalid_allowed_host")
            hosts.append(host)
        return hosts

    @property
    def db_path(self):
        return self.state_path / "agent.sqlite3"

    @property
    def bundle_path(self):
        return self.root / "data" / "demo_bundle.json"
