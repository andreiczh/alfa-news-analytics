"""Create the authorized free demo using the user's authenticated Render CLI.

Secrets are read locally and sent directly over HTTPS, never logged or committed.
Run from the implementation root: python deploy/render.py --owner WORKSPACE_ID
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import httpx

from alfa_agent.settings import read_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", default="https://github.com/andreiczh/alfa-news-analytics")
    parser.add_argument("--name", default="alfa-business-pulse")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = Path(os.environ.get("RENDER_CLI_CONFIG_DIR", Path.home() / ".render")) / "cli.yaml"
    match = re.search(r"^\s+key:\s*(\S+)\s*$", config.read_text(), re.MULTILINE)
    if not match:
        raise SystemExit("Run render login first")
    render_token = match[1].strip("\"'")
    llm_key = read_key(root)
    access = next(
        line.partition("=")[2].strip()
        for line in (root / ".env.render").read_text().splitlines()
        if line.startswith("ALFA_ACCESS_PASSWORD=")
    )
    if not llm_key or len(access) < 16:
        raise SystemExit("Local secrets are not configured")

    def redact(value):
        result = str(value)
        for secret in (render_token, llm_key, access):
            result = result.replace(secret, "[redacted]")
        return result

    env = {
        "PYTHON_VERSION": "3.12.14", "PYTHONPATH": "src",
        "ALFA_STATE_DIR": "/tmp/alfa-state", "ALFA_STORAGE_MODE": "ephemeral_demo",
        "ALFA_PUBLIC_DEPLOYMENT": "1", "ALFA_LLM_API_KEY": llm_key,
        "ALFA_ACCESS_PASSWORD": access,
    }
    try:
        with httpx.Client(
            base_url="https://api.render.com/v1/",
            headers={"Authorization": "Bearer " + render_token}, timeout=60,
        ) as client:
            listing = client.get("services", params={"ownerId": args.owner, "limit": 100})
            if listing.status_code != 200:
                raise SystemExit(redact(f"Render listing failed ({listing.status_code}): {listing.text}"))
            existing = [
                item["service"] for item in listing.json()
                if item["service"]["name"] == args.name
            ]
            if existing:
                service, deploy_id = existing[0], None
                if service.get("repo") != args.repo:
                    raise SystemExit("The service name is already used by another repository")
            else:
                response = client.post("services", json={
                    "type": "web_service", "name": args.name, "ownerId": args.owner,
                    "repo": args.repo, "branch": "main", "autoDeployTrigger": "off",
                    "envVars": [{"key": key, "value": value} for key, value in env.items()],
                    "serviceDetails": {
                        "runtime": "python", "plan": "free", "region": "frankfurt",
                        "numInstances": 1, "healthCheckPath": "/api/health",
                        "envSpecificDetails": {
                            "buildCommand": "pip install --require-hashes -r requirements.lock && python -m alfa_agent build-case-index",
                            "startCommand": "python -m alfa_agent serve",
                        },
                    },
                })
                if response.status_code != 201:
                    raise SystemExit(redact(f"Render create failed ({response.status_code}): {response.text}"))
                body = response.json()
                service, deploy_id = body["service"], body.get("deployId")
    except httpx.HTTPError as error:
        raise SystemExit(redact(str(error)) + "; inspect services before retrying a create") from None

    metadata = {
        "service_id": service["id"], "url": service["serviceDetails"]["url"],
        "dashboard_url": service.get("dashboardUrl"), "deploy_id": deploy_id,
        "repo": args.repo, "plan": "free", "storage": "ephemeral_demo",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    }
    (root / ".render").mkdir(exist_ok=True)
    (root / ".render/deployment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    access_path = root / "render-access.txt"
    access_path.write_text("Пульс бизнеса · доступ к демо Render\n" + metadata["url"] + "\nПароль: " + access + "\n")
    access_path.chmod(0o600)
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
