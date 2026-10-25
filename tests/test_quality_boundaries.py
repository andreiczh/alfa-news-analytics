import pytest

from alfa_agent.news_ranking import entity_matches
from alfa_agent.personalization import is_bank_company
from alfa_agent.narrative import validate_narrative
from test_analysis_policy import memo, LEDGER, AS_OF


@pytest.mark.parametrize(
    "name,title,expected",
    [
        ("Магнит", "Магнитная буря вызвала сбои связи", False),
        ("Магнит", "Продажи Магнита выросли", True),
        ("ВТБ", "ВТБ сообщил о финансовых результатах", True),
        ("Альфа-Банк", "У Альфа-Банка изменились условия вкладов", True),
        ("Сбербанк", "Сбербанкиры рассказали о рынке", False),
    ],
)
def test_whole_company_name_is_different_from_a_substring(name, title, expected):
    assert entity_matches(name, title) is expected


def test_bank_classification_uses_declared_industry_without_guessing_from_lenders():
    assert is_bank_company("ВТБ", {"context": {"fields": {"industry": {"value": "Банковские услуги"}}}})
    assert is_bank_company(
        "Компания",
        {
            "profile": {
                "features": [
                    {"name": "industry", "text_value": "Денежное посредничество", "status": "provided"}
                ]
            }
        },
    )
    assert not is_bank_company(
        "БанкротПром", {"context": {"fields": {"priorities": {"value": "Работаем с банками"}}}}
    )


def test_source_fact_cannot_smuggle_in_a_new_financial_interpretation():
    value = memo()
    value.situation[0].text = "Банк России сохранил ставку, поэтому маржа банка сохранится."
    issues = validate_narrative(value, LEDGER, {}, True, as_of=AS_OF)
    assert any(x.startswith("source_message_must_be_one_verbatim_quote") for x in issues)
    value = memo()
    value.recommendations[0].kind = "source_fact"
    assert "own_reasoning_cannot_be_labeled_source_fact" in validate_narrative(value, LEDGER, {}, as_of=AS_OF)


def test_calendar_window_counts_days_inclusively_across_year_and_moscow_midnight():
    from alfa_agent.analysis_policy import planning_policy

    assert planning_policy("2026-12-31T09:00:00+00:00", 7)["end"] == "2027-01-06"
    assert planning_policy("2026-10-24T22:00:00+00:00", 7)["start"] == "2026-10-25"


def test_historical_verbatim_calendar_keeps_original_tense_without_becoming_our_future_claim():
    value = memo()
    quote = "До конца года состоится два заседания ЦБ по ключевой ставке (23 октября и 18 декабря)"
    ledger = [
        *LEDGER,
        {
            "anchor_id": "a2",
            "source_id": "dated_report",
            "kind": "bank_excerpt",
            "quote": quote,
            "published_at": "2026-09-14",
        },
    ]
    value.situation[0].text = quote
    value.situation[0].anchor_ids = ["a2"]
    value.situation[
        0
    ].condition = (
        "Исторический календарь из обзора от 14 сентября 2026 года. Октябрьское заседание уже прошло."
    )
    assert not validate_narrative(value, ledger, {}, as_of=AS_OF)


def test_current_planning_window_does_not_mean_a_request_for_fresh_news():
    from alfa_agent.research import fresh_news_intent

    assert not fresh_news_intent("Отдели исторический прогноз от текущего планового сценария")
    assert fresh_news_intent("Какие свежие изменения по ставке важны для компании?")
    assert fresh_news_intent("Что происходит на рынке сейчас?")


def test_retrieval_keeps_the_complete_liquidity_sentence_in_its_anchor():
    from alfa_agent.research import evidence_ledger

    first = "Контекст обзора и факторы кредитного рынка. " * 8
    sentence = "Наблюдаемый рост дефицита банковской ликвидности уже привел к росту ставок денежного рынка выше ключевой ставки."
    source = {
        "source_id": "bank_test",
        "kind": "bank_excerpt",
        "text": first + sentence + " Для выводов о банке нужны внутренние данные.",
    }
    ledger = evidence_ledger([source], "дефицит банковской ликвидности денежного рынка")
    assert any(sentence in a["quote"] and a["excerpt_complete"] for a in ledger)
    assert all(a["quote"] in source["text"] for a in ledger)


def test_unverified_currency_date_is_scope_metadata_but_today_is_not_event_evidence():
    value = memo()
    value.scenarios[
        0
    ].confidence_reason = "Актуальность на 2026-10-25 не проверена; требуется прочитать новые источники."
    assert not validate_narrative(value, LEDGER, {}, as_of=AS_OF)
    value.scenarios[
        0
    ].outcome = "Центральный банк принял новое решение 2026-10-25 и изменил условия фондирования."
    assert "unsupported_forecast_date" in validate_narrative(value, LEDGER, {}, as_of=AS_OF)


@pytest.mark.parametrize(
    "name,industry,title,reason",
    [
        (
            "Магнит",
            "Розничная торговля продуктами",
            "Омский регоператор Магнит зовёт детей в мир экотворчества",
            "ambiguous_name_other_industry",
        ),
        (
            "Северсталь",
            "Металлургия и производство стали",
            "Локомотив — Северсталь: прямая трансляция матча КХЛ",
            "ambiguous_name_sports_entity",
        ),
        ("Магнит", "Розничная торговля продуктами", "Акции ПАО Магнит, ао", "instrument_page_without_event"),
    ],
)
def test_observed_live_entity_collisions_are_not_own_company_events(name, industry, title, reason):
    from datetime import datetime, timezone
    from alfa_agent.news_ranking import score_item

    item = {
        "title": title,
        "published_at": "2026-10-24T10:00:00Z",
        "url": "https://example.org/item",
        "read_status": "article_read",
        "excerpt": "Новости финансов, инвестиций и прибыли.",
    }
    context = {"name": name, "industry": industry, "bank": False, "anonymous": False}
    assert score_item(item, context, "cmo", datetime(2026, 10, 25, tzinfo=timezone.utc)) == (None, reason)


def test_confirmed_industry_field_is_used_even_without_an_uploaded_document():
    from alfa_agent.news_ranking import company_context

    context = company_context(
        {
            "context": {
                "fields": {
                    "industry": {"value": "Розничная торговля", "category": "general", "label": "Отрасль"}
                }
            }
        }
    )
    assert context["industry"] == "Розничная торговля"


def test_source_messages_are_host_bound_and_keep_forecast_modality():
    from alfa_agent.narrative import source_message_options, NarrativeSelection, bind_source_messages

    ledger = [
        {
            **LEDGER[0],
            "quote": "Банк России сохранил ключевую ставку. Мы ожидаем снижения инфляции в дальнейшем.",
        }
    ]
    options = source_message_options(ledger)
    assert {o["kind"] for o in options} == {"source_fact", "source_forecast"}
    value = memo().model_dump()
    option = next(o for o in options if o["kind"] == "source_forecast")
    value["situation"] = [
        {"message_id": option["message_id"], "condition": "Историческое ожидание автора, исход не проверен."}
    ]
    bound = bind_source_messages(NarrativeSelection.model_validate(value), options)
    assert bound.situation[0].text == option["text"]
    assert bound.situation[0].kind == "source_forecast"
    value["situation"][0]["message_id"] = "invented"
    with pytest.raises(ValueError, match="unknown_source_message"):
        bind_source_messages(NarrativeSelection.model_validate(value), options)


def test_deposit_beta_and_bond_price_stability_are_not_time_or_liquidity_proofs():
    from alfa_agent.analysis_policy import sentence_issues

    assert "deposit_beta_is_pass_through_not_a_time_lag" in sentence_issues(
        "Депозитная бета определяет скорость удорожания пассивов.", bank=True, pause=False
    )
    assert "price_stability_does_not_prove_liquidity" in sentence_issues(
        "Стабильность цен даёт ликвидный инструмент для размещения.", bank=True, pause=False
    )
    assert not sentence_issues(
        "Депозитная бета отражает степень переноса изменения ставки; лаг переоценки проверяется отдельно.",
        bank=True,
        pause=False,
    )
    assert not sentence_issues(
        "Стабильность цен не доказывает ликвидность облигации.", bank=True, pause=False
    )
