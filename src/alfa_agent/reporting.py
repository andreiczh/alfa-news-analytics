from html import escape
from pathlib import Path
from decimal import Decimal, InvalidOperation


def write_report(run, path: Path):
    result = run.get("result")
    if not result:
        raise ValueError("verified_result_not_ready")

    def esc(text):
        return escape(str(text))

    origin = {
        "case_csv": "Обезличенные данные кейса",
        "company_upload": "Документы и сведения компании",
        "synthetic_demo": "Синтетическая демонстрация",
    }.get(result.get("company_context", {}).get("source", "synthetic_demo"), "Профиль компании")

    metric_labels = {
        "floating_debt_rub": "Долг с плавающей ставкой",
        "rate_type": "Тип ставки",
        "capex_planned_rub": "Планируемые инвестиции",
        "inventory_days": "Оборачиваемость запасов",
        "receivables_days": "Срок дебиторской задолженности",
        "credit_sales_share": "Продажи с отсрочкой",
        "has_loan": "Кредитный продукт",
        "has_deposit": "Депозитный продукт",
        "has_guarantee": "Гарантии",
        "has_factoring": "Факторинг",
        "industry": "Основная деятельность",
        "business_stage": "Стадия развития",
        "employees_band": "Численность по ФНС",
        "revenue_source_units": "Выручка в единицах CSV",
        "sample_outgoing_counterparties": "Контрагентов по исходящим в срезе",
        "sample_incoming_counterparties": "Контрагентов по входящим в срезе",
        "sample_incoming_top_share": "Доля топ-1 по поступлениям в срезе",
    }

    def display_metric(metric):
        raw, unit = metric["value"], metric["unit"]
        try:
            number = Decimal(raw)
            if unit == "fraction":
                return format(number * 100, "f") + "%"
            if unit == "RUB":
                return format(number, ",.2f").replace(",", " ").replace(".", ",") + " ₽"
            return format(number, ",f").replace(",", " ").replace(".", ",") + (
                " дней" if unit == "days" else "" if unit in {None, "count"} else " " + unit
            )
        except (InvalidOperation, ValueError):
            return {
                "True": "Есть",
                "False": "Нет в наборе",
                "floating": "Плавающая",
                "fixed": "Фиксированная",
            }.get(raw, raw)

    chunks = [
        "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Новость о ставке — пять ролей</title><style>body{font:15px/1.6 -apple-system,Arial,sans-serif;color:#262b32;background:#f5f5f7;max-width:1000px;margin:auto;padding:30px}section{background:white;border:1px solid #ddd;border-radius:12px;margin:20px 0;padding:25px}h1{line-height:1.2}h2{color:#b91d26}small,.note{color:#767c87}.scenario{background:#fff7e5;padding:15px}li{margin:10px 0}blockquote{border-left:2px solid #ddd;padding:10px}a{color:#a51e26}</style>",
        "<p class='note'>Прототип для хакатона · " + esc(origin) + "</p>",
        "<h1>" + esc(result["core"]["headline"]) + "</h1>",
        "<p>Событие 11 сентября 2026. Статус: "
        + esc(run["status"])
        + ". Режим: "
        + esc(result["mode"])
        + ".</p>",
    ]
    for role in result["role_views"]:
        chunks.extend(["<section><h2>" + esc(role["label"]) + "</h2><p>" + esc(role["focus"]) + "</p>"])
        for metric in role["metrics"]:
            chunks.append(
                "<small>"
                + esc(metric_labels.get(metric["name"], metric["name"]))
                + ": "
                + esc(display_metric(metric))
                + " "
                + ""
                + " · "
                + esc(
                    {
                        "self_reported": "Документы / сведения компании",
                        "observed": "Данные источника",
                        "derived": "Расчёт по наблюдаемому срезу",
                    }.get(metric.get("provenance"), "Источник профиля")
                )
                + "</small><br>"
            )
        for statement in role["statements"]:
            label = (
                ("Банк · прогноз на дату обзора" if statement["claim_type"] == "FORECAST" else "Оценка банка")
                if statement["owner"]["kind"] == "bank"
                else "Условная интерпретация сервиса"
                if statement["derivation"] == "conditional"
                else "Расчёт сценария"
                if statement["derivation"] == "calculated"
                else "Факт источника"
            )
            chunks.append("<small>" + esc(label) + "</small><p>" + esc(statement["text"]) + "</p>")
            if statement["conditions"]:
                chunks.append("<p class='note'>Условия: " + esc("; ".join(statement["conditions"])) + "</p>")
        if role["scenario"]:
            s = role["scenario"]
            chunks.append(
                "<div class='scenario'><b>"
                + esc(s["amount_rub"])
                + " руб.</b><p>"
                + esc(s["label"])
                + "</p><p>Условный шаг "
                + esc(s["delta_rate_pp"])
                + " п.п.; горизонт "
                + esc(s["horizon_days"])
                + " дней; база "
                + esc(s.get("day_count_basis", result["calculations"][-1]["inputs"]["day_count_basis"]))
                + " дней.</p></div>"
            )
        chunks.append(
            "<ol>" + "".join("<li>" + esc(a["title"]) + "</li>" for a in role["actions"]) + "</ol></section>"
        )
    chunks.append(
        "<section><h2>Ограничения</h2>"
        + "".join("<p>" + esc(s) + "</p>" for s in result["core"]["limitations"])
        + "<p>"
        + esc(result["notice"])
        + "</p></section>"
    )
    chunks.append(
        "<section><h2>Проверки и процесс</h2><p>LLM calls: "
        + esc(run["llm_calls"])
        + "; tools: "
        + esc(run["tool_calls"])
        + "; replans: "
        + esc(run["replans"])
        + ".</p><ol>"
    )
    for event in run["trace"]:
        if event["event"] in {"tool_completed", "question_created", "verification", "replanned", "published"}:
            chunks.append(
                "<li>"
                + esc(event["event"])
                + ": "
                + esc(
                    event["data"].get(
                        "summary", event["data"].get("verdict", event["data"].get("reason", ""))
                    )
                )
                + "</li>"
            )
    chunks.append(
        "</ol></section><p><a href='https://www.cbr.ru/press/pr/?file=11092026_133000key.htm'>Пресс-служба Банка России</a> · <a href='https://drive.google.com/file/d/1shHG89LtMgHT1_uEebHmQSFZe_zbeq7-/view'>Банковский обзор, стр. 1</a></p></html>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(chunks))
