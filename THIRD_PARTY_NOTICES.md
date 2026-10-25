# Third-party notices

## Version 0.6 additions

- psycopg 3.3.6, psycopg-binary 3.3.6, psycopg-pool 3.3.3: LGPL-3.0-only, as declared in installed distribution metadata. Bundled native components retain their own notices; consult the distributions' license files. Used for PostgreSQL connections and pooling, not agent orchestration.
- GitHub Actions checkout/setup-python (MIT) and setup-uv (MIT/Apache-2.0): CI only, pinned to the verified upstream commit IDs in the workflow. No organizer API credentials are provided to CI.
- Password derivation uses Python's standard-library PBKDF2-HMAC-SHA256; no additional authentication service or external LLM provider is introduced.

## Version 0.4 additions

- Bank of Russia public RSS: `https://www.cbr.ru/rss/RssPress`, `/rss/eventrss`, `/rss/RssNews`; official channel register: `https://cbr.ru/development/RSS/`.
- RBC public RSS: `https://rssexport.rbc.ru/rbcnews/news/30/full.rss`; Bing News RSS: `https://www.bing.com/news/search?format=rss`. Fallback public feed: `https://www.interfax.ru/rss.asp`. These are bounded non-AI discovery sources, with no asserted SLA or completeness. Publisher copyright and attribution remain with the publishers. The service stores source URLs/dates and bounded text for analysis; a headline is not treated as a read article.
- Collection, freshness checks, entity/role ranking, duplicate handling, cache, scheduling and permission checks are team-owned Python. No external AI search/reranking service, embedding API or ready-made agent runtime was added. Collection/ranking makes no LLM calls.
- Prettier 3.6.2 used only to format development files; it is not a production dependency. No dependency or source licence change implied by the feature.

Runtime: Python (PSF), FastAPI (MIT), Starlette (BSD-3-Clause), HTTPX/httpcore/h11 (BSD-3-Clause/MIT), Pydantic (MIT), Uvicorn (BSD-3-Clause), SQLite (public domain). Transitive distributions and exact versions are recorded in `uv.lock` / `requirements.lock`; consult each installed distribution's license files for authoritative terms. Dev tools: pytest (MIT), Ruff (MIT). No OpenClaw/LangChain/LangGraph agent runtime is included.

LLMs are accessed through the organizer-provided API. Allowed model IDs come from the supplied case/API instruction. No model weights are distributed. Disabling reasoning is a request option tested through this gateway; OpenRouter's documentation is reference material, not proof of the underlying provider or its tariffs.

Case materials: nine Alfa-Bank PDF files provided with the case, 64 pages total. `data/bank_library.json` contains page OCR text, file SHA-256, original URLs and dates. The original PDFs remain in the separate case source folder and are not copied into the image. Three rate-related claims also have selected structured attribution, pending human review. OCR is not complete claim/relation annotation or proof of source accuracy. Source rights remain with the publisher; this is not a grant for public republication. The application is restricted to the authorized hackathon team.

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

## Version 0.3 additions

Page OCR of the nine supplied bank PDFs was prepared locally with Apple Vision (OS framework; no weights, framework binaries or extra AI API are distributed). The runtime research loop uses the case-approved organizer models only. Bank OCR retains pending-review status; table/chart reconstruction is not treated as verified evidence.

Open-web discovery uses Bing's public search RSS and bounded reads of user-selected public HTTPS pages. Search headlines are leads, not confirmed facts. Public source availability and usage terms belong to their publishers. No paid search service, autonomous external agent, embedding service or outside LLM is part of the application. HTTPS/DNS/TLS, HTML and RSS parsing use Python standard-library modules.

Development-only formatting: Prettier 3.6.2 (MIT). Browser validation: Playwright/Chromium (Apache-2.0 and Chromium third-party licenses). These are development tools, not agent components or runtime dependencies in the application image.

Miro was rechecked through the supplied public board on 23 October 2026. Fifteen textual objects were recovered from that board; embedded screenshots are design references, not bank balances, supplied client profiles or a new organizer regulation.

## Version 0.5 additions

No runtime dependency, external AI provider or additional model was added. The context interview and coherent research narrative use the existing organizer-approved gateway/model. Personalization, reviewed context, scenario/evidence contracts and ranking rules are team-owned Python/JavaScript. The user-supplied product revision packet is retained as team requirements with its source hash; it is not an organizer rulebook. Live QA company goals are explicitly synthetic and do not describe Alfa-Bank's internal plans or financials.
