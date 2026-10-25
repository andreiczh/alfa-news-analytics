"""Regression cases for rejected real outputs and contrasting valid mechanisms."""

import pytest

from alfa_agent.analysis_policy import planning_policy, sentence_issues
from alfa_agent.narrative import DecisionNarrative, validate_narrative, decision_schema


AS_OF = "2026-10-25T09:00:00+00:00"
LEDGER = [
    {
        "anchor_id": "a1",
        "source_id": "news_test",
        "kind": "public_page",
        "quote": "23 октября 2026 года Банк России сохранил ключевую ставку на уровне 14,00%. Мониторинг опубликован 21 октября 2026 года.",
        "published_at": "2026-10-23T10:30:00+00:00",
    }
]


def memo():
    point = {
        "kind": "interpretation",
        "text": "Проверить чувствительность плана к сохранению текущих рыночных условий.",
        "condition": "При наличии чувствительной к ставкам экспозиции компании.",
        "anchor_ids": ["a1"],
    }
    scenario = {
        "title": "Продолжение текущих внешних условий",
        "outcome": "Если внешние условия сохраняются, проверить запас ликвидности компании.",
        "horizon": planning_policy(AS_OF)["horizon"],
        "mechanism": "Потребность в ликвидности зависит от сроков платежей и условий договоров.",
        "assumptions": ["Условия договоров требуют внутренней проверки."],
        "change_signals": ["Изменение стоимости новых договоров."],
        "anchor_ids": ["a1"],
        "confidence_reason": "Это условный канал риска, вероятность исхода не рассчитана.",
    }
    return DecisionNarrative.model_validate(
        {
            "title": "Проверить план ликвидности при текущих рыночных условиях",
            "main_conclusion": point,
            "situation": [{**point, "kind": "source_fact", "text": LEDGER[0]["quote"]}],
            "drivers": [point],
            "company_impact": [point],
            "recommendations": [point],
            "forecast_status": "conditional_scenarios",
            "forecast_limit": "Внутренние договоры и финансовая модель компании не предоставлены.",
            "scenarios": [
                {**scenario, "variant": "base"},
                {**scenario, "variant": "alternative", "title": "Изменение внешних условий"},
            ],
            "context_keys": [],
        }
    )


def check(value, **kwargs):
    return validate_narrative(value, LEDGER, {}, as_of=AS_OF, **kwargs)


def test_planning_dates_are_computed_and_do_not_need_fake_source_quotes():
    assert planning_policy(AS_OF)["end"] == "2026-11-23"
    assert not check(memo())
    schema = decision_schema(LEDGER, {}, planning_policy(AS_OF))
    assert schema["$defs"]["ForecastScenario"]["properties"]["horizon"]["enum"] == [
        planning_policy(AS_OF)["horizon"]
    ]


@pytest.mark.parametrize(
    "wording", ["днём до заседания", "накануне заседания", "вчера", "за два дня до решения"]
)
def test_relative_date_in_an_interpretation_cannot_pass_on_matching_digits(wording):
    value = memo()
    value.drivers[0].condition = "Мониторинг опубликован " + wording + "; текущие условия требуют проверки."
    assert any(x.startswith("relative_calendar_not_allowed") for x in check(value))


def test_absolute_source_dates_and_qualitative_before_are_allowed():
    value = memo()
    value.drivers[
        0
    ].condition = "Мониторинг от 21 октября 2026 года опубликован до решения от 23 октября 2026 года."
    assert not check(value)


@pytest.mark.parametrize(
    "title",
    [
        "Пауза до конца года",
        "Изменения на декабрьском заседании",
        "Снижение после декабря",
        "План на 2027 год",
    ],
)
def test_title_cannot_silently_extend_the_horizon(title):
    value = memo()
    value.scenarios[0].title = title
    assert any(x.startswith("scenario_title_must_not_extend_horizon") for x in check(value))


def test_model_cannot_substitute_another_scenario_horizon():
    value = memo()
    value.scenarios[0].horizon = "Ближайший квартал до декабрьского заседания"
    assert any(x.startswith("scenario_horizon_must_match_policy") for x in check(value))


def test_schema_enforces_exact_source_quotes_and_separates_reasoning():
    schema = decision_schema(LEDGER, {}, planning_policy(AS_OF))
    assert schema["$defs"]["SourceMessage"]["properties"]["message_id"]["enum"]
    assert "text" not in schema["$defs"]["SourceMessage"]["properties"]
    assert "kind" not in schema["$defs"]["SourceMessage"]["properties"]
    assert schema["$defs"]["NarrativePoint"]["properties"]["kind"]["enum"] == ["interpretation", "hypothesis"]


def test_source_assessment_keeps_its_modality_and_cannot_become_a_fact():
    value = memo()
    ledger = [{**LEDGER[0], "claim_type": "ASSESSMENT"}]
    assert any(
        x.startswith("source_opinion_cannot_be_labeled_fact")
        for x in validate_narrative(value, ledger, {}, as_of=AS_OF)
    )
    value.situation[0].kind = "source_assessment"
    assert not validate_narrative(value, ledger, {}, as_of=AS_OF)


@pytest.mark.parametrize(
    "text",
    [
        "Маржа банка сохранится при неизменной ключевой ставке.",
        "Чистый процентный доход банка вырастет благодаря паузе по ставке.",
        "Прибыль банка стабилизируется, если ключевая ставка не изменится.",
    ],
)
def test_same_financial_guard_for_cfo_cmo_and_ceo(text):
    assert sentence_issues(text, bank=True, pause=True)


def test_conditional_balance_channel_is_different_from_predicting_actual_bank_profit():
    assert not sentence_issues(
        "Если пассивы переоцениваются быстрее активов, маржа может снизиться.", bank=True, pause=True
    )
    assert not sentence_issues(
        "Знак влияния на маржу банка не определён без модели баланса.", bank=True, pause=True
    )


@pytest.mark.parametrize(
    "text",
    [
        "Сохранение ставки означает, что кредит дорожает.",
        "Если ставка сохранена, кредит может подорожать.",
        "Пауза ведёт к росту стоимости фондирования.",
    ],
)
def test_pause_does_not_become_a_new_shock_even_with_the_word_may(text):
    assert "pause_is_not_a_new_cost_shock" in sentence_issues(text, bank=False, pause=True)


def test_pause_can_coexist_with_a_separate_conditional_repricing_lag():
    assert not sentence_issues(
        "Если действует лаг предыдущей переоценки, кредит может подорожать.", bank=False, pause=True
    )
    assert not sentence_issues("При текущих условиях кредит остаётся дорогим.", bank=False, pause=True)


def test_fixed_loan_is_distinct_from_new_contracts():
    assert "fixed_debt_does_not_reprice_automatically" in sentence_issues(
        "Снижение ставки снизит процентные расходы по долгу.", bank=False, pause=False, fixed_debt=True
    )
    assert not sentence_issues(
        "При рефинансировании снижение ставки может снизить процентные расходы.",
        bank=False,
        pause=False,
        fixed_debt=True,
    )


def test_disclaimer_does_not_cancel_an_opposite_financial_promise():
    assert sentence_issues(
        "Маржа банка сохранится, хотя чувствительность баланса неизвестна.", bank=True, pause=False
    )
    assert sentence_issues(
        "Прибыль банка вырастет при условии проверки активов и пассивов.", bank=True, pause=False
    )


def test_past_meeting_cannot_be_a_future_event_or_change_signal():
    value = memo()
    value.drivers[0].text = "Следующее заседание состоится 23 октября 2026 года."
    assert any(i.startswith("past_event_presented_as_future") for i in check(value))
    value = memo()
    value.scenarios[0].change_signals = ["Публикация решения от 23 октября 2026 года."]
    assert any(i.startswith("past_date_in_future_signal") for i in check(value))


def test_negated_cost_shock_is_not_a_positive_forecast_and_does_not_excuse_a_contrast():
    assert not sentence_issues(
        "Пауза сама по себе не удорожает стоимость фондирования.", bank=True, pause=True
    )
    assert not sentence_issues("Пауза не означает рост стоимости кредита.", bank=False, pause=True)
    assert "pause_is_not_a_new_cost_shock" in sentence_issues(
        "Пауза не означает рост стоимости кредита, однако фондирование подорожает.", bank=False, pause=True
    )


def test_conditional_spread_growth_and_a_new_hike_are_separate_causes_from_pause():
    assert not sentence_issues(
        "При углублении дефицита ликвидности спред может вырасти и новое фондирование может подорожать.",
        bank=True,
        pause=True,
    )
    assert not sentence_issues(
        "Если ключевая ставка повысится, новые кредиты могут подорожать.", bank=False, pause=True
    )
    value = memo()
    value.scenarios[0].change_signals = ["Решение по ставке 18 декабря 2026 года"]
    assert any(x.startswith("future_signal_outside_planning_window") for x in check(value))
