# Third-party notices

Runtime: Python (PSF), FastAPI (MIT), Starlette (BSD-3-Clause), HTTPX/httpcore/h11 (BSD-3-Clause/MIT), Pydantic (MIT), Uvicorn (BSD-3-Clause), SQLite (public domain). Transitive distributions and exact versions are recorded in `uv.lock` / `requirements.lock`; consult each installed distribution's license files for authoritative terms. Dev tools: pytest (MIT), Ruff (MIT). No OpenClaw/LangChain/LangGraph agent runtime is included.

LLMs are accessed through the organizer-provided API. Allowed model IDs come from the supplied case/API instruction. No model weights are distributed. Disabling reasoning is a request option tested through this gateway; OpenRouter's documentation is reference material, not proof of the underlying provider or its tariffs.

Case material: Альфа-Банк, «Ключевая ставка ЦБ РФ», 11 September 2026, page 1; source PDF SHA-256 `92a96805f2b720f76bd0c52d2491044812b38401ab62d0f4a9157b4ca81c49bb`. Only selected short source statements/evidence are included in the demonstration corpus, with pending human review. Full PDF is stored separately in the case source folder and is not copied into the Docker image. Source rights remain with the publisher; this is not a grant for public republication.

News: Пресс-служба Банка России, decision of 11 September 2026; the corpus contains a short curated excerpt and original source URL. The original version 0.1 company profiles are fictional synthetic development data. Version 0.2 additionally uses real anonymized case CSV records and explicitly user-bound documents, as described below.

## Version 0.2 additions

- python-multipart 0.0.32: Apache-2.0; upload handling.
- pypdf 6.21.2: BSD-3-Clause; local PDF text parsing.
- pypdfium2 5.14.0: distribution metadata identifies BSD-3-Clause, Apache-2.0 and dependency licenses; includes PDFium components. Refer to the bundled PDFium and third-party license files in that distribution.
- Pillow 13.0.0: MIT-CMU; bounded page images and clearly marked synthetic scan fixture.
- openpyxl 3.1.5 and et-xmlfile 2.0.0: MIT; XLSX reading without executing formulas or external links.
- DOCX/PPTX text parsing uses Python standard-library ZIP/XML readers; no Office runtime or macro execution.

The additional license identifiers above were checked against installed distribution metadata. Exact versions/hashes and transitive packages are in the lock files. System Arial is used through the operating system; no font files are bundled.

All three organizer-provided anonymized CSV files are used from the separate source folder, hashed and indexed locally. No attempt is made to reconstruct real identities. Unlike the original 0.1 demo, these case records now drive profiles and observed-network features. The old fictional demo profiles remain explicitly labelled; uploaded QA documents under `data/test_documents/` are also explicitly synthetic. They do not describe an actual customer and are not attached to a real case ID.

Miro: user-shared board uXjVEY6IJiQ=, read 23 October 2026. Textual design notes are retained locally with provenance; the board was not modified. Banking screenshot balance figures from Miro were not presented as real account data or copied into the application.

The allowed VL model was exercised through the organizer gateway for a one-page synthetic scanned PDF; no additional AI service or model was used. This confirms that tested route, not arbitrary vision capabilities or document-quality accuracy.
