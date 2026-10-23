from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3

from .domain import CompanyProfile, DocumentRef, Evidence, EventBrief, SourceClaim


class AccessDenied(Exception):
    pass


class Corpus:
    """A small real-source corpus. Private documents are filtered before use."""

    def __init__(self, path):
        raw = json.loads(path.read_text())
        self.source_bundle = raw
        self.workspace = None
        self.company_metadata = {}
        self.metadata = {
            **raw["metadata"],
            "scope": "Static news/bank corpus and legacy demo bundle; active company provenance is recorded separately in company_context.",
            "company_profiles": "The original bundle contains synthetic demo profiles. Active case/upload profiles are identified separately in company_context.",
        }
        self.documents = {x["document_id"]: DocumentRef.model_validate(x) for x in raw["documents"]}
        self.evidence = {x["evidence_id"]: Evidence.model_validate(x) for x in raw["evidence"]}
        self.claims = {x["claim_id"]: SourceClaim.model_validate(x) for x in raw["bank_claims"]}
        self.profiles = {x["company_id"]: CompanyProfile.model_validate(x) for x in raw["companies"]}
        self.events = {x["event_id"]: EventBrief.model_validate(x) for x in raw["events"]}
        self.content = raw["document_text"]
        self.index = sqlite3.connect(":memory:", check_same_thread=False)
        self.index.execute(
            "CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,kind UNINDEXED,text,tokenize='unicode61')"
        )
        for claim in self.claims.values():
            if claim.document_id not in self.documents or not set(claim.evidence_ids) <= self.evidence.keys():
                raise ValueError("corpus_claim_reference_invalid")
            self.index.execute(
                "INSERT INTO search VALUES(?,?,?)",
                (claim.claim_id, "bank", self.documents[claim.document_id].title + " " + claim.text),
            )
        for doc in self.documents.values():
            self.index.execute(
                "INSERT INTO search VALUES(?,?,?)",
                (doc.document_id, "document", self.content[doc.document_id]),
            )
        self.index.commit()

    def register_snapshot(self, snapshot):
        profile = CompanyProfile.model_validate(snapshot["profile"])
        accepted = {d["document_id"] for d in snapshot["documents"]}
        accepted_evidence = {e["evidence_id"] for e in snapshot["evidence"]}
        company_documents = {
            did for did, doc in self.documents.items() if doc.tenant_company_id == profile.company_id
        }
        obsolete = {
            did
            for did, doc in self.documents.items()
            if doc.tenant_company_id == profile.company_id and did not in accepted
        }
        for did in obsolete:
            self.documents.pop(did, None)
            self.content.pop(did, None)
            self.index.execute("DELETE FROM search WHERE id=? AND kind='document'", (did,))
        for eid in [
            eid
            for eid, e in self.evidence.items()
            if e.document_id in company_documents and eid not in accepted_evidence
        ]:
            self.evidence.pop(eid, None)
        self.profiles[profile.company_id] = profile
        self.company_metadata[profile.company_id] = snapshot["metadata"]
        for raw in snapshot["documents"]:
            doc = DocumentRef.model_validate(raw)
            if doc.tenant_company_id != profile.company_id:
                raise AccessDenied("snapshot_company_mismatch")
            self.documents[doc.document_id] = doc
            self.content[doc.document_id] = snapshot["document_text"][doc.document_id]
            self.index.execute("DELETE FROM search WHERE id=? AND kind='document'", (doc.document_id,))
            self.index.execute(
                "INSERT INTO search VALUES(?,?,?)",
                (doc.document_id, "document", self.content[doc.document_id]),
            )
        for raw in snapshot["evidence"]:
            item = Evidence.model_validate(raw)
            if item.document_id not in {d["document_id"] for d in snapshot["documents"]}:
                raise AccessDenied("snapshot_evidence_mismatch")
            self.evidence[item.evidence_id] = item
        self.index.commit()

    def load_run_snapshot(self, request):
        snapshot_id = request.get("profile_snapshot_id")
        if snapshot_id:
            if not self.workspace:
                raise AccessDenied("snapshot_store_missing")
            snapshot = self.workspace.load_snapshot(snapshot_id, request["company_id"])
            self.register_snapshot(snapshot)

    def close(self):
        self.index.close()

    def authorize(self, document_id, company_id, as_of):
        document = self.documents.get(document_id)
        if (
            not document
            or document.tenant_company_id not in (None, company_id)
            or document.available_at > as_of
        ):
            raise AccessDenied("evidence_not_accessible")
        return document

    def read_evidence(self, ids, company_id, as_of):
        result = []
        for evidence_id in ids:
            evidence = self.evidence.get(evidence_id)
            if not evidence:
                raise AccessDenied("evidence_not_accessible")
            self.authorize(evidence.document_id, company_id, as_of)
            result.append(evidence)
        return result

    def search(self, query, kind, company_id, as_of, limit=5):
        words = re.findall(r"[a-zа-яё0-9]{3,}", query.lower())[:20]
        if not words:
            return []
        # Query is data: all tokens are quoted; LLM cannot supply SQL/FTS operators.
        match = " OR ".join('"' + (word[:-1] if len(word) > 5 else word) + '"*' for word in words)
        rows = self.index.execute(
            "SELECT id FROM search WHERE search MATCH ? AND kind=? ORDER BY rank LIMIT 100", (match, kind)
        )
        found = []
        for row in rows:
            item = self.claims[row[0]] if kind == "bank" else self.documents[row[0]]
            document_id = item.document_id
            try:
                self.authorize(document_id, company_id, as_of)
            except AccessDenied:
                continue
            found.append(item)
            if len(found) >= limit:
                break
        return found

    def profile(self, company_id):
        if company_id not in self.profiles:
            raise AccessDenied("company_not_accessible")
        return self.profiles[company_id].model_copy(deep=True)

    @staticmethod
    def as_of(raw):
        return datetime.fromisoformat(raw)
