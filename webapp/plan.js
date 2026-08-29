/* webapp/plan.js
 * ================
 * Логика Telegram Mini App "План на год". Рисует строку зарплаты, строки
 * категорий (создание/переименование/удаление/защита/очистка прямо тут) и
 * три итоговые строки снизу (расходы всего, накопления за месяц, план
 * накоплений нарастающим итогом), плюс две служебные колонки слева
 * (всего/среднее в месяц) и подсветку текущего месяца по московскому
 * времени.
 *
 * Изменения сохраняются по blur/Enter в ячейке через HTTP API
 * (services/webapp_api.py). Если сервер отклоняет правку (план накоплений
 * ушёл бы в минус), мы НЕ откатываем введённое число - оно остаётся
 * видно локально (и участвует в подсчёте "Плана накоплений"), чтобы
 * человек видел, насколько сильно он промахнулся, и мог поправить
 * прошлые месяцы. При этом в БД это значение не попадает: при следующем
 * открытии Mini App вернётся последнее реально сохранённое число.
 */

(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

  let state = {
    months: [],
    categories: [],
    salary: {},
    plan: {},
    initial_savings: 0,
    cumulative: {},
    _unsaved: new Set(), // ключи "category:ID:month" / "salary:month" / "initial_savings"
  };

  // -------------------------------------------------------------------
  // HTTP-обёртки
  // -------------------------------------------------------------------

  function authHeaders() {
    return { "X-Telegram-Init-Data": tg ? tg.initData || "" : "" };
  }

  async function apiGet(path) {
    return fetch(path, { headers: authHeaders() });
  }

  async function apiSend(method, path, body) {
    return fetch(path, {
      method,
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
      body: JSON.stringify(body),
    });
  }

  async function saveSalaryCell(month, amount) {
    try {
      const res = await apiSend("PUT", "/api/salary", { month, amount });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, message: data.message, cumulative: data.cumulative };
      return { ok: true };
    } catch (e) {
      return { ok: false, message: "Не удалось связаться с сервером." };
    }
  }

  async function saveCategoryPlanCell(categoryId, month, amount, noRecalc) {
    try {
      const res = await apiSend("PUT", "/api/category-plan", {
        category_id: Number(categoryId),
        month,
        amount,
        no_recalc: !!noRecalc,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, message: data.message, cumulative: data.cumulative };
      return { ok: true };
    } catch (e) {
      return { ok: false, message: "Не удалось связаться с сервером." };
    }
  }

  // -------------------------------------------------------------------
  // Форматирование чисел, месяцев, текущий месяц по Москве
  // -------------------------------------------------------------------

  function formatNum(v) {
    const n = Math.round(Number(v) || 0);
    return n.toLocaleString("ru-RU");
  }

  function parseNum(text) {
    const digits = String(text || "").replace(/[^\d-]/g, "");
    if (digits === "" || digits === "-") return 0;
    return parseInt(digits, 10) || 0;
  }

  function monthLabel(iso) {
    const parts = iso.split("-");
    const monthIdx = parseInt(parts[1], 10) - 1;
    return `${MONTHS_RU[monthIdx] || iso} ${parts[0].slice(2)}`;
  }

  function shiftMonthIso(iso, delta) {
    let [y, m] = iso.split("-").map(Number);
    m += delta;
    while (m > 12) { m -= 12; y += 1; }
    while (m < 1) { m += 12; y -= 1; }
    return `${y}-${String(m).padStart(2, "0")}-01`;
  }

  // НОВОЕ: подсветка текущего месяца (п.7) - считаем именно по московскому
  // времени через Intl, а не по локальному часовому поясу устройства.
  function getMoscowMonthIso() {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Moscow",
      year: "numeric",
      month: "2-digit",
    }).formatToParts(new Date());
    const year = parts.find((p) => p.type === "year").value;
    const month = parts.find((p) => p.type === "month").value;
    return `${year}-${month}-01`;
  }

  // -------------------------------------------------------------------
  // Загрузка плана (с поддержкой пролистывания окна - см. п.11)
  // -------------------------------------------------------------------

  async function loadPlan(startIso) {
    try {
      const url = startIso ? `/api/plan?start=${encodeURIComponent(startIso)}` : "/api/plan";
      const res = await apiGet(url);
      const rawText = await res.text();
      let data = {};
      try {
        data = rawText ? JSON.parse(rawText) : {};
      } catch (parseErr) {
        console.error("plan.js: /api/plan вернул не-JSON тело:", rawText.slice(0, 500));
      }

      if (!res.ok) {
        console.error("plan.js: /api/plan ->", res.status, data);
        showFatalError(
          data.message || `Сервер ответил ошибкой ${res.status}. Откройте бота и попробуйте снова.`
        );
        return false;
      }

      state = data;
      state._unsaved = new Set();
      return true;
    } catch (e) {
      console.error("plan.js: сеть/фетч упал:", e);
      showFatalError("Нет связи с сервером. Проверьте интернет и попробуйте снова.");
      return false;
    }
  }

  function showFatalError(text) {
    const loading = document.getElementById("loading");
    loading.textContent = "⚠️ " + text;
  }

  // -------------------------------------------------------------------
  // Вычисление производных строк (расходы, накопления, план накоплений)
  // -------------------------------------------------------------------

  function computeDerived() {
    const totalExpense = {};
    const savings = {};
    const cumulative = {};
    let running = Number(state.initial_savings) || 0;

    for (const mk of state.months) {
      let sum = 0;
      for (const cat of state.categories) {
        const catPlan = state.plan[cat.id] || {};
        const cell = catPlan[mk];
        sum += cell ? Number(cell.amount) || 0 : 0;
      }
      totalExpense[mk] = sum;

      const income = Number(state.salary[mk]) || 0;
      savings[mk] = income - sum;
      running += savings[mk];
      cumulative[mk] = running;
    }

    return { totalExpense, savings, cumulative };
  }

  function sumOverMonths(catId) {
    const catPlan = state.plan[catId] || {};
    let total = 0;
    for (const mk of state.months) total += (catPlan[mk] || {}).amount || 0;
    return total;
  }

  // -------------------------------------------------------------------
  // Рендер
  // -------------------------------------------------------------------

  function render() {
    document.getElementById("horizon-subtitle").textContent = state.months.length
      ? `${monthLabel(state.months[0])} — ${monthLabel(state.months[state.months.length - 1])}`
      : "";

    renderHeader();
    renderSalaryRow();
    renderCategoryRows();
    renderAddCategoryRow();
    renderSummaryRows();
  }

  function renderHeader() {
    const row = document.getElementById("header-row");
    row.innerHTML = "";

    const thName = document.createElement("th");
    thName.className = "name-col";
    thName.textContent = "Категория";
    row.appendChild(thName);

    const thTotal = document.createElement("th");
    thTotal.className = "total-col";
    thTotal.textContent = "Всего";
    row.appendChild(thTotal);

    const thAvg = document.createElement("th");
    thAvg.className = "avg-col";
    thAvg.textContent = "Средн/мес";
    row.appendChild(thAvg);

    const currentMonthIso = getMoscowMonthIso();
    for (const mk of state.months) {
      const th = document.createElement("th");
      th.className = "month-col" + (mk === currentMonthIso ? " current-month" : "");
      th.textContent = monthLabel(mk);
      row.appendChild(th);
    }
  }

  function renderSalaryRow() {
    const body = document.getElementById("salary-body");
    body.innerHTML = "";

    const tr = document.createElement("tr");
    tr.className = "salary-row";

    const nameTd = document.createElement("td");
    nameTd.className = "name-col";
    nameTd.textContent = "💰 Зарплата";
    tr.appendChild(nameTd);

    let total = 0;
    for (const mk of state.months) total += Number(state.salary[mk]) || 0;
    const avg = state.months.length ? total / state.months.length : 0;

    const totalTd = document.createElement("td");
    totalTd.className = "total-col";
    totalTd.textContent = formatNum(total);
    tr.appendChild(totalTd);

    const avgTd = document.createElement("td");
    avgTd.className = "avg-col";
    avgTd.textContent = formatNum(avg);
    tr.appendChild(avgTd);

    const currentMonthIso = getMoscowMonthIso();
    state.months.forEach((mk, idx) => {
      const td = document.createElement("td");
      td.className = "month-col" + (mk === currentMonthIso ? " current-month" : "");

      const wrap = document.createElement("div");
      wrap.className = "cell-wrap";
      if (state._unsaved.has(`salary:${mk}`)) wrap.classList.add("unsaved");

      const input = document.createElement("input");
      input.className = "amount";
      input.type = "text";
      input.inputMode = "numeric";
      input.autocomplete = "off";
      input.value = formatNum(state.salary[mk] || 0);
      input.dataset.kind = "salary";
      input.dataset.month = mk;
      input.dataset.index = String(idx);
      attachAmountHandlers(input);

      wrap.appendChild(input);
      td.appendChild(wrap);
      tr.appendChild(td);
    });

    body.appendChild(tr);
  }

  function renderCategoryRows() {
    const body = document.getElementById("category-body");
    body.innerHTML = "";
    const currentMonthIso = getMoscowMonthIso();

    for (const cat of state.categories) {
      const tr = document.createElement("tr");
      tr.className = "category-row";
      tr.dataset.categoryId = String(cat.id);

      const nameTd = document.createElement("td");
      nameTd.className = "name-col";

      const nameWrap = document.createElement("div");
      nameWrap.className = "category-name-cell";

      const shieldBtn = document.createElement("button");
      shieldBtn.type = "button";
      shieldBtn.className = "icon-btn" + (cat.is_protected ? " active" : "");
      shieldBtn.title = "Защитить от пересчёта во всех месяцах (проставит 🔒 на всю строку)";
      shieldBtn.textContent = "🛡";
      shieldBtn.addEventListener("click", () => toggleProtected(cat.id));

      const nameInput = document.createElement("input");
      nameInput.className = "cat-name";
      nameInput.type = "text";
      nameInput.value = cat.name;
      nameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") nameInput.blur();
      });
      nameInput.addEventListener("blur", () => renameCategory(cat.id, nameInput));

      const clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "icon-btn clear-btn";
      clearBtn.title = "Обнулить лимиты этой категории на все видимые месяцы";
      clearBtn.textContent = "🧹";
      clearBtn.addEventListener("click", () => clearCategoryRow(cat.id, cat.name));

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "icon-btn danger";
      delBtn.title = "Удалить категорию";
      delBtn.textContent = "✕";
      delBtn.addEventListener("click", () => deleteCategory(cat.id, cat.name));

      nameWrap.appendChild(shieldBtn);
      nameWrap.appendChild(nameInput);
      nameWrap.appendChild(clearBtn);
      nameWrap.appendChild(delBtn);
      nameTd.appendChild(nameWrap);
      tr.appendChild(nameTd);

      const total = sumOverMonths(cat.id);
      const avg = state.months.length ? total / state.months.length : 0;

      const totalTd = document.createElement("td");
      totalTd.className = "total-col";
      totalTd.textContent = formatNum(total);
      tr.appendChild(totalTd);

      const avgTd = document.createElement("td");
      avgTd.className = "avg-col";
      avgTd.textContent = formatNum(avg);
      tr.appendChild(avgTd);

      const catPlan = state.plan[cat.id] || {};

      state.months.forEach((mk, idx) => {
        const cellData = catPlan[mk] || { amount: 0, no_recalc: false };

        const td = document.createElement("td");
        td.className = "month-col" + (mk === currentMonthIso ? " current-month" : "");

        const wrap = document.createElement("div");
        wrap.className = "cell-wrap" + (cellData.no_recalc ? " locked" : "");
        if (state._unsaved.has(`category:${cat.id}:${mk}`)) wrap.classList.add("unsaved");

        const input = document.createElement("input");
        input.className = "amount";
        input.type = "text";
        input.inputMode = "numeric";
        input.autocomplete = "off";
        input.value = formatNum(cellData.amount);
        input.dataset.kind = "category";
        input.dataset.categoryId = String(cat.id);
        input.dataset.month = mk;
        input.dataset.index = String(idx);
        attachAmountHandlers(input);

        const lockBtn = document.createElement("button");
        lockBtn.type = "button";
        lockBtn.className = "lock-toggle" + (cellData.no_recalc ? " active" : "");
        lockBtn.title = "Не пересчитывать лимит этой категории в этом месяце";
        lockBtn.textContent = "🔒";
        lockBtn.addEventListener("click", () => toggleNoRecalc(cat.id, mk));

        wrap.appendChild(input);
        wrap.appendChild(lockBtn);
        td.appendChild(wrap);
        tr.appendChild(td);
      });

      body.appendChild(tr);
    }
  }

  function renderAddCategoryRow() {
    const body = document.getElementById("add-category-body");
    body.innerHTML = "";

    const tr = document.createElement("tr");
    tr.className = "add-category-row";

    const td = document.createElement("td");
    td.colSpan = state.months.length + 3;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "+ Добавить категорию";
    btn.addEventListener("click", addCategory);

    td.appendChild(btn);
    tr.appendChild(td);
    body.appendChild(tr);
  }

  function renderSummaryRows() {
    const body = document.getElementById("summary-body");
    body.innerHTML = "";

    const { totalExpense, savings, cumulative } = computeDerived();
    state.cumulative = cumulative;

    body.appendChild(buildSummaryRow("Расходы всего", totalExpense, "total-expense-row", false, true));
    body.appendChild(buildSummaryRow("Накопления", savings, "savings-row", true, true));
    body.appendChild(buildSummaryRow("План накоплений", cumulative, "cumulative-row", true, false));

    updateCards(cumulative);
    updateBanner(cumulative);
  }

  function buildSummaryRow(label, valuesByMonth, cls, colorize, showTotals) {
    const tr = document.createElement("tr");
    tr.className = cls;

    const nameTd = document.createElement("td");
    nameTd.className = "name-col";
    nameTd.textContent = label;
    tr.appendChild(nameTd);

    const totalTd = document.createElement("td");
    totalTd.className = "total-col";
    const avgTd = document.createElement("td");
    avgTd.className = "avg-col";

    if (showTotals) {
      let total = 0;
      for (const mk of state.months) total += valuesByMonth[mk] || 0;
      const avg = state.months.length ? total / state.months.length : 0;
      totalTd.textContent = formatNum(total);
      avgTd.textContent = formatNum(avg);
    } else {
      totalTd.textContent = "—";
      avgTd.textContent = "—";
    }
    tr.appendChild(totalTd);
    tr.appendChild(avgTd);

    const currentMonthIso = getMoscowMonthIso();
    for (const mk of state.months) {
      const td = document.createElement("td");
      td.className = "month-col" + (mk === currentMonthIso ? " current-month" : "");
      const v = valuesByMonth[mk] || 0;
      td.textContent = formatNum(v);
      if (colorize) td.classList.add(v < 0 ? "value-negative" : "value-positive");
      tr.appendChild(td);
    }

    return tr;
  }

  function updateCards(cumulative) {
    const lastMonth = state.months[state.months.length - 1];
    const total = cumulative[lastMonth] || 0;

    const el = document.getElementById("year-total");
    el.textContent = formatNum(total) + " ₽";
    el.classList.toggle("positive", total >= 0);
    el.classList.toggle("negative", total < 0);

    const input = document.getElementById("initial-savings-input");
    if (document.activeElement !== input) {
      input.value = formatNum(state.initial_savings);
    }
    input.classList.toggle("input-unsaved", state._unsaved.has("initial_savings"));
  }

  function updateBanner(cumulative) {
    const banner = document.getElementById("banner");
    const negativeMonths = state.months.filter((mk) => (cumulative[mk] || 0) < 0);
    const unsavedCount = state._unsaved.size;

    if (negativeMonths.length === 0 && unsavedCount === 0) {
      banner.classList.add("hidden");
      banner.innerHTML = "";
      return;
    }

    const lines = [];
    if (negativeMonths.length > 0) {
      lines.push(`⚠️ План накоплений уходит в минус: ${negativeMonths.map(monthLabel).join(", ")}.`);
    }
    if (unsavedCount > 0) {
      lines.push(
        `Несохранённых значений (видно только вам): ${unsavedCount}. Они пропадут при следующем ` +
        `открытии, если их не поправить.`
      );
    }
    banner.innerHTML = lines.map((l) => `<div class="banner-line">${l}</div>`).join("");
    banner.classList.remove("hidden");
  }

  // -------------------------------------------------------------------
  // Редактирование ячеек с суммами (зарплата / план по категории)
  //
  // ВАЖНО (см. обсуждение): если сервер отклоняет правку (план
  // накоплений уходит в минус), мы НЕ откатываем введённое значение -
  // локально (state.*, отображение) оно остаётся как есть, чтобы человек
  // видел настоящий "План накоплений" с минусом и мог поправить прошлые
  // месяцы. В БД оно при этом не сохраняется - ячейка помечается classом
  // "unsaved" и попадает в state._unsaved, откуда исчезнет либо при
  // успешном повторном сохранении, либо при следующей загрузке плана.
  // -------------------------------------------------------------------

  function attachAmountHandlers(input) {
    input.addEventListener("focus", () => {
      const raw = parseNum(input.value);
      input.value = raw === 0 ? "" : String(raw);
      input.select();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") input.blur();
    });
    input.addEventListener("blur", () => onAmountBlur(input));
  }

  async function onAmountBlur(input) {
    const kind = input.dataset.kind;
    const amount = parseNum(input.value);
    input.value = formatNum(amount);

    if (kind === "salary") {
      await handleSalaryEdit(input, input.dataset.month, amount, Number(input.dataset.index));
    } else {
      await handleCategoryEdit(
        input,
        input.dataset.categoryId,
        input.dataset.month,
        amount,
        Number(input.dataset.index)
      );
    }
  }

  async function handleSalaryEdit(input, month, amount, index) {
    const wrap = input.closest(".cell-wrap");
    const previous = state.salary[month] || 0;
    if (amount === previous) return;

    const key = `salary:${month}`;
    const isFirstColumn = index === 0;
    const otherMonthsEmpty =
      isFirstColumn && state.months.slice(1).every((mk) => !(state.salary[mk] || 0));

    // Предпросмотр сразу, ещё до ответа сервера.
    state.salary[month] = amount;
    renderSummaryRows();

    wrap.classList.add("pending");
    wrap.classList.remove("unsaved");

    const result = await saveSalaryCell(month, amount);
    wrap.classList.remove("pending");

    if (!result.ok) {
      state._unsaved.add(key);
      wrap.classList.add("unsaved");
      showToast(result.message || "Не сохранено: план накоплений уходит в минус. Видно только у вас.");
      renderSummaryRows();
      return;
    }

    state._unsaved.delete(key);

    if (isFirstColumn && otherMonthsEmpty && amount > 0) {
      await fillSalaryRow(amount, month);
    }

    renderSummaryRows();
  }

  async function fillSalaryRow(amount, skipMonth) {
    let saved = 0;
    let unsavedCount = 0;

    for (const mk of state.months) {
      if (mk === skipMonth) continue;

      state.salary[mk] = amount;
      const cellInput = document.querySelector(
        `input.amount[data-kind="salary"][data-month="${mk}"]`
      );
      const wrap = cellInput ? cellInput.closest(".cell-wrap") : null;
      if (cellInput) cellInput.value = formatNum(amount);

      const result = await saveSalaryCell(mk, amount);
      const key = `salary:${mk}`;
      if (result.ok) {
        state._unsaved.delete(key);
        if (wrap) wrap.classList.remove("unsaved");
        saved++;
      } else {
        state._unsaved.add(key);
        if (wrap) wrap.classList.add("unsaved");
        unsavedCount++;
      }
    }

    renderSummaryRows();

    if (unsavedCount > 0) {
      showToast(`Заполнено ${saved} мес., ${unsavedCount} не сохранено (план ушёл бы в минус).`);
    } else if (saved > 0) {
      showToast(`Зарплата заполнена на ${saved + 1} мес.`);
    }
  }

  async function handleCategoryEdit(input, categoryId, month, amount, index) {
    const wrap = input.closest(".cell-wrap");
    const catPlan = state.plan[categoryId] || (state.plan[categoryId] = {});
    const cell = catPlan[month] || { amount: 0, no_recalc: false };
    const previous = cell.amount || 0;
    if (amount === previous) return;

    const key = `category:${categoryId}:${month}`;
    const isFirstColumn = index === 0;
    const otherMonthsEmpty =
      isFirstColumn && state.months.slice(1).every((mk) => !((catPlan[mk] || {}).amount));

    catPlan[month] = { amount, no_recalc: cell.no_recalc };
    renderSummaryRows();

    wrap.classList.add("pending");
    wrap.classList.remove("unsaved");

    const result = await saveCategoryPlanCell(categoryId, month, amount, cell.no_recalc);
    wrap.classList.remove("pending");

    if (!result.ok) {
      state._unsaved.add(key);
      wrap.classList.add("unsaved");
      showToast(result.message || "Не сохранено: план накоплений уходит в минус. Видно только у вас.");
      renderSummaryRows();
      return;
    }

    state._unsaved.delete(key);

    if (isFirstColumn && otherMonthsEmpty && amount > 0) {
      await fillCategoryRow(categoryId, amount, month, cell.no_recalc);
    }

    renderSummaryRows();
  }

  async function fillCategoryRow(categoryId, amount, skipMonth, noRecalc) {
    const catPlan = state.plan[categoryId] || (state.plan[categoryId] = {});
    let saved = 0;
    let unsavedCount = 0;

    for (const mk of state.months) {
      if (mk === skipMonth) continue;

      catPlan[mk] = { amount, no_recalc: noRecalc };
      const cellInput = document.querySelector(
        `input.amount[data-kind="category"][data-category-id="${categoryId}"][data-month="${mk}"]`
      );
      const wrap = cellInput ? cellInput.closest(".cell-wrap") : null;
      if (cellInput) cellInput.value = formatNum(amount);

      const result = await saveCategoryPlanCell(categoryId, mk, amount, noRecalc);
      const key = `category:${categoryId}:${mk}`;
      if (result.ok) {
        state._unsaved.delete(key);
        if (wrap) wrap.classList.remove("unsaved");
        saved++;
      } else {
        state._unsaved.add(key);
        if (wrap) wrap.classList.add("unsaved");
        unsavedCount++;
      }
    }

    renderSummaryRows();

    if (unsavedCount > 0) {
      showToast(`Заполнено ${saved} мес., ${unsavedCount} не сохранено (план ушёл бы в минус).`);
    } else if (saved > 0) {
      showToast(`Заполнено на ${saved + 1} мес.`);
    }
  }

  async function toggleNoRecalc(categoryId, month) {
    const catPlan = state.plan[categoryId] || {};
    const cell = catPlan[month] || { amount: 0, no_recalc: false };
    const newFlag = !cell.no_recalc;

    const result = await saveCategoryPlanCell(categoryId, month, cell.amount, newFlag);
    if (!result.ok) {
      showToast(result.message || "Не удалось изменить.");
      return;
    }

    if (!state.plan[categoryId]) state.plan[categoryId] = {};
    state.plan[categoryId][month] = { amount: cell.amount, no_recalc: newFlag };
    renderCategoryRows();
  }

  // -------------------------------------------------------------------
  // Категории: добавление / переименование / защита / очистка / удаление
  // -------------------------------------------------------------------

  async function addCategory() {
    const name = await showTextPrompt("Название новой категории:", "Например, Продукты");
    if (!name) return;

    try {
      const res = await apiSend("POST", "/api/categories", { name });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast(data.message || "Не удалось добавить категорию.");
        return;
      }

      const existing = state.categories.find((c) => String(c.id) === String(data.id));
      if (!existing) {
        state.categories.push({ id: data.id, name: data.name, is_protected: false });
      }
      if (!state.plan[data.id]) {
        state.plan[data.id] = {};
        for (const mk of state.months) state.plan[data.id][mk] = { amount: 0, no_recalc: false };
      }

      renderCategoryRows();
      renderAddCategoryRow();
      renderSummaryRows();
    } catch (e) {
      showToast("Не удалось связаться с сервером.");
    }
  }

  async function renameCategory(categoryId, input) {
    const cat = state.categories.find((c) => String(c.id) === String(categoryId));
    if (!cat) return;

    const name = input.value.trim();
    if (!name || name === cat.name) {
      input.value = cat.name;
      return;
    }

    try {
      const res = await apiSend("PATCH", `/api/categories/${categoryId}`, { name });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast(data.message || "Не удалось переименовать категорию.");
        input.value = cat.name;
        return;
      }
      cat.name = name;
    } catch (e) {
      showToast("Не удалось связаться с сервером.");
      input.value = cat.name;
    }
  }

  async function toggleProtected(categoryId) {
    const cat = state.categories.find((c) => String(c.id) === String(categoryId));
    if (!cat) return;

    const newValue = !cat.is_protected;
    try {
      const res = await apiSend("PATCH", `/api/categories/${categoryId}`, { is_protected: newValue });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast(data.message || "Не удалось изменить защиту категории.");
        return;
      }
      cat.is_protected = newValue;

      // По просьбе пользователя: включение/выключение защиты категории
      // сразу проставляет/снимает замки 🔒 (no_recalc) на КАЖДОЙ видимой
      // ячейке этой строки - так эффект защиты виден прямо в таблице, а
      // не только по значку щита у названия. Смена только no_recalc (без
      // изменения суммы) никогда не может увести план накоплений в минус,
      // поэтому здесь не нужно обрабатывать отказ сервера по этой причине.
      const catPlan = state.plan[categoryId] || (state.plan[categoryId] = {});
      for (const mk of state.months) {
        const cell = catPlan[mk] || { amount: 0, no_recalc: false };
        if (cell.no_recalc === newValue) continue;
        const result = await saveCategoryPlanCell(categoryId, mk, cell.amount, newValue);
        if (result.ok) {
          catPlan[mk] = { amount: cell.amount, no_recalc: newValue };
        }
      }

      renderCategoryRows();
      showToast(
        newValue
          ? "Категория защищена, замки 🔒 проставлены на все месяцы."
          : "Защита снята, замки убраны."
      );
    } catch (e) {
      showToast("Не удалось связаться с сервером.");
    }
  }

  async function clearCategoryRow(categoryId, name) {
    const ok = await showConfirmDialog(`Обнулить лимиты категории «${name}» на все видимые месяцы?`);
    if (!ok) return;

    const catPlan = state.plan[categoryId] || (state.plan[categoryId] = {});
    let cleared = 0;

    for (const mk of state.months) {
      const cell = catPlan[mk] || { amount: 0, no_recalc: false };
      if (!cell.amount) continue;
      // Обнуление траты может только увеличить накопления, поэтому здесь
      // сервер никогда не откажет - защита от негативного сценария не
      // нужна, но всё равно проверяем result.ok на случай сбоя сети.
      const result = await saveCategoryPlanCell(categoryId, mk, 0, cell.no_recalc);
      if (result.ok) {
        catPlan[mk] = { amount: 0, no_recalc: cell.no_recalc };
        state._unsaved.delete(`category:${categoryId}:${mk}`);
        cleared++;
      }
    }

    renderCategoryRows();
    renderSummaryRows();
    if (cleared > 0) showToast(`Обнулено ${cleared} мес.`);
  }

  async function deleteCategory(categoryId, name) {
    const ok = await showConfirmDialog(`Удалить категорию «${name}» и весь план по ней?`);
    if (!ok) return;

    try {
      const res = await apiSend("DELETE", `/api/categories/${categoryId}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showToast(data.message || "Не удалось удалить категорию.");
        return;
      }
      state.categories = state.categories.filter((c) => String(c.id) !== String(categoryId));
      delete state.plan[categoryId];
      renderCategoryRows();
      renderSummaryRows();
    } catch (e) {
      showToast("Не удалось связаться с сервером.");
    }
  }

  // -------------------------------------------------------------------
  // Начальные накопления (отдельное поле, не в таблице)
  // -------------------------------------------------------------------

  function bindInitialSavingsInput() {
    const input = document.getElementById("initial-savings-input");

    input.addEventListener("focus", () => {
      const raw = parseNum(input.value);
      input.value = raw === 0 ? "" : String(raw);
      input.select();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") input.blur();
    });
    input.addEventListener("blur", async () => {
      const amount = parseNum(input.value);
      input.value = formatNum(amount);
      if (amount === (Number(state.initial_savings) || 0)) return;

      state.initial_savings = amount;
      renderSummaryRows();

      try {
        const res = await apiSend("PUT", "/api/initial-savings", { amount });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          state._unsaved.add("initial_savings");
          showToast(data.message || "Не сохранено: план накоплений уходит в минус. Видно только у вас.");
          renderSummaryRows();
          return;
        }
        state._unsaved.delete("initial_savings");
        renderSummaryRows();
      } catch (e) {
        showToast("Не удалось связаться с сервером.");
      }
    });
  }

  // -------------------------------------------------------------------
  // Пролистывание окна на 12 месяцев (п.11)
  // -------------------------------------------------------------------

  async function navigateWindow(deltaMonths) {
    const base = state.months[0];
    if (!base) return;
    const newStart = shiftMonthIso(base, deltaMonths);
    const ok = await loadPlan(newStart);
    if (ok) render();
  }

  function bindNavButtons() {
    document.getElementById("prev-window-btn").addEventListener("click", () => navigateWindow(-1));
    document.getElementById("next-window-btn").addEventListener("click", () => navigateWindow(1));
    document.getElementById("today-window-btn").addEventListener("click", async () => {
      const ok = await loadPlan();
      if (ok) render();
    });
  }

  // -------------------------------------------------------------------
  // Модальное окно вместо window.prompt()/window.confirm() (см. п.1) -
  // нативные диалоги в некоторых встроенных webview (в частности, судя по
  // симптомам, в Telegram Desktop) после закрытия иногда оставляют
  // страницу в состоянии, когда обычные поля ввода перестают ловить фокус
  // до следующего переключения окон. Для да/нет дополнительно
  // используется родной Telegram.WebApp.showConfirm, если доступен - он
  // реализован самим Telegram, а не веб-движком, и не подвержен этой проблеме.
  // -------------------------------------------------------------------

  let modalResolver = null;

  function openModal(message, withInput, placeholder) {
    return new Promise((resolve) => {
      modalResolver = resolve;
      const overlay = document.getElementById("modal-overlay");
      const msgEl = document.getElementById("modal-message");
      const inputEl = document.getElementById("modal-input");

      msgEl.textContent = message;
      if (withInput) {
        inputEl.classList.remove("hidden");
        inputEl.placeholder = placeholder || "";
        inputEl.value = "";
      } else {
        inputEl.classList.add("hidden");
      }

      overlay.classList.remove("hidden");
      if (withInput) setTimeout(() => inputEl.focus(), 50);
    });
  }

  function closeModal(result) {
    document.getElementById("modal-overlay").classList.add("hidden");
    if (modalResolver) {
      const resolve = modalResolver;
      modalResolver = null;
      resolve(result);
    }
  }

  function bindModal() {
    const overlay = document.getElementById("modal-overlay");
    const inputEl = document.getElementById("modal-input");

    document.getElementById("modal-cancel-btn").addEventListener("click", () => closeModal(false));
    document.getElementById("modal-confirm-btn").addEventListener("click", () => {
      if (!inputEl.classList.contains("hidden")) {
        closeModal(inputEl.value.trim() || false);
      } else {
        closeModal(true);
      }
    });
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") document.getElementById("modal-confirm-btn").click();
    });
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeModal(false);
    });
  }

  async function showTextPrompt(message, placeholder) {
    const result = await openModal(message, true, placeholder);
    return result === false ? null : result;
  }

  function showConfirmDialog(message) {
    if (tg && typeof tg.showConfirm === "function") {
      return new Promise((resolve) => tg.showConfirm(message, (ok) => resolve(!!ok)));
    }
    return openModal(message, false).then((result) => result === true);
  }

  // -------------------------------------------------------------------
  // Тосты, тема Telegram, полноэкранный режим
  // -------------------------------------------------------------------

  let toastTimer = null;
  function showToast(text) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  function applyThemeVars() {
    if (!tg || !tg.themeParams) return;
    const root = document.documentElement.style;
    const map = {
      bg_color: "--tg-theme-bg-color",
      secondary_bg_color: "--tg-theme-secondary-bg-color",
      text_color: "--tg-theme-text-color",
      hint_color: "--tg-theme-hint-color",
      link_color: "--tg-theme-link-color",
      button_color: "--tg-theme-button-color",
      button_text_color: "--tg-theme-button-text-color",
    };
    for (const key in map) {
      if (tg.themeParams[key]) root.setProperty(map[key], tg.themeParams[key]);
    }
  }

  // НОВОЕ (п.8): просим максимально доступную площадь под Mini App - на
  // телефоне expand() обычно и так растягивает на весь экран, а вот на
  // Desktop без requestFullscreen() (Bot API 8.0+) окно нередко остаётся
  // маленьким. Полностью управлять размером ОС-окна Telegram Desktop
  // снаружи нельзя - это максимум, что можно запросить через API.
  function requestMaxSize() {
    if (!tg) return;
    tg.expand();
    if (typeof tg.requestFullscreen === "function") {
      try {
        tg.requestFullscreen();
      } catch (e) {
        // Старые версии клиента могут не поддерживать метод - тихо игнорируем.
      }
    }
  }

  // -------------------------------------------------------------------
  // Инициализация
  // -------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", async () => {
    if (tg) {
      tg.ready();
      requestMaxSize();
      applyThemeVars();
      if (tg.onEvent) tg.onEvent("themeChanged", applyThemeVars);
    }

    if (!tg || !tg.initData) {
      showFatalError(
        "Эта страница открывается только через кнопку «📅 План на год» в Telegram, " +
        "а не по прямой ссылке в обычном браузере."
      );
      return;
    }

    bindInitialSavingsInput();
    bindNavButtons();
    bindModal();

    const ok = await loadPlan();
    if (ok) {
      render();
      document.getElementById("loading").classList.add("hidden");
      document.getElementById("content").classList.remove("hidden");
    }
  });
})();
