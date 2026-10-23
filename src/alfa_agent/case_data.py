"""Read-only source CSVs, indexed locally without inventing company identities."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .domain import Citation, CompanyProfile, DocumentRef, Evidence, FeatureCandidate, NumericValue, TimeScope
from .settings import utcnow

FILES = ("df_Uid_data_hack.csv", "df_Uid_payment_hack.csv", "df_Fid_Uid_link_hack.csv")
LABELS = {
    "ORG_TYPE": ("organization_type", "Тип организации"),
    "OKFS_NAME": ("ownership_form", "Форма собственности"),
    "COMPANY_AGE_YEARS_GRADATION": ("company_age_band", "Возраст компании"),
    "ECO_MARK_LABEL": ("business_stage", "Стадия бизнеса"),
    "MAINOKVED_CODE": ("okved_code", "ОКВЭД"),
    "MAINOKVED_NAME": ("industry", "Основная деятельность"),
    "AMOUNT_WORKERS_FNS_GRADATION": ("employees_band", "Численность по ФНС"),
    "AMOUNT_WORKERS_SPARK_GRADATION": ("employees_spark_band", "Численность по СПАРК"),
    "CL_TYPE": ("client_type", "Статус в наборе"),
    "GROUP_ID": ("group_id", "Группа в наборе"),
    "CITY": ("registration_city", "Город регистрации"),
    "REGION": ("registration_region", "Регион регистрации"),
    "FTS_COUNTRY_NAME": ("customs_countries", "Страны в данных ФТС"),
}
FLAGS = {
    "CL_PRODUCT_RKO_MARK": ("has_settlement", "РКО"),
    "CL_PRODUCT_GAR_MARK": ("has_guarantee", "Гарантии"),
    "CL_PRODUCT_LOA_MARK": ("has_loan", "Кредит"),
    "CL_PRODUCT_LEA_MARK": ("has_leasing", "Лизинг"),
    "CL_PRODUCT_DEP_MARK": ("has_deposit", "Депозит"),
    "CL_PRODUCT_ZPP_MARK": ("has_payroll", "Зарплатный проект"),
    "FACTORING_MARK": ("has_factoring", "Факторинг"),
    "FTS_FLAG": ("has_customs_record", "Сведения ФТС"),
    "NEGATIVEMARK": ("owner_information_flag", "Признак сведений о собственнике"),
}
DATA_WARNINGS = [
    "ID обезличены. Сопоставление с реальными названиями компаний не выполняется.",
    "Флаги продуктов не содержат суммы, валюты, сроки или формулы ставок.",
    "Единицы и период финансовых CSV не заданы в словаре ТЗ. Рубли/2025 год и период платежей до 31.10.2026 известны только из пересказа вебинара и требуют первичного подтверждения.",
    "Срез платежей неполный: связь не доказывает поставку, контроль, кредитную экспозицию или долю рынка.",
    "Платежи себе показаны отдельно; повторяющиеся пары и строки сохранены без произвольного удаления.",
    "Страны ФТС не устанавливают направление торговли или валюту; NEGATIVEMARK не трактуется как негативный риск.",
]


def decimal_value(raw):
    try:
        value = Decimal(str(raw).replace(",", "."))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


class CaseData:
    def __init__(self, settings, prefer_packaged=True):
        packaged = settings.root / "data/case"
        default_folder = packaged if packaged.is_dir() else settings.root.parent / "sources/data"
        self.folder = Path(os.environ.get("ALFA_CASE_DATA_DIR", default_folder))
        prepared = settings.root / "deploy/case-index/case.sqlite3"
        self.prebuilt = prefer_packaged and prepared.is_file()
        self.path = prepared if self.prebuilt else settings.state_path / "case.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.available = all((self.folder / n).is_file() for n in FILES)
        if self.available:
            if self.prebuilt:
                with self.connect() as db:
                    hashes = json.loads(db.execute("SELECT value FROM manifest WHERE key='hashes'").fetchone()[0])
                if any(hashlib.sha256((self.folder / name).read_bytes()).hexdigest() != hashes.get(name) for name in FILES):
                    raise ValueError("packaged_case_index_source_mismatch")
            else:
                self._ensure_index()

    @contextmanager
    def connect(self):
        target = self.path.as_uri() + "?mode=ro&immutable=1" if self.prebuilt else self.path
        db = sqlite3.connect(target, timeout=60, uri=self.prebuilt)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _ensure_index(self):
        stamp = json.dumps(
            {n: [(self.folder / n).stat().st_size, (self.folder / n).stat().st_mtime_ns] for n in FILES},
            sort_keys=True,
        )
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS manifest(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT value FROM manifest WHERE key='stamp'").fetchone()
            if old and old[0] == stamp:
                return
            for table in ("companies", "payments", "people"):
                db.execute(f"DROP TABLE IF EXISTS {table}")
            db.execute(
                "CREATE TABLE companies(line INTEGER PRIMARY KEY,uid TEXT,cl_type TEXT,industry TEXT,okved TEXT,region TEXT,loan INTEGER,deposit INTEGER,raw TEXT)"
            )
            db.execute(
                "CREATE TABLE payments(line INTEGER PRIMARY KEY,payer TEXT,beneficiary TEXT,amount TEXT,count TEXT)"
            )
            db.execute("CREATE TABLE people(line INTEGER PRIMARY KEY,person TEXT,company TEXT,raw TEXT)")
            hashes, counts = {}, {}
            for name in FILES:
                hashes[name] = hashlib.sha256((self.folder / name).read_bytes()).hexdigest()
                batch, count = [], 0
                with (self.folder / name).open(encoding="utf-8-sig", newline="") as file:
                    for line, row in enumerate(csv.DictReader(file, delimiter=";"), 2):
                        if name == FILES[0]:
                            values = (
                                line,
                                row["SYNTHETIC_U"],
                                row["CL_TYPE"],
                                row["MAINOKVED_NAME"],
                                row["MAINOKVED_CODE"],
                                row["REGION"],
                                int(row["CL_PRODUCT_LOA_MARK"] == "1.0"),
                                int(row["CL_PRODUCT_DEP_MARK"] == "1.0"),
                                json.dumps(row, ensure_ascii=False),
                            )
                            sql = "INSERT INTO companies VALUES(?,?,?,?,?,?,?,?,?)"
                        elif name == FILES[1]:
                            values = (line, row["PAYER"], row["BENEFICIARY"], row["PAY_AMT"], row["PAY_CNT"])
                            sql = "INSERT INTO payments VALUES(?,?,?,?,?)"
                        else:
                            values = (
                                line,
                                row["SYNTHETIC_F"],
                                row["SYNTHETIC_U"],
                                json.dumps(row, ensure_ascii=False),
                            )
                            sql = "INSERT INTO people VALUES(?,?,?,?)"
                        batch.append(values)
                        count += 1
                        if len(batch) == 10000:
                            db.executemany(sql, batch)
                            batch.clear()
                    if batch:
                        db.executemany(sql, batch)
                counts[name] = count
            db.execute("CREATE INDEX company_uid ON companies(uid)")
            db.execute("CREATE INDEX company_client ON companies(cl_type,loan,uid)")
            db.execute("CREATE INDEX payment_payer ON payments(payer)")
            db.execute("CREATE INDEX payment_beneficiary ON payments(beneficiary)")
            db.execute("CREATE INDEX people_company ON people(company)")
            db.execute("CREATE INDEX people_person ON people(person)")
            for key, value in {
                "stamp": stamp,
                "hashes": json.dumps(hashes),
                "counts": json.dumps(counts),
                "indexed_at": utcnow().isoformat(),
            }.items():
                db.execute("INSERT OR REPLACE INTO manifest VALUES(?,?)", (key, value))

    def summary(self):
        if not self.available:
            return {
                "available": False,
                "warnings": [
                    "CSV кейса не найдены. Укажите ALFA_CASE_DATA_DIR или поместите их в sources/data."
                ],
            }
        with self.connect() as db:
            meta = {r[0]: r[1] for r in db.execute("SELECT key,value FROM manifest")}
            count = db.execute("SELECT COUNT(DISTINCT uid) FROM companies").fetchone()[0]
            clients = db.execute(
                "SELECT COUNT(DISTINCT uid) FROM companies WHERE cl_type='CLIENT'"
            ).fetchone()[0]
        return {
            "available": True,
            "company_count": count,
            "client_count": clients,
            "row_counts": json.loads(meta["counts"]),
            "source_hashes": json.loads(meta["hashes"]),
            "indexed_at": meta["indexed_at"],
            "warnings": DATA_WARNINGS,
        }

    def exists(self, uid):
        if not self.available:
            return False
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM companies WHERE uid=? LIMIT 1", (uid,)).fetchone())

    def search(self, query="", limit=20, offset=0):
        if not self.available:
            return {"items": [], "total": 0}
        query = query.strip()[:100]
        # SQLite lower is ASCII-only: Cyrillic is matched in a bounded Python scan.
        with self.connect() as db:
            if query:
                rows = db.execute(
                    "SELECT uid,industry,okved,region,MAX(loan) loan,MAX(deposit) deposit,COUNT(*) source_rows FROM companies WHERE cl_type='CLIENT' GROUP BY uid ORDER BY uid"
                )
                found = [
                    dict(r)
                    for r in rows
                    if query.casefold()
                    in " ".join(str(r[k]) for k in ("uid", "industry", "okved", "region")).casefold()
                ]
                total = len(found)
                rows = found[offset : offset + limit]
            else:
                total = db.execute(
                    "SELECT COUNT(DISTINCT uid) FROM companies WHERE cl_type='CLIENT'"
                ).fetchone()[0]
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT uid,MIN(industry) industry,MIN(okved) okved,MIN(region) region,MAX(loan) loan,MAX(deposit) deposit,COUNT(*) source_rows FROM companies WHERE cl_type='CLIENT' GROUP BY uid ORDER BY loan DESC,uid LIMIT ? OFFSET ?",
                        (limit, offset),
                    )
                ]
        return {
            "items": [
                {
                    "company_id": r["uid"],
                    "label": "Компания " + r["uid"],
                    "industry": r["industry"] if r["source_rows"] == 1 else "Противоречивые строки профиля",
                    "okved": r["okved"] if r["source_rows"] == 1 else None,
                    "region": r["region"],
                    "has_loan": bool(r["loan"]),
                    "has_deposit": bool(r["deposit"]),
                    "source": "case_csv",
                    "synthetic": False,
                    "unresolved_identity": r["source_rows"] > 1,
                }
                for r in rows
            ],
            "total": total,
        }

    def build(self, uid):
        with self.connect() as db:
            rows = [
                (r["line"], json.loads(r["raw"]))
                for r in db.execute("SELECT line,raw FROM companies WHERE uid=? ORDER BY line", (uid,))
            ]
            hashes = json.loads(db.execute("SELECT value FROM manifest WHERE key='hashes'").fetchone()[0])
            indexed_at = db.execute("SELECT value FROM manifest WHERE key='indexed_at'").fetchone()[0]
        if not rows:
            raise KeyError(uid)
        from datetime import datetime

        now = datetime.fromisoformat(indexed_at)
        did = "case_profile_" + uid
        doc = DocumentRef(
            document_id=did,
            source_kind="case_csv",
            title=FILES[0],
            publisher="Данные организаторов, обезличенный набор",
            published_at=None,
            publication_date_raw=None,
            available_at=now,
            sha256=hashes[FILES[0]],
            tenant_company_id=uid,
        )
        evidence, features, conflicts = [], [], []

        def ev(column, value, line):
            eid = f"case_{uid}_{column}_{line}"
            evidence.append(
                Evidence(
                    evidence_id=eid,
                    document_id=did,
                    object_kind="csv_row",
                    locator=f"Строка {line}; {column}; SYNTHETIC_U={uid}",
                    quote=f"{column}={value}",
                    extraction_method="csv",
                )
            )
            return Citation(kind="evidence", ref_id=eid)

        features.append(
            FeatureCandidate(
                feature_id=uid + "_company_name",
                name="company_name",
                text_value="Компания " + uid,
                status="observed",
                period=TimeScope(),
                source_refs=[ev("SYNTHETIC_U", uid, rows[0][0])],
                note="Обезличенный ID, не юридическое наименование.",
            )
        )
        for column, (name, label) in {
            **LABELS,
            **FLAGS,
            "REVENUE_COMPANY": ("revenue_source_units", "Выручка в единицах источника"),
        }.items():
            present = [(line, r[column]) for line, r in rows if r[column].strip()]
            distinct = {v for _, v in present}
            refs = [ev(column, v, line) for line, v in present]
            value = next(iter(distinct)) if len(distinct) == 1 else None
            status = "conflicting" if len(distinct) > 1 else "observed" if value is not None else "missing"
            kw = {}
            note = None
            if status == "conflicting":
                conflicts.append(
                    {
                        "field": name,
                        "label": label,
                        "values": sorted(distinct),
                        "rows": [line for line, _ in present],
                    }
                )
            elif value is not None:
                if column in FLAGS:
                    if value not in {"0.0", "1.0", "0", "1"}:
                        status = "missing"
                        note = "Нераспознанное значение флага."
                    else:
                        kw["boolean_value"] = value in {"1.0", "1"}
                elif column == "REVENUE_COMPANY":
                    number = decimal_value(value)
                    if number is None or number in {Decimal(0), Decimal(1000)}:
                        status = "missing"
                        note = "Пусто/0/1000 трактуются как неподтверждённые по пересказу вебинара; исходное значение сохранено."
                    else:
                        kw["numeric_value"] = NumericValue(value=number, unit="ед. источника", raw=value)
                        note = "Рубли и 2025 год указаны в пересказе вебинара, первичное подтверждение отсутствует. Не используется как финансовый знаменатель."
                else:
                    kw["text_value"] = value
            features.append(
                FeatureCandidate(
                    feature_id=uid + "_" + name,
                    name=name,
                    status=status,
                    period=TimeScope(),
                    source_refs=refs,
                    note=note,
                    **kw,
                )
            )
        for name in (
            "rate_type",
            "floating_debt_rub",
            "capex_planned_rub",
            "inventory_days",
            "receivables_days",
            "credit_sales_share",
        ):
            features.append(
                FeatureCandidate(
                    feature_id=uid + "_" + name,
                    name=name,
                    status="missing",
                    period=TimeScope(),
                    source_refs=[],
                    note="В CSV этого поля нет. Можно извлечь из документов компании.",
                )
            )
        docs = [doc]
        links = self.connections(uid)
        pay_did, person_did = "case_payments_" + uid, "case_people_" + uid
        for current, name, title in [
            (pay_did, FILES[1], "Наблюдаемый платёжный срез"),
            (person_did, FILES[2], "Связи обезличенных ID физлиц и компаний"),
        ]:
            docs.append(
                DocumentRef(
                    document_id=current,
                    source_kind="case_csv",
                    title=name + " — " + title,
                    publisher="Данные организаторов, обезличенный набор",
                    published_at=None,
                    publication_date_raw=None,
                    available_at=now,
                    sha256=hashes[name],
                    tenant_company_id=uid,
                )
            )
        aggregates = [
            (
                "sample_outgoing_counterparties",
                links["outgoing"]["counterparty_count"],
                "count",
                pay_did,
                "COUNT DISTINCT BENEFICIARY WHERE PAYER=" + uid + " AND BENEFICIARY!=" + uid,
            ),
            (
                "sample_incoming_counterparties",
                links["incoming"]["counterparty_count"],
                "count",
                pay_did,
                "COUNT DISTINCT PAYER WHERE BENEFICIARY=" + uid + " AND PAYER!=" + uid,
            ),
            (
                "linked_person_count",
                links["people_link_count"],
                "count",
                person_did,
                "COUNT DISTINCT SYNTHETIC_F WHERE SYNTHETIC_U=" + uid,
            ),
        ]
        for side in ("outgoing", "incoming"):
            current = links[side]
            if current["top"] and Decimal(current["amount"]) > 0:
                share = (Decimal(current["top"][0]["amount"]) / Decimal(current["amount"])).quantize(
                    Decimal(".000001")
                )
                aggregates.append(
                    (
                        "sample_" + side + "_top_share",
                        share,
                        "fraction",
                        pay_did,
                        "MAX(counterparty SUM(PAY_AMT)) / external SUM(PAY_AMT), "
                        + side
                        + ", company="
                        + uid
                        + ", self transfers excluded; rounded to 6 decimals",
                    )
                )
        for name, number, unit, current, formula in aggregates:
            eid = "case_" + uid + "_derived_" + name
            evidence.append(
                Evidence(
                    evidence_id=eid,
                    document_id=current,
                    object_kind="csv_row",
                    locator=formula,
                    quote=formula + " = " + str(number),
                    extraction_method="csv",
                )
            )
            features.append(
                FeatureCandidate(
                    feature_id=uid + "_" + name,
                    name=name,
                    numeric_value=NumericValue(value=Decimal(number), unit=unit, raw=str(number)),
                    status="derived",
                    period=TimeScope(raw="Период набора не подтверждён"),
                    source_refs=[Citation(kind="evidence", ref_id=eid)],
                    note="Агрегат только наблюдаемого среза. Повторные строки сохранены; не доказательство поставки, владения, общей концентрации или кредитной экспозиции.",
                )
            )
        profile = CompanyProfile(
            company_id=uid,
            version=1,
            as_of=now,
            features=features,
            unresolved_identity=len(rows) > 1,
            source_snapshot_ids=[d.document_id for d in docs],
        )
        return {
            "profile": profile.model_dump(mode="json"),
            "documents": [d.model_dump(mode="json") for d in docs],
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "document_text": {
                d.document_id: "\n".join(e.quote for e in evidence if e.document_id == d.document_id)
                for d in docs
            },
            "metadata": {
                "source": "case_csv",
                "anonymized": True,
                "conflicts": conflicts,
                "warnings": DATA_WARNINGS,
                "source_rows": [line for line, _ in rows],
            },
        }

    def connections(self, uid):
        outgoing, incoming = defaultdict(lambda: [Decimal(0), 0, 0]), defaultdict(lambda: [Decimal(0), 0, 0])
        self_amount, self_count, raw_rows = Decimal(0), 0, 0
        with self.connect() as db:
            for row in db.execute("SELECT * FROM payments WHERE payer=? OR beneficiary=?", (uid, uid)):
                raw_rows += 1
                amount = decimal_value(row["amount"])
                count = decimal_value(row["count"])
                if amount is None or count is None:
                    continue
                if row["payer"] == row["beneficiary"]:
                    self_amount += amount
                    self_count += int(count)
                else:
                    group = outgoing if row["payer"] == uid else incoming
                    other = row["beneficiary"] if row["payer"] == uid else row["payer"]
                    group[other][0] += amount
                    group[other][1] += int(count)
                    group[other][2] += 1
            people = [
                dict(r)
                for r in db.execute(
                    "SELECT person,COUNT(*) source_rows FROM people WHERE company=? GROUP BY person LIMIT 100",
                    (uid,),
                )
            ]
            people_link_count = db.execute(
                "SELECT COUNT(DISTINCT person) FROM people WHERE company=?", (uid,)
            ).fetchone()[0]

            def side(groups):
                total = sum((v[0] for v in groups.values()), Decimal(0))
                top = []
                for other, (amount, count, rows) in sorted(
                    groups.items(), key=lambda x: x[1][0], reverse=True
                )[:5]:
                    company = db.execute(
                        "SELECT MIN(industry),COUNT(*),COUNT(DISTINCT industry) FROM companies WHERE uid=?",
                        (other,),
                    ).fetchone()
                    top.append(
                        {
                            "company_id": other,
                            "amount": str(amount),
                            "payment_count": count,
                            "source_rows": rows,
                            "share_percent": str((amount / total * 100).quantize(Decimal(".01")))
                            if total > 0
                            else None,
                            "profile_available": bool(company[1]),
                            "profile_conflicting": company[2] > 1,
                            "industry": company[0] if company[2] == 1 else None,
                        }
                    )
                return {
                    "amount": str(total),
                    "payment_count": sum(v[1] for v in groups.values()),
                    "counterparty_count": len(groups),
                    "top": top,
                }

            result = {"outgoing": side(outgoing), "incoming": side(incoming)}
        result.update(
            {
                "company_id": uid,
                "self_transfers": {"amount": str(self_amount), "payment_count": self_count},
                "source_rows": raw_rows,
                "people_links": people,
                "people_link_count": people_link_count,
                "amount_unit": "ед. источника; рубли требуют подтверждения",
                "period": None,
                "source_file": FILES[1],
                "warnings": DATA_WARNINGS[2:5],
            }
        )
        return result
