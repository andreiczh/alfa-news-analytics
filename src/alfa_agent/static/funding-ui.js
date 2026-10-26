"use strict";

function currentDebtTerms() {
  let value = S.snapshot?.context?.fields?.debt_contract?.value || "{}";
  if (value.startsWith("Синтетические данные.\n"))
    value = value.split("\n").slice(1).join("\n");
  try {
    return JSON.parse(value);
  } catch {
    return {};
  }
}

function fundingExampleTerms(variant) {
  const today = new Date().toLocaleDateString("en-CA", {
    timeZone: "Europe/Moscow",
  });
  const after = (days) => {
    const d = new Date(today + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  };
  return {
    facility_name: "Синтетический договор для демонстрации",
    rate_type: variant,
    principal_rub: variant === "unknown" ? null : "200000000",
    currency: "RUB",
    benchmark: variant === "floating" ? "key_rate" : "unknown",
    repricing_at: variant === "floating" ? after(30) : null,
    maturity_at: variant === "unknown" ? null : after(365),
    fixed_until: variant === "fixed" ? after(365) : null,
    constant_principal_confirmed: variant === "floating",
    no_hedge_confirmed: variant === "floating",
    no_rate_review_confirmed: variant === "fixed",
    synthetic: true,
  };
}

async function createFundingExample(variant) {
  if (S.creatingFundingExample) return;
  S.creatingFundingExample = true;
  try {
    const names = {
      floating: "Синтетический завод «Вектор»",
      fixed: "Синтетическая торговая компания «Маяк»",
      unknown: "Синтетическая логистическая компания «Транзит»",
    };
    const created = await api("/api/companies", {
      method: "POST",
      body: JSON.stringify({ name: names[variant] }),
    });
    await api("/api/companies/" + created.company_id + "/funding", {
      method: "PUT",
      body: JSON.stringify({
        expected_revision: 0,
        terms: fundingExampleTerms(variant),
      }),
    });
    S.companies = await api("/api/companies");
    await selectCompany(created.company_id);
    go("company");
    toast(
      "Создан отдельный синтетический пример. Выберите новость или задайте вопрос о финансировании.",
    );
  } catch (error) {
    report(error);
  } finally {
    S.creatingFundingExample = false;
  }
}

function renderFundingProfile() {
  const c = card("funding-profile");
  c.id = "funding-profile";
  c.append(node("h2", "", "Условия финансирования"));
  c.append(
    p(
      "Укажите один договор для сценария. Сведения сохраняются после подтверждения и используются вместе с новостями и аналитикой банка.",
      "caption",
    ),
  );
  const examples = node("div", "row");
  for (const [v, label] of [
    ["floating", "Плавающий долг"],
    ["fixed", "Фиксированный долг"],
    ["unknown", "Условия неизвестны"],
  ])
    examples.append(
      button("Создать пример: " + label, () => createFundingExample(v), "chip"),
    );
  c.append(
    detail(
      "Три синтетических примера для проверки",
      p(
        "Каждая кнопка создаёт отдельную тестовую компанию в вашем пространстве; текущая компания не перезаписывается.",
        "caption",
      ),
      examples,
    ),
  );
  if (isBank()) {
    c.append(
      box(
        "Для банка требуется ALM-модель активов и обязательств. Калькулятор ниже предназначен для корпоративного клиента банка.",
      ),
    );
    return c;
  }
  const terms = currentDebtTerms(),
    form = node("form", "funding-form"),
    controls = {};
  const fields = [
    ["facility_name", "Название договора", "text", null],
    [
      "rate_type",
      "Тип ставки",
      "select",
      [
        ["unknown", "Неизвестно"],
        ["floating", "Плавающая"],
        ["fixed", "Фиксированная"],
      ],
    ],
    ["principal_rub", "Остаток долга, руб.", "number", null],
    [
      "currency",
      "Валюта договора",
      "select",
      [
        ["RUB", "RUB"],
        ["USD", "USD"],
        ["EUR", "EUR"],
        ["CNY", "CNY"],
        ["other", "Другая"],
      ],
    ],
    [
      "benchmark",
      "Бенчмарк",
      "select",
      [
        ["unknown", "Неизвестно"],
        ["key_rate", "Ключевая ставка"],
        ["ruonia", "RUONIA"],
        ["other", "Другой"],
      ],
    ],
    ["repricing_at", "Ближайшая дата переоценки", "date", null],
    ["maturity_at", "Дата погашения", "date", null],
    ["fixed_until", "Фиксация действует по", "date", null],
  ];
  for (const [key, label, type, choices] of fields) {
    const input = node(type === "select" ? "select" : "input");
    input.id = "debt-" + key;
    if (type === "select") {
      for (const [value, text] of choices) {
        const option = node("option", "", text);
        option.value = value;
        input.append(option);
      }
    } else input.type = type;
    input.value = terms[key] ?? (type === "select" ? choices[0][0] : "");
    if (type === "number") {
      input.min = "0.01";
      input.step = "0.01";
      input.max = "1000000000000000";
    }
    if (key === "facility_name") input.maxLength = 120;
    controls[key] = input;
    form.append(field(label, input));
  }
  for (const [key, label] of [
    ["constant_principal_confirmed", "В сценарии остаток постоянен"],
    ["no_hedge_confirmed", "Хеджирование в сценарии отсутствует"],
    [
      "no_rate_review_confirmed",
      "Договор исключает пересмотр ставки в период фиксации",
    ],
    ["synthetic", "Это синтетические тестовые данные"],
  ]) {
    const input = node("input");
    input.type = "checkbox";
    input.id = "debt-" + key;
    input.checked = !!terms[key];
    controls[key] = input;
    form.append(
      add(node("label", "check-row"), input, node("span", "", label)),
    );
  }
  const submit = node(
    "button",
    "button primary",
    "Подтвердить условия договора",
  );
  submit.type = "submit";
  form.append(submit);
  const cid = S.company.company_id,
    revision = S.snapshot.context?.revision || 0;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const value = {};
      for (const [key, input] of Object.entries(controls))
        value[key] =
          input.type === "checkbox" ? input.checked : input.value || null;
      value.facility_name = value.facility_name || "Договор для сценария";
      await api("/api/companies/" + cid + "/funding", {
        method: "PUT",
        body: JSON.stringify({ expected_revision: revision, terms: value }),
      });
      if (S.company.company_id === cid) {
        await selectCompany(cid);
        go("company");
        toast(
          "Условия сохранены. Для старого отчёта выберите пересчёт по актуальным данным.",
        );
      }
    } catch (error) {
      report(error);
    } finally {
      submit.disabled = false;
    }
  });
  c.append(
    form,
    p(
      "Расчёт — чувствительность процентных расходов. Комиссии, хедж, налоги, денежные разрывы и прибыль не рассчитаны. Для валютного долга рублёвый расчёт блокируется.",
      "fine-print",
    ),
  );
  return c;
}

function renderFundingAnalysis(job) {
  const value = job.result?.funding_analysis;
  if (!value) return null;
  const c = card("funding-analysis");
  c.id = "funding-analysis";
  c.append(node("h2", "", "Финансирование: сценарии для вашего договора"));
  c.append(
    p(
      ({
        floating: "Плавающий долг",
        fixed: "Фиксированный долг",
        unknown: "Условия неизвестны",
      }[value.rate_type] || value.rate_type) +
        " · " +
        value.terms.facility_name,
    ),
  );
  if (value.synthetic) c.append(badge("Синтетические вводные", "amber"));
  c.append(p(value.next_action));
  if (value.news_lens) c.append(box(value.news_lens.interpretation));
  if (value.scenarios.length) {
    c.append(
      p(
        value.rate_type === "fixed"
          ? "Рыночный сценарий: действующая ставка договора фиксирована и в защищённом периоде не меняется. Рефинансирование оценивается отдельно."
          : "Гипотетическое изменение договорной ставки. Знак «+» означает рост процентных расходов; это не фактический эффект новости. Строка ноль — база сравнения, не прогноз.",
        "caption",
      ),
    );
    const wrap = node("div", "table-scroll"),
      table = node("table", "funding-table");
    table.append(
      add(
        node("thead"),
        add(
          node("tr"),
          ...[
            value.rate_type === "fixed"
              ? "Рыночная ставка, п.п."
              : "Договорная ставка, п.п.",
            "Дни",
            "Изменение расходов, руб.",
          ].map((x) => node("th", "", x)),
        ),
      ),
    );
    const body = node("tbody");
    for (const s of value.scenarios)
      body.append(
        add(
          node("tr"),
          node("td", "", s.scenario_delta_pp),
          node("td", "", String(s.exposure_days)),
          node(
            "td",
            "",
            Number(s.gross_interest_change_rub).toLocaleString("ru-RU", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            }),
          ),
        ),
      );
    table.append(body);
    wrap.append(table);
    c.append(wrap);
    if (value.zero_reason) c.append(p(value.zero_reason, "caption"));
    if (value.effective_start && value.effective_end)
      c.append(
        p(
          "Период начисления сценария: " + value.effective_start + " — " + value.effective_end + ".",
          "caption",
        ),
      );
    if (value.protected_end)
      c.append(
        p(
          "Защищённый период действующего договора — по " +
            value.protected_end +
            ". После него условия оцениваются отдельно.",
          "caption",
        ),
      );
  } else
    c.append(
      box(
        "Численный эффект не рассчитан. Неизвестные условия не заменяются нулём.",
      ),
    );
  if (value.missing_data.length)
    c.append(detail("Что нужно уточнить", list(value.missing_data)));
  c.append(
    detail(
      "Методика и исходные условия",
      p(value.formula),
      list(value.assumptions),
      node("pre", "", JSON.stringify(value.terms, null, 2)),
    ),
  );
  const bank = node("div", "bank-funding-basis");
  bank.append(node("h3", "", "Как использована аналитика банка"));
  for (const b of value.bank_basis || []) {
    const s = job.sources.find((x) => x.source_id === b.source_id);
    bank.append(
      p(
        (s?.title || "Материал банка") +
          " · " +
          (b.published_at || "Дата неизвестна") +
          " · " +
          b.claim_type,
        "caption",
      ),
      node("blockquote", "quote", b.quote),
      p(b.application),
      link(s?.url, "Источник банка"),
    );
  }
  if (!value.bank_basis?.length)
    bank.append(
      p(
        "В текущих основаниях нет подходящего структурированного тезиса банка для этого расчёта.",
        "caption",
      ),
    );
  bank.append(
    p(
      "Исторические оценки и прогнозы сохраняют дату. Независимая человеческая экспертиза разметки ещё не выполнена.",
      "fine-print",
    ),
  );
  c.append(bank);
  return c;
}

function exportFunding(lines, value) {
  if (!value) return;
  lines.push(
    "## Финансирование: сценарии договора",
    "",
    value.next_action,
    "",
    value.formula,
    "",
    ...(value.news_lens ? [value.news_lens.interpretation, ""] : []),
    ...(value.effective_start ? ["Период начисления: " + value.effective_start + " — " + value.effective_end, ""] : []),
    ...(value.protected_end ? ["Защищённый период — по " + value.protected_end, ""] : []),
    ...(value.zero_reason ? [value.zero_reason, ""] : []),
    ...value.scenarios.map(
      (s) =>
        `Сценарий ставки ${s.scenario_delta_pp} п.п. (${s.shock_basis}); ${s.exposure_days} дней; изменение процентных расходов ${s.gross_interest_change_rub} RUB.`,
    ),
    "",
    ...value.assumptions,
    "",
    ...value.missing_data.map((x) => "Нужно уточнить: " + x),
    "",
    "Это гипотетическая чувствительность; фактический эффект новости и чистый результат не установлены.",
    "",
  );
  for (const b of value.bank_basis || [])
    lines.push(
      "Банковское основание (" + b.published_at + "):",
      "> " + b.quote,
      b.application,
      "",
    );
}
