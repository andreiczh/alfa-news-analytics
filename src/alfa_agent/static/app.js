"use strict";
const $ = (s) => document.querySelector(s);
const TOKEN = $('meta[name="app-token"]').content;
const ROLES = {
  cfo: "CFO",
  treasurer: "Казначей",
  ceo: "CEO",
  coo: "COO",
  cco: "CCO",
};
const ROLE_NAMES = {
  cfo: "Финансовый директор",
  treasurer: "Казначейство",
  ceo: "Генеральный директор",
  coo: "Операционный директор",
  cco: "Коммерческий директор",
};
const ICONS = {
  home: '<path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
  spark:
    '<path d="m12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5Z"/>',
  building:
    '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h2m4 0h2M8 11h2m4 0h2M8 15h2m4 0h2M10 21v-3h4v3"/>',
  book: '<path d="M12 5c-3-2-6-2-10-1v15c4-1 7-1 10 1 3-2 6-2 10-1V4c-4-1-7-1-10 1Z"/><path d="M12 5v15"/>',
  arrow: '<path d="M4 12h16m-5-5 5 5-5 5"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M3 17v4h18v-4"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M3 15v6h18v-6"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  refresh:
    '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>',
  star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3l-5.6 2.9 1.1-6.2L3 9.6l6.2-.9Z"/>',
};
const LABELS = {
  company_name: "Название",
  company_description: "О компании",
  reported_name: "Название в документе",
  industry: "Деятельность",
  okved_code: "ОКВЭД",
  business_model: "Бизнес-модель",
  international_activity: "Международная деятельность",
  competitors: "Указанные конкуренты",
  strategy: "Стратегия",
  tactics: "Тактика",
  rate_type: "Тип ставки по долгу",
  floating_debt_rub: "Плавающий долг",
  total_debt_rub: "Общий долг",
  capex_planned_rub: "Планируемые инвестиции",
  inventory_days: "Оборачиваемость запасов",
  receivables_days: "Срок дебиторской задолженности",
  credit_sales_share: "Доля продаж с отсрочкой",
  annual_revenue_rub: "Выручка по документам",
  ebitda_rub: "EBITDA по документам",
  export_share: "Доля экспорта",
  import_share: "Доля импорта",
  key_products: "Основные продукты",
  markets: "Рынки",
  employees: "Численность",
  has_loan: "Кредит",
  has_deposit: "Депозит",
  has_factoring: "Факторинг",
  has_leasing: "Лизинг",
  has_guarantee: "Гарантия",
  has_payroll: "Зарплатный проект",
  has_settlement: "Расчётный счёт",
  organization_type: "Тип организации",
  ownership_form: "Форма собственности",
  linked_person_count: "Связанных ID физлиц",
  sample_outgoing_counterparties: "Контрагентов по исходящим в срезе",
  sample_incoming_counterparties: "Контрагентов по входящим в срезе",
  sample_outgoing_top_share: "Топ-1 исходящих в срезе",
  sample_incoming_top_share: "Топ-1 входящих в срезе",
};
const S = {
  me: null,
  workspace: null,
  library: null,
  companies: [],
  company: null,
  snapshot: null,
  docs: [],
  history: [],
  legacyHistory: [],
  connections: null,
  view: "home",
  job: null,
  legacy: null,
  legacySources: null,
  role: "cfo",
  filter: "all",
  epoch: 0,
  poll: null,
  docPoll: null,
  news: null,
  newsPoll: null,
  newsRole: "cfo",
  newsDays: 7,
  newsExpanded: false,
  newsLoading: false,
  uploading: false,
  starting: false,
  draft: { question: "", news: "", urls: "", public: true },
};

function node(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt !== undefined && txt !== null) e.textContent = String(txt);
  return e;
}
function add(e, ...children) {
  e.append(...children.filter(Boolean));
  return e;
}
function icon(name) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({
    viewBox: "0 0 24 24",
    width: "20",
    height: "20",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.6",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  }))
    e.setAttribute(k, v);
  e.innerHTML = ICONS[name] || ICONS.spark;
  return e;
}
function button(txt, fn, cls = "button", ico) {
  const b = node("button", cls);
  b.type = "button";
  if (ico) b.append(icon(ico));
  b.append(document.createTextNode(txt));
  if (fn)
    b.addEventListener("click", () => Promise.resolve().then(fn).catch(report));
  return b;
}
function card(cls = "") {
  return node("section", "card " + cls);
}
function p(txt, cls = "subtle") {
  return node("p", cls, txt);
}
function badge(txt, tone = "") {
  return node("span", "badge " + tone, txt);
}
function box(txt, tone = "") {
  return node("div", "info-box " + tone, txt);
}
function list(items, cls = "check-list", ordered = false) {
  const ul = node(ordered ? "ol" : "ul", cls);
  items.forEach((t) => ul.append(node("li", "", t)));
  return ul;
}
function detail(title, ...children) {
  const d = node("details");
  add(d, node("summary", "", title), ...children);
  return d;
}
function heading(title, desc, kicker = "") {
  return add(
    node("div", "page-heading"),
    add(
      node("div"),
      kicker ? node("span", "eyebrow", kicker) : null,
      node("h1", "", title),
      p(desc),
    ),
  );
}
function empty(title, desc, action) {
  return add(node("div", "empty"), node("h3", "", title), p(desc), action);
}
function date(value) {
  if (!value) return "Дата не установлена";
  const d = new Date(value);
  return isNaN(d)
    ? String(value)
    : d.toLocaleDateString("ru-RU", {
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "Europe/Moscow",
      });
}
function shortDate(value) {
  return value
    ? new Date(value).toLocaleString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Europe/Moscow",
      })
    : "";
}
function link(url, title, cls = "text-button") {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:" || u.username || u.password) return null;
  } catch {
    return null;
  }
  const a = node("a", cls, title);
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
function roleFor(title) {
  const t = (title || "").toLowerCase();
  if (/казнач|treasur/.test(t)) return "treasurer";
  if (/\bceo\b|генеральн/.test(t)) return "ceo";
  if (/\bcoo\b|операцион/.test(t)) return "coo";
  if (/\bcco\b|коммерческ/.test(t)) return "cco";
  return "cfo";
}
function statusText(status) {
  return (
    {
      QUEUED: "В очереди",
      RUNNING: "Исследуем",
      WAITING_INPUT: "Нужно уточнение",
      SUCCEEDED: "Готово",
      PARTIAL: "Готово с ограничениями",
      FAILED: "Не завершено",
      CANCELLED: "Остановлено",
      DEFERRED_BUDGET: "Ожидает лимита API",
      READY: "Прочитан",
      EXCLUDED: "Исключён",
    }[status] || status
  );
}
const ERRORS = {
  company_documents_processing:
    "Документы ещё обрабатываются. Дождитесь обновления профиля и повторите.",
  research_queue_limit:
    "Сначала завершите или остановите текущие исследования.",
  research_context_limit:
    "Достигнут лимит источников в цепочке уточнений. Начните отдельное исследование с нужными материалами.",
  access_denied: "Нет доступа к этому рабочему пространству.",
  bank_requires_custom_research:
    "Для банка используйте исследование: эффект ставки зависит от активов и обязательств.",
  file_size_limit: "Файл больше 15 МБ. Уменьшите его и повторите.",
  company_upload_quota: "Достигнут лимит: 20 файлов или 100 МБ на компанию.",
  unsupported_extension: "Этот формат не поддерживается.",
  file_type_not_supported:
    "Этот формат не поддерживается. Выберите PDF, DOCX, PPTX, XLSX, CSV или TXT.",
  pdf_page_limit:
    "В PDF больше 40 страниц. Загрузите нужный раздел отдельным файлом.",
  public_https_required:
    "Для источника нужна полная ссылка, начинающаяся с https://.",
  website_requires_https: "Укажите адрес сайта, начинающийся с https://.",
  research_verification_failed:
    "Вывод не прошёл проверку оснований. Он не опубликован. Уточните вопрос или добавьте источники.",
  model_contract_invalid:
    "Модель не вернула ответ нужного формата. Сузьте вопрос и повторите исследование.",
  shared_budget_exhausted:
    "Общий лимит API команды исчерпан. Запрос продолжится после обновления лимита в 03:00 МСК.",
  api_auth_failed:
    "Организаторский API не принял ключ. Нужна проверка подключения.",
  api_key_missing: "Ключ организаторского API не настроен.",
  external_result_unknown:
    "Ответ API не получен. Автоматический повтор отключён, чтобы избежать повторного списания.",
  interrupted_manual_retry:
    "Исследование прервано перезапуском. Для нового запуска нажмите «Повторить».",
  model_output_truncated:
    "API вернул неполный ответ. Попробуйте сузить вопрос.",
  context_limit:
    "Слишком большой контекст. Сузьте вопрос или исключите лишние документы.",
  unsupported_public_topic: "Эта тема поиска пока не поддерживается.",
  research_limit:
    "Достигнут лимит шагов исследования. Сузьте вопрос и повторите.",
  rate_limited: "API временно ограничил частоту запросов. Повторите позже.",
  provider_unavailable: "Организаторский API временно недоступен.",
};
function message(error) {
  const code = typeof error === "string" ? error : error?.message;
  return (
    ERRORS[code] ||
    "Не удалось выполнить действие. Проверьте соединение и повторите."
  );
}
let toastTimer;
function toast(txt) {
  clearTimeout(toastTimer);
  $("#toast").textContent = txt;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 6500);
}
function report(error) {
  toast(message(error));
}
async function api(path, options = {}) {
  const headers = { "x-app-token": TOKEN, ...options.headers };
  if (options.body && !(options.body instanceof FormData))
    headers["Content-Type"] = "application/json";
  const r = await fetch(path, { ...options, headers });
  if (r.status === 401) {
    location.assign("/auth/login");
    throw new Error("team_login_required");
  }
  let body;
  try {
    body = await r.json();
  } catch {
    throw new Error("request_failed");
  }
  if (!r.ok)
    throw new Error(
      typeof body.detail === "string" ? body.detail : "request_failed",
    );
  return body;
}
function modal(title, content) {
  $("#dialog-title").textContent = title;
  $("#dialog-content").replaceChildren(content);
  if (!$("#dialog").open) $("#dialog").showModal();
}
function closeModal() {
  $("#dialog").close();
}
function companyName() {
  return S.company?.label || S.me?.company_name || "Ваша компания";
}
function origin() {
  return (
    {
      case_csv: "Обезличенная компания кейса",
      company_upload: "Компания, указанная вами",
      synthetic_demo: "Синтетический пример",
    }[S.snapshot?.metadata.source] || "Рабочая компания"
  );
}
function busyDocs() {
  return (
    S.uploading ||
    S.docs.some((d) =>
      ["QUEUED", "RUNNING", "DEFERRED_BUDGET"].includes(d.status),
    )
  );
}
function featureValue(f) {
  if (f.status === "missing") return "Нет данных";
  if (f.status === "conflicting")
    return "Есть противоречие — уточните источники";
  if (f.numeric_value) {
    const n = f.numeric_value;
    if (n.value === null)
      return `${n.minimum ?? "…"} – ${n.maximum ?? "…"} ${n.unit}`;
    const v = new Intl.NumberFormat("ru-RU", {
      maximumFractionDigits: 2,
    }).format(Number(n.value) * (n.unit === "fraction" ? 100 : 1));
    return (
      v +
      ({ RUB: " ₽", days: " дней", people: " чел.", count: "", fraction: "%" }[
        n.unit
      ] ?? " " + n.unit)
    );
  }
  if (f.boolean_value !== null && f.boolean_value !== undefined)
    return f.boolean_value ? "Есть в источнике" : "Нет в источнике";
  return (
    {
      floating: "Плавающая",
      fixed: "Фиксированная",
      mixed: "Смешанная",
      none: "Долг отсутствует",
    }[f.text_value] ||
    f.text_value ||
    "Нет данных"
  );
}
function isBank() {
  return /банк|\bbank\b/i.test(companyName());
}
function defaultQuestion() {
  return isBank()
    ? "Какие изменения внешней среды стоит учесть при планировании фондирования, ликвидности и процентного результата?"
    : "Какие изменения внешней среды важны для нашей компании и что стоит проверить в первую очередь?";
}

function go(view) {
  S.view = view;
  $("#sidebar").classList.remove("open");
  $("#mobile-menu").setAttribute("aria-expanded", "false");
  render();
  window.scrollTo({ top: 0, behavior: "instant" });
}
function render() {
  document
    .querySelectorAll("[data-view]")
    .forEach((b) =>
      b.classList.toggle(
        "active",
        b.dataset.view === (S.view === "legacy" ? "research" : S.view),
      ),
    );
  $("#company-name").textContent = companyName();
  $("#main").setAttribute("aria-busy", "false");
  const main = $("#main");
  main.replaceChildren();
  if (!S.snapshot) {
    main.append(
      empty(
        "Выберите компанию",
        "Добавьте свою компанию или откройте демонстрационный пример.",
        button("Выбрать компанию", switchCompany, "button primary"),
      ),
    );
    return;
  }
  (
    ({
      home: renderHome,
      research: renderResearch,
      company: renderCompany,
      library: renderLibrary,
      legacy: renderLegacy,
    })[S.view] || renderHome
  )(main);
}
function contextCard() {
  const c = card();
  add(
    c,
    node("span", "eyebrow", "КОНТЕКСТ ИССЛЕДОВАНИЯ"),
    node("h2", "context-title", companyName()),
    p(origin(), "fine-print"),
  );
  for (const [a, b] of [
    ["Ваша роль", S.me?.job_title || ROLES[S.role]],
    [
      "Прочитано файлов",
      String(S.docs.filter((d) => d.status === "READY").length),
    ],
    ["Банковских материалов", String(S.library?.bank_documents?.length || 9)],
  ])
    c.append(
      add(
        node("div", "context-line"),
        node("span", "", a),
        node("strong", "", b),
      ),
    );
  const description = S.snapshot.context?.description;
  if (description)
    c.append(
      p(
        description.slice(0, 230) + (description.length > 230 ? "…" : ""),
        "fine-print",
      ),
    );
  else
    c.append(
      p(
        "Добавьте описание бизнеса и документы: пока известны только исходные сведения.",
        "fine-print",
      ),
    );
  add(
    c,
    button("Уточнить профиль", () => go("company"), "text-button"),
    box(
      "Вывод будет учитывать доступные источники. Недостающие данные останутся видимыми.",
    ),
  );
  if (S.history.length) {
    const completed = S.history.filter((j) =>
      ["SUCCEEDED", "PARTIAL"].includes(j.status),
    ).length;
    const failed = S.history.filter((j) => j.status === "FAILED").length;
    c.append(
      detail(
        "Проверки ваших исследований",
        p(
          "Из последних " +
            S.history.length +
            " запросов: " +
            completed +
            " завершены с проверкой оснований, " +
            failed +
            " не завершены. Это статус работы системы, не оценка истинности источников.",
          "fine-print",
        ),
      ),
    );
  }
  return c;
}

function composer(compact = false) {
  const c = card(compact ? "hero" : "");
  if (compact)
    add(
      c,
      node("h2", "", "Что важно для вашей компании?"),
      p("Задайте вопрос. Агент свяжет события, материалы банка и ваш бизнес."),
    );
  const form = node("form");
  const question = node("textarea", "question-input");
  question.id = "research-question";
  question.name = "question";
  question.placeholder = defaultQuestion();
  question.setAttribute("aria-label", "Вопрос для исследования");
  question.maxLength = 1500;
  question.minLength = 12;
  question.required = true;
  question.value = S.draft.question;
  question.addEventListener("input", () => (S.draft.question = question.value));
  form.append(question);
  const chips = node("div", "chips");
  let suggestions = isBank()
    ? [
        "Фондирование и ликвидность",
        "Риски процентной маржи",
        "Спрос на кредитование",
      ]
    : ["Стоимость финансирования", "Спрос и конкуренты", "Влияние инфляции"];
  let questions = isBank()
    ? [
        defaultQuestion(),
        "Какие факторы могут повлиять на процентную маржу банка и какие внутренние данные нужны CFO для оценки?",
        "Как изменения спроса и стоимости риска могут повлиять на планирование кредитования банка?",
      ]
    : [
        "Как изменения ставки могут повлиять на стоимость финансирования нашей компании?",
        "Какие изменения спроса и действий конкурентов стоит проверить нашей компании?",
        "Как инфляция может повлиять на затраты и маржу нашей компании?",
      ];
  try {
    const saved = JSON.parse(localStorage.getItem(quickKey()) || "null");
    if (
      Array.isArray(saved) &&
      saved.length &&
      saved.every(
        (q) => typeof q === "string" && q.length >= 12 && q.length <= 300,
      )
    ) {
      questions = saved.slice(0, 3);
      suggestions = questions.map((q) =>
        q.length > 42 ? q.slice(0, 41) + "…" : q,
      );
    }
  } catch {
    /* A browser preference never prevents a research request. */
  }
  suggestions.forEach((txt, i) =>
    chips.append(
      button(
        txt,
        () => {
          question.value = questions[i];
          S.draft.question = questions[i];
          question.focus();
        },
        "chip",
      ),
    ),
  );
  chips.append(button("Настроить", () => quickSettings(questions), "chip"));
  form.append(chips);
  const advanced = node("div", "advanced-fields");
  const title = node("input");
  title.id = "research-role";
  title.maxLength = 120;
  title.value = S.draft.jobTitle || S.me?.job_title || ROLES[S.role];
  title.addEventListener("input", () => (S.draft.jobTitle = title.value));
  title.placeholder = "CFO, казначей, CEO…";
  advanced.append(field("Для какой должности готовим вывод", title));
  const urls = node("textarea");
  urls.id = "research-urls";
  urls.rows = 2;
  urls.placeholder = "https://…\nКаждая ссылка с новой строки, до трёх";
  urls.value = S.draft.urls;
  urls.addEventListener("input", () => (S.draft.urls = urls.value));
  advanced.append(field("Ссылки на новость или первоисточники", urls));
  const news = node("textarea");
  news.id = "research-news";
  news.rows = 4;
  news.maxLength = 10000;
  news.placeholder = "Вставьте текст новой публикации, если ссылки нет";
  news.value = S.draft.news;
  news.addEventListener("input", () => (S.draft.news = news.value));
  advanced.append(field("Текст новости", news));
  form.append(
    detail("Добавить новость, ссылки или изменить должность", advanced),
  );
  const checkbox = node("input");
  checkbox.type = "checkbox";
  checkbox.id = "public-search";
  checkbox.checked = S.draft.public;
  checkbox.addEventListener(
    "change",
    () => (S.draft.public = checkbox.checked),
  );
  const checkboxLabel = add(
    node("label", "check-row"),
    checkbox,
    node(
      "span",
      "",
      "Искать открытые источники. В поиск попадут название компании и общая тема; документы и внутренние показатели в поисковый запрос не включаются.",
    ),
  );
  form.append(checkboxLabel);
  const submit = node("button", "button primary");
  submit.type = "submit";
  add(
    submit,
    icon("spark"),
    document.createTextNode(S.starting ? "Запускаем…" : "Исследовать"),
  );
  submit.disabled = S.starting || busyDocs();
  const actions = add(
    node("div", "composer-actions"),
    button("Добавить документы компании", () => go("company"), "text-button"),
    submit,
  );
  form.append(actions);
  if (busyDocs())
    form.append(
      p(
        "Сначала дождитесь обработки файлов. Результат будет построен по обновлённому профилю.",
        "fine-print",
      ),
    );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (S.starting) return;
    const q = question.value.trim();
    if (q.length < 12) {
      question.setCustomValidity(
        "Опишите вопрос подробнее — хотя бы 12 символов.",
      );
      question.reportValidity();
      question.setCustomValidity("");
      return;
    }
    try {
      await startResearch(q, {
        job_title: title.value,
        source_urls: urls.value
          .split(/\n/)
          .map((x) => x.trim())
          .filter(Boolean),
        news_text: news.value,
        public_search: checkbox.checked,
      });
    } catch (error) {
      report(error);
    }
  });
  c.append(form);
  return c;
}
function field(label, input) {
  const wrap = node("div", "field"),
    l = node("label", "", label);
  if (!input.id) input.id = "field-" + crypto.randomUUID();
  l.htmlFor = input.id;
  add(wrap, l, input);
  return wrap;
}

function renderHome(main) {
  main.append(
    heading(
      "События. Контекст. Следующий шаг.",
      "Аналитика для " +
        companyName() +
        ", роль — " +
        (S.me?.job_title || ROLES[S.role]) +
        ".",
    ),
  );
  const left = node("div", "stack"),
    aside = node("aside", "stack");
  left.append(composer(true));
  const news = node("section", "stack");
  news.id = "personal-news";
  news.setAttribute("aria-label", "Новости для компании и роли");
  left.append(news);
  renderNews(news);
  const title = node("div", "section-heading");
  title.append(node("h2", "", "Ваши исследования"));
  const filters = node("div", "segmented");
  for (const [id, txt] of [
    ["all", "Все"],
    ["favorites", "Избранное"],
  ])
    filters.append(
      button(
        txt,
        () => {
          S.filter = id;
          render();
        },
        "segment" + (S.filter === id ? " active" : ""),
      ),
    );
  title.append(filters);
  left.append(title);
  const visible = S.history.filter((j) => S.filter === "all" || j.favorite);
  if (!visible.length)
    left.append(
      empty(
        S.filter === "favorites"
          ? "Пока нет избранного"
          : "Начните с одного вопроса",
        S.filter === "favorites"
          ? "Сохраняйте полезные исследования, чтобы быстро вернуться к ним."
          : "Добавьте сведения о компании — тогда вывод станет точнее.",
      ),
    );
  else visible.slice(0, 5).forEach((j) => left.append(historyCard(j)));
  left.append(
    add(
      node("div", "section-heading"),
      node("h2", "", "Событие из материалов кейса"),
    ),
    newsCard(),
  );
  add(
    aside,
    contextCard(),
    add(
      card(),
      node("h3", "", "Банковский взгляд — с датой"),
      p(
        "В исследовании доступны 9 материалов кейса. Прогноз своего периода останется прогнозом.",
      ),
      button("Открыть материалы", () => go("library"), "text-button"),
    ),
  );
  main.append(add(node("div", "grid"), left, aside));
}
function historyCard(j) {
  return add(
    card("history-card"),
    add(
      node("div", "between"),
      node("span", "caption", shortDate(j.created_at)),
      badge(statusText(j.status), j.status === "PARTIAL" ? "amber" : ""),
    ),
    node("h3", "", j.question),
    button("Открыть исследование", () => loadJob(j.run_id), "text-button"),
  );
}

function renderNews(container) {
  container.replaceChildren();
  const state = S.news;
  const roles = { ...ROLES, other: S.me?.job_title || "Руководитель" };
  const title = add(
    node("div", "section-heading"),
    node("h2", "", "Что важно сейчас"),
  );
  const refresh = button(
    S.newsLoading || ["QUEUED", "RUNNING"].includes(state?.status)
      ? "Ищем новости…"
      : "Обновить",
    () => refreshNews(),
    "button small",
    "refresh",
  );
  refresh.disabled =
    S.newsLoading || ["QUEUED", "RUNNING"].includes(state?.status);
  title.append(refresh);
  container.append(title);
  const controls = node("div", "news-controls");
  const role = node("select");
  role.id = "news-role";
  for (const [key, label] of Object.entries(roles)) {
    const option = node("option", "", label);
    option.value = key;
    role.append(option);
  }
  role.value = S.newsRole;
  role.addEventListener("change", () => {
    S.newsRole = role.value;
    S.newsExpanded = false;
    loadNews(false);
  });
  const period = node("select");
  period.id = "news-window";
  for (const [days, label] of [
    [7, "За 7 дней"],
    [30, "За 30 дней"],
    [90, "За 90 дней"],
  ]) {
    const option = node("option", "", label);
    option.value = days;
    period.append(option);
  }
  period.value = S.newsDays;
  period.addEventListener("change", () => {
    S.newsDays = Number(period.value);
    S.newsExpanded = false;
    loadNews(false);
  });
  add(
    controls,
    field("Новости для роли", role),
    field("Период публикации", period),
  );
  container.append(controls);
  container.append(
    p(
      "Приоритет учитывает компанию, профиль и задачи роли. Это повод проверить влияние, а не оценка финансового эффекта.",
      "fine-print",
    ),
  );
  if (!state) {
    container.append(
      box(
        "Собираем свежие публикации из открытых источников. В поиск уйдёт только публичное название компании.",
      ),
    );
    return;
  }
  const busy = ["QUEUED", "RUNNING"].includes(state.status);
  if (state.status === "FAILED")
    container.append(
      box(
        "Не удалось обновить все источники. " +
          (state.items.length
            ? "Ниже — последняя доступная подборка; проверьте время обновления."
            : "Повторите поиск позже или вставьте свою новость в исследование."),
        "amber",
      ),
    );
  else if (busy)
    container.append(
      p(
        "Обновляем источники и читаем подходящие публикации. Можно продолжать работу.",
        "fine-print",
      ),
    );
  else if (state.checked_at)
    container.append(
      p(
        "Подборка обновлена " +
          new Date(state.checked_at).toLocaleString("ru-RU", {
            timeZone: "Europe/Moscow",
            day: "numeric",
            month: "short",
            hour: "2-digit",
            minute: "2-digit",
          }) +
          " МСК" +
          (state.stale ? "; требуется обновление" : ""),
        "caption",
      ),
    );
  const items = state.items || [];
  if (!items.length && !busy)
    container.append(
      empty(
        "Подходящих свежих публикаций пока нет",
        "Старые материалы и публикации без подтверждённой даты не выдаются за свежие. Можно расширить период или добавить свой источник.",
      ),
    );
  for (const [index, item] of items
    .slice(0, S.newsExpanded ? 20 : 5)
    .entries()) {
    const c = card(
      "fresh-news-card" + (index === 0 ? " fresh-news-first" : ""),
    );
    c.dataset.newsId = item.news_id;
    add(
      c,
      add(
        node("div", "between"),
        badge(
          {
            check_now: "Проверить в первую очередь",
            monitor: "Наблюдать",
            background: "Контекст",
          }[item.priority],
          item.priority === "check_now" ? "amber" : "",
        ),
        node("span", "caption", date(item.published_at)),
      ),
      node("h3", "", item.title),
      p(
        item.publisher +
          " · " +
          (item.read_status === "article_read"
            ? "Текст прочитан"
            : "Только заголовок — требуется чтение") +
          (item.date_source === "search_feed"
            ? " · Дата из поисковой ленты"
            : ""),
        "caption",
      ),
      p(item.focus, "news-focus"),
      p(item.reasons.slice(0, 2).join(". "), "fine-print"),
    );
    const why = detail("Почему эта новость", list(item.reasons));
    why.append(
      p(
        "Приоритет " +
          item.score +
          "/100 — балл отбора, не вероятность. " +
          (item.read_status === "article_read"
            ? "Прочитанный текст ещё не означает независимое подтверждение."
            : "Заголовок не используется как подтверждённое событие."),
        "fine-print",
      ),
    );
    if (item.related_sources?.length)
      why.append(
        p(
          "Объединено публикаций по одному событию: " +
            (item.related_sources.length + 1),
          "fine-print",
        ),
      );
    c.append(why);
    const actions = node("div", "news-actions");
    const analyze = button(
      "Разобрать для " + roles[S.newsRole],
      async () => {
        try {
          await startResearch(
            "Как событие «" +
              item.title +
              "» может повлиять на " +
              companyName() +
              " и что стоит проверить " +
              roles[S.newsRole] +
              "?",
            {
              news_item_id: item.news_id,
              job_title: roles[S.newsRole],
              public_search: true,
            },
          );
        } catch (error) {
          report(error);
        }
      },
      index === 0 ? "button primary" : "button",
    );
    add(actions, analyze, link(item.url, "Первоисточник", "text-button"));
    c.append(actions);
    c.append(
      button(
        "Не по моей задаче",
        async () => {
          try {
            await api(
              "/api/companies/" + S.company.company_id + "/news/feedback",
              {
                method: "POST",
                body: JSON.stringify({
                  news_id: item.news_id,
                  rating: "irrelevant",
                }),
              },
            );
            toast("Отзыв сохранён. Он попадёт в проверку качества отбора.");
          } catch (error) {
            report(error);
          }
        },
        "text-button news-feedback",
      ),
    );
    container.append(c);
  }
  if (items.length > 5)
    container.append(
      button(
        S.newsExpanded
          ? "Свернуть подборку"
          : "Показать ещё " + (items.length - 5),
        () => {
          S.newsExpanded = !S.newsExpanded;
          renderNews(container);
        },
        "button",
      ),
    );
  const scope = detail("Источники и границы подбора");
  scope.append(p(state.scope, "fine-print"));
  if (state.sources?.length)
    scope.append(
      list(
        state.sources.map(
          (s) =>
            s.name +
            ": " +
            (s.status === "ok"
              ? "доступен, публикаций " + s.count
              : "недоступен"),
        ),
      ),
    );
  const excluded = state.excluded || {};
  scope.append(
    p(
      "Получено публикаций: " +
        state.candidate_count +
        ". Вне периода: " +
        (excluded.outside_window || 0) +
        ". Без даты: " +
        (excluded.unknown_date || 0) +
        ". Будущая дата: " +
        (excluded.future_date || 0) +
        ". Дубликаты объединены: " +
        (excluded.duplicate_story || 0) +
        ".",
      "fine-print",
    ),
  );
  scope.append(
    p(
      "Охват ограничен доступными каналами. Пропуск события возможен; одинаковое название компании не подтверждает её юридическую идентичность.",
      "fine-print",
    ),
  );
  container.append(scope);
}

function updateNewsView() {
  const region = $("#personal-news");
  if (region && S.view === "home") renderNews(region);
}
async function loadNews(auto = true) {
  const cid = S.company?.company_id;
  if (!cid) return;
  const role = S.newsRole,
    days = S.newsDays;
  clearTimeout(S.newsPoll);
  try {
    const result = await api(
      "/api/companies/" +
        encodeURIComponent(cid) +
        "/news?role=" +
        role +
        "&days=" +
        days,
    );
    if (
      cid !== S.company?.company_id ||
      role !== S.newsRole ||
      days !== S.newsDays
    )
      return;
    S.news = result;
    updateNewsView();
    if (
      auto &&
      result.refresh_due &&
      !["QUEUED", "RUNNING"].includes(result.status)
    )
      return refreshNews(false);
    if (["QUEUED", "RUNNING"].includes(result.status))
      S.newsPoll = setTimeout(() => loadNews(false), 3000);
    else if (S.view === "home")
      S.newsPoll = setTimeout(() => loadNews(true), 60000);
  } catch (error) {
    if (cid !== S.company?.company_id) return;
    S.news = {
      status: "FAILED",
      items: [],
      sources: [],
      excluded: {},
      candidate_count: 0,
    };
    updateNewsView();
  }
}
async function refreshNews(force = true) {
  const cid = S.company?.company_id;
  if (!cid || S.newsLoading) return;
  S.newsLoading = true;
  updateNewsView();
  try {
    await api("/api/companies/" + encodeURIComponent(cid) + "/news/refresh", {
      method: "POST",
      body: JSON.stringify({ role: S.newsRole, force }),
    });
    if (cid === S.company?.company_id) await loadNews(false);
  } catch (error) {
    report(error);
  } finally {
    S.newsLoading = false;
    updateNewsView();
  }
}
function newsCard() {
  const c = card("news-card");
  add(
    c,
    add(
      node("div", "between"),
      badge("Событие кейса"),
      node("span", "news-date", "11 сентября 2026"),
    ),
    node("h3", "", "Ключевая ставка: пауза, которая требует проверки сценария"),
    p(
      "В датированном наборе кейса ставка сохранена на уровне 14%. Для компании важны условия финансирования, для банка — переоценка активов и обязательств.",
    ),
    box("Это материал кейса, а не проверка сегодняшней ставки."),
  );
  c.append(
    button(
      "Что это значит для нас",
      () => {
        if (
          S.snapshot.metadata.source === "synthetic_demo" ||
          S.snapshot.metadata.source === "case_csv"
        )
          return startLegacy();
        S.draft.question = isBank()
          ? "Как сценарий сохранения ставки из материалов кейса влияет на планирование фондирования, ликвидности и процентного результата банка?"
          : "Что сценарий сохранения ставки из материалов кейса означает для нашей компании?";
        return startResearch(S.draft.question, {
          public_search: S.draft.public,
        });
      },
      "button small",
    ),
  );
  return c;
}

async function startResearch(question, extra = {}) {
  if (S.starting) return;
  S.starting = true;
  const company = S.company.company_id;
  try {
    const request = {
      company_id: company,
      question,
      job_title: S.me?.job_title || ROLES[S.role],
      public_search: S.draft.public,
      ...extra,
    };
    const job = await api("/api/research", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(request),
    });
    if (S.company.company_id !== company) return;
    S.job = job;
    S.role = roleFor(request.job_title);
    S.view = "research";
    S.draft = {
      question: "",
      news: "",
      urls: "",
      public: request.public_search,
      jobTitle: request.job_title,
    };
    await refreshHistory();
    render();
    window.scrollTo({ top: 0, behavior: "instant" });
    pollJob(job.run_id, company);
  } finally {
    S.starting = false;
  }
}
async function loadJob(id) {
  clearTimeout(S.poll);
  const company = S.company.company_id;
  const job = await api("/api/research/" + encodeURIComponent(id));
  if (S.company.company_id !== company) return;
  S.job = job;
  go("research");
  pollJob(id, company);
}
function pollJob(id, company) {
  clearTimeout(S.poll);
  if (!["QUEUED", "RUNNING", "DEFERRED_BUDGET"].includes(S.job?.status)) return;
  S.poll = setTimeout(async () => {
    try {
      const job = await api("/api/research/" + encodeURIComponent(id));
      if (S.company?.company_id !== company || S.job?.run_id !== id) return;
      S.job = job;
      if (S.view === "research") render();
      if (["QUEUED", "RUNNING", "DEFERRED_BUDGET"].includes(job.status))
        pollJob(id, company);
      else {
        await refreshHistory();
        if (S.view === "home") render();
      }
    } catch (error) {
      report(error);
    }
  }, 1800);
}
async function refreshHistory() {
  const cid = S.company.company_id;
  const data = await api("/api/research?company_id=" + encodeURIComponent(cid));
  if (S.company.company_id === cid) S.history = data;
}

function renderResearch(main) {
  const j = S.job;
  if (!j) {
    main.append(
      heading(
        "Исследование под вашу задачу",
        "Вопрос, контекст компании и проверяемые основания.",
      ),
      composer(),
    );
    return;
  }
  if (j.result) {
    renderResult(main, j);
    return;
  }
  main.append(
    heading(j.question, companyName() + " · " + j.job_title, "ИССЛЕДОВАНИЕ"),
  );
  const c = card("progress-card");
  if (["QUEUED", "RUNNING"].includes(j.status)) {
    add(
      c,
      add(
        node("div", "progress-head"),
        node("span", "spinner"),
        add(
          node("div"),
          node("h2", "", "Собираем основания для вывода"),
          p("Результат появится после проверки источников и интерпретации."),
        ),
      ),
    );
    const labels = [
      "Источники компании и банка",
      "Поиск и углубление по вопросу",
      "Вывод и ролевые срезы",
      "Проверка перед публикацией",
    ];
    const phase = { SOURCES: 1, PLAN: 1, WRITE: 2, VERIFY: 3 }[j.phase] ?? 0;
    const steps = node("ol", "steps");
    labels.forEach((t, i) =>
      steps.append(
        add(
          node("li", i < phase ? "done" : i === phase ? "current" : ""),
          node("span", "step-number", i < phase ? "✓" : i + 1),
          node("span", "", t),
        ),
      ),
    );
    add(
      c,
      steps,
      button(
        "Остановить",
        async () => {
          S.job = await api("/api/research/" + j.run_id + "/cancel", {
            method: "POST",
          });
          render();
        },
        "text-button",
      ),
    );
  } else if (j.status === "WAITING_INPUT") {
    add(
      c,
      badge("Нужно уточнение", "amber"),
      node("h2", "", "Не хватает важного контекста"),
      list(j.questions),
    );
    const form = node("form"),
      answer = node("textarea");
    answer.id = "research-answer";
    answer.required = true;
    answer.minLength = 2;
    answer.maxLength = 5000;
    answer.rows = 4;
    answer.placeholder = "Ответьте здесь. Если данных нет, так и напишите.";
    form.append(field("Ваш ответ", answer));
    const b = node("button", "button primary", "Продолжить исследование");
    b.type = "submit";
    form.append(b);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        S.job = await api("/api/research/" + j.run_id + "/answers", {
          method: "POST",
          body: JSON.stringify({
            answer: answer.value,
            expected_version: j.version,
          }),
        });
        render();
        pollJob(j.run_id, j.company_id);
      } catch (error) {
        report(error);
      }
    });
    c.append(form);
  } else {
    add(
      c,
      badge(statusText(j.status), "amber"),
      node(
        "h2",
        "",
        j.status === "CANCELLED"
          ? "Исследование остановлено"
          : "Проверенный вывод пока не готов",
      ),
      p(message(j.error_code || "interrupted_manual_retry")),
    );
    if (j.status === "DEFERRED_BUDGET")
      c.append(
        p(
          "Следующая попытка после " + shortDate(j.next_eligible_at),
          "fine-print",
        ),
      );
    else
      c.append(
        button(
          "Повторить с тем же вопросом",
          () => startResearch(j.question, { job_title: j.job_title }),
          "button primary",
        ),
      );
    c.append(
      button(
        "Изменить вопрос",
        () => {
          S.draft.question = j.question;
          S.job = null;
          render();
        },
        "button",
      ),
    );
  }
  c.append(traceDetails(j));
  main.append(c);
}

function roleTabs(views, onChange) {
  const tabs = node("div", "role-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Роль для просмотра вывода");
  for (const view of views) {
    const b = button(
      ROLES[view.role] || view.role,
      () => {
        S.role = view.role;
        onChange();
      },
      "role-tab" + (S.role === view.role ? " active" : ""),
    );
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(S.role === view.role));
    tabs.append(b);
  }
  return tabs;
}
function renderResult(main, j) {
  const r = j.result;
  const title = heading(
    r.title,
    j.company_name + " · " + j.job_title,
    "РЕЗУЛЬТАТ ИССЛЕДОВАНИЯ",
  );
  const actions = node("div", "result-actions");
  actions.append(
    button(
      j.favorite ? "В избранном" : "Сохранить",
      async () => {
        const value = !j.favorite;
        await api("/api/research/" + j.run_id + "/favorite", {
          method: "PUT",
          body: JSON.stringify({ saved: value }),
        });
        j.favorite = value;
        await refreshHistory();
        render();
      },
      "button small",
      "star",
    ),
    button("Скачать отчёт", () => exportReport(j), "button small", "download"),
    button(
      "Новый вопрос",
      () => {
        S.job = null;
        render();
      },
      "button small",
    ),
  );
  title.append(actions);
  main.append(title);
  const summary = card();
  add(
    summary,
    add(
      node("div", "row"),
      badge(
        j.status === "PARTIAL" ? "Есть ограничения" : "Готово",
        j.status === "PARTIAL" ? "amber" : "green",
      ),
      node("span", "caption", date(r.as_of)),
    ),
    p(r.summary, "result-summary"),
    node(
      "div",
      "result-state",
      "Цитаты и основания проверены автоматически. Истинность источников независимо не подтверждена.",
    ),
  );
  const bankViews = r.findings.filter((f) => f.kind === "bank_view");
  if (!bankViews.length)
    summary.append(
      add(
        node("div", "info-box bank-view"),
        badge("Банковский взгляд", "red"),
        p(
          "По этому вопросу позиция банка пока не подтверждена доступными основаниями. В выводе показаны условные сценарии.",
        ),
      ),
    );
  for (const f of bankViews) {
    const bank = node("div", "info-box bank-view");
    add(bank, badge("Банковский взгляд", "red"), p(f.text));
    const evidence = detail("Основания банковской позиции");
    f.citations.forEach((q) =>
      add(
        evidence,
        node("blockquote", "quote", q.quote),
        sourceCaption(j.sources.find((s) => s.source_id === q.source_id)),
      ),
    );
    bank.append(evidence);
    summary.append(bank);
  }
  summary.append(
    button(
      "Какие данные нужны",
      () =>
        $("#result-gaps").scrollIntoView({
          behavior: "smooth",
          block: "start",
        }),
      "text-button",
    ),
  );
  main.append(summary);
  const roleCard = card();
  const views = r.role_views;
  const active = views.find((v) => v.role === S.role) || views[0];
  if (active) S.role = active.role;
  add(
    roleCard,
    node("span", "eyebrow", "ДЛЯ ВАШЕЙ РОЛИ"),
    roleTabs(views, render),
    node("h2", "", active?.focus || "Следующий шаг"),
  );
  if (active)
    add(
      roleCard,
      p(active.interpretation),
      list(active.actions, "action-list", true),
    );
  main.append(
    add(
      node("div", "section-heading"),
      node("h2", "", "Что проверить в первую очередь"),
    ),
    roleCard,
  );
  main.append(
    add(node("div", "section-heading"), node("h2", "", "На чём основан вывод")),
  );
  const sorted = r.findings
    .filter((f) => f.kind !== "bank_view")
    .sort(
      (a, b) =>
        ["check_now", "monitor", "background"].indexOf(a.priority) -
        ["check_now", "monitor", "background"].indexOf(b.priority),
    );
  sorted.forEach((f, i) => {
    const c = card("finding");
    add(
      c,
      add(
        node("div", "between"),
        add(
          node("div", "row"),
          node("span", "finding-number", String(i + 1).padStart(2, "0")),
          badge(
            {
              source_fact: "Содержание источника",
              bank_view: "Позиция банка",
              analysis: "Вывод агента",
              hypothesis: "Гипотеза",
            }[f.kind],
            f.kind === "bank_view" ? "red" : "",
          ),
        ),
        badge(
          {
            check_now: "Проверить сейчас",
            monitor: "Наблюдать",
            background: "Контекст",
          }[f.priority],
          f.priority === "check_now" ? "amber" : "",
        ),
      ),
      p(f.text, "finding-main"),
      node("div", "finding-label", "Почему касается компании"),
      p(f.why_company),
      node("div", "finding-label", "Возможное последствие"),
      p(f.consequence),
    );
    if (f.condition) c.append(p("Условие: " + f.condition, "condition"));
    const d = detail("Источники и цитаты · " + f.citations.length);
    f.citations.forEach((q) => {
      const s = j.sources.find((x) => x.source_id === q.source_id);
      add(d, node("blockquote", "quote", q.quote), sourceCaption(s));
    });
    c.append(d);
    main.append(c);
  });
  const gaps = card();
  gaps.id = "result-gaps";
  gaps.append(node("h2", "", "Границы вывода"));
  if (r.missing_data.length)
    add(
      gaps,
      p("Для точной оценки по компании нужны:", "fine-print"),
      list(r.missing_data),
    );
  gaps.append(
    detail("Ограничения и дата банковского взгляда", list(r.limitations)),
  );
  main.append(gaps);
  const follow = card();
  add(
    follow,
    node("h2", "", "Углубить исследование"),
    p("Уточнение сохранит источники и контекст этого разбора.", "fine-print"),
  );
  const form = node("form"),
    input = node("textarea", "question-input");
  input.id = "follow-up-question";
  input.placeholder = "Что проверить глубже?";
  input.required = true;
  input.minLength = 12;
  input.maxLength = 1500;
  input.rows = 2;
  input.setAttribute("aria-label", "Уточняющий вопрос");
  form.append(input);
  const suggestions = node("div", "chips");
  r.follow_up_questions.forEach((q) =>
    suggestions.append(
      button(
        q,
        () => {
          input.value = q;
          input.focus();
        },
        "chip",
      ),
    ),
  );
  form.append(suggestions);
  const send = node("button", "button primary", "Уточнить");
  send.type = "submit";
  form.append(send);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await startResearch(input.value, {
        parent_id: j.run_id,
        job_title: j.job_title,
      });
    } catch (error) {
      report(error);
    }
  });
  follow.append(form);
  main.append(follow);
  const bottom = card();
  const feedback = add(
    node("div", "feedback"),
    node("span", "", "Был ли полезен вывод?"),
  );
  for (const [rating, label] of [
    ["useful", "Полезен"],
    ["irrelevant", "Не по задаче"],
    ["error", "Есть ошибка"],
  ])
    feedback.append(
      button(
        label,
        async () => {
          let comment = "";
          if (rating === "error") {
            const area = node("textarea");
            area.id = "feedback-comment";
            area.maxLength = 500;
            area.placeholder = "Что нужно исправить?";
            const content = add(
              node("div"),
              field("Комментарий", area),
              button(
                "Отправить отзыв",
                async () => {
                  await api("/api/research/" + j.run_id + "/feedback", {
                    method: "POST",
                    body: JSON.stringify({ rating, comment: area.value }),
                  });
                  closeModal();
                  toast("Отзыв сохранён.");
                },
                "button primary",
              ),
            );
            modal("Сообщить об ошибке", content);
            return;
          }
          await api("/api/research/" + j.run_id + "/feedback", {
            method: "POST",
            body: JSON.stringify({ rating, comment }),
          });
          toast("Отзыв сохранён.");
        },
        "button small",
      ),
    );
  add(
    bottom,
    feedback,
    traceDetails(j),
    detail(
      "Все источники исследования",
      ...j.sources
        .filter((s) => s.kind !== "url_to_read")
        .map((s) =>
          add(
            node("div"),
            sourceCaption(s),
            s.kind === "search_lead"
              ? p(
                  "Поисковая наводка: в фактические основания не включается.",
                  "fine-print",
                )
              : null,
          ),
        ),
    ),
  );
  main.append(bottom);
}
function sourceCaption(s) {
  if (!s) return p("Источник недоступен", "source-caption");
  const d = node("div", "source-caption");
  add(
    d,
    node("strong", "", s.title || s.source_id),
    node(
      "div",
      "",
      (s.publisher || "Источник") +
        " · " +
        (s.published_at
          ? date(s.published_at)
          : s.publication_date_raw || "Дата не установлена") +
        (s.page ? " · с. " + s.page : ""),
    ),
    link(s.url, "Открыть первоисточник"),
  );
  if (s.kind === "bank_excerpt")
    d.append(
      node(
        "small",
        "",
        "OCR материала кейса; полная ручная разметка не завершена.",
      ),
    );
  if (
    s.kind === "company_evidence" &&
    s.document_id &&
    S.docs.some((x) => x.document_id === s.document_id)
  )
    d.append(
      button(
        "Открыть файл компании",
        () => downloadFile(s.document_id),
        "text-button",
      ),
    );
  return d;
}
function traceDetails(j) {
  const d = detail("Ход исследования");
  d.append(
    p(
      `${j.llm_calls || 0} обращений к модели · ${j.tool_calls || 0} вызовов инструментов · ${j.replans || 0} изменений плана после проверки`,
      "fine-print",
    ),
  );
  for (const item of j.trace || []) {
    const title = {
      queued: "Запрос принят",
      plan: "Выбран следующий шаг",
      tool_completed: "Получены источники",
      verification: "Проверен вывод",
      replan: "План изменён после проверки",
      published: "Вывод опубликован",
      question_created: "Запрошено уточнение",
      stopped: "Работа остановлена",
    }[item.event];
    if (!title) continue;
    let txt = title;
    if (item.event === "plan") txt += ": " + (item.data.rationale || "");
    if (item.event === "tool_completed")
      txt +=
        ": " +
        ({
          company: "компания",
          bank: "банк",
          public: "открытый поиск",
          read_source: "чтение страницы",
        }[item.data.tool] || item.data.tool) +
        ", " +
        item.data.sources_added;
    if (item.data?.error) txt += " (источник недоступен)";
    if (item.event === "verification")
      txt += item.data.approved
        ? " — проверка пройдена"
        : " — требуется исправление";
    d.append(node("div", "trace-row", txt));
  }
  return d;
}

function exportReport(j) {
  const r = j.result;
  const lines = [
    "# " + r.title,
    "",
    j.company_name + " · " + j.job_title + " · " + date(r.as_of),
    "",
    "Статус: " + statusText(j.status),
    "",
    r.summary,
    "",
  ];
  for (const f of r.findings) {
    lines.push("## " + f.text, "", f.why_company, "", f.consequence, "");
    if (f.condition) lines.push("Условие: " + f.condition, "");
    for (const c of f.citations) {
      const s = j.sources.find((x) => x.source_id === c.source_id);
      lines.push(
        "> " + c.quote.replaceAll("\n", "\n> "),
        "",
        `${s?.title || c.source_id} · ${s?.publisher || ""} · ${date(s?.published_at)}${s?.page ? " · с. " + s.page : ""}`,
        s?.url || "",
        "",
      );
    }
  }
  for (const v of r.role_views)
    lines.push(
      "## " + ROLES[v.role] + " — " + v.focus,
      "",
      v.interpretation,
      "",
      ...v.actions.map((x) => "- " + x),
      "",
    );
  lines.push(
    "## Недостающие данные",
    "",
    ...r.missing_data.map((x) => "- " + x),
    "",
    "## Ограничения",
    "",
    ...r.limitations.map((x) => "- " + x),
  );
  saveBlob(
    new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" }),
    "Исследование — " + j.company_name.replace(/[\\/:*?"<>|]/g, "") + ".md",
  );
}
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob),
    a = node("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function downloadFile(id) {
  const d = S.docs.find((x) => x.document_id === id);
  const r = await fetch(
    "/api/companies/" +
      encodeURIComponent(S.company.company_id) +
      "/documents/" +
      encodeURIComponent(id) +
      "/download",
    { headers: { "x-app-token": TOKEN } },
  );
  if (!r.ok) throw new Error("access_denied");
  saveBlob(await r.blob(), d?.name || "document");
}

function renderCompany(main) {
  main.append(
    heading(
      companyName(),
      "Источники и сведения, которые агент использует в исследованиях.",
      origin().toUpperCase(),
    ),
  );
  if (!S.snapshot.writable)
    main.append(
      box(
        "Это доступный команде пример. Чтобы загрузить свои материалы, добавьте отдельную компанию.",
      ),
    );
  const left = node("div", "stack"),
    right = node("aside", "stack");
  if (S.snapshot.writable) {
    const c = card();
    add(
      c,
      node("h2", "", "О компании"),
      p(
        "Кратко опишите бизнес, рынки и задачу. Агент будет считать это сведениями от вас.",
        "fine-print",
      ),
    );
    const form = node("form"),
      description = node("textarea");
    description.id = "company-description";
    description.rows = 5;
    description.maxLength = 5000;
    description.placeholder = isBank()
      ? "Например: универсальный банк. Интересуют стоимость фондирования, ликвидность и процентный результат."
      : "Чем занимается компания, где работает, какие продукты и приоритеты";
    description.value = S.snapshot.context?.description || "";
    const website = node("input");
    website.id = "company-website";
    website.type = "url";
    website.placeholder = "https://company.ru";
    website.maxLength = 300;
    website.value = S.snapshot.context?.website || "";
    add(
      form,
      field("Описание бизнеса", description),
      field("Официальный сайт — необязательно", website),
    );
    const save = node("button", "button primary", "Сохранить сведения");
    save.type = "submit";
    form.append(save);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        const cid = S.company.company_id;
        await api("/api/companies/" + cid + "/context", {
          method: "PUT",
          body: JSON.stringify({
            description: description.value,
            website: website.value,
          }),
        });
        const snap = await api("/api/companies/" + cid + "/profile");
        if (S.company.company_id === cid) S.snapshot = snap;
        toast("Сведения сохранены. Новые исследования учтут изменения.");
      } catch (error) {
        report(error);
      } finally {
        save.disabled = false;
      }
    });
    c.append(form);
    left.append(c);
  }
  const docs = card();
  add(
    docs,
    node("h2", "", "Документы компании"),
    p(
      "Презентации, отчёты и условия финансирования. Каждый файл привязан к выбранной компании.",
      "fine-print",
    ),
  );
  if (S.snapshot.writable) {
    const area = node("div", "upload-area");
    const input = node("input");
    input.type = "file";
    input.id = "company-files";
    input.multiple = true;
    input.accept = ".pdf,.docx,.pptx,.xlsx,.csv,.txt,.md";
    input.hidden = true;
    input.setAttribute("aria-label", "Документы компании");
    input.addEventListener("change", () => uploadFiles(input.files));
    add(
      area,
      input,
      button("Выбрать файлы", () => input.click(), "button", "upload"),
      p("PDF, DOCX, PPTX, XLSX, CSV, TXT · до 15 МБ на файл"),
    );
    area.addEventListener("dragover", (e) => {
      e.preventDefault();
      area.classList.add("dragover");
    });
    area.addEventListener("dragleave", () => area.classList.remove("dragover"));
    area.addEventListener("drop", (e) => {
      e.preventDefault();
      area.classList.remove("dragover");
      uploadFiles(e.dataTransfer.files).catch(report);
    });
    docs.append(area);
  }
  const files = node("div");
  files.id = "file-list";
  docs.append(files);
  left.append(docs);
  renderFileList(files);
  const profile = card();
  add(profile, node("h2", "", "Что известно из источников"));
  const features = node("div", "feature-grid");
  features.id = "company-features";
  profile.append(features);
  renderFeatures(features);
  left.append(profile);
  if (S.connections?.available) {
    const c = card();
    add(
      c,
      node("h2", "", "Платёжный срез кейса"),
      box(
        "Это наблюдаемые платежи в выборке, не выручка и не полный оборот компании. Связанные лица остаются обезличенными.",
      ),
      detail(
        "Показать детали среза",
        ...["incoming", "outgoing"].map((side) => {
          const data = S.connections[side],
            block = node("div");
          add(
            block,
            node(
              "h3",
              "",
              side === "incoming" ? "Входящие платежи" : "Исходящие платежи",
            ),
            p(
              "Контрагентов: " +
                data.counterparty_count +
                ". Платежей в срезе: " +
                data.payment_count +
                ".",
              "fine-print",
            ),
          );
          for (const item of data.top)
            block.append(
              add(
                node("div", "context-line"),
                node(
                  "span",
                  "",
                  item.company_id +
                    (item.industry
                      ? " · " + item.industry
                      : " · профиль недоступен"),
                ),
                node("strong", "", (item.share_percent ?? "—") + "% суммы"),
              ),
            );
          return block;
        }),
        p(
          "Единицы суммы: " +
            S.connections.amount_unit +
            ". Период в исходнике не установлен.",
          "fine-print",
        ),
      ),
    );
    left.append(c);
  }
  add(
    right,
    contextCard(),
    add(
      card(),
      node("h3", "", "Как подготовить материалы"),
      list([
        "Добавьте актуальное описание бизнеса.",
        "Укажите период отчёта и единицы измерения.",
        "При расхождении значений агент сохранит противоречие.",
        "Изменения профиля применяются к новым исследованиям.",
      ]),
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
  main.append(add(node("div", "grid"), left, right));
}
function renderFileList(container) {
  container.replaceChildren();
  if (!S.docs.length) {
    container.append(
      p("Файлов пока нет. Можно начать с описания компании.", "fine-print"),
    );
    return;
  }
  S.docs.forEach((d) => {
    const row = node("div", "file-row"),
      info = node("div", "file-info");
    add(
      info,
      node("strong", "", d.name),
      add(
        node("div", "row"),
        badge(
          statusText(d.status),
          d.status === "READY" ? "green" : d.status === "FAILED" ? "amber" : "",
        ),
        node(
          "small",
          "",
          d.feature_count ? d.feature_count + " извлечённых признаков" : "",
        ),
      ),
    );
    const controls = node("div", "file-controls");
    controls.append(
      button("Скачать", () => downloadFile(d.document_id), "text-button"),
    );
    if (S.snapshot.writable) {
      if (d.status === "READY")
        controls.append(
          button(
            "Исключить из анализа",
            () => fileAction(d.document_id, "exclude"),
            "text-button",
          ),
        );
      if (d.status === "EXCLUDED")
        controls.append(
          button(
            "Вернуть в анализ",
            () => fileAction(d.document_id, "restore"),
            "text-button",
          ),
        );
      if (d.status === "FAILED")
        controls.append(
          button(
            "Повторить обработку",
            () => fileAction(d.document_id, "retry"),
            "text-button",
          ),
        );
    }
    info.append(controls);
    if (d.error_code) info.append(p(message(d.error_code), "fine-print"));
    if (d.issues?.length)
      info.append(detail("Замечания к чтению файла", list(d.issues)));
    add(
      row,
      node(
        "div",
        "file-icon",
        (d.name.split(".").pop() || "FILE").toUpperCase(),
      ),
      info,
    );
    container.append(row);
  });
}
function renderFeatures(container) {
  container.replaceChildren();
  const features = S.snapshot.profile.features
    .filter((f) => f.status !== "missing" && f.name !== "company_name")
    .slice(0, 28);
  if (!features.length) {
    container.append(
      p(
        "Пока известно только название. Добавьте сведения или документы.",
        "fine-print",
      ),
    );
    return;
  }
  for (const f of features) {
    const c = node("div", "feature");
    add(
      c,
      node("span", "caption", LABELS[f.name] || f.name),
      node("div", "feature-value", featureValue(f)),
      node(
        "small",
        "",
        {
          observed: "Наблюдается в источнике",
          self_reported: "Сообщено компанией",
          derived: "Вычислено по данным",
          conflicting: "Есть противоречие",
        }[f.status] || f.status,
      ),
    );
    const refs = f.source_refs || [];
    if (refs.length)
      c.append(button("Основание", () => showFeature(f), "text-button"));
    container.append(c);
  }
}
function showFeature(f) {
  const content = node("div");
  content.append(p(featureValue(f)));
  const refs = new Set((f.source_refs || []).map((x) => x.ref_id));
  for (const e of S.snapshot.evidence || [])
    if (refs.has(e.evidence_id)) {
      const d = S.snapshot.documents.find(
        (x) => x.document_id === e.document_id,
      );
      add(
        content,
        node("blockquote", "quote", e.quote),
        p(
          (d?.title || e.document_id) + (e.page ? " · с. " + e.page : ""),
          "source-caption",
        ),
      );
    }
  if (!content.querySelector("blockquote"))
    content.append(
      p(
        "Признак вычислен по строкам источников. Подробная связь доступна в выгрузке исследования.",
        "fine-print",
      ),
    );
  modal(LABELS[f.name] || f.name, content);
}
async function uploadFiles(files) {
  if (S.uploading || !files?.length) return;
  const cid = S.company.company_id;
  S.uploading = true;
  try {
    for (const f of Array.from(files)) {
      if (f.size > 15 * 1024 * 1024) throw new Error("file_size_limit");
      const form = new FormData();
      form.append("bound_company_id", cid);
      form.append("file", f);
      await api("/api/companies/" + cid + "/documents", {
        method: "POST",
        body: form,
      });
    }
    toast("Файлы добавлены. Агент собирает профиль с цитатами.");
    if (S.company.company_id === cid) {
      S.docs = await api("/api/companies/" + cid + "/documents");
      if (S.view === "company") renderFileList($("#file-list"));
      pollDocuments(cid);
    }
  } catch (error) {
    report(error);
  } finally {
    S.uploading = false;
  }
}
async function fileAction(id, action) {
  const cid = S.company.company_id;
  await api("/api/companies/" + cid + "/documents/" + id + "/" + action, {
    method: "POST",
  });
  await updateDocuments(cid);
  if (action === "retry") pollDocuments(cid);
  toast(
    {
      exclude: "Файл исключён из новых исследований.",
      restore: "Файл возвращён в анализ.",
      retry: "Обработка запущена повторно.",
    }[action],
  );
}
async function updateDocuments(cid) {
  const [docs, snapshot] = await Promise.all([
    api("/api/companies/" + cid + "/documents"),
    api("/api/companies/" + cid + "/profile"),
  ]);
  if (S.company.company_id !== cid) return;
  S.docs = docs;
  S.snapshot = snapshot;
  if (S.view === "company") {
    renderFileList($("#file-list"));
    renderFeatures($("#company-features"));
  } else if (["home", "research"].includes(S.view)) {
    const form = $("#research-question")?.closest("form");
    if (form) {
      form.querySelector('button[type="submit"]').disabled =
        S.starting || busyDocs();
      for (const note of form.querySelectorAll(".fine-print"))
        if (note.textContent.startsWith("Сначала дождитесь"))
          note.hidden = !busyDocs();
    }
  }
}
function pollDocuments(cid) {
  clearTimeout(S.docPoll);
  if (!busyDocs()) return;
  S.docPoll = setTimeout(async () => {
    try {
      await updateDocuments(cid);
      if (S.company.company_id !== cid) return;
      if (busyDocs()) pollDocuments(cid);
      else toast("Профиль обновлён по документам. Можно начать исследование.");
    } catch (error) {
      report(error);
    }
  }, 2000);
}

function renderLibrary(main) {
  main.append(
    heading(
      "Материалы банка",
      "Исходные материалы кейса с датами и ссылками на оригиналы.",
    ),
  );
  main.append(
    box(
      "В поиске доступны 9 PDF и 64 страницы OCR. Это датированный набор кейса. Полная ручная разметка тезисов ещё не завершена.",
    ),
  );
  const grid = node("div", "bank-grid");
  for (const d of S.library.bank_documents || []) {
    const c = card("bank-card");
    add(
      c,
      badge(
        d.published_at?.startsWith("2021")
          ? "Исторический материал"
          : "Материал кейса",
      ),
      node("h3", "", d.title),
      p(d.published_at ? date(d.published_at) : d.publication_date_raw),
      p(d.page_count + " стр. · " + d.publisher),
      add(
        node("div", "row"),
        link(d.url, "Открыть PDF", "button small"),
        button(
          "Исследовать тему",
          () => {
            S.draft.question =
              "Как выводы материала «" +
              d.title +
              "» могут относиться к нашей компании с учётом даты публикации?";
            S.job = null;
            go("research");
          },
          "text-button",
        ),
      ),
    );
    grid.append(c);
  }
  main.append(
    add(node("div", "section-heading"), node("h2", "", "Библиотека кейса")),
    grid,
  );
  const c = card();
  add(
    c,
    node("h2", "", "Проверить готовый сценарий"),
    p(
      "Расчёт по долгу доступен на обезличенных данных кейса и явно синтетических примерах. Свою компанию добавляйте отдельно.",
      "fine-print",
    ),
    button("Выбрать компанию или пример", switchCompany, "button"),
  );
  if (S.legacyHistory.length)
    c.append(
      detail(
        "Сохранённые расчётные разборы",
        ...S.legacyHistory.map((j) =>
          button(
            statusText(j.status) + " · " + shortDate(j.created_at),
            () => loadLegacy(j.id),
            "list-button",
          ),
        ),
      ),
    );
  main.append(
    add(node("div", "section-heading"), node("h2", "", "Демонстрация расчёта")),
    c,
  );
}

async function startLegacy() {
  const cid = S.company.company_id;
  const run = await api("/api/runs", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      company_id: cid,
      scenario_delta_pp: "-1",
      horizon_days: 90,
    }),
  });
  S.legacy = run;
  S.view = "legacy";
  render();
  pollLegacy(run.run_id, cid);
}
async function loadLegacy(id) {
  S.legacy = await api("/api/runs/" + id);
  S.legacySources = await api("/api/runs/" + id + "/sources");
  go("legacy");
  pollLegacy(id, S.company.company_id);
}
function pollLegacy(id, cid) {
  clearTimeout(S.poll);
  if (!["QUEUED", "RUNNING", "DEFERRED_BUDGET"].includes(S.legacy?.status))
    return;
  S.poll = setTimeout(async () => {
    try {
      const r = await api("/api/runs/" + id);
      if (S.company.company_id !== cid) return;
      S.legacy = r;
      if (r.result) S.legacySources = await api("/api/runs/" + id + "/sources");
      if (S.view === "legacy") render();
      pollLegacy(id, cid);
    } catch (error) {
      report(error);
    }
  }, 1800);
}
function renderLegacy(main) {
  const j = S.legacy;
  main.append(
    heading(
      "Расчётный сценарий ставки",
      "Событие кейса → источники компании → условный расчёт → ролевой вывод.",
    ),
  );
  if (!j) return main.append(newsCard());
  if (!j.result) {
    const c = card();
    if (j.status === "WAITING_INPUT") {
      add(c, node("h2", "", j.question.question), p(j.question.why_needed));
      const form = node("form"),
        input = node("input");
      input.type = "number";
      input.id = "debt-amount";
      input.min = "0";
      input.step = ".01";
      input.max = "1000000000000000";
      input.required = true;
      form.append(field("Остаток плавающего долга, ₽", input));
      const send = node("button", "button primary", "Продолжить");
      send.type = "submit";
      form.append(send);
      const answer = async (skip) => {
        S.legacy = await api("/api/runs/" + j.run_id + "/answers", {
          method: "POST",
          body: JSON.stringify({
            answer_id: crypto.randomUUID(),
            question_id: j.question_id,
            expected_version: j.version,
            value_rub: skip ? null : input.value,
            skip,
          }),
        });
        render();
        pollLegacy(j.run_id, S.company.company_id);
      };
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        answer(false).catch(report);
      });
      form.append(
        button("Данных нет — качественный вывод", () => answer(true), "button"),
      );
      c.append(form);
    } else
      add(
        c,
        badge(statusText(j.status), "amber"),
        p(
          j.error_code
            ? message(j.error_code)
            : "Агент сопоставляет контекст и проверяет расчёт.",
        ),
        ["QUEUED", "RUNNING"].includes(j.status)
          ? node("span", "spinner")
          : null,
      );
    main.append(c);
    return;
  }
  const core = j.result.core;
  main.append(
    box(
      "Это условный расчёт на датированных материалах. Значения сценария не являются прогнозом ставки или оценкой фактической экономии.",
    ),
  );
  const c = card();
  add(
    c,
    node("h2", "", core.headline),
    badge(statusText(j.status), j.status === "PARTIAL" ? "amber" : "green"),
  );
  const sources = S.legacySources;
  for (const s of core.statements.filter(
    (x) => x.owner.kind !== "model" || x.derivation === "calculated",
  )) {
    const block = node("div", "finding");
    add(
      block,
      badge(
        {
          FACT: "Содержание источника",
          FORECAST: "Прогноз на дату публикации",
          ASSESSMENT: "Оценка банка",
          SCENARIO: "Условный сценарий",
        }[s.claim_type] || s.claim_type,
      ),
      p(s.text, "finding-main"),
    );
    if (s.conditions.length) block.append(list(s.conditions));
    block.append(
      detail(
        "Основания",
        ...s.citations.map((ref) => {
          const ev = sources?.evidence?.find(
            (x) => x.evidence_id === ref.ref_id,
          );
          const calc = sources?.calculations?.find(
            (x) => x.calculation_id === ref.ref_id,
          );
          return ev
            ? node("blockquote", "quote", ev.quote)
            : calc
              ? node("pre", "mono", JSON.stringify(calc, null, 2))
              : p("Ссылка: " + ref.ref_id, "fine-print");
        }),
      ),
    );
    c.append(block);
  }
  main.append(c);
  const roles = Object.keys(ROLES).map((role) => ({ role })),
    roleCard = card();
  roleCard.append(roleTabs(roles, render));
  const statement = core.statements.find(
    (x) => x.statement_id === "ai_" + S.role,
  );
  if (statement) {
    add(roleCard, node("h2", "", ROLE_NAMES[S.role]), p(statement.text));
    if (statement.conditions.length)
      add(
        roleCard,
        p("Предпосылки интерпретации требуют подтверждения:", "fine-print"),
        list(statement.conditions),
      );
  }
  const actions = core.actions.filter((a) => a.target_roles.includes(S.role));
  roleCard.append(
    list(
      actions.map((a) => a.title),
      "action-list",
      true,
    ),
  );
  main.append(roleCard);
  if (core.limitations?.length)
    main.append(
      add(card(), node("h2", "", "Ограничения"), list(core.limitations)),
    );
  main.append(
    add(
      card(),
      traceDetails(j),
      button(
        "Свой вопрос по компании",
        () => {
          S.job = null;
          go("research");
        },
        "button primary",
      ),
    ),
  );
}

async function selectCompany(id) {
  const epoch = ++S.epoch;
  clearTimeout(S.poll);
  clearTimeout(S.docPoll);
  clearTimeout(S.newsPoll);
  S.news = null;
  S.newsExpanded = false;
  S.job = null;
  S.legacy = null;
  S.legacySources = null;
  S.draft = { question: "", news: "", urls: "", public: true };
  const companies = S.companies;
  const company = companies.find((x) => x.company_id === id);
  if (!company) throw new Error("access_denied");
  S.company = company;
  S.draft.question = defaultQuestion();
  const encoded = encodeURIComponent(id);
  const result = await Promise.all([
    api("/api/companies/" + encoded + "/profile"),
    api("/api/companies/" + encoded + "/documents"),
    api("/api/research?company_id=" + encoded),
    api("/api/runs?company_id=" + encoded),
    api("/api/companies/" + encoded + "/connections"),
  ]);
  if (epoch !== S.epoch) return;
  [S.snapshot, S.docs, S.history, S.legacyHistory, S.connections] = result;
  S.view = "home";
  render();
  pollDocuments(id);
  loadNews(true);
}
function switchCompany() {
  const content = node("div"),
    input = node("input", "search-input");
  input.id = "company-search";
  input.type = "search";
  input.placeholder = "Найти свою компанию или ID из кейса";
  input.setAttribute("aria-label", "Поиск компании");
  const results = node("div");
  content.append(input, results);
  let timer;
  const show = (items) => {
    results.replaceChildren();
    for (const c of items) {
      const text = add(
        node("div"),
        node("strong", "", c.label),
        node(
          "small",
          "",
          {
            case_csv: "Обезличенные данные кейса",
            synthetic_demo: "Синтетический пример",
            company_upload: c.writable
              ? "Ваше рабочее пространство"
              : "Синтетический пример с документами",
          }[c.source] || c.source,
        ),
      );
      const b = button(
        "",
        async () => {
          if (!S.companies.some((x) => x.company_id === c.company_id))
            S.companies.push(c);
          closeModal();
          await selectCompany(c.company_id);
        },
        "list-button",
      );
      b.append(text);
      if (c.company_id === S.company.company_id) b.append(badge("Выбрана"));
      results.append(b);
    }
    if (!items.length)
      results.append(
        p("Совпадений нет. Добавьте свою компанию.", "fine-print"),
      );
  };
  show(S.companies);
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        show(await api("/api/companies?q=" + encodeURIComponent(input.value)));
      } catch (error) {
        report(error);
      }
    }, 250);
  });
  content.append(node("hr", "muted-rule"));
  const addForm = node("form"),
    name = node("input");
  name.id = "new-company-name";
  name.minLength = 2;
  name.maxLength = 160;
  name.required = true;
  name.placeholder = "Название новой компании";
  addForm.append(field("Добавить свою компанию", name));
  const send = node("button", "button primary", "Добавить компанию");
  send.type = "submit";
  addForm.append(send);
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    send.disabled = true;
    try {
      const c = await api("/api/companies", {
        method: "POST",
        body: JSON.stringify({ name: name.value }),
      });
      S.companies = await api("/api/companies");
      closeModal();
      await selectCompany(c.company_id);
      go("company");
    } catch (error) {
      report(error);
      send.disabled = false;
    }
  });
  add(
    content,
    addForm,
    p(
      "Совпадение названия не объединяет документы разных сотрудников.",
      "fine-print",
    ),
  );
  modal("Рабочая компания", content);
}
function quickKey() {
  return (
    "alfa-quick:" + (S.me?.user_id || "local") + ":" + S.company.company_id
  );
}
function quickSettings(questions) {
  const content = node("div");
  content.append(
    p(
      "До трёх ваших вопросов для быстрого доступа. Сохраняются в этом браузере для выбранной компании.",
      "fine-print",
    ),
  );
  const form = node("form"),
    inputs = [];
  for (let i = 0; i < 3; i++) {
    const input = node("textarea");
    input.rows = 2;
    input.maxLength = 300;
    input.minLength = 12;
    input.value = questions[i] || "";
    inputs.push(input);
    form.append(field("Вопрос " + (i + 1), input));
  }
  const submit = node("button", "button primary", "Сохранить вопросы");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const values = inputs.map((x) => x.value.trim()).filter(Boolean);
    if (!values.length) {
      toast("Добавьте хотя бы один вопрос.");
      return;
    }
    localStorage.setItem(quickKey(), JSON.stringify(values));
    closeModal();
    render();
    toast("Быстрые вопросы сохранены.");
  });
  content.append(form);
  modal("Ваши быстрые вопросы", content);
}
function showAccount() {
  const content = node("div");
  for (const [label, value] of [
    ["Имя и фамилия", S.me.full_name],
    ["Компания при входе", S.me.company_name],
    ["Должность", S.me.job_title],
  ])
    add(content, p(label, "caption"), p(value));
  content.append(
    box(
      "Роль определяет содержание вывода. Она не даёт полномочий на документы других пользователей. В этом демо вход действует 12 часов.",
    ),
  );
  const form = node("form");
  form.method = "post";
  form.action = "/auth/logout";
  const b = node("button", "button small", "Выйти");
  b.type = "submit";
  form.append(b);
  content.append(
    detail(
      "Выйти из рабочего пространства",
      p(
        "Новый вход создаст новое пространство. Сначала скачайте важные результаты.",
        "fine-print",
      ),
      form,
    ),
  );
  modal("Профиль сотрудника", content);
}
function help() {
  modal(
    "Как работает Пульс бизнеса",
    add(
      node("div"),
      list(
        [
          "Укажите сведения о компании и добавьте документы.",
          "Задайте свой вопрос или выберите событие кейса.",
          "Агент соберёт основания, проверит вывод и покажет ограничения.",
          "Уточните вопрос, переключите должность или откройте первоисточник.",
        ],
        "action-list",
        true,
      ),
      box(
        "Для компании из CSV доступны только обезличенные данные. Подключение к банковскому контуру в демо отсутствует.",
      ),
      p(
        "Открытый поиск передаёт только указанное вами название компании и общую тему. Внутренние документы передаются разрешённой модели организаторов для анализа.",
        "fine-print",
      ),
      S.workspace?.hosting?.notice
        ? p(S.workspace.hosting.notice, "fine-print")
        : null,
    ),
  );
}

document
  .querySelectorAll("[data-icon]")
  .forEach((e) => e.append(icon(e.dataset.icon)));
document
  .querySelectorAll("[data-view]")
  .forEach((b) => b.addEventListener("click", () => go(b.dataset.view)));
$("#mobile-menu").addEventListener("click", () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#mobile-menu").setAttribute("aria-expanded", String(open));
});
$("#company-switch").addEventListener("click", switchCompany);
$("#account").addEventListener("click", showAccount);
$("#help").addEventListener("click", help);
$("#close-dialog").addEventListener("click", closeModal);
$("#dialog").addEventListener("click", (e) => {
  if (e.target === $("#dialog")) {
    const r = $("#dialog").getBoundingClientRect();
    if (
      e.clientX < r.left ||
      e.clientX > r.right ||
      e.clientY < r.top ||
      e.clientY > r.bottom
    )
      closeModal();
  }
});
async function init() {
  try {
    const [me, workspace, library, companies] = await Promise.all([
      api("/api/me"),
      api("/api/workspace"),
      api("/api/library"),
      api("/api/companies"),
    ]);
    Object.assign(S, {
      me,
      workspace,
      library,
      companies,
      role: roleFor(me.job_title),
      newsRole: me.role || roleFor(me.job_title),
    });
    $("#user-name").textContent = me.full_name;
    $("#user-role").textContent = me.job_title;
    $("#avatar").textContent = me.full_name
      .split(/\s+/)
      .slice(0, 2)
      .map((x) => x[0])
      .join("")
      .toUpperCase();
    if (workspace.hosting?.notice) {
      $("#storage-notice").textContent = workspace.hosting.notice;
      $("#storage-notice").hidden = false;
    }
    const first =
      companies.find((x) => x.company_id === me.company_id) ||
      companies.find((x) => x.writable && x.source === "company_upload") ||
      companies[0];
    if (first) await selectCompany(first.company_id);
    else render();
  } catch (error) {
    $("#main").replaceChildren(
      empty(
        "Не удалось открыть рабочее пространство",
        message(error),
        button("Повторить", init, "button primary"),
      ),
    );
  }
}
init();
