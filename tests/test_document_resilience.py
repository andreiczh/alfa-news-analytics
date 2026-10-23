import asyncio
import json

import httpx

from alfa_agent.gateway import LiveGateway
from alfa_agent.ingestion import DocumentProcessor, ExtractedFacts


class InvalidExtractor:
    mode = "test_double"

    def __init__(self):
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        return "Deliberately malformed model output."


def test_readable_text_survives_extractor_failure_without_fabricated_features(service):
    cid = service.workspace.create_company("Синтетическая проверка отказа")["company_id"]
    text = "Синтетический отчёт. Остаток плавающего долга: 1200 рублей."
    document, _ = service.workspace.add_file(cid, "resilience.txt", text.encode())
    gateway = InvalidExtractor()
    asyncio.run(DocumentProcessor(service.workspace, gateway).once())
    info = service.workspace.file_info(cid, document["document_id"])
    assert gateway.calls == 2
    assert info["status"] == "READY" and info["feature_count"] == 0
    assert any("не прошли проверку формата" in issue for issue in info["issues"])
    snapshot = service.workspace.snapshot(cid, service.corpus)
    assert text in snapshot["document_text"][document["document_id"]]
    assert not any(
        f["name"] == "floating_debt_rub" and f["numeric_value"] for f in snapshot["profile"]["features"]
    )


def test_profile_extraction_uses_allowed_structured_model(service):
    def response(request):
        body = json.loads(request.content)
        assert body["model"] == service.settings.research_model
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "profile_extractor"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"facts":[],"issues":[]}'}, "finish_reason": "stop"}]},
        )

    async def run():
        gateway = LiveGateway(service.settings, service.store, transport=httpx.MockTransport(response))
        try:
            raw = await gateway.complete(
                "synthetic_document", "profile_extractor", {}, ExtractedFacts.model_json_schema()
            )
            assert not ExtractedFacts.model_validate_json(raw).facts
        finally:
            await gateway.close()

    asyncio.run(run())
