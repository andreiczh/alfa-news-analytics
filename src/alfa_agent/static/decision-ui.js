"use strict";
// One personalization state lives in S. These views never create independent role filters.
const CONTEXT_CATEGORIES = {
  general: "Общая информация",
  strategy: "Стратегия и развитие",
  finance: "Продажи и финансы",
  risks: "Риски и ограничения",
  priorities: "Что отслеживать",
  other: "Дополнительный контекст",
};
const POINT_NAMES = {
  source_fact: "Сообщение источника",
  source_forecast: "Прогноз источника",
  source_assessment: "Оценка источника",
  interpretation: "Интерпретация",
  hypothesis: "Гипотеза",
};

function activeRoleName() {
  return S.me?.job_title || ROLE_NAMES[S.role] || "Руководитель";
}
function updateAccountCaption() {
  $("#user-role").textContent = activeRoleName() + " · " + companyName();
  $("#account").setAttribute(
    "aria-label",
    "Профиль: " + companyName() + ", " + activeRoleName(),
  );
}

async function savePersonalization(companyId, role, jobTitle = "") {
  const previous = S.company?.company_id;
  const draft = { ...S.draft };
  const previousDefault = defaultQuestion();
  const prefs = await api("/api/me/preferences", {
    method: "PUT",
    body: JSON.stringify({ company_id: companyId, role, job_title: jobTitle }),
  });
  S.me = {
    ...S.me,
    role: prefs.role,
    job_title: prefs.job_title,
    selected_company_id: companyId,
  };
  S.role = prefs.role;
  closeModal();
  await selectCompany(companyId, false);
  if (
    previous === companyId &&
    draft.question &&
    draft.question !== previousDefault
  ) {
    S.draft = draft;
    render();
  }
  updateAccountCaption();
  toast("Компания и роль обновлены во всех разделах.");
}

function profileSettings() {
  const content = node("div", "stack");
  content.append(p(S.me.full_name, "profile-name"));
  const form = node("form", "stack"),
    company = node("select"),
    role = node("select"),
    title = node("input");
  company.id = "profile-company";
  for (const item of S.companies) {
    const option = node("option", "", item.label);
    option.value = item.company_id;
    company.append(option);
  }
  company.value = S.company?.company_id || S.me.selected_company_id;
  role.id = "profile-role";
  for (const [key, label] of Object.entries({
    ...ROLE_NAMES,
    other: "Другая должность",
  })) {
    const option = node("option", "", label);
    option.value = key;
    role.append(option);
  }
  role.value = S.role;
  title.id = "profile-job-title";
  title.maxLength = 120;
  title.value = S.me.job_title;
  const titleField = field("Название должности", title);
  titleField.hidden = role.value !== "other";
  role.addEventListener("change", () => {
    titleField.hidden = role.value !== "other";
  });
  add(
    form,
    field("Рабочая компания", company),
    field("Ваша роль во всей системе", role),
    titleField,
    p(
      "Подборка новостей, материалы банка, вопросы и рекомендации используют этот выбор. Роль не меняет права доступа.",
      "fine-print",
    ),
  );
  const save = node("button", "button primary", "Применить во всех разделах");
  save.type = "submit";
  form.append(save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      await savePersonalization(company.value, role.value, title.value);
    } catch (error) {
      report(error);
      save.disabled = false;
    }
  });
  content.append(
    form,
    button("Найти или добавить компанию", switchCompany, "text-button"),
    button(
      "Настроить контекст компании",
      () => {
        closeModal();
        go("company");
      },
      "text-button",
    ),
  );
  const logout = node("form");
  logout.method = "post";
  logout.action = "/auth/logout";
  const out = node("button", "button small", "Выйти из рабочего пространства");
  out.type = "submit";
  logout.append(out);
  content.append(
    detail(
      "Сессия и выход",
      p(
        S.me.auth_mode === "accounts"
          ? "Сессия действует 12 часов. Повторный вход с вашим логином возвращает прежнее пространство. Выход отзывает текущую сессию."
          : "Демонстрационная сессия действует 12 часов. Сначала скачайте важные результаты.",
        "fine-print",
      ),
      logout,
    ),
  );
  if (S.me.auth_mode === "accounts")
    content.append(
      button("Доступ команды и приглашения", teamSettings, "text-button"),
      button("Сменить личный пароль", passwordSettings, "text-button"),
    );
  modal("Компания и роль", content);
}

function passwordSettings() {
  const form = node("form", "stack"),
    old = node("input"),
    next = node("input");
  old.type = next.type = "password";
  old.autocomplete = "current-password";
  next.autocomplete = "new-password";
  old.required = next.required = true;
  old.maxLength = next.maxLength = 256;
  next.minLength = 12;
  const submit = node("button", "button primary", "Сменить пароль и выйти");
  submit.type = "submit";
  add(
    form,
    field("Текущий пароль", old),
    field("Новый пароль, не менее 12 символов", next),
    p(
      "Все открытые сессии будут закрыты. Данные рабочего пространства сохранятся.",
      "fine-print",
    ),
    submit,
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try {
      await api("/api/account/password", {
        method: "PUT",
        body: JSON.stringify({
          current_password: old.value,
          new_password: next.value,
        }),
      });
      location.assign("/auth/login");
    } catch (error) {
      report(error);
      submit.disabled = false;
    }
  });
  modal("Личный пароль", form);
}

async function teamSettings() {
  const content = node("div", "stack"),
    cid = S.company.company_id;
  content.append(
    p(
      "Должность определяет аналитику. Доступ к документам выдаётся отдельным приглашением.",
      "fine-print",
    ),
  );
  try {
    const data = await api("/api/companies/" + cid + "/team");
    for (const m of data.members) {
      const line = node("div", "row");
      line.append(
        p(
          m.full_name +
            " · " +
            {
              owner: "Владелец",
              admin: "Администратор",
              member: "Участник",
              viewer: "Просмотр",
            }[m.permission] +
            (m.active ? "" : " · доступ закрыт"),
        ),
      );
      if (m.active && !["owner", "admin"].includes(m.permission))
        line.append(
          button(
            "Закрыть доступ",
            async () => {
              await api("/api/companies/" + cid + "/team/" + m.user_id, {
                method: "DELETE",
              });
              await teamSettings();
            },
            "button small",
          ),
        );
      content.append(line);
    }
    const permission = node("select");
    for (const [value, label] of [
      ["member", "Работа с контекстом и документами"],
      ["viewer", "Просмотр без редактирования компании"],
    ]) {
      const option = node("option", "", label);
      option.value = value;
      permission.append(option);
    }
    content.append(
      field("Права приглашённого", permission),
      button(
        "Создать одноразовый код",
        async () => {
          const invite = await api("/api/companies/" + cid + "/team/invites", {
            method: "POST",
            body: JSON.stringify({ permission: permission.value }),
          });
          const code = node("textarea");
          code.readOnly = true;
          code.value = invite.code;
          code.rows = 2;
          code.setAttribute("aria-label", "Одноразовый код приглашения");
          modal(
            "Приглашение на 48 часов",
            add(
              node("div", "stack"),
              p(
                "Передайте код коллеге самостоятельно. Он войдёт в свой аккаунт и примет приглашение. Код можно использовать один раз.",
              ),
              code,
            ),
          );
          code.select();
        },
        "button",
      ),
    );
  } catch (error) {
    if (
      !String(error.message).includes("403") &&
      !String(error.message).includes("access_denied")
    )
      content.append(p("Список команды доступен владельцу пространства."));
  }
  const form = node("form", "stack"),
    code = node("input"),
    join = node("button", "button primary", "Принять приглашение");
  code.minLength = 32;
  code.maxLength = 100;
  code.required = true;
  code.autocomplete = "off";
  join.type = "submit";
  add(form, field("Код приглашения от коллеги", code), join);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    join.disabled = true;
    try {
      const result = await api("/api/account/join", {
        method: "POST",
        body: JSON.stringify({ code: code.value.trim() }),
      });
      S.companies = await api("/api/companies");
      await savePersonalization(result.company_id, S.role);
      toast("Доступ к пространству открыт.");
    } catch (error) {
      report(error);
      join.disabled = false;
    }
  });
  content.append(detail("Присоединиться к пространству коллеги", form));
  modal("Доступ команды", content);
}

function renderDecisionOverview() {
  const c = card("decision-overview news-digest");
  c.id = "decision-overview";
  add(c, node("h2", "", "Кратко: важные новости"));
  const items = (S.news?.items || []).slice(0, 3);
  if (S.news?.status === "FAILED")
    c.append(
      box(
        "Не удалось обновить все источники. Ниже — последняя доступная подборка; проверьте её время обновления.",
        "amber",
      ),
    );
  else if (S.news?.stale)
    c.append(
      p(
        "Подборка требует обновления; доступные публикации сохранены с исходными датами.",
        "fine-print",
      ),
    );
  if (!items.length) {
    c.append(
      p(
        S.newsLoading || ["QUEUED", "RUNNING"].includes(S.news?.status)
          ? "Собираем новости для вашей компании. Поиск по готовым материалам уже доступен."
          : "Подходящих свежих публикаций пока нет. Можно расширить период или добавить источник в исследование.",
      ),
    );
  }
  for (const item of items) {
    const row = node("article", "digest-row");
    add(
      row,
      p(item.publisher + " · " + date(item.published_at), "caption"),
      node("h3", "", item.title),
      renderDecisionSignal(item),
    );
    add(
      row,
      add(
        node("div", "row"),
        badge(
          item.read_status === "article_read"
            ? "Текст прочитан"
            : "Только заголовок — требуется проверка",
        ),
        saveNewsButton(item),
        button("Разобрать влияние", () => researchNews(item), "text-button"),
        button(
          "Подробнее",
          () => {
            const target = document.querySelector(
              '#personal-news [data-news-id="' + item.news_id + '"]',
            );
            if (target) {
              const disclosure = target.closest("details");
              if (disclosure) disclosure.open = true;
              target.scrollIntoView({ behavior: "smooth", block: "center" });
            }
          },
          "text-button",
        ),
      ),
    );
    c.append(row);
  }
  const refresh = button(
    S.newsLoading || ["QUEUED", "RUNNING"].includes(S.news?.status)
      ? "Обновляем…"
      : "Обновить подборку",
    () => refreshNews(),
    "text-button",
    "refresh",
  );
  refresh.disabled =
    S.newsLoading || ["QUEUED", "RUNNING"].includes(S.news?.status);
  c.append(refresh);
  if (S.news?.checked_at)
    c.append(
      p(
        "Подборка обновлена " +
          new Date(S.news.checked_at).toLocaleString("ru-RU", {
            timeZone: "Europe/Moscow",
            day: "numeric",
            month: "short",
            hour: "2-digit",
            minute: "2-digit",
          }) +
          " МСК" +
          " · публикации за " +
          S.newsDays +
          " дней",
        "fine-print",
      ),
    );
  const memo = S.news?.brief?.last_research;
  if (memo)
    c.append(
      add(
        node("div", "digest-report-link"),
        p("Последний разбор: " + memo.title, "fine-print"),
        button(
          "Открыть аргументацию и альтернативы",
          () => loadJob(memo.run_id),
          "text-button",
        ),
      ),
    );
  return c;
}

function refreshDecisionOverview() {
  const current = $("#decision-overview");
  if (current && S.view === "home")
    current.replaceWith(renderDecisionOverview());
}

async function reloadPersonalizedContent() {
  const cid = S.company?.company_id,
    epoch = S.epoch;
  if (!cid) return;
  const [snapshot, library] = await Promise.all([
    api("/api/companies/" + encodeURIComponent(cid) + "/profile"),
    api("/api/library?company_id=" + encodeURIComponent(cid)),
  ]);
  if (epoch !== S.epoch || cid !== S.company?.company_id) return;
  S.snapshot = snapshot;
  S.library = library;
  S.news = null;
  resetWorkspaceViews(true);
  S.newsExpanded = false;
  render();
  loadNews(true);
}

async function loadContextConversation() {
  const cid = S.company?.company_id,
    epoch = S.epoch;
  if (!cid || !S.snapshot?.writable) return;
  clearTimeout(S.contextPoll);
  try {
    const data = await api(
      "/api/companies/" + encodeURIComponent(cid) + "/context/dialogue",
    );
    if (epoch !== S.epoch || cid !== S.company?.company_id) return;
    const changed = JSON.stringify(data) !== JSON.stringify(S.contextChat);
    S.contextChat = data;
    if (changed) updateContextPanel();
    if (
      data.turns.some((t) =>
        ["QUEUED", "RUNNING", "DEFERRED_BUDGET"].includes(t.status),
      )
    )
      S.contextPoll = setTimeout(
        loadContextConversation,
        data.turns.some((t) => ["QUEUED", "RUNNING"].includes(t.status))
          ? 2000
          : 30000,
      );
  } catch (error) {
    if (epoch !== S.epoch) return;
    S.contextError = message(error);
    updateContextPanel();
  }
}

function updateContextPanel() {
  const target = $("#company-context-panel");
  if (!target || S.view !== "company") return;
  const input = $("#context-message"),
    focused = input === document.activeElement,
    start = input?.selectionStart,
    end = input?.selectionEnd;
  const oldConversation = target.querySelector(".context-conversation");
  const atEnd =
    !oldConversation ||
    oldConversation.scrollTop + oldConversation.clientHeight >=
      oldConversation.scrollHeight - 45;
  target.replaceWith(renderCompanyContext());
  const conversation = $(".context-conversation");
  if (atEnd && conversation) conversation.scrollTop = conversation.scrollHeight;
  if (focused) {
    const next = $("#context-message");
    next?.focus();
    next?.setSelectionRange(start, end);
  }
}

async function sendContextMessage(text) {
  if (S.contextSending) return;
  const cid = S.company?.company_id,
    epoch = S.epoch;
  S.contextSending = true;
  S.contextError = null;
  updateContextPanel();
  try {
    await api(
      "/api/companies/" + encodeURIComponent(cid) + "/context/dialogue",
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          message: text,
          expected_revision: S.snapshot.context?.revision || 0,
        }),
      },
    );
    if (epoch !== S.epoch) return;
    S.contextDraft = "";
    await loadContextConversation();
  } catch (error) {
    if (epoch === S.epoch) S.contextError = message(error);
  } finally {
    if (epoch === S.epoch) {
      S.contextSending = false;
      updateContextPanel();
    }
  }
}

function renderCompanyContext() {
  const root = node("section", "context-workspace");
  root.id = "company-context-panel";
  const c = card("context-interview"),
    saved = card("saved-context");
  const context = S.snapshot.context || { fields: {}, revision: 0 };
  add(
    c,
    node("span", "eyebrow", "КОНТЕКСТ ДЛЯ РЕШЕНИЙ"),
    node("h2", "", "Расскажите, что важно компании"),
    p(
      "Цели, рынки, планы и ограничения. AI поможет уточнить и структурировать ответы. Изменения попадут в аналитику после вашего подтверждения.",
    ),
  );
  if (!S.snapshot.writable) {
    c.append(
      box(
        "Это пример на чтение. Добавьте свою компанию, чтобы настроить контекст.",
      ),
      button("Добавить свою компанию", switchCompany, "button"),
    );
  } else {
    const turns = S.contextChat?.turns || [],
      conversation = node("div", "context-conversation");
    conversation.setAttribute("aria-label", "Диалог о компании");
    for (const turn of turns.slice(-8)) {
      if (turn.message)
        conversation.append(
          add(
            node("div", "chat-bubble user"),
            p("Вы · " + (ROLE_NAMES[turn.role] || "Руководитель"), "caption"),
            p(turn.message),
          ),
        );
      if (turn.result) {
        const reply = add(
          node("div", "chat-bubble assistant"),
          p("Помощник по контексту", "caption"),
          p(turn.result.response),
        );
        if (turn.result.questions.length)
          reply.append(list(turn.result.questions, "context-questions"));
        if (turn.status === "SUCCEEDED" && turn.result.fields.length)
          reply.append(
            button(
              "Просмотреть " + turn.result.fields.length + " изменений",
              () =>
                reviewContextChanges(
                  turn.result.fields,
                  turn.turn_id,
                  turn.base_revision,
                ),
              "button primary small",
            ),
          );
        if (turn.status === "CONFIRMED")
          reply.append(badge("Изменения подтверждены", "green"));
        conversation.append(reply);
      }
      if (["QUEUED", "RUNNING", "DEFERRED_BUDGET"].includes(turn.status)) {
        const pending = add(
          node("div", "chat-bubble assistant"),
          p(
            turn.status === "DEFERRED_BUDGET"
              ? "Лимит API исчерпан. Продолжение возможно после следующего сброса бюджета."
              : "Изучаем известный контекст и готовим уточнения…",
            "fine-print",
          ),
        );
        pending.append(
          button(
            "Отменить",
            async () => {
              await api(
                "/api/companies/" +
                  S.company.company_id +
                  "/context/dialogue/" +
                  turn.turn_id +
                  "/cancel",
                { method: "POST" },
              );
              await loadContextConversation();
            },
            "text-button",
          ),
        );
        conversation.append(pending);
      }
      if (turn.status === "FAILED")
        conversation.append(
          box(
            "Не удалось подготовить уточнения. " +
              message({ message: turn.error_code }) +
              " Сведения не изменены.",
            "amber",
          ),
        );
    }
    if (!turns.length)
      conversation.append(
        add(
          node("div", "chat-welcome"),
          p(
            "Начните своими словами. Например: чем занимаетесь, что планируете и какие внешние изменения могут помешать.",
          ),
          button(
            "Пусть AI задаст первые вопросы",
            () => sendContextMessage(""),
            "button small",
          ),
        ),
      );
    c.append(conversation);
    if (S.contextError) c.append(box(S.contextError, "amber"));
    const form = node("form", "context-compose"),
      input = node("textarea");
    input.id = "context-message";
    input.rows = 3;
    input.maxLength = 5000;
    input.value = S.contextDraft || "";
    input.placeholder = "Расскажите о бизнесе, целях или новых приоритетах…";
    input.setAttribute("aria-label", "Сообщение о компании");
    input.addEventListener("input", () => {
      S.contextDraft = input.value;
    });
    const send = node(
      "button",
      "button primary",
      S.contextSending ? "Отправляем…" : "Отправить помощнику",
    );
    send.type = "submit";
    send.disabled =
      S.contextSending ||
      turns.some((t) =>
        ["QUEUED", "RUNNING", "DEFERRED_BUDGET"].includes(t.status),
      );
    add(
      form,
      input,
      add(
        node("div", "between"),
        p(
          "Только API организаторов. До подтверждения профиль не меняется.",
          "fine-print",
        ),
        send,
      ),
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (input.value.trim())
        sendContextMessage(input.value.trim()).catch(report);
    });
    c.append(form);
  }
  add(
    saved,
    node("h2", "", "Подтверждённый контекст"),
    p(
      context.revision
        ? "Сохранено вами · " + date(context.updated_at)
        : "Сведения ещё не подтверждены вами",
      "caption",
    ),
  );
  if (context.synthetic)
    saved.append(badge("Синтетический тестовый контекст", "amber"));
  const fields = Object.values(context.fields || {});
  if (!fields.length && !context.description)
    saved.append(
      p(
        "Здесь появятся сведения, которые вы просмотрели и сохранили. Они будут влиять на новости, исследования и материалы банка.",
        "fine-print",
      ),
    );
  if (context.description)
    saved.append(detail("Свободное описание", p(context.description)));
  for (const [key, title] of Object.entries(CONTEXT_CATEGORIES)) {
    const items = fields.filter((f) => f.category === key);
    if (!items.length) continue;
    const group = detail(
      title,
      ...items.map((item) =>
        add(
          node("div", "saved-field"),
          node("h3", "", item.label),
          p(item.value),
        ),
      ),
    );
    group.open = ["strategy", "priorities"].includes(key);
    saved.append(group);
  }
  if (S.snapshot.writable) {
    saved.append(
      add(
        node("div", "row"),
        button(
          "Редактировать контекст",
          () => editSavedContext(),
          "button small",
        ),
        button("Название, сайт и описание", editCompanyBasics, "text-button"),
      ),
      p(
        "Можно добавить собственное поле. Планы и сведения компании сохраняют статус сообщения пользователя.",
        "fine-print",
      ),
    );
  }
  add(root, c, saved);
  return root;
}

function editSavedContext() {
  const current = Object.values(S.snapshot.context?.fields || {});
  reviewContextChanges(current, null, S.snapshot.context?.revision || 0, true);
}

function reviewContextChanges(
  fields,
  turnId,
  revision,
  allowAdditional = true,
) {
  const cid = S.company.company_id;
  const content = node("div", "context-review stack");
  content.append(
    p(
      "Проверьте изменения перед сохранением. Можно исправить текст или снять выбор. Пустое значение удаляет выбранное поле. Документы и старые исследования останутся со своим контекстом.",
    ),
  );
  const rows = [];
  for (const original of fields) {
    const f = {
      key: original.key,
      category: original.category,
      label: original.label,
      value: original.value,
    };
    const c = card("review-field"),
      check = node("input"),
      value = node("textarea");
    check.type = "checkbox";
    check.checked = true;
    check.setAttribute("aria-label", "Применить: " + f.label);
    value.rows = 3;
    value.maxLength = 1800;
    value.value = f.value;
    value.setAttribute("aria-label", f.label);
    const old = S.snapshot.context?.fields?.[f.key]?.value;
    add(
      c,
      add(node("label", "check-row"), check, node("strong", "", f.label)),
      p(CONTEXT_CATEGORIES[f.category] || "Дополнительно", "caption"),
    );
    if (old !== undefined && old !== f.value)
      c.append(p("Было: " + old, "previous-value"));
    if (original.quote)
      c.append(
        detail(
          "Ваши исходные слова",
          node("blockquote", "quote", original.quote),
        ),
      );
    c.append(value);
    rows.push({ field: f, check, value });
    content.append(c);
  }
  const label = node("input"),
    additional = node("textarea");
  if (allowAdditional) {
    label.maxLength = 80;
    label.placeholder = "Например: ограничения по поставщикам";
    additional.rows = 3;
    additional.maxLength = 1800;
    additional.placeholder = "Любые сведения вне готовых категорий";
    content.append(
      detail(
        "Добавить своё поле",
        field("Название поля", label),
        field("Сведения", additional),
      ),
    );
  }
  const save = button(
    "Подтвердить и сохранить",
    async () => {
      if (cid !== S.company?.company_id)
        throw new Error("company_context_changed");
      const changes = rows
        .filter((r) => r.check.checked)
        .map((r) => ({ ...r.field, value: r.value.value }));
      if (additional.value.trim())
        changes.push({
          key: "extra_" + crypto.randomUUID().replaceAll("-", ""),
          category: "other",
          label: label.value.trim() || "Дополнительный контекст",
          value: additional.value.trim(),
        });
      if (!changes.length) {
        toast("Выберите изменение или добавьте поле.");
        return;
      }
      save.disabled = true;
      try {
        const path =
          "/api/companies/" +
          cid +
          "/context/" +
          (turnId ? "dialogue/" + turnId + "/confirm" : "fields");
        await api(path, {
          method: turnId ? "POST" : "PUT",
          body: JSON.stringify({
            expected_revision: revision,
            fields: changes,
          }),
        });
        closeModal();
        await reloadPersonalizedContent();
        await loadContextConversation();
        toast(
          "Контекст сохранён. Новые подборки и исследования учтут изменения.",
        );
      } catch (error) {
        report(error);
        save.disabled = false;
      }
    },
    "button primary",
  );
  content.append(
    add(
      node("div", "row"),
      save,
      button("Продолжить без сохранения", closeModal, "button"),
    ),
  );
  modal(
    turnId ? "Проверьте предложения AI" : "Редактирование контекста",
    content,
  );
}

function editCompanyBasics() {
  const cid = S.company.company_id,
    form = node("form", "stack"),
    name = node("input"),
    site = node("input"),
    description = node("textarea");
  name.value = companyName();
  name.maxLength = 160;
  name.minLength = 2;
  name.required = true;
  site.type = "url";
  site.maxLength = 300;
  site.value = S.snapshot.context?.website || "";
  site.placeholder = "https://company.ru";
  description.value = S.snapshot.context?.description || "";
  description.rows = 4;
  description.maxLength = 5000;
  const save = node("button", "button primary", "Сохранить сведения");
  save.type = "submit";
  add(
    form,
    field("Название компании", name),
    field("Официальный сайт", site),
    field("Свободное описание", description),
    save,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (site.value && !site.value.startsWith("https://")) {
      site.setCustomValidity("Укажите HTTPS-ссылку.");
      site.reportValidity();
      site.setCustomValidity("");
      return;
    }
    save.disabled = true;
    try {
      if (name.value.trim() !== companyName()) {
        const item = await api("/api/companies/" + cid, {
          method: "PUT",
          body: JSON.stringify({ name: name.value.trim() }),
        });
        const old = S.companies.find((x) => x.company_id === cid);
        if (old) old.label = item.label;
      }
      await api("/api/companies/" + cid + "/context", {
        method: "PUT",
        body: JSON.stringify({
          description: description.value,
          website: site.value,
        }),
      });
      closeModal();
      await reloadPersonalizedContent();
      await loadContextConversation();
      updateAccountCaption();
    } catch (error) {
      report(error);
      save.disabled = false;
    }
  });
  modal("Сведения о компании", form);
}

function narrativeEvidence(value, ids, job) {
  return (value.evidence || [])
    .filter((a) => ids.includes(a.anchor_id))
    .map((a) =>
      add(
        node("div", "evidence-item"),
        node("blockquote", "quote", a.quote),
        sourceCaption(job.sources.find((s) => s.source_id === a.source_id)),
      ),
    );
}
function narrativePoint(point, value, job) {
  return add(
    node("div", "narrative-point"),
    badge(POINT_NAMES[point.kind] || "Условная оценка"),
    ["source_fact", "source_forecast", "source_assessment"].includes(point.kind)
      ? p("Дата источника: " + date(point.source_published_at), "caption")
      : null,
    p(point.text),
    point.condition ? p("Условие: " + point.condition, "condition") : null,
    detail("Основания", ...narrativeEvidence(value, point.anchor_ids, job)),
  );
}

function reviewResearch(job) {
  const form = node("form", "stack"),
    sources = node("input"),
    assumptions = node("input"),
    comment = node("textarea"),
    verdict = node("select");
  sources.type = assumptions.type = "checkbox";
  comment.minLength = 20;
  comment.maxLength = 1000;
  comment.required = true;
  comment.rows = 3;
  for (const [value, label] of [
    ["accepted", "Проверено мной, можно использовать с ограничениями"],
    ["rejected", "Отклонить: есть ошибки или недостаточно оснований"],
  ]) {
    const option = node("option", "", label);
    option.value = value;
    verdict.append(option);
  }
  const submit = node("button", "button primary", "Сохранить решение");
  submit.type = "submit";
  add(
    form,
    p(
      "Отметка относится к вашему просмотру конкретной версии записки. Она не превращает условный сценарий в установленный факт.",
      "fine-print",
    ),
    add(
      node("label", "check-row"),
      sources,
      node("span", "", "Я сверил даты, числа и источники"),
    ),
    add(
      node("label", "check-row"),
      assumptions,
      node("span", "", "Я проверил применимость к компании и допущения"),
    ),
    field("Решение", verdict),
    field("Что проверено или что нужно исправить", comment),
    submit,
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try {
      await api("/api/research/" + job.run_id + "/review", {
        method: "POST",
        body: JSON.stringify({
          verdict: verdict.value,
          comment: comment.value,
          sources_checked: sources.checked,
          assumptions_checked: assumptions.checked,
        }),
      });
      S.job = await api("/api/research/" + job.run_id);
      closeModal();
      render();
    } catch (error) {
      report(error);
      submit.disabled = false;
    }
  });
  modal("Проверка аналитической записки", form);
}

function renderNarrative(main, job) {
  const value = job.result.narrative;
  if (!value) return;
  const sections = node("div", "analytical-memo");
  const gate = job.quality_gate || {},
    notice = card("memo-section");
  const reviewed = gate.status === "reviewed_by_user";
  notice.append(
    badge(
      reviewed
        ? "Просмотрено пользователем"
        : gate.status === "rejected_by_user"
          ? "Отклонено пользователем"
          : "Черновик: требуется проверка",
    ),
    p(
      "Автоматическая проверка не заменяет финансовую экспертизу. Чистый эффект на компанию не рассчитан без её финансовой модели.",
      "fine-print",
    ),
  );
  if (value.generation_mode === "rate_pause_policy_v1")
    notice.append(
      p(
        "Условные сценарии для проверки допущений. Вероятности и финансовый эффект не рассчитаны.",
        "fine-print",
      ),
    );
  if (!gate.current_rules)
    notice.append(
      p(
        "Отчёт создан до текущих правил проверки. Для использования выводов запустите исследование заново.",
        "fine-print",
      ),
    );
  else if (S.me.auth_mode === "accounts")
    notice.append(
      button(
        "Проверить и отметить результат",
        () => reviewResearch(job),
        "button small",
      ),
    );
  if (job.user_review) notice.append(p(job.user_review.comment, "fine-print"));
  sections.append(notice);
  const appendSection = (title, points, fallback) => {
    const section = card("memo-section");
    section.append(node("h2", "", title));
    if (!points.length)
      section.append(
        p(
          fallback || "По доступным источникам отдельный вывод не обоснован.",
          "fine-print",
        ),
      );
    else
      points.forEach((point) =>
        section.append(narrativePoint(point, value, job)),
      );
    sections.append(section);
  };
  appendSection("Что происходит", value.situation);
  appendSection("Почему это происходит", value.drivers);
  const evidence = card("memo-section");
  evidence.append(
    node("h2", "", "Что подтверждает вывод"),
    p(
      "Каждое звено опирается на источник. Сообщение источника, интерпретация и условный сценарий имеют разные статусы.",
      "fine-print",
    ),
    detail(
      "Проследить аргументацию от источника к выводу",
      ...narrativeEvidence(
        value,
        [
          ...new Set(
            [
              value.main_conclusion,
              ...value.situation,
              ...value.drivers,
            ].flatMap((p) => p.anchor_ids),
          ),
        ],
        job,
      ),
    ),
  );
  sections.append(evidence);
  const forecast = card("memo-section forecast-section");
  forecast.id = "forecast-scenarios";
  add(
    forecast,
    node("span", "eyebrow", "УСЛОВНОЕ РАЗВИТИЕ"),
    node("h2", "", "Что может произойти дальше"),
    p(value.forecast_limit),
  );
  if (value.forecast_status === "insufficient_evidence")
    forecast.append(
      box(
        "Данных для обоснованного прогноза недостаточно. Показаны условия и следующие проверки, без выдуманного исхода.",
      ),
    );
  const base = value.scenarios.filter((s) => s.variant === "base"),
    alternatives = value.scenarios.filter((s) => s.variant === "alternative");
  const scenarioCard = (scenario) => {
    const c = add(
      node("article", "scenario-card"),
      badge(scenario.variant === "base" ? "Рабочий сценарий" : "Альтернатива"),
      node("h3", "", scenario.title),
      p(scenario.outcome),
      p("Горизонт: " + scenario.horizon, "caption"),
      node("h4", "", "Почему такой сценарий возможен"),
      p(scenario.mechanism),
      detail("Предпосылки", list(scenario.assumptions)),
      detail("Что изменит сценарий", list(scenario.change_signals)),
      p(scenario.confidence_reason, "fine-print"),
      detail(
        "Источники сценария",
        ...narrativeEvidence(value, scenario.anchor_ids, job),
      ),
    );
    return c;
  };
  base.forEach((s) => forecast.append(scenarioCard(s)));
  sections.append(forecast);
  const alt = card("memo-section");
  alt.append(node("h2", "", "Альтернативные сценарии"));
  alternatives.length
    ? alternatives.forEach((s) => alt.append(scenarioCard(s)))
    : alt.append(
        p(
          "Альтернативный исход по имеющимся данным не обоснован. Нужны дополнительные источники и внутренние параметры.",
          "fine-print",
        ),
      );
  sections.append(alt);
  appendSection("Влияние на компанию", value.company_impact);
  appendSection(
    "Риски",
    value.risks,
    "Отдельные риски для компании не подтверждены; проверьте условия применимости и пробелы.",
  );
  appendSection(
    "Возможности",
    value.opportunities,
    "Отдельная возможность для компании по этим основаниям не установлена.",
  );
  appendSection(
    "Рекомендации для " + (ROLE_NAMES[value.role] || job.job_title),
    value.recommendations,
  );
  if (
    value.role !== S.role ||
    value.context_revision !== (S.snapshot.context?.revision || 0) ||
    job.result.profile_version !== S.snapshot.profile?.version
  ) {
    sections.prepend(
      add(
        node("div", "info-box amber"),
        p(
          "Этот материал сохранён для прежней роли или версии контекста. Исходные выводы не переписаны. Текущие ролевые проверки показаны отдельно.",
        ),
        button(
          "Обновить исследование для текущего профиля",
          () => continueResearch(job, job.question, "current"),
          "button small",
        ),
      ),
    );
  }
  main.append(sections);
}

function exportNarrative(lines, value) {
  if (!value) return;
  const blocks = [
    ["Что происходит", value.situation],
    ["Почему это происходит", value.drivers],
    ["Влияние на компанию", value.company_impact],
    ["Риски", value.risks],
    ["Возможности", value.opportunities],
    ["Рекомендации", value.recommendations],
  ];
  for (const [title, points] of blocks) {
    lines.push("## " + title, "");
    for (const point of points)
      lines.push(
        "**" + (POINT_NAMES[point.kind] || "Оценка") + "**. " + point.text,
        point.condition ? "Условие: " + point.condition : "",
        "Основания: " + point.anchor_ids.join(", "),
        "",
      );
  }
  lines.push("## Сценарии и условия пересмотра", "", value.forecast_limit, "");
  for (const scenario of value.scenarios)
    lines.push(
      "### " + scenario.title,
      "",
      scenario.outcome,
      "Горизонт: " + scenario.horizon,
      "Механизм: " + scenario.mechanism,
      ...scenario.assumptions.map((x) => "- Условие: " + x),
      ...scenario.change_signals.map((x) => "- Сигнал пересмотра: " + x),
      scenario.confidence_reason,
      "",
    );
  lines.push("## Опоры связной аргументации", "");
  for (const anchor of value.evidence)
    lines.push(
      "### " + anchor.anchor_id + " · " + (anchor.title || anchor.source_id),
      "",
      "> " + anchor.quote.replaceAll("\n", "\n> "),
      "",
    );
}
