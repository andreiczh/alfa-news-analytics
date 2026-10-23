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

    def search(self, query, limit=6):
        wanted = terms(query)
        ranked = []
        for document in self.documents:
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
