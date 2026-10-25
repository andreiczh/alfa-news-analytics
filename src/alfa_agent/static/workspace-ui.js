"use strict";

function resetWorkspaceViews(keepQuery = false) {
  S.quick = {
    query: keepQuery ? S.quick.query : "",
    result: null,
    loading: false,
    error: null,
  };
  S.companySearch = { query: "", result: null, loading: false, error: null };
  S.reportList = null;
  S.savedList = null;
}

function renderWorkspaceSearch(scope = "all") {
  const state = scope === "company" ? S.companySearch : S.quick;
  const c = card(scope === "all" ? "workspace-search" : "company-search");
  c.id = "workspace-search-" + scope;
  c.append(
    node(
      "h2",
      "",
      scope === "company"
        ? "Найти в материалах компании"
        : "Что важно для вашего бизнеса?",
    ),
  );
  const form = node("form", "search-line");
  const input = node("input", "search-input");
  input.type = "search";
  input.id = "workspace-query-" + scope;
  input.setAttribute(
    "aria-label",
    scope === "company"
      ? "Поиск по материалам компании"
      : "Вопрос или тема для поиска",
  );
  input.placeholder =
    scope === "company"
      ? "Например, стратегия, фондирование, целевые сегменты"
      : "Задайте вопрос или тему: ставка, фондирование, конкуренты…";
  input.value = state.query;
  input.minLength = 2;
  input.maxLength = 500;
  input.required = true;
  input.addEventListener("input", () => {
    (scope === "company" ? S.companySearch : S.quick).query = input.value;
  });
  const submit = node("button", "button primary", "Найти");
  submit.type = "submit";
  submit.id = "workspace-submit-" + scope;
  add(form, input, submit);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    runWorkspaceSearch(scope);
  });
  c.append(form);
  if (scope === "all") {
    const chips = node("div", "chips");
    const questions = isBank()
      ? ["Фондирование и ликвидность", "Процентная маржа", "Ставка"]
      : ["Ставка", "Спрос и конкуренты", "Инфляция и затраты"];
    for (const q of questions)
      chips.append(
        button(
          q,
          () => {
            S.quick.query = q;
            input.value = q;
            return runWorkspaceSearch(scope);
          },
          "chip",
        ),
      );
    c.append(
      chips,
      p(
        "Найдём новости, материалы банка и сведения вашей компании. Для связного вывода можно запустить исследование.",
        "fine-print",
      ),
    );
  }
  const results = node("div", "search-results");
  results.id = "workspace-results-" + scope;
  results.setAttribute("aria-live", "polite");
  results.append(renderSearchResults(scope));
  c.append(results);
  return c;
}

async function runWorkspaceSearch(scope = "all", silent = false) {
  const key = scope === "company" ? "companySearch" : "quick";
  const state = S[key],
    cid = S.company?.company_id,
    role = S.role;
  const query = (
    silent && state.result ? state.result.query : state.query
  ).trim();
  if (!cid || query.length < 2) return;
  const requestId = (state.requestId = (state.requestId || 0) + 1);
  state.loading = true;
  state.error = null;
  updateWorkspaceSearch(scope);
  try {
    const result = await api(
      "/api/companies/" +
        encodeURIComponent(cid) +
        "/search?" +
        new URLSearchParams({ q: query, scope, days: String(S.newsDays) }),
    );
    if (
      S[key] !== state ||
      state.requestId !== requestId ||
      cid !== S.company?.company_id ||
      role !== S.role
    )
      return;
    state.result = result;
  } catch (error) {
    if (S[key] === state && state.requestId === requestId)
      state.error = message(error);
  } finally {
    if (S[key] === state && state.requestId === requestId) {
      state.loading = false;
      updateWorkspaceSearch(scope);
    }
  }
}

function updateWorkspaceSearch(scope) {
  const state = scope === "company" ? S.companySearch : S.quick;
  const results = $("#workspace-results-" + scope),
    submit = $("#workspace-submit-" + scope);
  if (results) results.replaceChildren(renderSearchResults(scope));
  if (submit) {
    submit.disabled = state.loading;
    submit.textContent = state.loading ? "Ищем…" : "Найти";
  }
}

function renderSearchResults(scope) {
  const state = scope === "company" ? S.companySearch : S.quick;
  const c = node("div", "stack");
  if (state.loading) c.append(p("Ищем в доступных источниках…", "caption"));
  if (state.error) c.append(box(state.error, "amber"));
  const result = state.result;
  if (!result) return c;
  add(
    c,
    node("h3", "search-query-title", "По запросу «" + result.query + "»"),
    p(result.summary, "caption"),
  );
  if (result.empty_query_terms)
    c.append(
      p("Добавьте предмет поиска: название, событие, продукт или показатель."),
    );
  if (result.processing_documents)
    c.append(
      box(
        "Часть файлов ещё обрабатывается. Сейчас показаны доступные фрагменты; после обработки повторите поиск.",
      ),
    );
  if (scope === "all" && ["QUEUED", "RUNNING"].includes(result.news_status))
    c.append(
      p(
        "Подборка новостей обновляется. Новые результаты появятся после завершения сбора.",
        "fine-print",
      ),
    );
  const groups = node("div", scope === "all" ? "search-groups" : "stack");
  const addGroup = (title, items, renderer, limit) => {
    const section = node("section", "search-group");
    section.append(node("h3", "", title));
    if (!items.length)
      section.append(p("Совпадений в доступных источниках нет.", "fine-print"));
    items.slice(0, limit).forEach((item) => section.append(renderer(item)));
    if (items.length > limit)
      section.append(
        detail(
          "Ещё " + (items.length - limit),
          ...items.slice(limit).map(renderer),
        ),
      );
    groups.append(section);
  };
  if (scope === "all") addGroup("Новости", result.news, renderSearchNews, 3);
  addGroup("Сведения вашей компании", result.company, renderInternalHit, 3);
  if (scope === "all")
    addGroup("Материалы банка", result.bank, renderBankHit, 2);
  c.append(groups);
  if (scope === "all") {
    const q =
      result.query.length >= 12
        ? result.query
        : "Что означают новости по теме «" +
          result.query +
          "» для компании «" +
          companyName() +
          "» и какие данные нужно проверить для моей роли?";
    const actions = node("div", "row");
    const start = button(
      "Исследовать вопрос",
      () =>
        startResearch(q, {
          bank_document_id: null,
          seed_news_ids: result.news.slice(0, 3).map((item) => item.news_id),
        }),
      "button primary",
      "spark",
    );
    start.disabled = busyDocs() || S.starting;
    add(
      actions,
      start,
      button(
        "Сбросить поиск",
        () => {
          S.quick = { query: "", result: null, loading: false, error: null };
          const old = $("#workspace-search-all");
          if (old) old.replaceWith(renderWorkspaceSearch());
        },
        "text-button",
      ),
    );
    const configure = button(
      "Настроить исследование",
      () => {
        S.draft.question = q;
        S.job = null;
        go("research");
      },
      "text-button",
    );
    actions.append(configure);
    add(
      c,
      actions,
      p(
        "Горизонт исследования: " +
          (S.draft.planningDays || 30) +
          " дней. Его можно изменить в настройке исследования.",
        "fine-print",
      ),
      p(result.scope_note, "fine-print"),
    );
  }
  return c;
}

function renderSearchNews(item) {
  const c = node("article", "search-hit");
  c.dataset.newsId = item.news_id;
  add(
    c,
    node("h4", "", item.title),
    p(item.publisher + " · " + date(item.published_at), "caption"),
  );
  add(
    c,
    badge(
      item.read_status === "article_read"
        ? "Текст прочитан"
        : "Только заголовок — не подтверждение",
    ),
    p(item.decision?.why || "Связь с компанией нужно проверить.", "fine-print"),
  );
  if (item.excerpt)
    c.append(
      detail("Фрагмент публикации", node("blockquote", "quote", item.excerpt)),
    );
  add(
    c,
    add(
      node("div", "row"),
      button("Разобрать новость", () => researchNews(item), "text-button"),
      saveNewsButton(item),
      link(item.url, "Источник"),
    ),
  );
  return c;
}

function renderInternalHit(item) {
  const c = node("article", "search-hit");
  add(
    c,
    node("h4", "", item.title),
    p(item.locator + (item.page ? " · с. " + item.page : ""), "caption"),
    node("blockquote", "quote", item.quote),
  );
  if (item.kind === "company_context")
    c.append(
      p(
        "Из контекста, подтверждённого вами; независимой проверки нет.",
        "fine-print",
      ),
    );
  else
    c.append(
      p(
        "Фрагмент документа компании; сведения независимо не подтверждены.",
        "fine-print",
      ),
    );
  if (
    item.document_id &&
    S.docs.some((d) => d.document_id === item.document_id)
  )
    c.append(
      button(
        "Скачать документ",
        () => downloadFile(item.document_id),
        "text-button",
      ),
    );
  return c;
}

function renderBankHit(item) {
  const c = node("article", "search-hit");
  add(
    c,
    node("h4", "", item.title),
    p(
      date(item.published_at) + (item.page ? " · с. " + item.page : ""),
      "caption",
    ),
    detail("Фрагмент материала", node("blockquote", "quote", item.quote)),
    p(
      "Материал банка: сохранена исходная дата. OCR требует проверки по оригиналу.",
      "fine-print",
    ),
    link(item.url, "Открыть оригинал"),
  );
  return c;
}

function saveNewsButton(item) {
  const b = button(
    item.saved ? "В избранном" : "Сохранить",
    async () => {
      const cid = S.company.company_id,
        value = !item.saved;
      b.disabled = true;
      try {
        await api(
          "/api/companies/" +
            encodeURIComponent(cid) +
            "/news/" +
            encodeURIComponent(item.news_id) +
            "/saved",
          { method: "PUT", body: JSON.stringify({ saved: value }) },
        );
        if (cid !== S.company?.company_id) return;
        item.saved = value;
        for (const list of [S.news?.items, S.quick.result?.news])
          for (const row of list || [])
            if (row.news_id === item.news_id) row.saved = value;
        for (const el of document.querySelectorAll(
          '[data-save-news="' + item.news_id + '"]',
        )) {
          el.replaceChildren(
            icon("star"),
            document.createTextNode(value ? "В избранном" : "Сохранить"),
          );
          el.setAttribute("aria-pressed", String(value));
        }
        S.savedList = null;
        if (S.view === "saved") await loadSaved();
        toast(
          value
            ? "Новость сохранена в избранном"
            : "Новость удалена из избранного",
        );
      } finally {
        b.disabled = false;
      }
    },
    "text-button",
    "star",
  );
  b.dataset.saveNews = item.news_id;
  b.setAttribute("aria-pressed", String(Boolean(item.saved)));
  return b;
}

function researchNews(item) {
  return startResearch(
    "Как событие «" +
      item.title +
      "» может повлиять на компанию «" +
      companyName() +
      "» и что стоит проверить для роли «" +
      activeRoleName() +
      "»?",
    { news_item_id: item.news_id, bank_document_id: null },
  );
}

async function loadReports(more = false) {
  const cid = S.company?.company_id;
  if (!cid) return;
  let state = S.reportList;
  if (!more || !state)
    state = S.reportList = {
      items: [],
      loading: false,
      hasMore: true,
      error: null,
    };
  if (state.loading) return;
  state.loading = true;
  if (S.view === "reports") render();
  try {
    const items = await api(
      "/api/research?" +
        new URLSearchParams({
          company_id: cid,
          limit: "25",
          offset: String(state.items.length),
        }),
    );
    if (cid !== S.company?.company_id || S.reportList !== state) return;
    state.items.push(...items);
    state.hasMore = items.length === 25;
  } catch (error) {
    if (S.reportList === state) state.error = message(error);
  } finally {
    if (S.reportList === state) {
      state.loading = false;
      if (S.view === "reports") render();
    }
  }
}

function renderReports(main) {
  const title = heading("Отчёты", companyName() + " · " + activeRoleName());
  title.append(
    button(
      "Новое исследование",
      () => {
        S.job = null;
        go("research");
      },
      "button primary",
      "spark",
    ),
  );
  main.append(title);
  const state = S.reportList;
  if (!state || state.loading) main.append(p("Загружаем исследования…"));
  if (state?.error) main.append(box(state.error, "amber"));
  const items = state?.items || [];
  if (!items.length && !state?.loading)
    main.append(
      empty(
        "Отчётов пока нет",
        "Запустите исследование. Здесь будут результаты, уточнения и незавершённые задачи.",
        button(
          "Задать вопрос",
          () => {
            S.job = null;
            go("research");
          },
          "button primary",
        ),
      ),
    );
  const list = node("div", "stack");
  for (const item of items) list.append(reportCard(item));
  main.append(list);
  if (state?.hasMore && !state.loading)
    main.append(button("Показать ещё", () => loadReports(true), "button"));
}

function reportCard(item) {
  const c = historyCard(item);
  if (item.role) c.prepend(badge(ROLES[item.role] || "Руководитель"));
  if (item.parent_id)
    c.append(
      button(
        "Исходное исследование",
        () => loadJob(item.parent_id),
        "text-button",
      ),
    );
  if (item.favorite) c.append(badge("В избранном"));
  return c;
}

async function loadSaved(more = false) {
  const cid = S.company?.company_id;
  if (!cid) return;
  let state = S.savedList;
  if (!more || !state)
    state = S.savedList = {
      news: [],
      reports: [],
      tab: state?.tab || "news",
      loading: false,
      hasMoreNews: false,
      hasMoreReports: false,
      error: null,
    };
  if (state.loading) return;
  state.loading = true;
  if (S.view === "saved") render();
  try {
    const [news, reports] = await Promise.all([
      api(
        "/api/companies/" +
          encodeURIComponent(cid) +
          "/news/saved?limit=25&offset=" +
          state.news.length,
      ),
      api(
        "/api/research?" +
          new URLSearchParams({
            company_id: cid,
            saved_only: "true",
            limit: "25",
            offset: String(state.reports.length),
          }),
      ),
    ]);
    if (cid !== S.company?.company_id || S.savedList !== state) return;
    state.news.push(...news.items);
    state.reports.push(...reports);
    state.hasMoreNews = news.has_more;
    state.hasMoreReports = reports.length === 25;
  } catch (error) {
    if (S.savedList === state) state.error = message(error);
  } finally {
    if (S.savedList === state) {
      state.loading = false;
      if (S.view === "saved") render();
    }
  }
}

function renderSaved(main) {
  main.append(
    heading(
      "Избранное",
      "Сохранённые новости и исследования выбранной компании.",
    ),
  );
  const state = S.savedList;
  if (!state) {
    main.append(p("Загружаем избранное…"));
    return;
  }
  const tabs = node("div", "segmented");
  for (const [id, label] of [
    ["news", "Новости"],
    ["reports", "Исследования"],
  ])
    tabs.append(
      button(
        label,
        () => {
          state.tab = id;
          render();
        },
        "segment" + (state.tab === id ? " active" : ""),
      ),
    );
  main.append(tabs);
  if (state.error) main.append(box(state.error, "amber"));
  if (state.loading) main.append(p("Загружаем…"));
  const items = state[state.tab];
  if (!items.length && !state.loading)
    main.append(
      empty(
        "Здесь пока пусто",
        state.tab === "news"
          ? "Нажмите «Сохранить» у подходящей новости."
          : "Сохраните полезное исследование на странице результата.",
      ),
    );
  const list = node("div", "stack saved-items");
  for (const item of items) {
    if (state.tab === "reports") list.append(reportCard(item));
    else {
      const c = card("saved-news-card");
      add(
        c,
        badge("Сохранённая публикация"),
        node("h2", "", item.title),
        p(
          item.publisher + " · опубликовано " + date(item.published_at),
          "caption",
        ),
        p(
          "Сохранено " +
            date(item.saved_at) +
            ". Дата публикации остаётся исходной.",
          "fine-print",
        ),
      );
      if (item.excerpt) c.append(node("blockquote", "quote", item.excerpt));
      add(
        c,
        add(
          node("div", "row"),
          button("Разобрать новость", () => researchNews(item), "button"),
          saveNewsButton(item),
          link(item.url, "Первоисточник"),
        ),
      );
      list.append(c);
    }
  }
  main.append(list);
  if (
    !state.loading &&
    (state.tab === "news" ? state.hasMoreNews : state.hasMoreReports)
  )
    main.append(button("Показать ещё", () => loadSaved(true), "button"));
}

function researchScopeNote() {
  return detail(
    "Как строится исследование",
    p(
      "Агент проверяет контекст компании и материалы банка, ищет внешние основания по вопросу, сопоставляет их и проверяет вывод. Рынок, конкуренты и продукты изучаются, когда это нужно для ответа. При пробеле агент меняет поиск или задаёт уточнение. Полнота анализа зависит от доступных источников.",
    ),
  );
}

function renderResearchWorkflow(job) {
  const c = node("section", "research-workflow");
  c.append(node("h3", "", "Этапы исследования"));
  const trace = job.trace || [],
    completed = trace.filter((e) => e.event === "tool_completed");
  const latestStart = trace.filter((e) => e.event === "tool_started").at(-1);
  const activeTool =
    latestStart &&
    !completed.some((e) => e.data.step_id === latestStart.data.step_id) &&
    job.status === "RUNNING"
      ? latestStart.data.tool
      : null;
  const workStarted = trace.some(
    (e) =>
      e.event === "llm_started" &&
      ["research_write", "research_narrative"].includes(e.data.stage),
  );
  const verificationStarted = trace.some(
    (e) => e.event === "llm_started" && e.data.stage === "research_verify",
  );
  const tools = [
    ["company", "Контекст и документы компании"],
    ["bank", "Материалы банка"],
    ["external", "Внешние источники и событие"],
    ["analysis", "Сопоставление и вывод"],
    ["verify", "Проверка оснований"],
  ];
  const steps = node("ol", "workflow-steps");
  for (const [tool, label] of tools) {
    const events = completed.filter((e) =>
      tool === "external"
        ? ["public", "read_source"].includes(e.data.tool)
        : e.data.tool === tool,
    );
    let status = "Ожидает",
      tone = "";
    if (events.length) {
      status = events.some((e) => !e.data.error)
        ? "Получено фрагментов: " +
          events.reduce((n, e) => n + (e.data.sources_added || 0), 0)
        : "Источники недоступны";
      tone = events.some((e) => !e.data.error) ? "done" : "limited";
    }
    if (
      activeTool === tool ||
      (tool === "external" && ["public", "read_source"].includes(activeTool))
    ) {
      status = "В работе";
      tone = "current";
    }
    if (tool === "external" && !events.length && !activeTool) {
      const existing = (job.sources || []).filter((s) =>
        ["public_page", "user_news"].includes(s.kind),
      );
      if (existing.length) {
        status = "Источников в контексте: " + existing.length;
        tone = "done";
      } else if (workStarted) {
        status = "Нет прочитанных внешних оснований";
        tone = "limited";
      }
    }
    if (tool === "analysis" && workStarted) {
      status =
        verificationStarted || job.result ? "Вывод подготовлен" : "В работе";
      tone = verificationStarted || job.result ? "done" : "current";
    }
    if (tool === "verify" && verificationStarted) {
      status = job.result
        ? "Проверка завершена"
        : job.replans
          ? "Проверяем после пересмотра"
          : "В работе";
      tone = job.result ? "done" : "current";
    }
    if (tool === "verify" && job.verification?.passed) {
      status = "Автоматическая проверка пройдена";
      tone = "done";
    }
    if (["FAILED", "CANCELLED"].includes(job.status) && tone === "current") {
      status = "Остановлено";
      tone = "limited";
    }
    if (["FAILED", "CANCELLED"].includes(job.status) && status === "Ожидает") {
      status = "Не выполнялось";
      tone = "limited";
    }
    if (
      !job.result &&
      job.replans &&
      ["PLAN", "SOURCES"].includes(job.phase) &&
      ["analysis", "verify"].includes(tool)
    ) {
      status = "Пересматриваем основания";
      tone = "current";
    }
    if (tool === "verify" && job.result && !job.quality_gate?.current_rules) {
      status = "Историческая проверка; требуется обновление";
      tone = "limited";
    }
    steps.append(
      add(
        node("li", tone),
        node("strong", "", label),
        node("span", "caption", status),
      ),
    );
  }
  c.append(steps);
  const plans = trace.filter((e) => e.event === "plan");
  if (plans.length) {
    const plan = plans.at(-1),
      d = detail(
        "План агента" +
          (plans.length > 1 ? " · пересмотр " + (plans.length - 1) : ""),
      );
    d.append(p(plan.data.rationale));
    for (const action of plan.data.actions || [])
      d.append(
        p(
          ({
            company: "Компания",
            bank: "Банк",
            public: "Внешний поиск",
            read_source: "Чтение источника",
            ask_user: "Уточнение",
            write: "Вывод",
          }[action.tool] || action.tool) +
            ": " +
            action.reason,
          "fine-print",
        ),
      );
    c.append(d);
  }
  if (job.parent_id)
    c.append(
      button(
        "Открыть исходное исследование",
        () => loadJob(job.parent_id),
        "text-button",
      ),
    );
  return c;
}
