/* webapp/plan.js
 * ================
 * Логика Telegram Mini App "План на год". Работает поверх HTML-таблицы из
 * plan.html: рисует строку зарплаты, строки категорий (с возможностью
 * добавлять/переименовывать/удалять/защищать прямо тут) и три итоговые
 * строки снизу (расходы всего, накопления за месяц, план накоплений
 * нарастающим итогом).
 *
 * Все изменения сохраняются сразу по blur/Enter в конкретной ячейке через
 * HTTP API (services/webapp_api.py). Сервер - источник истины по вопросу
 * "можно ли сохранить": если после правки план накоплений уходит в минус
 * хоть в одном месяце, сервер отвечает 409 и ничего не пишет в БД - эта
 * функция откатывает отображаемое значение и подсвечивает ячейку.
 */

(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

  // Состояние целиком приходит с сервера через GET /api/plan и правится
  // локально по мере успешных сохранений - см. loadPlan()/render().
  let state = {
    months: [],
    categories: [],
    salary: {},
    plan: {},
    initial_savings: 0,
    cumulative: {},
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
      if (data.cumulative) state.cumulative = data.cumulative;
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
      if (data.cumulative) state.cumulative = data.cumulative;
      return { ok: true };
    } catch (e) {
      return { ok: false, message: "Не удалось связаться с сервером." };
    }
  }

  // -------------------------------------------------------------------
  // Форматирование чисел и месяцев
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

  // -------------------------------------------------------------------
  // Загрузка плана
  // -------------------------------------------------------------------

  async function loadPlan() {
    try {
      const res = await apiGet("/api/plan");
      const rawText = await res.text();
      let data = {};
      try {
        data = rawText ? JSON.parse(rawText) : {};
      } catch (parseErr) {
        // Ответ пришёл не в JSON - скорее всего это НЕ наш aiohttp-сервер,
        // а промежуточный узел (например, страница ошибки от Railway),
        // т.е. запрос вообще не дошёл до services/webapp_api.py.
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

    const th0 = document.createElement("th");
    th0.className = "name-col";
    th0.textContent = "Категория";
    row.appendChild(th0);

    for (const mk of state.months) {
      const th = document.createElement("th");
      th.className = "month-col";
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

    state.months.forEach((mk, idx) => {
      const td = document.createElement("td");
      td.className = "month-col";

      const wrap = document.createElement("div");
      wrap.className = "cell-wrap";

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
      shieldBtn.title = "Защитить категорию от пересчёта во всех месяцах";
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

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "icon-btn danger";
      delBtn.title = "Удалить категорию";
      delBtn.textContent = "✕";
      delBtn.addEventListener("click", () => deleteCategory(cat.id, cat.name));

      nameWrap.appendChild(shieldBtn);
      nameWrap.appendChild(nameInput);
      nameWrap.appendChild(delBtn);
      nameTd.appendChild(nameWrap);
      tr.appendChild(nameTd);

      state.months.forEach((mk, idx) => {
        const catPlan = state.plan[cat.id] || {};
        const cellData = catPlan[mk] || { amount: 0, no_recalc: false };

        const td = document.createElement("td");
        td.className = "month-col";

        const wrap = document.createElement("div");
        wrap.className = "cell-wrap" + (cellData.no_recalc ? " locked" : "");

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
    td.colSpan = state.months.length + 1;

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

    body.appendChild(buildSummaryRow("Расходы всего", totalExpense, "total-expense-row", false));
    body.appendChild(buildSummaryRow("Накопления", savings, "savings-row", true));
    body.appendChild(buildSummaryRow("План накоплений", cumulative, "cumulative-row", true));

    updateCards(cumulative);
    updateBanner(cumulative);
  }

  function buildSummaryRow(label, valuesByMonth, cls, colorize) {
    const tr = document.createElement("tr");
    tr.className = cls;

    const nameTd = document.createElement("td");
    nameTd.className = "name-col";
    nameTd.textContent = label;
    tr.appendChild(nameTd);

    for (const mk of state.months) {
      const td = document.createElement("td");
      td.className = "month-col";
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
  }

  function updateBanner(cumulative) {
    const banner = document.getElementById("banner");
    const negativeMonths = state.months.filter((mk) => (cumulative[mk] || 0) < 0);

    if (negativeMonths.length === 0) {
      banner.classList.add("hidden");
      banner.textContent = "";
      return;
    }

    const labels = negativeMonths.map(monthLabel).join(", ");
    banner.textContent = `⚠️ План накоплений уходит в минус: ${labels}. Уменьшите траты, увеличьте зарплату или начальные накопления в этих месяцах.`;
    banner.classList.remove("hidden");
  }

  // -------------------------------------------------------------------
  // Редактирование ячеек с суммами (зарплата / план по категории)
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

    wrap.classList.add("pending");
    wrap.classList.remove("rejected");

    // "Заполнение первой колонки" - если это первый месяц окна и во всех
    // остальных месяцах строки ещё ничего не задано, после успешного
    // сохранения размножим значение на всю строку (см. п.2 требований).
    const isFirstColumn = index === 0;
    const otherMonthsEmpty =
      isFirstColumn && state.months.slice(1).every((mk) => !(state.salary[mk] || 0));

    const result = await saveSalaryCell(month, amount);
    wrap.classList.remove("pending");

    if (!result.ok) {
      wrap.classList.add("rejected");
      showToast(result.message || "Не удалось сохранить: план накоплений уходит в минус.");
      input.value = formatNum(previous);
      if (result.cumulative) {
        state.cumulative = result.cumulative;
        renderSummaryRows();
      }
      return;
    }

    state.salary[month] = amount;

    if (isFirstColumn && otherMonthsEmpty && amount > 0) {
      await fillSalaryRow(amount, month);
    }

    renderSummaryRows();
  }

  async function fillSalaryRow(amount, skipMonth) {
    let filled = 0;
    for (const mk of state.months) {
      if (mk === skipMonth) continue;
      const result = await saveSalaryCell(mk, amount);
      if (!result.ok) {
        showToast("Заполнил не все месяцы: дальше план накоплений ушёл бы в минус.");
        break;
      }
      state.salary[mk] = amount;
      const cellInput = document.querySelector(
        `input.amount[data-kind="salary"][data-month="${mk}"]`
      );
      if (cellInput) cellInput.value = formatNum(amount);
      filled++;
    }
    if (filled > 0) showToast(`Зарплата заполнена на ${filled + 1} мес.`);
  }

  async function handleCategoryEdit(input, categoryId, month, amount, index) {
    const wrap = input.closest(".cell-wrap");
    const catPlan = state.plan[categoryId] || {};
    const cell = catPlan[month] || { amount: 0, no_recalc: false };
    const previous = cell.amount || 0;
    if (amount === previous) return;

    wrap.classList.add("pending");
    wrap.classList.remove("rejected");

    const isFirstColumn = index === 0;
    const rowValues = state.months.map((mk) => ((catPlan[mk] || {}).amount) || 0);
    const otherMonthsEmpty = isFirstColumn && rowValues.slice(1).every((v) => !v);

    const result = await saveCategoryPlanCell(categoryId, month, amount, cell.no_recalc);
    wrap.classList.remove("pending");

    if (!result.ok) {
      wrap.classList.add("rejected");
      showToast(result.message || "Не удалось сохранить: план накоплений уходит в минус.");
      input.value = formatNum(previous);
      if (result.cumulative) {
        state.cumulative = result.cumulative;
        renderSummaryRows();
      }
      return;
    }

    if (!state.plan[categoryId]) state.plan[categoryId] = {};
    state.plan[categoryId][month] = { amount, no_recalc: cell.no_recalc };

    if (isFirstColumn && otherMonthsEmpty && amount > 0) {
      await fillCategoryRow(categoryId, amount, month, cell.no_recalc);
    }

    renderSummaryRows();
  }

  async function fillCategoryRow(categoryId, amount, skipMonth, noRecalc) {
    let filled = 0;
    for (const mk of state.months) {
      if (mk === skipMonth) continue;
      const result = await saveCategoryPlanCell(categoryId, mk, amount, noRecalc);
      if (!result.ok) {
        showToast("Заполнил не все месяцы: дальше план накоплений ушёл бы в минус.");
        break;
      }
      state.plan[categoryId][mk] = { amount, no_recalc: noRecalc };
      const cellInput = document.querySelector(
        `input.amount[data-kind="category"][data-category-id="${categoryId}"][data-month="${mk}"]`
      );
      if (cellInput) cellInput.value = formatNum(amount);
      filled++;
    }
    if (filled > 0) showToast(`Заполнено на ${filled + 1} мес.`);
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
  // Категории: добавление / переименование / защита / удаление
  // -------------------------------------------------------------------

  async function addCategory() {
    const name = (window.prompt("Название новой категории:") || "").trim();
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
      renderCategoryRows();
    } catch (e) {
      showToast("Не удалось связаться с сервером.");
    }
  }

  async function deleteCategory(categoryId, name) {
    if (!window.confirm(`Удалить категорию «${name}» и весь план по ней?`)) return;

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

      try {
        const res = await apiSend("PUT", "/api/initial-savings", { amount });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          showToast(data.message || "Не удалось сохранить: план накоплений уходит в минус.");
          input.value = formatNum(state.initial_savings);
          if (data.cumulative) {
            state.cumulative = data.cumulative;
            renderSummaryRows();
          }
          return;
        }
        state.initial_savings = amount;
        if (data.cumulative) state.cumulative = data.cumulative;
        renderSummaryRows();
      } catch (e) {
        showToast("Не удалось связаться с сервером.");
        input.value = formatNum(state.initial_savings);
      }
    });
  }

  // -------------------------------------------------------------------
  // Тосты, тема Telegram
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

  // -------------------------------------------------------------------
  // Инициализация
  // -------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", async () => {
    if (tg) {
      tg.ready();
      tg.expand();
      applyThemeVars();
      if (tg.onEvent) tg.onEvent("themeChanged", applyThemeVars);
    }

    bindInitialSavingsInput();

    const ok = await loadPlan();
    if (ok) {
      render();
      document.getElementById("loading").classList.add("hidden");
      document.getElementById("content").classList.remove("hidden");
    }
  });
})();
