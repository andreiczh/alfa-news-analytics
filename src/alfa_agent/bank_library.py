"""Case bank library: original dates and page OCR, never an undated bank opinion."""

from __future__ import annotations

import hashlib
import json
import re


def terms(text):
    return {w[:5] if len(w) > 5 else w[:4] for w in re.findall(r"[a-zа-яё]+", text.casefold()) if len(w) > 3}


class BankLibrary:
    def __init__(self, root):
        path = root / "data/bank_library.json"
        self.documents = json.loads(path.read_text()) if path.is_file() else []

    def summary(self):
        return [{k: v for k, v in d.items() if k != "pages"} for d in self.documents]

    def search(self, query, limit=6, document_id=None):
        wanted = terms(query)
        ranked = []
        for document in self.documents:
            if document_id and document["document_id"] != document_id:
                continue
            for page in document["pages"]:
                lines = page["text"].splitlines()
                if (
                    "дисклеймер" in " ".join(lines[:3]).casefold()
                    or "контакты" in " ".join(lines[:3]).casefold()
                ):
                    continue
                blocks, current = [], []
                for line in lines:
                    if sum(map(len, current)) + len(line) > 1300 and current:
                        blocks.append("\n".join(current))
                        current = current[-2:]
                    current.append(line)
                if current:
                    blocks.append("\n".join(current))
                for index, text in enumerate(blocks):
                    # No graph interpolation, table row reconstruction or contact-card facts.
                    if len(text) < 120 or len(re.findall(r"[А-Яа-яA-Za-z]{4,}", text)) < 15:
                        continue
                    narrative = sum(len(line) for line in text.splitlines() if len(line) >= 45) / len(text)
                    if narrative < 0.45:
                        continue
                    score = len(terms(text) & wanted) + narrative * 2
                    if not score:
                        continue
                    historical = (document.get("published_at") or "2026")[:4] < "2026"
                    ranked.append((score - (3 if historical else 0), document, page, index, text))
        ranked.sort(key=lambda x: x[0], reverse=True)
        result, per_document = [], {}
        for score, doc, page, index, text in ranked:
            if per_document.get(doc["document_id"], 0) >= 2:
                continue
            per_document[doc["document_id"]] = per_document.get(doc["document_id"], 0) + 1
            sid = doc["document_id"] + f"_p{page['page']}_b{index}"
            result.append(
                {
                    "source_id": sid,
                    "document_id": doc["document_id"],
                    "title": doc["title"],
                    "kind": "bank_excerpt",
                    "publisher": doc["publisher"],
                    "url": doc["url"],
                    "published_at": doc["published_at"],
                    "publication_date_raw": doc["publication_date_raw"],
                    "page": page["page"],
                    "text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "review_status": "ocr_pending_review",
                    "extraction_method": "native_ocr",
                    "limitations": "OCR материала кейса. Автор конкретного тезиса, модальность и период должны быть проверены по цитате. Табличные/графические числа без явной текстовой связи не использовать.",
                }
            )
            if len(result) >= limit:
                break
        return result

    def recommend(self, snapshot, role):
        from .news_ranking import company_context, ROLE_WEIGHTS, TOPIC_RULES
        from .personalization import ROLE_NAMES

        context = company_context(snapshot)
        weights = ROLE_WEIGHTS.get(role, ROLE_WEIGHTS["other"])
        wanted = set(context.get("priority_topics", [])) | set(context.get("exposure_topics", []))
        descriptions = {
            "bank_case_01": (
                ["demand"],
                "Изменения потребления и спроса: сопоставьте с целевыми сегментами и планом продаж.",
            ),
            "bank_case_02": (
                ["strategy", "demand"],
                "Исторический отраслевой пример; показатели не описывают текущую ситуацию.",
            ),
            "bank_case_03": (
                ["inflation", "demand", "earnings"],
                "Макроэкономические предпосылки для сценариев спроса, затрат и бюджета.",
            ),
            "bank_case_04": (
                ["rates", "funding"],
                "Датированный взгляд на ставку: проверьте предпосылки переоценки и финансирования.",
            ),
            "bank_case_05": (
                ["inflation", "demand", "earnings"],
                "Макроэкономический фон и исходные факторы планирования.",
            ),
            "bank_case_06": (
                ["funding", "rates", "fx", "strategy"],
                "Рынок долга и валют: сопоставьте с задачами фондирования и управления риском.",
            ),
            "bank_case_07": (
                ["inflation"],
                "Факторы изменения цен для проверки себестоимости, цен и потребительского спроса.",
            ),
            "bank_case_08": (
                ["fx", "strategy"],
                "Отраслевой обзор золота; применимость зависит от связи бизнеса с этим рынком.",
            ),
            "bank_case_09": (
                ["funding", "credit"],
                "Долговой рынок и эмитенты: экспозиции компании и актуальность условий проверяются отдельно.",
            ),
        }
        profile_text = " ".join(
            (f.get("text_value") or "")
            for f in snapshot.get("profile", {}).get("features", [])
            if f.get("status") not in {"missing", "conflicting"}
        )
        result = []
        for doc in self.summary():
            topics, use = descriptions.get(doc["document_id"], ([], "Применимость материала нужно уточнить."))
            matched = sorted(set(topics) & wanted)
            score = max((weights.get(t, 0) for t in topics), default=0) + len(matched) * 14
            reasons = []
            if matched:
                labels = [label for t in matched for label in context.get("priority_labels", {}).get(t, [])]
                reasons.append(
                    "Связано с вашим приоритетом: " + labels[0]
                    if labels
                    else "В профиле указан связанный фактор: " + TOPIC_RULES[matched[0]][0].lower()
                )
            if topics:
                best = max(topics, key=lambda t: weights.get(t, 0))
                reasons.append(
                    "Для роли «" + ROLE_NAMES.get(role, "Руководитель") + "»: " + TOPIC_RULES[best][0].lower()
                )
            specialized = doc["document_id"] in {"bank_case_02", "bank_case_08"}
            related = (
                bool(
                    re.search(
                        r"стал|металлург|руда"
                        if doc["document_id"] == "bank_case_02"
                        else r"золот|драгоцен|ювелир",
                        profile_text,
                        re.I,
                    )
                )
                if specialized
                else True
            )
            historical = (doc.get("published_at") or "2026")[:4] < "2026"
            if specialized and not related:
                score = 0
                reasons = ["Связь вашей компании с отраслью материала не подтверждена."]
            if historical:
                score = min(score, 5)
                reasons.append("Материал исторический; его прогноз нельзя переносить на текущий период.")
            result.append(
                {
                    **doc,
                    "relevance": {
                        "score": score,
                        "recommended": score >= 20 and not historical,
                        "reasons": reasons,
                        "use": use,
                        "topics": topics,
                        "scope": "Подбор по роли и подтверждённому вами контексту; не доказательство финансового эффекта.",
                    },
                }
            )
        return sorted(result, key=lambda d: d["relevance"]["score"], reverse=True)
