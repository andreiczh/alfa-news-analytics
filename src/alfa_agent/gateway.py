from __future__ import annotations

from datetime import datetime
import hashlib
import json

import httpx

from .settings import BASE_URL, MODELS, PACKAGE, Settings, next_reset, read_key, utcnow
from .storage import Store


class GatewayError(Exception):
    def __init__(self, code, retry_at=None):
        self.code = code
        self.retry_at = retry_at
        super().__init__(code)


class LiveGateway:
    mode = "live"

    def __init__(self, settings: Settings, store: Store, transport=None):
        if settings.model not in MODELS or settings.research_model not in MODELS:
            raise ValueError("model_not_allowed")
        self.settings, self.store = settings, store
        self.client = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=1),
            transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def complete(self, run_id, stage, payload, schema, repair=None, *, images=None, model=None):
        selected_model = model or (
            self.settings.research_model
            if stage.startswith("research_") or stage == "profile_extractor"
            else self.settings.model
        )
        if selected_model not in MODELS:
            raise ValueError("model_not_allowed")
        if images and (stage != "document_ocr" or selected_model != "qwen/qwen3-vl-30b-a3b-instruct"):
            raise ValueError("vision_route_not_allowed")
        circuit = self.store.meta("budget_until")
        if circuit and datetime.fromisoformat(circuit) > utcnow():
            raise GatewayError("shared_budget_exhausted", circuit)
        key = read_key(self.settings.root)
        if not key:
            raise GatewayError("api_key_missing")
        common = (PACKAGE / "prompts/common_system.md").read_text()
        instruction = (PACKAGE / "prompts" / (stage + ".md")).read_text()
        if stage in {"controller", "analyst", "verifier"}:
            instruction += "\nВ runtime доступны роли treasurer, cfo, ceo, coo, cco."
        messages = [
            {
                "role": "system",
                "content": common
                + "\n"
                + instruction
                + "\nOUTPUT_SCHEMA:\n"
                + json.dumps(schema, ensure_ascii=False),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        if repair:
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "repair_instruction": (PACKAGE / "prompts/repair_json.md").read_text(),
                            "previous_untrusted_output": repair["raw"],
                            "validation_errors": repair["errors"],
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        # This bound covers schema + instructions + data, not just source text.
        if sum(len(m["content"]) for m in messages) > (90000 if stage == "research_narrative" else 60000):
            raise GatewayError("context_limit")
        if images:
            import base64

            if len(images) != 1 or len(images[0]) > 4_000_000:
                raise GatewayError("image_limit")
            text = messages[1]["content"]
            messages[1]["content"] = [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(images[0]).decode()},
                },
            ]
        request = {
            "model": selected_model,
            "messages": messages,
            "max_tokens": {
                "controller": 6000,
                "analyst": 9000,
                "verifier": 5000,
                "profile_extractor": 6000,
                "document_ocr": 6000,
                "research_plan": 3500,
                "research_write": 8000,
                "research_verify": 4500,
                "context_interview": 6000,
                "research_narrative": 10000,
            }.get(stage, 3000),
        }
        if stage.startswith(("research_", "context_")) or stage == "profile_extractor":
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": stage, "strict": True, "schema": schema},
            }
        if self.settings.disable_reasoning:
            request["reasoning"] = {"enabled": False}
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        attempt = self.store.attempt_start(run_id, stage, digest, selected_model)
        try:
            async with self.client.stream(
                "POST",
                BASE_URL + "/chat/completions",
                json=request,
                headers={"Authorization": "Bearer " + key},
            ) as response:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        raise GatewayError("response_limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if "budget has been exceeded" in raw.decode(errors="replace").lower():
                    reset = next_reset(utcnow()).isoformat()
                    self.store.meta("budget_until", reset)
                    raise GatewayError("shared_budget_exhausted", reset)
                if response.status_code in (401, 403):
                    raise GatewayError("api_auth_failed")
                if response.status_code == 429:
                    raise GatewayError("rate_limited")
                if response.status_code >= 500:
                    raise GatewayError("provider_unavailable")
                if response.status_code != 200:
                    raise GatewayError("api_request_rejected")
                try:
                    body = json.loads(raw)
                    msg = body["choices"][0]["message"]["content"]
                    finish = body["choices"][0].get("finish_reason")
                except (ValueError, KeyError, IndexError, TypeError):
                    raise GatewayError("provider_response_invalid") from None
                usage = {
                    k: v
                    for k, v in body.get("usage", {}).items()
                    if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(v) is int
                }
                self.store.attempt_end(attempt, "received", usage)
                self.store.audit(
                    run_id,
                    stage.upper(),
                    "model_response",
                    {
                        "finish_reason": finish,
                        "content_characters": len(msg) if isinstance(msg, str) else 0,
                        "usage": usage,
                    },
                )
                if finish == "length":
                    raise GatewayError("model_output_truncated")
                if not isinstance(msg, str):
                    raise GatewayError("model_content_missing")
                return msg
        except (httpx.TimeoutException, httpx.TransportError):
            self.store.attempt_end(attempt, "unknown")
            raise GatewayError("external_result_unknown") from None
        except GatewayError as error:
            self.store.attempt_end(attempt, error.code)
            raise
