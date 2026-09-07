const state = {
  user: null,
  accounts: [],
  categories: [],
  forecast: [],
  forecastFloor: 0,
  report: null,
  dashboard: null,
  captureDraft: null,
  captureAudio: null,
  captureRecorder: null,
  advisorHistory: [],
};
const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const compactMoney = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", notation: "compact", maximumFractionDigits: 1 });
const dateFormat = new Intl.DateTimeFormat("pt-BR", { timeZone: "UTC" });
const monthFormat = new Intl.DateTimeFormat("pt-BR", { month: "long", year: "numeric", timeZone: "UTC" });
const accountTypeLabels = { checking: "Conta corrente", credit_card: "Cartão de crédito", investment: "Conta de liquidez / investimento", cash: "Dinheiro", other: "Outros" };

function largeEntryThreshold() {
  return Math.max(5000, Number(state.dashboard?.cash_cap || 0) * 2);
}

function selectedLargeTransactions(items) {
  const threshold = largeEntryThreshold();
  return items.filter((item) => item.kind !== "obligation" && item.kind !== "payroll" && Number(item.amount || 0) >= threshold);
}

function confirmLargeTransactions(items) {
  const largeItems = selectedLargeTransactions(items);
  if (!largeItems.length) return { allowed: true, confirmed: false };
  const largest = Math.max(...largeItems.map((item) => Number(item.amount || 0)));
  const allowed = window.confirm(
    `Atenção: há movimentação de ${money.format(largest)}. Confirme somente se ela realmente aconteceu. Para testar uma compra, use o Consultor financeiro.`,
  );
  return { allowed, confirmed: allowed };
}
const systemCategories = new Set(["Conciliação", "Transferência patrimonial", "Transferência interna", "Repasses a confirmar", "Reembolsos e estornos", "Receitas", "Revisar"]);
const pageNames = {
  dashboard: "Visão geral",
  reports: "Relatórios e análises",
  capture: "Lançar agora",
  entradas: "Entradas",
  saidas: "Saídas",
  payables: "Contas a pagar",
  transferencias: "Transferências",
  imports: "Importações",
  transactions: "Lançamentos",
  reviews: "Revisar",
  income: "Rendas",
  planning: "Planejamento",
  advisor: "Consultor financeiro",
  integrity: "Integridade",
  users: "Acessos",
  settings: "Configurações",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toast(message, error = false) {
  const item = document.querySelector("#toast");
  item.textContent = message;
  item.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { item.className = "toast"; }, 3600);
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    showAuth(true);
    throw new Error("Sua sessão expirou. Entre novamente.");
  }
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    const detail = typeof payload.detail === "string"
      ? payload.detail
      : Array.isArray(payload.detail)
        ? payload.detail.map((item) => item.msg).filter(Boolean).join("; ")
        : payload.detail && Array.isArray(payload.detail.reasons)
          // Structured gate-failure detail, e.g. POST .../monthly-closes/{period}/trust:
          // {"message": "...", "reasons": ["...", "..."]}.
          ? [payload.detail.message, ...payload.detail.reasons].filter(Boolean).join("; ")
          : "Não foi possível concluir a operação";
    throw new Error(detail);
  }
  return payload;
}

function formJson(form, numericFields = []) {
  const data = Object.fromEntries(new FormData(form).entries());
  numericFields.forEach((field) => {
    if (field in data) data[field] = Number(data[field] || 0);
  });
  Object.keys(data).forEach((key) => { if (data[key] === "") data[key] = null; });
  return data;
}

function enhanceResponsiveTables(root = document) {
  root.querySelectorAll("table").forEach((table) => {
    const labels = [...table.querySelectorAll("thead th")].map((header) => header.textContent.trim());
    if (!labels.length) return;
    table.classList.add("responsive-table");
    table.querySelectorAll("tbody tr").forEach((row) => {
      [...row.children].forEach((cell, index) => {
        if (cell.matches("td") && !cell.hasAttribute("colspan")) {
          cell.dataset.label = labels[index] || "Detalhe";
        }
      });
    });
  });
}

function setMobileMenu(open) {
  const sidebar = document.querySelector(".sidebar");
  const toggle = document.querySelector("#mobile-menu-toggle");
  if (!sidebar || !toggle) return;
  sidebar.classList.toggle("menu-open", open);
  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", open ? "Fechar menu" : "Abrir menu");
  toggle.textContent = open ? "×" : "☰";
}

function showAuth(configured) {
  document.querySelector("#app-shell").classList.add("hidden");
  document.querySelector("#auth-shell").classList.remove("hidden");
  document.querySelector("#login-form").classList.toggle("hidden", !configured);
  document.querySelector("#setup-form").classList.toggle("hidden", configured);
}

function integrityUiEnabled() {
  return document.body.dataset.integrityUiEnabled === "true";
}

async function showApp() {
  document.querySelector("#auth-shell").classList.add("hidden");
  document.querySelector("#app-shell").classList.remove("hidden");
  document.querySelector("#current-user").textContent = state.user.name;
  document.querySelector("#nav-users").classList.toggle("hidden", !state.user.is_admin);
  document.querySelector("#nav-integrity").classList.toggle("hidden", !integrityUiEnabled());
  await Promise.all([loadAccounts(), loadCategories()]);
  await navigate("dashboard");
  if (integrityUiEnabled()) await refreshIntegrityBanner();
}

async function bootstrap() {
  const configured = document.body.dataset.configured === "true";
  if (!configured) return showAuth(false);
  try {
    state.user = await api("/auth/me");
    await showApp();
  } catch (_) {
    showAuth(true);
  }
}

async function navigate(view) {
  setMobileMenu(false);
  document.querySelectorAll(".view").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll("#main-nav button").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  document.querySelector(`#view-${view}`).classList.add("active");
  document.querySelector("#page-title").textContent = pageNames[view];
  const loaders = {
    dashboard: loadDashboard,
    reports: loadReports,
    capture: loadCapture,
    entradas: loadEntradas,
    saidas: loadSaidas,
    payables: loadPayables,
    transferencias: loadTransferencias,
    imports: loadImports,
    transactions: loadTransactions,
    reviews: loadReviews,
    income: loadIncome,
    planning: loadForecast,
    advisor: loadAdvisor,
    integrity: loadIntegrity,
    users: loadUsers,
    settings: loadProfile,
  };
  try { await loaders[view]?.(); } catch (error) { toast(error.message, true); }
  if (integrityUiEnabled() && view !== "integrity") {
    try { await refreshIntegrityBanner(); } catch (_) { /* banner is best-effort */ }
  }
}

async function loadAccounts() {
  state.accounts = await api("/accounts");
  const select = document.querySelector("#import-account");
  select.innerHTML = state.accounts.length
    ? state.accounts.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("")
    : '<option value="">Cadastre uma conta primeiro</option>';
  const accountOptions = state.accounts.length
    ? state.accounts.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("")
    : '<option value="">Cadastre uma conta primeiro</option>';
  document.querySelector("#transaction-account").innerHTML = accountOptions;
  document.querySelector("#income-entry-account").innerHTML = accountOptions;
  document.querySelector("#expense-entry-account").innerHTML = accountOptions;
  document.querySelector("#transfer-from-account").innerHTML = accountOptions;
  document.querySelector("#transfer-to-account").innerHTML = accountOptions;
  // Payment of a card invoice only ever reconciles against a `checking`
  // account -- the same direction contract `pay_card_invoice` enforces
  // server-side; the UI never offers an account the backend would reject.
  const checkingAccounts = state.accounts.filter((item) => item.account_type === "checking");
  document.querySelector("#pay-invoice-account").innerHTML = checkingAccounts.length
    ? checkingAccounts.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("")
    : '<option value="">Cadastre uma conta corrente primeiro</option>';
  updateExpenseCompetenceField();
  const captureSelect = document.querySelector("#capture-account");
  if (captureSelect) {
    const selected = captureSelect.value;
    captureSelect.innerHTML = '<option value="">Escolher na prévia</option>' + state.accounts.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("");
    if ([...captureSelect.options].some((option) => option.value === selected)) captureSelect.value = selected;
  }
}

async function loadCategories() {
  state.categories = await api("/categories");
  document.querySelector("#expense-entry-category").innerHTML = manualCategoryOptions();
}

function emptyRow(columns, text = "Nenhum registro encontrado") {
  return `<tr><td colspan="${columns}" class="empty">${escapeHtml(text)}</td></tr>`;
}

function currentMonthKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function currentDateKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function shiftMonth(month, offset) {
  const [year, value] = month.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, value - 1 + offset, 1));
  return `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}`;
}

function monthLabel(month) {
  const label = monthFormat.format(new Date(`${month}-01T00:00:00Z`));
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function shortMonthLabel(month) {
  const [name, year] = monthLabel(month).split(" de ");
  return `${name.slice(0, 3)}/${String(year).slice(-2)}`;
}

function safeColor(value) {
  return /^#[0-9a-f]{6}$/i.test(String(value || "")) ? value : "#64748B";
}

function changeBadge(value) {
  if (value === null || value === undefined) return { label: "Sem comparação", tone: "neutral" };
  if (Math.abs(value) < 0.01) return { label: "Estável", tone: "neutral" };
  const direction = value > 0 ? "↑" : "↓";
  return {
    label: `${direction} ${Math.abs(value).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}% vs mês anterior`,
    tone: value > 0 ? "bad" : "good",
  };
}

function setTrendBadge(element, value) {
  const badge = changeBadge(value);
  element.textContent = badge.label;
  element.className = `trend-badge ${badge.tone}`;
}

function renderTrendChart(container, rows, compact = false) {
  if (!rows.length || !rows.some((item) => item.transaction_count > 0)) {
    container.innerHTML = '<div class="empty chart-empty">Ainda não há meses suficientes para desenhar a evolução.</div>';
    return;
  }
  const width = 760;
  const height = compact ? 190 : 260;
  const padding = { top: 22, right: 20, bottom: 38, left: compact ? 18 : 64 };
  const values = rows.flatMap((item) => [Number(item.spending || 0), Number(item.cash_cap || 0)]);
  const max = Math.max(...values, 1) * 1.12;
  const usableWidth = width - padding.left - padding.right;
  const usableHeight = height - padding.top - padding.bottom;
  const x = (index) => padding.left + (rows.length === 1 ? usableWidth / 2 : index * (usableWidth / (rows.length - 1)));
  const y = (value) => padding.top + usableHeight - (Number(value || 0) / max) * usableHeight;
  const points = rows.map((item, index) => `${x(index)},${y(item.spending)}`).join(" ");
  const area = `${padding.left},${padding.top + usableHeight} ${points} ${x(rows.length - 1)},${padding.top + usableHeight}`;
  const gradientId = `trend-${container.id}`;
  const grid = [0, .25, .5, .75, 1].map((step) => {
    const gridY = padding.top + usableHeight * step;
    const value = max * (1 - step);
    return `<line x1="${padding.left}" y1="${gridY}" x2="${width - padding.right}" y2="${gridY}" class="chart-grid-line" />${compact ? "" : `<text x="${padding.left - 9}" y="${gridY + 4}" text-anchor="end" class="chart-axis-label">${escapeHtml(compactMoney.format(value))}</text>`}`;
  }).join("");
  const cap = rows[rows.length - 1].cash_cap || 0;
  const capLine = cap > 0 ? `<line x1="${padding.left}" y1="${y(cap)}" x2="${width - padding.right}" y2="${y(cap)}" class="chart-cap-line" />` : "";
  const labels = rows.map((item, index) => `<text x="${x(index)}" y="${height - 12}" text-anchor="middle" class="chart-month-label">${escapeHtml(shortMonthLabel(item.month))}</text>`).join("");
  const dots = rows.map((item, index) => `<circle cx="${x(index)}" cy="${y(item.spending)}" r="${compact ? 4 : 5}" class="chart-dot"><title>${escapeHtml(monthLabel(item.month))}: ${escapeHtml(money.format(item.spending))}</title></circle>`).join("");
  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Evolução mensal dos gastos">
    <defs><linearGradient id="${gradientId}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6C63E8" stop-opacity=".34"/><stop offset="1" stop-color="#6C63E8" stop-opacity=".02"/></linearGradient></defs>
    ${grid}${capLine}<polygon points="${area}" fill="url(#${gradientId})"/><polyline points="${points}" class="chart-trend-line"/>${dots}${labels}
  </svg>`;
}

function renderDashboardPulse(report) {
  renderTrendChart(document.querySelector("#dashboard-trend-chart"), report.monthly, true);
  setTrendBadge(document.querySelector("#dashboard-trend-badge"), report.summary.last_change_percentage);
  const top = report.categories[0];
  document.querySelector("#dashboard-top-category").textContent = top ? top.category : "Sem dados";
  document.querySelector("#dashboard-top-category-detail").textContent = top
    ? `${money.format(top.amount)} • ${top.share.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}% dos gastos do período`
    : "Importe lançamentos para formar a análise.";
  document.querySelector("#dashboard-average-spending").textContent = money.format(report.summary.average_spending);
  document.querySelector("#dashboard-average-detail").textContent = `${report.covered_months} de ${report.months} meses possuem movimentações.`;
}

async function loadDashboard() {
  const monthControl = document.querySelector("#dashboard-month");
  if (!monthControl.value) monthControl.value = currentMonthKey();
  const selectedMonth = monthControl.value;
  const [summary, transactions, cutPlan, pulse] = await Promise.all([
    api(`/dashboard?month=${encodeURIComponent(selectedMonth)}`),
    api(`/transactions?limit=8&month=${encodeURIComponent(selectedMonth)}`),
    api(`/cut-plan?month=${encodeURIComponent(selectedMonth)}`),
    api(`/reports?end_month=${encodeURIComponent(selectedMonth)}&months=6`),
  ]);
  const selectedLabel = monthLabel(summary.month);
  state.dashboard = summary;
  monthControl.value = summary.month;
  document.querySelector("#dashboard-period-label").textContent = selectedLabel;
  document.querySelector("#monthly-categories-title").textContent = `Gastos de ${selectedLabel} por categoria`;
  const liquidityName = summary.liquidity_name || "Privilège DI";
  const liquidityStarting = Number(summary.liquidity_starting_balance || 0);
  const liquidityClosing = Number(summary.liquidity_closing_balance || summary.liquidity_balance || 0);
  const liquidityWithdrawal = Number(summary.liquidity_withdrawal || 0);
  const liquidityDeposit = Number(summary.liquidity_deposit || 0);
  const liquidityUncovered = Number(summary.liquidity_uncovered_deficit || 0);
  document.querySelector("#kpi-liquidity-name").textContent = `Saldo após fechamento no ${liquidityName}`;
  document.querySelector("#kpi-investment").textContent = money.format(summary.liquidity_balance);
  document.querySelector("#kpi-liquidity-caption").textContent = liquidityUncovered > 0
    ? `Saldo informado de ${money.format(liquidityStarting)} totalmente consumido • faltam ${money.format(liquidityUncovered)}`
    : liquidityWithdrawal > 0
      ? `Saldo informado ${money.format(liquidityStarting)} • retirada de ${money.format(liquidityWithdrawal)}`
      : liquidityDeposit > 0
        ? `Saldo informado ${money.format(liquidityStarting)} + sobra de ${money.format(liquidityDeposit)}`
        : `Saldo informado ${money.format(liquidityStarting)} • sem movimentação líquida`;
  document.querySelector("#liquidity-kpi-card").classList.toggle(
    "under-floor",
    liquidityClosing < Number(summary.emergency_floor || 0) || liquidityUncovered > 0,
  );
  document.querySelector("#kpi-cash-in").textContent = money.format(summary.cash_in);
  document.querySelector("#kpi-cash-out").textContent = money.format(summary.bank_cash_out);
  document.querySelector("#kpi-cash-out-caption").textContent = `Cartões: ${money.format(summary.card_spending)} • compromissos totais: ${money.format(summary.cash_out)}`;
  document.querySelector("#kpi-spending").textContent = money.format(summary.spending);
  document.querySelector("#kpi-cap-caption").textContent = `de ${money.format(summary.cash_cap)} em ${selectedLabel}`;
  const remainingValue = document.querySelector("#kpi-remaining");
  remainingValue.textContent = money.format(summary.remaining_cap);
  remainingValue.classList.toggle("amount-expense", summary.remaining_cap < 0);
  document.querySelector("#remaining-cap-card").classList.toggle("over-budget", summary.remaining_cap < 0);
  document.querySelector("#kpi-remaining-caption").textContent = summary.remaining_cap < 0
    ? `Teto ultrapassado em ${money.format(Math.abs(summary.remaining_cap))}`
    : "Valor ainda disponível no limite mensal";
  document.querySelector("#kpi-reviews").textContent = summary.review_count;
  document.querySelector("#nav-review-count").textContent = summary.review_count;
  document.querySelector("#food-benefits").textContent = money.format(summary.food_benefits);
  const percent = summary.cash_cap > 0 ? Math.round((summary.spending / summary.cash_cap) * 100) : 0;
  const width = Math.min(percent, 100);
  const bar = document.querySelector("#budget-progress");
  bar.style.width = `${width}%`;
  bar.classList.toggle("over", percent > 100);
  const percentPill = document.querySelector("#budget-percent");
  percentPill.textContent = `${percent}%`;
  percentPill.classList.toggle("over", percent > 100);
  document.querySelector("#budget-used").textContent = `${money.format(summary.spending)} usados`;
  document.querySelector("#budget-total").textContent = `${money.format(summary.cash_cap)} de teto`;
  const liquidityBridge = document.querySelector("#liquidity-bridge");
  const liquidityFlow = Number(summary.liquidity_flow || 0);
  const liquidityDirection = summary.liquidity_direction || "balanced";
  liquidityBridge.classList.remove("deposit", "withdrawal", "balanced");
  liquidityBridge.classList.add(liquidityDirection);
  liquidityBridge.querySelector(".liquidity-bridge-icon").textContent = liquidityDirection === "deposit" ? "↗" : liquidityDirection === "withdrawal" ? "↘" : "↔";
  if (liquidityUncovered > 0) {
    document.querySelector("#liquidity-bridge-label").textContent = `Saldo do ${liquidityName} esgotado`;
    document.querySelector("#liquidity-bridge-detail").textContent = `O resultado negativo de ${money.format(Math.abs(liquidityFlow))} consome todo o saldo informado. O piso de segurança é uma meta de alerta, não um valor bloqueado.`;
    document.querySelector("#liquidity-bridge-value-label").textContent = "Déficit sem cobertura";
    document.querySelector("#liquidity-bridge-value").textContent = money.format(liquidityUncovered);
    document.querySelector("#liquidity-bridge-balance").textContent = `Retirada de ${money.format(liquidityWithdrawal)} • saldo final ${money.format(liquidityClosing)}`;
  } else if (liquidityWithdrawal > 0) {
    document.querySelector("#liquidity-bridge-label").textContent = `Déficit de ${selectedLabel} coberto pelo ${liquidityName}`;
    document.querySelector("#liquidity-bridge-detail").textContent = "O resultado negativo é retirado da conta central de liquidez; o piso serve para sinalizar a necessidade de recomposição.";
    document.querySelector("#liquidity-bridge-value-label").textContent = "Retirada necessária";
    document.querySelector("#liquidity-bridge-value").textContent = money.format(liquidityWithdrawal);
    document.querySelector("#liquidity-bridge-balance").textContent = `Saldo informado ${money.format(liquidityStarting)} • saldo final ${money.format(liquidityClosing)}`;
  } else if (liquidityDeposit > 0) {
    document.querySelector("#liquidity-bridge-label").textContent = `Sobra de ${selectedLabel} destinada ao ${liquidityName}`;
    document.querySelector("#liquidity-bridge-detail").textContent = "Depois das receitas e compromissos registrados, a sobra aumenta a conta central de liquidez.";
    document.querySelector("#liquidity-bridge-value-label").textContent = "Valor para aplicar";
    document.querySelector("#liquidity-bridge-value").textContent = money.format(liquidityDeposit);
    document.querySelector("#liquidity-bridge-balance").textContent = `Saldo informado ${money.format(liquidityStarting)} • saldo final ${money.format(liquidityClosing)}`;
  } else {
    document.querySelector("#liquidity-bridge-label").textContent = `Resultado de ${selectedLabel} equilibrado`;
    document.querySelector("#liquidity-bridge-detail").textContent = "As receitas reais e os compromissos registrados se compensam no período.";
    document.querySelector("#liquidity-bridge-value-label").textContent = "Movimento líquido";
    document.querySelector("#liquidity-bridge-value").textContent = money.format(0);
    document.querySelector("#liquidity-bridge-balance").textContent = `Saldo final ${money.format(liquidityClosing)}`;
  }
  renderDashboardPulse(pulse);
  const qualityItems = [
    [summary.review_count === 0, "Fila de revisão", summary.review_count === 0 ? "Sem pendências" : `${summary.review_count} itens`],
    [state.accounts.length > 0, "Contas cadastradas", state.accounts.length ? `${state.accounts.length} fontes` : "Cadastre a primeira"],
    [summary.cash_cap > 0, "Perfil financeiro", summary.cash_cap > 0 ? "Premissas configuradas" : "Configuração pendente"],
  ];
  if (summary.duplicates_ignored > 0) qualityItems.unshift([true, "Cópias de gastos desconsideradas", `${summary.duplicates_ignored} cópias apareceram em mais de uma fonte; uma única versão entrou no total`]);
  document.querySelector("#quality-list").innerHTML = qualityItems.map(([ok, title, detail]) => `<div class="quality-item"><div><span class="quality-dot${ok ? "" : " warn"}"></span><strong>${escapeHtml(title)}</strong></div><small>${escapeHtml(detail)}</small></div>`).join("");
  const duplicateNote = document.querySelector("#monthly-duplicates-note");
  duplicateNote.classList.toggle("hidden", summary.duplicates_ignored === 0);
  duplicateNote.textContent = summary.duplicates_ignored > 0
    ? `${summary.duplicates_ignored} cópias do mesmo gasto apareceram em mais de uma fonte, por exemplo na planilha e na fatura. As cópias continuam guardadas para conferência, mas somente o registro original compõe os totais.`
    : "";
  document.querySelector("#cash-flow-accounts-table").innerHTML = summary.cash_flow_by_account.length
    ? summary.cash_flow_by_account.map((item) => `
      <tr><td><strong>${escapeHtml(item.account)}</strong></td><td>${escapeHtml(accountTypeLabels[item.account_type] || item.account_type)}</td><td class="right amount-income">${money.format(item.cash_in)}</td><td class="right amount-expense">${money.format(item.cash_out)}</td><td class="right">${money.format(item.refunds)}</td></tr>
    `).join("")
    : emptyRow(5, `Nenhuma movimentação operacional identificada em ${selectedLabel}`);
  document.querySelector("#dashboard-obligation-alerts").innerHTML = summary.obligation_alerts.length
    ? summary.obligation_alerts.map((item) => `
      <div class="obligation-alert ${escapeHtml(item.alert_level)}"><div><strong>${escapeHtml(item.name)}</strong><small>${dateFormat.format(new Date(`${item.next_due_date}T00:00:00Z`))} • ${escapeHtml(item.alert_label)}</small></div><span>${money.format(item.amount)}</span></div>
    `).join("")
    : '<div class="empty compact-empty">Nenhuma obrigação vence nos próximos 30 dias.</div>';
  document.querySelector("#monthly-categories-table").innerHTML = summary.category_spending.length
    ? summary.category_spending.map((item) => {
      const share = summary.spending > 0 ? Math.round((item.amount / summary.spending) * 100) : 0;
      return `<tr><td><strong>${escapeHtml(item.category)}</strong></td><td class="right">${share}%</td><td class="right amount-expense">${money.format(item.amount)}</td></tr>`;
    }).join("")
    : emptyRow(3, `Nenhum gasto considerado em ${selectedLabel}`);
  document.querySelector("#cut-plan-savings").textContent = money.format(cutPlan.potential_monthly_savings);
  document.querySelector("#cut-plan-period").textContent = cutPlan.covered_months
    ? `Histórico de ${monthLabel(cutPlan.analysis_start_month)} a ${monthLabel(cutPlan.analysis_end_month)} • ${cutPlan.covered_months} meses com dados • média de ${money.format(cutPlan.monthly_average)}`
    : "Importe os extratos e cartões para receber recomendações específicas";
  document.querySelector("#cut-plan-table").innerHTML = cutPlan.recommendations.length ? cutPlan.recommendations.slice(0, 8).map((item) => `
    <tr><td><span class="status-chip ${item.priority === "alta" ? "warn" : "muted"}">${escapeHtml(item.priority)}</span></td><td><strong>${escapeHtml(item.category)}</strong></td><td class="right">${money.format(item.average)}</td><td class="right">${money.format(item.target)}</td><td class="right amount-expense"><strong>${money.format(item.suggested_cut)}</strong></td><td><small>${escapeHtml(item.rationale)}</small></td></tr>
  `).join("") : emptyRow(6, cutPlan.covered_months ? "Os gastos analisados já estão dentro dos tetos definidos" : "Importe seus documentos para calcular os cortes");
  document.querySelector("#recent-transactions").innerHTML = transactions.length ? transactions.map((item) => `
    <tr><td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td><td>${escapeHtml(item.description)}</td><td><span class="status-chip">${escapeHtml(item.category)}</span></td><td>${escapeHtml(item.account)}</td><td class="right ${item.amount < 0 ? "amount-expense" : "amount-income"}">${money.format(item.amount)}</td></tr>
  `).join("") : emptyRow(5);
}

function reportInsight(icon, label, value, detail, tone = "") {
  return `<div class="report-insight ${tone}"><span class="report-insight-icon">${escapeHtml(icon)}</span><div><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong><p>${escapeHtml(detail)}</p></div></div>`;
}

function renderReport(report) {
  state.report = report;
  const singleMonth = report.months === 1;
  const periodText = singleMonth
    ? monthLabel(report.end_month)
    : `${monthLabel(report.start_month)} a ${monthLabel(report.end_month)}`;
  document.querySelector("#report-period-label").textContent = `${periodText} • ${report.covered_months} mês(es) com dados`;
  document.querySelector("#report-total-spending").textContent = money.format(report.summary.total_spending);
  document.querySelector("#report-total-caption").textContent = singleMonth ? "No mês escolhido" : `Acumulado em ${report.months} meses`;
  document.querySelector("#report-average-spending").textContent = money.format(report.summary.average_spending);
  document.querySelector("#report-total-income").textContent = money.format(report.summary.total_cash_in);
  const net = document.querySelector("#report-net-cash");
  net.textContent = money.format(report.summary.cash_net);
  net.classList.toggle("amount-expense", report.summary.cash_net < 0);
  net.classList.toggle("amount-income", report.summary.cash_net >= 0);
  document.querySelector("#report-liquidity-name").textContent = `Saldo após o período no ${report.summary.liquidity_name}`;
  document.querySelector("#report-liquidity-balance").textContent = money.format(report.summary.liquidity_balance);
  const reportLiquidityAvailable = document.querySelector("#report-liquidity-available");
  reportLiquidityAvailable.textContent = money.format(report.summary.liquidity_available);
  reportLiquidityAvailable.classList.toggle("amount-expense", report.summary.liquidity_available < 0);
  reportLiquidityAvailable.classList.toggle("amount-income", report.summary.liquidity_available >= 0);
  document.querySelector("#report-liquidity-floor").textContent = report.summary.liquidity_available >= 0
    ? `${money.format(report.summary.liquidity_available)} acima do piso de ${money.format(report.summary.emergency_floor)}`
    : `Faltam ${money.format(Math.abs(report.summary.liquidity_available))} para recompor o piso`;
  renderTrendChart(document.querySelector("#report-trend-chart"), report.monthly);
  setTrendBadge(document.querySelector("#report-change-badge"), report.summary.last_change_percentage);

  const top = report.categories[0];
  const change = changeBadge(report.summary.last_change_percentage);
  const insights = [
    reportInsight("↑", "MÊS DE MAIOR GASTO", monthLabel(report.summary.highest_month), money.format(report.summary.highest_spending), "coral"),
    reportInsight("↓", "MÊS DE MENOR GASTO", monthLabel(report.summary.lowest_month), money.format(report.summary.lowest_spending), "mint"),
    reportInsight("◎", "CATEGORIA PRINCIPAL", top ? top.category : "Sem dados", top ? `${money.format(top.amount)} no período` : "Nenhum gasto classificado", "violet"),
    report.summary.liquidity_uncovered_deficit > 0
      ? reportInsight("!", "DÉFICIT SEM COBERTURA", money.format(report.summary.liquidity_uncovered_deficit), `Todo o saldo informado de ${money.format(report.summary.liquidity_starting_balance)} foi consumido`, "bad")
      : report.summary.liquidity_withdrawal > 0
        ? reportInsight("↘", "RETIRADA DO PRIVILÈGE", money.format(report.summary.liquidity_withdrawal), `Saldo após o período: ${money.format(report.summary.liquidity_balance)}`, "bad")
        : reportInsight("↗", "SOBRA PARA O PRIVILÈGE", money.format(report.summary.liquidity_deposit), `Saldo após o período: ${money.format(report.summary.liquidity_balance)}`, "mint"),
  ];
  if (!singleMonth) {
    const changeIcon = report.summary.last_change_percentage === null
      ? "↔"
      : report.summary.last_change_percentage > 0 ? "↗" : "↘";
    insights.push(reportInsight(changeIcon, "ÚLTIMA VARIAÇÃO", change.label, "Comparação entre os dois últimos meses", change.tone));
  }
  document.querySelector("#report-insights").innerHTML = insights.join("");

  const maxCategory = Math.max(...report.categories.map((item) => item.amount), 1);
  document.querySelector("#report-category-ranking").innerHTML = report.categories.length
    ? report.categories.slice(0, 10).map((item, index) => `<div class="category-rank-item" style="--category-color:${safeColor(item.color)}">
      <div class="category-rank-heading"><span class="category-rank-number">${index + 1}</span><strong>${escapeHtml(item.category)}</strong><span>${money.format(item.amount)}</span></div>
      <div class="category-rank-track"><i style="width:${Math.max(3, (item.amount / maxCategory) * 100)}%"></i></div>
      <small>${item.share.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}% do período • média ${money.format(item.average)}/mês</small>
    </div>`).join("")
    : '<div class="empty compact-empty">Nenhuma categoria de gasto no período.</div>';

  document.querySelector("#report-accounts-table").innerHTML = report.accounts.length
    ? report.accounts.map((item) => `<tr><td><strong>${escapeHtml(item.account)}</strong></td><td>${escapeHtml(accountTypeLabels[item.account_type] || item.account_type)}</td><td class="right amount-income">${money.format(item.cash_in)}</td><td class="right amount-expense">${money.format(item.cash_out)}</td><td class="right ${item.net < 0 ? "amount-expense" : "amount-income"}">${money.format(item.net)}</td></tr>`).join("")
    : emptyRow(5, "Nenhum fluxo operacional no período");

  document.querySelector("#report-monthly-table").innerHTML = report.monthly.map((item) => {
    const variation = item.change_percentage === null
      ? "—"
      : `${item.change_percentage > 0 ? "+" : ""}${item.change_percentage.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;
    return `<tr><td><strong>${escapeHtml(monthLabel(item.month))}</strong><br><small>${item.transaction_count} movimentações</small></td><td class="right amount-income">${money.format(item.cash_in)}</td><td class="right amount-expense">${money.format(item.bank_cash_out)}</td><td class="right amount-expense">${money.format(item.card_spending)}</td><td class="right">${money.format(item.spending)}</td><td class="right ${item.cash_net < 0 ? "amount-expense" : "amount-income"}">${money.format(item.cash_net)}</td><td class="right ${item.remaining_cap < 0 ? "amount-expense" : "amount-income"}">${money.format(item.remaining_cap)}</td><td class="right ${item.change_percentage > 0 ? "amount-expense" : item.change_percentage < 0 ? "amount-income" : ""}">${variation}</td></tr>`;
  }).join("");
  document.querySelector("#report-data-quality").textContent = report.duplicates_ignored
    ? `${report.duplicates_ignored} cópia(s) entre fontes foram desconsideradas; os registros permanecem preservados para auditoria.`
    : "Nenhuma sobreposição entre fontes foi identificada neste período.";
}

async function loadReports() {
  const endControl = document.querySelector("#report-end-month");
  const monthsControl = document.querySelector("#report-months");
  if (!endControl.value) endControl.value = document.querySelector("#dashboard-month").value || currentMonthKey();
  const report = await api(`/reports?end_month=${encodeURIComponent(endControl.value)}&months=${encodeURIComponent(monthsControl.value)}`);
  renderReport(report);
}

// Downloads the backend-generated `.xlsx`/`.pdf` artifact for the report
// period currently selected on screen. This never parses, sums or formats
// a financial figure client-side -- it only requests the same canonical
// report `loadReports()` already displays, in a different file format, and
// hands the browser the exact bytes the backend returned.
async function downloadReportExport(format) {
  const endControl = document.querySelector("#report-end-month");
  const monthsControl = document.querySelector("#report-months");
  if (!endControl.value) endControl.value = document.querySelector("#dashboard-month").value || currentMonthKey();
  const query = `end_month=${encodeURIComponent(endControl.value)}&months=${encodeURIComponent(monthsControl.value)}&format=${format}`;
  const response = await fetch(`/api/reports/export?${query}`, { credentials: "same-origin" });
  if (response.status === 401) { showAuth(true); throw new Error("Sua sessão expirou. Entre novamente."); }
  if (!response.ok) {
    let detail = "Não foi possível gerar o arquivo";
    try { const payload = await response.json(); if (typeof payload.detail === "string") detail = payload.detail; } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : `relatorio.${format}`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function loadImports() {
  await loadAccounts();
  const items = await api("/imports");
  const labels = { bank_statement: "Extrato", credit_card: "Cartão", payroll: "Holerite" };
  document.querySelector("#imports-table").innerHTML = items.length ? items.map((item) => `
    <tr><td><strong>${escapeHtml(item.name)}</strong>${item.notes ? `<br><small>${escapeHtml(item.notes)}</small>` : ""}</td><td>${labels[item.type] || escapeHtml(item.type)}</td><td><span class="status-chip ${item.status.includes("review") ? "warn" : "ok"}">${escapeHtml(item.status)}</span></td><td>${item.records}</td><td>${new Date(item.created_at).toLocaleString("pt-BR")}</td></tr>
  `).join("") : emptyRow(5, "Nenhum arquivo importado");
}

function categoryOptions(selected) {
  return state.categories.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === selected ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
}

function manualCategoryOptions(selected) {
  const categories = state.categories.filter((item) => !systemCategories.has(item.name));
  return categories.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === selected ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")
    + '<option value="__other__">Outra categoria...</option>';
}

function updateExpenseCustomCategoryField() {
  const select = document.querySelector("#expense-entry-category");
  const field = document.querySelector("#expense-entry-custom-category-field");
  const input = document.querySelector("#expense-entry-custom-category");
  const custom = select.value === "__other__";
  field.classList.toggle("hidden", !custom);
  input.disabled = !custom;
  input.required = custom;
}

function selectedExpenseEntryAccount() {
  const id = document.querySelector("#expense-entry-account").value;
  return state.accounts.find((item) => item.id === id) || null;
}

// docs/FINANCIAL_RULES.md: cartões são conciliados pela competência da
// fatura; contas correntes usam a data exata do lançamento. So the field is
// only editable/required for a card account, where the user must explicitly
// confirm the invoice competence (INV-017) instead of the backend
// fabricating it from `booked_at` (see `create_manual_transaction`). For any
// other account type it is disabled and locked to `booked_at`'s month --
// disabled fields are excluded from `FormData` (`formJson`), so the backend
// never even receives an explicit `competence` for a non-card submission,
// matching `_resolve_expense_competence`'s policy without a second rule here.
function updateExpenseCompetenceField() {
  const field = document.querySelector("#expense-entry-competence");
  const hint = document.querySelector("#expense-entry-competence-hint");
  const bookedAt = document.querySelector("#expense-entry-form").elements.booked_at.value;
  const isCard = selectedExpenseEntryAccount()?.account_type === "credit_card";
  field.required = isCard;
  field.disabled = !isCard;
  hint.textContent = isCard
    ? "Compra no cartão: confirme o mês da fatura em que ela deve entrar; não é sempre o mês da compra (INV-017)."
    : "Contas correntes usam sempre o mês da data do lançamento; não é editável (docs/FINANCIAL_RULES.md).";
  if (isCard) {
    if (!field.value && bookedAt) field.value = bookedAt.slice(0, 7);
  } else {
    field.value = bookedAt ? bookedAt.slice(0, 7) : "";
  }
}

let installmentPreviewRequestId = 0;

// Fetches the canonical schedule/projection effect from the backend
// (`GET /transactions/manual/installment-preview`) instead of computing it
// in JS -- the plan requires the prévia to derive from the backend's own
// projection path, never a parallel formula in the browser.
async function refreshExpenseInstallmentPreview() {
  const form = document.querySelector("#expense-entry-form");
  const panel = document.querySelector("#expense-entry-installment-preview");
  const accountId = form.elements.account_id.value;
  const description = form.elements.description.value;
  const amount = form.elements.amount.value;
  const bookedAt = form.elements.booked_at.value;
  const current = form.elements.installment_current.value;
  const total = form.elements.installment_total.value;
  const isCard = selectedExpenseEntryAccount()?.account_type === "credit_card";
  const competence = form.elements.competence.value;
  if (!accountId || !description || !amount || !bookedAt || !current || !total) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  const requestId = ++installmentPreviewRequestId;
  try {
    const query = new URLSearchParams({
      account_id: accountId,
      // Required so the backend can recognize this candidate as the *next
      // observed installment of an existing series* and simulate the same
      // replacement `_future_installments` applies once persisted, instead
      // of double-counting it (see `_project_installments`).
      description,
      amount,
      booked_at: bookedAt,
      installment_current: current,
      installment_total: total,
    });
    // INV-017: only a card purchase may anchor on a confirmed invoice
    // competence diverging from booked_at's month -- forward it only then,
    // so the prévia asks the backend the same question
    // `create_manual_transaction`/`_resolve_expense_competence` will answer
    // when the purchase is actually persisted. For any other account type
    // the backend always anchors on booked_at's month by itself.
    if (isCard && competence) query.set("competence", competence);
    const preview = await api(`/transactions/manual/installment-preview?${query.toString()}`);
    if (requestId !== installmentPreviewRequestId) return;
    const affectedMonths = new Set(preview.schedule.map((item) => item.month));
    const effect = preview.canonical_projection_effect.filter((row) => affectedMonths.has(row.month));
    const scheduleLine = preview.schedule.length
      ? preview.schedule.map((item) => `${escapeHtml(monthLabel(item.month))}: ${escapeHtml(money.format(item.amount))}`).join(" · ")
      : "nenhum -- esta é a última parcela";
    const effectLine = effect.length
      ? effect.map((row) => `${escapeHtml(monthLabel(row.month))} passa de ${escapeHtml(money.format(row.installments_before))} para ${escapeHtml(money.format(row.installments_after))} em parcelas`).join(" · ")
      : "sem novos meses na projeção canônica de parcelas";
    panel.innerHTML = `<strong>Parcela ${escapeHtml(String(preview.installment_current))}/${escapeHtml(String(preview.installment_total))}</strong> de ${escapeHtml(money.format(preview.monthly_payment))}.<br>Meses futuros afetados: ${scheduleLine}.<br>Efeito na projeção canônica: ${effectLine}.`;
    panel.classList.remove("hidden");
  } catch (error) {
    if (requestId !== installmentPreviewRequestId) return;
    panel.classList.add("hidden");
    panel.innerHTML = "";
  }
}

function movementEntryRow(item, columns) {
  const actionCell = item.manual
    ? `<button class="danger-button delete-movement-entry" data-id="${escapeHtml(item.id)}">Excluir</button>`
    : '<span class="muted-copy">Importado</span>';
  return `<tr data-id="${escapeHtml(item.id)}">${columns(item)}<td class="right">${actionCell}</td></tr>`;
}

async function loadMovementEntries({ month, type, tableSelector, columns, columnCount, emptyText }) {
  const items = (await api(`/transactions?limit=500${month ? `&month=${month}` : ""}`)).filter(
    (item) => item.type === type,
  );
  const table = document.querySelector(tableSelector);
  table.innerHTML = items.length ? items.map((item) => movementEntryRow(item, columns)).join("") : emptyRow(columnCount, emptyText);
  table.querySelectorAll(".delete-movement-entry").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Excluir definitivamente este lançamento manual?")) return;
    try {
      await api(`/transactions/${button.dataset.id}`, { method: "DELETE" });
      await Promise.all([loadEntradas(), loadSaidas(), loadDashboard()]);
      toast("Lançamento excluído");
    } catch (error) { toast(error.message, true); }
  }));
}

async function loadEntradas() {
  const month = document.querySelector("#income-entry-month").value;
  await loadMovementEntries({
    month,
    type: "income",
    tableSelector: "#income-entries-table",
    columnCount: 5,
    emptyText: "Nenhuma entrada registrada",
    columns: (item) => `
      <td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td>
      <td><strong>${escapeHtml(item.description)}</strong></td>
      <td>${escapeHtml(item.account)}</td>
      <td class="right amount-income">${money.format(item.amount)}</td>
    `,
  });
}

async function loadSaidas() {
  const month = document.querySelector("#expense-entry-month").value;
  await loadMovementEntries({
    month,
    type: "expense",
    tableSelector: "#expense-entries-table",
    columnCount: 6,
    emptyText: "Nenhuma saída registrada",
    columns: (item) => `
      <td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td>
      <td><strong>${escapeHtml(item.description)}</strong>${item.installment ? `<br><small>Parcela ${escapeHtml(item.installment)}</small>` : ""}</td>
      <td>${escapeHtml(item.category)}</td>
      <td>${escapeHtml(item.account)}</td>
      <td class="right amount-expense">${money.format(item.amount)}</td>
    `,
  });
}

function payablesObligationRow(item) {
  return `
    <tr>
      <td>${dateFormat.format(new Date(`${item.next_due_date}T00:00:00Z`))}</td>
      <td><strong>${escapeHtml(item.name)}</strong></td>
      <td><span class="status-chip obligation-${escapeHtml(item.alert_level)}">${escapeHtml(item.alert_label)}</span></td>
      <td class="right amount-expense">${money.format(item.amount)}</td>
    </tr>
  `;
}

const payableInvoiceStatusLabels = {
  pending: '<span class="status-chip warn">Pendente</span>',
  paid: '<span class="status-chip ok">Paga</span>',
};

async function loadPayables() {
  // "Contas a pagar": centro de obrigações e pagamentos (go-live manual
  // slice 3). Every table here reads an already-canonical service --
  // `GET /obligations` (unchanged, slice-1-era) and
  // `GET /card-payment-reconciliations/invoices` (this slice) -- no new
  // calculation happens in this screen.
  const month = document.querySelector("#payables-month").value;
  const [obligations, invoices] = await Promise.all([
    api("/obligations"),
    api(`/card-payment-reconciliations/invoices${month ? `?period=${month}` : ""}`),
  ]);

  document.querySelector("#payables-obligations-table").innerHTML = obligations.length
    ? obligations.map(payablesObligationRow).join("")
    : emptyRow(4, "Nenhuma obrigação cadastrada. Cadastre em Planejamento.");

  const pending = invoices.filter((item) => item.status === "pending");
  document.querySelector("#payables-pending-invoices-table").innerHTML = pending.length ? pending.map((item) => `
    <tr>
      <td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td>
      <td>${escapeHtml(item.account)}</td>
      <td>${escapeHtml(item.description)}</td>
      <td class="right">${money.format(Number(item.amount))}</td>
      <td class="right"><button class="text-action pay-invoice" data-id="${escapeHtml(item.card_transaction_id)}" data-amount="${escapeHtml(item.amount)}" data-description="${escapeHtml(item.description)}" data-account="${escapeHtml(item.account)}">Pagar</button></td>
    </tr>
  `).join("") : emptyRow(5, "Nenhuma fatura pendente de pagamento");
  document.querySelectorAll(".pay-invoice").forEach((button) => button.addEventListener("click", () => {
    const form = document.querySelector("#pay-invoice-form");
    form.elements.card_transaction_id.value = button.dataset.id;
    form.elements.amount.value = button.dataset.amount;
    document.querySelector("#pay-invoice-summary").value = `${button.dataset.account} • ${button.dataset.description} • ${money.format(Number(button.dataset.amount))}`;
    form.elements.booked_at.focus();
  }));

  const paid = invoices.filter((item) => item.status === "paid");
  document.querySelector("#payables-paid-invoices-table").innerHTML = paid.length ? paid.map((item) => `
    <tr>
      <td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td>
      <td>${escapeHtml(item.description)}</td>
      <td class="right">${money.format(Number(item.amount))}</td>
      <td>${item.paid_date ? dateFormat.format(new Date(`${item.paid_date}T00:00:00Z`)) : ""}</td>
    </tr>
  `).join("") : emptyRow(4, "Nenhuma fatura paga neste período");
}

async function loadTransferencias() {
  // Read-only consultation for this view: filters the same rows
  // `GET /transactions` already returns (no recalculation, no second
  // classification) down to the ones this screen is about -- transfer legs
  // (`type === "transfer"`) and the patrimonial-movement category. Editing
  // and deletion (including atomic paired deletion of a transfer's two
  // legs) stay in "Lançamentos", the single livro-razão surface.
  const month = document.querySelector("#transferencias-month").value;
  const items = await api(`/transactions?limit=500${month ? `&month=${month}` : ""}`);
  const relevant = items.filter((item) => item.type === "transfer" || item.category === "Transferência patrimonial");
  document.querySelector("#transferencias-table").innerHTML = relevant.length ? relevant.map((item) => `
    <tr>
      <td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td>
      <td><strong>${escapeHtml(item.description)}</strong></td>
      <td>${escapeHtml(item.category)}</td>
      <td>${escapeHtml(item.account)}</td>
      <td class="right ${item.amount < 0 ? "amount-expense" : "amount-income"}">${money.format(item.amount)}</td>
    </tr>
  `).join("") : emptyRow(5, "Nenhuma transferência ou movimentação patrimonial neste mês");
}

async function loadTransactions() {
  const month = document.querySelector("#transaction-month").value;
  const items = await api(`/transactions?limit=500${month ? `&month=${month}` : ""}`);
  document.querySelector("#transactions-table").innerHTML = items.length ? items.map((item) => `
    <tr data-id="${escapeHtml(item.id)}">
      <td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td>
      <td><strong>${escapeHtml(item.description)}</strong>${item.installment ? `<br><small>Parcela ${escapeHtml(item.installment)}</small>` : ""}</td>
      <td><select class="category-select" data-id="${escapeHtml(item.id)}">${categoryOptions(item.category_id)}</select></td>
      <td>${escapeHtml(item.owner)}</td><td>${escapeHtml(item.account)}</td>
      <td>${item.possible_duplicate ? '<span class="status-chip warn">Possível duplicidade</span>' : item.excluded ? `<span class="status-chip muted">${item.manual ? "Fora do teto" : "Ignorado no cálculo"}</span>` : '<span class="status-chip ok">Considerado</span>'}</td>
      <td class="right ${item.amount < 0 ? "amount-expense" : "amount-income"}">${money.format(item.amount)}</td>
      <td class="right">${item.manual
        ? `<button class="danger-button delete-transaction" data-id="${escapeHtml(item.id)}">Excluir</button>`
        : `<button class="text-action toggle-transaction" data-id="${escapeHtml(item.id)}" data-excluded="${item.excluded}">${item.excluded ? "Reconsiderar" : "Ignorar"}</button>`}</td>
    </tr>
  `).join("") : emptyRow(8);
  document.querySelectorAll(".category-select").forEach((select) => select.addEventListener("change", async (event) => {
    try {
      await api(`/transactions/${event.target.dataset.id}`, { method: "PATCH", body: JSON.stringify({ category_id: event.target.value, reviewed: true }) });
      toast("Categoria atualizada e item revisado");
    } catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll(".delete-transaction").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Excluir definitivamente este lançamento manual?")) return;
    try { await api(`/transactions/${button.dataset.id}`, { method: "DELETE" }); await loadTransactions(); await loadDashboard(); toast("Lançamento excluído"); }
    catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll(".toggle-transaction").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/transactions/${button.dataset.id}`, { method: "PATCH", body: JSON.stringify({ excluded: button.dataset.excluded !== "true", reviewed: true }) });
      await loadTransactions(); await loadDashboard(); toast("Cálculo atualizado");
    } catch (error) { toast(error.message, true); }
  }));
  await loadCardPaymentReconciliations(month);
}

function cardPaymentTransactionSummary(item) {
  if (!item) return "";
  return `${dateFormat.format(new Date(`${item.date}T00:00:00Z`))} · ${escapeHtml(item.description)} · ${escapeHtml(item.account || "")} · ${money.format(Number(item.amount))}`;
}

const cardPaymentStatusLabels = {
  linked: '<span class="status-chip ok">Conciliado</span>',
  matched: '<span class="status-chip warn">Candidato encontrado</span>',
  ambiguous: '<span class="status-chip warn">Vários candidatos</span>',
  unmatched: '<span class="status-chip muted">Sem candidato</span>',
};

async function loadCardPaymentReconciliations(month) {
  const table = document.querySelector("#card-payment-reconciliation-table");
  if (!table) return;
  const items = await api(`/card-payment-reconciliations${month ? `?period=${month}` : ""}`);
  table.innerHTML = items.length ? items.map((item) => {
    const checkingId = item.checking_transaction.id;
    let counterpart;
    let actions;
    if (item.status === "linked") {
      counterpart = cardPaymentTransactionSummary(item.linked_transaction);
      actions = `<button class="text-action unlink-card-payment" data-id="${escapeHtml(checkingId)}">Desvincular</button>`;
    } else if (item.status === "unmatched") {
      counterpart = '<span class="muted-copy">Nenhum lançamento de pagamento de fatura corresponde ainda.</span>';
      actions = "";
    } else {
      counterpart = item.candidates.map((candidate) => `<div>${cardPaymentTransactionSummary({ ...candidate, id: candidate.transaction_id })}${candidate.difference !== "0.00" ? ` <small>(diferença ${money.format(Number(candidate.difference))})</small>` : ""}</div>`).join("");
      actions = item.candidates.map((candidate) => `<button class="text-action link-card-payment" data-checking-id="${escapeHtml(checkingId)}" data-card-id="${escapeHtml(candidate.transaction_id)}">Confirmar vínculo${item.candidates.length > 1 ? ` (${dateFormat.format(new Date(`${candidate.date}T00:00:00Z`))})` : ""}</button>`).join("");
    }
    return `
    <tr data-id="${escapeHtml(checkingId)}">
      <td>${cardPaymentTransactionSummary(item.checking_transaction)}</td>
      <td>${counterpart}</td>
      <td>${cardPaymentStatusLabels[item.status] || item.status}</td>
      <td class="right">${actions}</td>
    </tr>
  `;
  }).join("") : emptyRow(4, "Nenhum pagamento de fatura importado neste período");
  document.querySelectorAll(".link-card-payment").forEach((button) => button.addEventListener("click", async () => {
    const reason = window.prompt("Motivo para confirmar este vínculo (obrigatório):");
    if (reason === null) return;
    if (reason.trim().length < 3) { toast("Motivo deve ter ao menos 3 caracteres", true); return; }
    try {
      await api("/card-payment-reconciliations/link", {
        method: "POST",
        body: JSON.stringify({
          checking_transaction_id: button.dataset.checkingId,
          card_transaction_id: button.dataset.cardId,
          reason: reason.trim(),
        }),
      });
      toast("Pagamento da fatura vinculado ao débito bancário");
      await loadCardPaymentReconciliations(document.querySelector("#transaction-month").value);
    } catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll(".unlink-card-payment").forEach((button) => button.addEventListener("click", async () => {
    const reason = window.prompt("Motivo para desvincular (obrigatório):");
    if (reason === null) return;
    if (reason.trim().length < 3) { toast("Motivo deve ter ao menos 3 caracteres", true); return; }
    try {
      await api("/card-payment-reconciliations/unlink", {
        method: "POST",
        body: JSON.stringify({ transaction_id: button.dataset.id, reason: reason.trim() }),
      });
      toast("Vínculo desfeito; os lançamentos originais continuam intactos");
      await loadCardPaymentReconciliations(document.querySelector("#transaction-month").value);
    } catch (error) { toast(error.message, true); }
  }));
}

async function loadReviews() {
  const items = await api("/reviews");
  document.querySelector("#nav-review-count").textContent = items.length;
  document.querySelector("#reviews-list").innerHTML = items.length ? items.map((item) => `
    <article class="review-card" data-review-id="${escapeHtml(item.id)}">
      <div class="review-icon">!</div>
      <div class="review-content"><h3>${escapeHtml(item.description)}</h3><p>${escapeHtml(item.details || item.reason)}${item.amount !== null ? ` • ${money.format(item.amount)}` : ""}${item.date ? ` • ${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}` : ""}${item.account ? ` • ${escapeHtml(item.account)}` : ""}</p>
        ${item.transaction_id ? `<div class="review-controls"><select class="review-category" aria-label="Categoria">${categoryOptions(item.category_id)}</select><button class="text-action save-review-category" data-transaction-id="${escapeHtml(item.transaction_id)}">Salvar categoria</button><button class="text-action review-decision" data-transaction-id="${escapeHtml(item.transaction_id)}" data-action="consider">Considerar no cálculo</button><button class="text-action review-decision" data-transaction-id="${escapeHtml(item.transaction_id)}" data-action="ignore">Ignorar no cálculo</button></div>` : ""}
      </div>
      <button class="secondary resolve-review" data-id="${escapeHtml(item.id)}">Apenas confirmar</button>
    </article>
  `).join("") : '<div class="empty">Nenhuma pendência. Todos os lançamentos estão conciliados.</div>';
  document.querySelectorAll(".save-review-category").forEach((button) => button.addEventListener("click", async () => {
    const select = button.closest(".review-card").querySelector(".review-category");
    try {
      await api(`/transactions/${button.dataset.transactionId}`, { method: "PATCH", body: JSON.stringify({ category_id: select.value, reviewed: true }) });
      await loadReviews(); await loadDashboard(); toast("Categoria corrigida e pendência concluída");
    } catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll(".review-decision").forEach((button) => button.addEventListener("click", async () => {
    const consider = button.dataset.action === "consider";
    try {
      await api(`/transactions/${button.dataset.transactionId}`, { method: "PATCH", body: JSON.stringify({ excluded: !consider, possible_duplicate: consider ? false : undefined, reviewed: true }) });
      await loadReviews(); await loadDashboard(); toast(consider ? "Lançamento incluído no cálculo" : "Lançamento ignorado no cálculo");
    } catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll(".resolve-review").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/reviews/${button.dataset.id}/resolve`, { method: "POST" }); await loadReviews(); toast("Pendência confirmada"); }
    catch (error) { toast(error.message, true); }
  }));
}

async function loadIncome() {
  const [commissions, payroll] = await Promise.all([api("/commissions"), api("/payroll")]);
  document.querySelector("#commissions-table").innerHTML = commissions.length ? commissions.map((item) => `
    <tr><td>${dateFormat.format(new Date(`${item.expected_date}T00:00:00Z`))}</td><td>${escapeHtml(item.description)}</td><td class="right">${money.format(item.gross)}</td><td class="right amount-expense">${money.format(item.tax)}</td><td class="right amount-income">${money.format(item.net)}</td><td class="right"><button class="danger-button delete-commission" data-id="${escapeHtml(item.id)}">Excluir</button></td></tr>
  `).join("") : emptyRow(6, "Nenhuma comissão cadastrada");
  const kindLabels = { regular: "Salário", "13_first": "1ª do 13º", "13_second": "2ª do 13º", vacation_extra: "Férias adicionais", other: "Outro" };
  document.querySelector("#payroll-table").innerHTML = payroll.length ? payroll.map((item) => `
    <tr><td>${dateFormat.format(new Date(`${item.payment_date}T00:00:00Z`))}</td><td>${escapeHtml(item.person_name)}</td><td>${kindLabels[item.kind] || escapeHtml(item.kind)}</td><td class="right amount-income">${money.format(item.net)}</td><td class="right">${item.manual ? `<button class="danger-button delete-payroll" data-id="${escapeHtml(item.id)}">Excluir</button>` : '<span class="status-chip muted">Importado</span>'}</td></tr>
  `).join("") : emptyRow(5, "Nenhum holerite cadastrado");
  document.querySelectorAll(".delete-commission").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Excluir esta comissão da projeção?")) return;
    try { await api(`/commissions/${button.dataset.id}`, { method: "DELETE" }); await loadIncome(); toast("Comissão excluída"); }
    catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll(".delete-payroll").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Excluir este registro manual de folha?")) return;
    try { await api(`/payroll/${button.dataset.id}`, { method: "DELETE" }); await loadIncome(); toast("Registro de folha excluído"); }
    catch (error) { toast(error.message, true); }
  }));
}

async function loadProfile() {
  const profile = await api("/profile");
  const form = document.querySelector("#profile-form");
  Object.entries(profile).forEach(([key, value]) => { if (form.elements[key] && value !== null) form.elements[key].value = value; });
}

async function loadForecast() {
  const [data, obligations] = await Promise.all([api("/forecast"), api("/obligations")]);
  state.forecast = data.rows;
  state.forecastFloor = data.summary.emergency_floor;
  document.querySelector("#forecast-final").textContent = money.format(data.summary.final_delayed);
  document.querySelector("#forecast-min").textContent = money.format(data.summary.minimum_delayed);
  const status = document.querySelector("#forecast-status");
  status.textContent = data.summary.viable ? "Viável" : "Revisar gastos";
  status.className = data.summary.viable ? "amount-income" : "amount-expense";
  document.querySelector("#forecast-table").innerHTML = data.rows.length ? data.rows.map((item) => `
    <tr><td>${escapeHtml(item.month)}</td><td class="right">${money.format(item.salary)}</td><td class="right">${money.format(item.payroll_extras)}</td><td class="right amount-income">${money.format(item.commission_delayed)}</td><td class="right amount-expense">${money.format(item.obligations)}</td><td class="right amount-expense">${money.format(item.installments)}</td><td class="right amount-income">${money.format(item.investment_return_delayed)}</td><td class="right ${item.balance_delayed < data.summary.emergency_floor ? "amount-expense" : "amount-income"}">${money.format(item.balance_delayed)}</td></tr>
  `).join("") : emptyRow(8, "Configure as premissas financeiras");
  document.querySelector("#obligations-table").innerHTML = obligations.length ? obligations.map((item) => `
    <tr><td>${dateFormat.format(new Date(`${item.next_due_date}T00:00:00Z`))}</td><td><strong>${escapeHtml(item.name)}</strong></td><td>${escapeHtml(item.category)}</td><td>${item.recurrence_months ? `A cada ${item.recurrence_months} mês(es) • ${item.occurrence_count} vez(es)` : "Pagamento único"}</td><td><span class="status-chip obligation-${escapeHtml(item.alert_level)}">${escapeHtml(item.alert_label)}</span></td><td class="right amount-expense">${money.format(item.amount)}</td><td class="right"><button class="danger-button delete-obligation" data-id="${escapeHtml(item.id)}">Excluir</button></td></tr>
  `).join("") : emptyRow(7, "Nenhum compromisso ativo");
  document.querySelectorAll(".delete-obligation").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Excluir este compromisso das projeções futuras?")) return;
    try { await api(`/obligations/${button.dataset.id}`, { method: "DELETE" }); await loadForecast(); toast("Compromisso excluído"); }
    catch (error) { toast(error.message, true); }
  }));
  drawForecast(data.rows, data.summary.emergency_floor);
}

const captureSourceLabels = { text: "Texto", audio: "Áudio", image: "Imagem", document: "Documento" };
const captureTypeLabels = { text: "Mensagem", receipt: "Comprovante", boleto: "Boleto", credit_card: "Fatura", bank_statement: "Extrato", payroll: "Holerite", auto: "Automático" };
const captureStatusLabels = { preview: "Aguardando confirmação", needs_input: "Precisa de ajuste", confirmed: "Confirmado", cancelled: "Cancelado" };

function captureAccountOptions(selected) {
  return '<option value="">Escolha a conta</option>' + state.accounts.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === selected ? "selected" : ""}>${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("");
}

function captureCategoryOptions(item) {
  const selected = item.category_id || "";
  const options = state.categories.filter((category) => !systemCategories.has(category.name) || category.id === selected)
    .map((category) => `<option value="${escapeHtml(category.id)}" ${category.id === selected ? "selected" : ""}>${escapeHtml(category.name)}</option>`).join("");
  const otherSelected = item.new_category && !selected;
  return options + `<option value="__other__" ${otherSelected ? "selected" : ""}>Outra categoria...</option>`;
}

function captureWarnings(item) {
  const warnings = item.warnings || [];
  return warnings.length ? `<ul class="capture-warning-list">${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : "";
}

function renderCaptureTransaction(item, index) {
  const checked = item.selected !== false ? "checked" : "";
  const otherVisible = item.new_category && !item.category_id;
  return `
    <article class="capture-item ${item.possible_duplicate ? "duplicate" : ""}" data-index="${index}" data-kind="transaction">
      <label class="capture-item-selector" title="Incluir este item"><input class="capture-selected" type="checkbox" ${checked}></label>
      <div class="capture-item-fields">
        <div class="capture-item-heading"><strong>Lançamento ${index + 1}</strong><span class="status-chip ${item.possible_duplicate ? "warn" : "ok"}">${item.possible_duplicate ? "Possível duplicidade" : `${Math.round((item.confidence || 0) * 100)}% de confiança`}</span></div>
        <label class="wide">Descrição<input data-field="description" value="${escapeHtml(item.description)}" maxlength="500" required></label>
        <label>Data<input data-field="booked_at" type="date" value="${escapeHtml(item.booked_at)}" required></label>
        <label>Valor<input data-field="amount" type="number" min="0.01" step="0.01" value="${escapeHtml(item.amount)}" required></label>
        <label>Tipo<select data-field="movement_type"><option value="expense" ${item.movement_type === "expense" ? "selected" : ""}>Despesa</option><option value="income" ${item.movement_type === "income" ? "selected" : ""}>Receita</option><option value="investment" ${item.movement_type === "investment" ? "selected" : ""}>Aplicação/investimento</option><option value="redemption" ${item.movement_type === "redemption" ? "selected" : ""}>Resgate</option><option value="refund" ${item.movement_type === "refund" ? "selected" : ""}>Reembolso/estorno</option><option value="transfer" ${item.movement_type === "transfer" ? "selected" : ""}>Transferência interna</option><option value="reconciliation" ${item.movement_type === "reconciliation" ? "selected" : ""}>Pagamento/conciliação</option></select></label>
        <label>Conta ou cartão<select data-field="account_id" required>${captureAccountOptions(item.account_id)}</select></label>
        <label>Categoria<select data-field="category_id" class="capture-category">${captureCategoryOptions(item)}</select></label>
        <label class="capture-new-category ${otherVisible ? "" : "hidden"}">Nova categoria<input data-field="category_name" value="${otherVisible ? escapeHtml(item.category_name) : ""}" maxlength="100" placeholder="Ex.: Jardinagem"></label>
        ${captureWarnings(item)}
      </div>
    </article>`;
}

function renderCaptureObligation(item, index) {
  return `
    <article class="capture-item" data-index="${index}" data-kind="obligation">
      <label class="capture-item-selector"><input class="capture-selected" type="checkbox" ${item.selected !== false ? "checked" : ""}></label>
      <div class="capture-item-fields">
        <div class="capture-item-heading"><strong>Boleto ou obrigação</strong><span class="status-chip">${Math.round((item.confidence || 0) * 100)}% de confiança</span></div>
        <label class="wide">Descrição<input data-field="description" value="${escapeHtml(item.description)}" maxlength="500" required></label>
        <label>Vencimento<input data-field="due_date" type="date" value="${escapeHtml(item.due_date)}" required></label>
        <label>Valor<input data-field="amount" type="number" min="0.01" step="0.01" value="${escapeHtml(item.amount)}" required></label>
        <label>Repetir a cada (meses)<input data-field="recurrence_months" type="number" min="0" max="120" value="${item.recurrence_months || 0}"></label>
        <label>Quantidade de ocorrências<input data-field="occurrence_count" type="number" min="1" max="240" value="${item.occurrence_count || 1}"></label>
        ${captureWarnings(item)}
      </div>
    </article>`;
}

function renderCapturePayroll(item, index) {
  return `
    <article class="capture-item" data-index="${index}" data-kind="payroll">
      <label class="capture-item-selector"><input class="capture-selected" type="checkbox" ${item.selected !== false ? "checked" : ""}></label>
      <div class="capture-item-fields">
        <div class="capture-item-heading"><strong>Holerite</strong><span class="status-chip">${Math.round((item.confidence || 0) * 100)}% de confiança</span></div>
        <label class="wide">Pessoa<input data-field="description" value="${escapeHtml(item.description)}" maxlength="120" required></label>
        <label>Competência<input data-field="competence" type="date" value="${escapeHtml(item.competence)}" required></label>
        <label>Data do pagamento<input data-field="payment_date" type="date" value="${escapeHtml(item.payment_date)}" required></label>
        <label>Valor líquido<input data-field="amount" type="number" min="0.01" step="0.01" value="${escapeHtml(item.amount)}" required></label>
        <label>Tipo<select data-field="payroll_kind"><option value="regular">Salário</option><option value="13_first">1ª parcela do 13º</option><option value="13_second">2ª parcela do 13º</option><option value="vacation_extra">Férias</option><option value="other">Outro</option></select></label>
        ${captureWarnings(item)}
      </div>
    </article>`;
}

function renderCapturePreview(capture) {
  state.captureDraft = capture;
  const panel = document.querySelector("#capture-preview-panel");
  panel.classList.remove("hidden");
  panel.classList.toggle("needs-input", capture.status === "needs_input");
  document.querySelector("#capture-preview-summary").textContent = `${captureSourceLabels[capture.source_type] || capture.source_type} • ${captureTypeLabels[capture.detected_type] || capture.detected_type} • ${capture.items.length} item(ns)`;
  document.querySelector("#capture-confidence").textContent = `${Math.round((capture.confidence || 0) * 100)}%`;
  const notes = document.querySelector("#capture-preview-notes");
  notes.classList.toggle("hidden", !capture.notes);
  notes.textContent = capture.notes || "";
  document.querySelector("#capture-preview-items").innerHTML = capture.items.length
    ? capture.items.map((item, index) => item.kind === "transaction" ? renderCaptureTransaction(item, index) : item.kind === "obligation" ? renderCaptureObligation(item, index) : renderCapturePayroll(item, index)).join("")
    : '<div class="capture-empty-preview">Não foi possível montar uma prévia automática. Escreva os dados principais no campo de mensagem e tente novamente.</div>';
  document.querySelector("#capture-confirm").disabled = !capture.items.length;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearCaptureAudio() {
  state.captureAudio = null;
  const button = document.querySelector("#capture-record");
  button.classList.remove("recording");
  button.innerHTML = "<span>●</span> Gravar áudio";
  document.querySelector("#capture-record-status").textContent = "Nenhum áudio gravado";
  document.querySelector("#capture-clear-audio").classList.add("hidden");
}

function closeCapturePreview() {
  state.captureDraft = null;
  document.querySelector("#capture-preview-panel").classList.add("hidden");
  document.querySelector("#capture-preview-items").innerHTML = "";
}

async function loadCapture() {
  await Promise.all([loadAccounts(), loadCategories()]);
  const captures = await api("/captures");
  document.querySelector("#captures-table").innerHTML = captures.length ? captures.map((item) => `
    <tr><td>${escapeHtml(captureSourceLabels[item.source_type] || item.source_type)}${item.file_name ? `<br><small>${escapeHtml(item.file_name)}</small>` : ""}</td><td>${escapeHtml(captureTypeLabels[item.detected_type] || item.detected_type)}</td><td><span class="status-chip ${item.status === "confirmed" ? "ok" : item.status === "needs_input" ? "warn" : "muted"}">${escapeHtml(captureStatusLabels[item.status] || item.status)}</span></td><td>${item.items.length}</td><td>${escapeHtml(item.processor)}</td><td>${new Date(item.created_at).toLocaleString("pt-BR")}</td></tr>
  `).join("") : emptyRow(6, "Nenhuma captura realizada");
}

function capturePayload() {
  return [...document.querySelectorAll("#capture-preview-items .capture-item")].map((card) => {
    const index = Number(card.dataset.index);
    const original = state.captureDraft.items[index] || {};
    const value = (name) => card.querySelector(`[data-field="${name}"]`)?.value || null;
    const item = {
      kind: card.dataset.kind,
      selected: card.querySelector(".capture-selected").checked,
      description: value("description"),
      amount: Number(value("amount") || 0),
    };
    if (item.kind === "transaction") {
      Object.assign(item, {
        booked_at: value("booked_at"),
        movement_type: value("movement_type"),
        account_id: value("account_id"),
        category_id: value("category_id") === "__other__" ? null : value("category_id"),
        category_name: value("category_id") === "__other__" ? value("category_name") : null,
        source_line: original.source_line || null,
        installment_current: original.installment_current || null,
        installment_total: original.installment_total || null,
        card_last_four: original.card_last_four || null,
        signed_amount: original.signed_amount == null
          ? null
          : Math.sign(Number(original.signed_amount)) * Number(value("amount") || 0),
      });
    } else if (item.kind === "obligation") {
      Object.assign(item, {
        due_date: value("due_date"),
        category_name: original.category_name || "general",
        recurrence_months: Number(value("recurrence_months") || 0),
        occurrence_count: Number(value("occurrence_count") || 1),
      });
    } else {
      Object.assign(item, {
        competence: value("competence"),
        payment_date: value("payment_date"),
        payroll_kind: value("payroll_kind") || "regular",
      });
    }
    return item;
  });
}

async function toggleAudioRecording() {
  const button = document.querySelector("#capture-record");
  const status = document.querySelector("#capture-record-status");
  if (state.captureRecorder?.state === "recording") {
    state.captureRecorder.stop();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    toast("Este navegador não permite gravação de áudio. Envie um arquivo de áudio.", true);
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    const preferred = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "";
    const recorder = new MediaRecorder(stream, preferred ? { mimeType: preferred } : undefined);
    state.captureRecorder = recorder;
    recorder.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener("stop", () => {
      state.captureAudio = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      stream.getTracks().forEach((track) => track.stop());
      button.classList.remove("recording");
      button.innerHTML = "<span>●</span> Gravar novamente";
      status.textContent = `Áudio pronto • ${Math.max(1, Math.round(state.captureAudio.size / 1024))} KB`;
      document.querySelector("#capture-clear-audio").classList.remove("hidden");
    });
    recorder.start();
    button.classList.add("recording");
    button.innerHTML = "<span>■</span> Parar gravação";
    status.textContent = "Gravando... fale normalmente";
  } catch (error) {
    toast(`Não foi possível acessar o microfone: ${error.message}`, true);
  }
}

function appendAdvisorMessage(role, text, status = "") {
  const messages = document.querySelector("#advisor-messages");
  const bubble = document.createElement("div");
  bubble.className = `advisor-message ${role}${status ? ` ${status}` : ""}`;
  bubble.textContent = text;
  messages.appendChild(bubble);
  messages.scrollTop = messages.scrollHeight;
  return bubble;
}

function advisorElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function advisorDecisionLabel(intent, status) {
  if (intent === "purchase") {
    if (status === "not_recommended") return "Compra não recomendada agora";
    if (status === "caution") return "Compra possível, mas exige cautela";
    if (status === "favorable") return "Compra compatível com o cenário";
    if (status === "insufficient_data") return "Precisamos de mais informações";
  }
  const labels = {
    obligations: "Próximos compromissos",
    cuts: "Oportunidades de economia",
    cash_in: "Entradas do mês",
    cash_out: "Saídas do mês",
    spending: "Gastos do mês",
  };
  return labels[intent] || "Análise financeira";
}

function advisorAnswerSections(answer) {
  const text = String(answer || "").trim();
  const markers = [
    { key: "reflection", label: "Compra consciente:" },
    { key: "schedule", label: "Cronograma dos próximos meses já considerado no cálculo:" },
  ];
  const found = markers
    .map((marker) => ({ ...marker, index: text.indexOf(marker.label) }))
    .filter((marker) => marker.index >= 0)
    .sort((left, right) => left.index - right.index);
  if (!found.length) return { main: text, reflection: "", schedule: "" };

  const sections = { main: text.slice(0, found[0].index).trim(), reflection: "", schedule: "" };
  found.forEach((marker, index) => {
    const start = marker.index + marker.label.length;
    const end = found[index + 1]?.index ?? text.length;
    sections[marker.key] = text.slice(start, end).trim();
  });
  return sections;
}

function appendAdvisorText(parent, text) {
  const lines = String(text || "").split("\n").map((line) => line.trim()).filter(Boolean);
  let list = null;
  lines.forEach((line) => {
    if (line.startsWith("- ")) {
      if (!list) {
        list = advisorElement("ul", "advisor-answer-list");
        parent.appendChild(list);
      }
      list.appendChild(advisorElement("li", "", line.slice(2)));
      return;
    }
    list = null;
    parent.appendChild(advisorElement("p", "advisor-answer-paragraph", line));
  });
}

function appendAdvisorPurchaseMetrics(parent, metrics) {
  if (!metrics?.purchase_amount || !metrics?.payment) return;
  const quick = metrics.purchase_context?.analysis_depth === "quick";
  const values = quick
    ? [
      ["Valor da compra", metrics.purchase_amount],
      ["Teto após a compra", metrics.remaining_after],
    ]
    : [
      ["Valor da compra", metrics.purchase_amount],
      [Number(metrics.payment.installments) > 1 ? "Parcela mensal" : "Impacto à vista", metrics.payment.monthly_payment],
      ["Teto após a compra", metrics.remaining_after],
      ["Margem acima do piso", metrics.projection_margin_after],
    ];
  const grid = advisorElement("div", "advisor-metric-grid");
  values.forEach(([label, value]) => {
    if (value === undefined || value === null) return;
    const card = advisorElement("div", `advisor-metric${Number(value) < 0 ? " negative" : ""}`);
    card.append(
      advisorElement("span", "", label),
      advisorElement("strong", "", money.format(Number(value))),
    );
    grid.appendChild(card);
  });
  if (grid.children.length) parent.appendChild(grid);
}

function appendAdvisorReflection(parent, reflection, purchaseContext = null) {
  if (!reflection) return;
  const section = advisorElement("section", "advisor-reflection");
  const heading = advisorElement("div", "advisor-section-heading");
  const quick = purchaseContext?.analysis_depth === "quick";
  heading.append(
    advisorElement("span", `advisor-section-icon${quick ? " proportional" : ""}`, quick ? "✓" : "?"),
    advisorElement("h4", "", quick ? "Leitura proporcional" : "Antes de decidir"),
  );
  section.appendChild(heading);

  const lines = reflection.split("\n").map((line) => line.trim()).filter(Boolean);
  const opening = [];
  const closing = [];
  const questions = [];
  let foundQuestion = false;
  lines.forEach((line) => {
    if (line.startsWith("- ")) {
      foundQuestion = true;
      questions.push(line.slice(2));
    } else if (foundQuestion) {
      closing.push(line);
    } else {
      opening.push(line);
    }
  });
  if (opening.length) section.appendChild(advisorElement("p", "advisor-reflection-intro", opening.join(" ")));
  if (questions.length) {
    const list = advisorElement("div", "advisor-question-list");
    questions.forEach((question) => {
      const separator = question.indexOf(":");
      const item = advisorElement("div", "advisor-question");
      item.appendChild(advisorElement("span", "advisor-question-dot", ""));
      const content = advisorElement("p");
      if (separator > 0) {
        content.append(
          advisorElement("strong", "", `${question.slice(0, separator)}: `),
          document.createTextNode(question.slice(separator + 1).trim()),
        );
      } else {
        content.textContent = question;
      }
      item.appendChild(content);
      list.appendChild(item);
    });
    section.appendChild(list);
  }
  if (closing.length) section.appendChild(advisorElement("p", "advisor-reflection-close", closing.join(" ")));
  parent.appendChild(section);
}

function advisorMonthLabel(value) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(value || ""));
  if (!match) return String(value || "");
  const label = monthFormat.format(new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1)));
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function appendAdvisorSchedule(parent, schedule) {
  if (!Array.isArray(schedule) || !schedule.length) return;
  const section = advisorElement("section", "advisor-schedule");
  const heading = advisorElement("div", "advisor-section-heading");
  heading.append(
    advisorElement("span", "advisor-section-icon calendar", "▦"),
    advisorElement("div", ""),
  );
  heading.lastChild.append(
    advisorElement("h4", "", "Compromissos dos próximos meses"),
    advisorElement("small", "", "Parcelas dos cartões e obrigações já consideradas na análise"),
  );
  section.appendChild(heading);

  const wrapper = advisorElement("div", "advisor-schedule-scroll");
  const table = advisorElement("table", "advisor-schedule-table");
  const head = advisorElement("thead");
  const headRow = advisorElement("tr");
  ["Mês", "Cartões", "Obrigações", "Total"].forEach((label) => headRow.appendChild(advisorElement("th", "", label)));
  head.appendChild(headRow);
  table.appendChild(head);
  const body = advisorElement("tbody");
  schedule.forEach((row) => {
    const tr = advisorElement("tr");
    tr.append(
      advisorElement("td", "advisor-schedule-month", advisorMonthLabel(row.month)),
      advisorElement("td", "", money.format(Number(row.card_installments || 0))),
      advisorElement("td", "", money.format(Number(row.obligations || 0))),
      advisorElement("td", "advisor-schedule-total", money.format(Number(row.total || 0))),
    );
    body.appendChild(tr);
  });
  table.appendChild(body);
  wrapper.appendChild(table);
  section.appendChild(wrapper);
  parent.appendChild(section);
}

function renderAdvisorAnswer(bubble, result) {
  bubble.replaceChildren();
  bubble.className = `advisor-message assistant ${result.status}`;
  bubble.dataset.source = result.provider === "codex" ? "Explicado pelo Codex" : "Motor financeiro local";

  const header = advisorElement("div", "advisor-answer-header");
  const statusIcon = result.status === "not_recommended" ? "!" : result.status === "favorable" ? "✓" : result.status === "caution" ? "!" : "i";
  header.append(
    advisorElement("span", "advisor-answer-icon", statusIcon),
    advisorElement("strong", "", advisorDecisionLabel(result.intent, result.status)),
  );
  bubble.appendChild(header);

  const sections = advisorAnswerSections(result.answer);
  const summary = advisorElement("div", "advisor-answer-summary");
  appendAdvisorText(summary, sections.main);
  bubble.appendChild(summary);
  appendAdvisorPurchaseMetrics(bubble, result.metrics);
  appendAdvisorReflection(bubble, sections.reflection, result.metrics?.purchase_context);
  if (result.metrics?.show_commitment_schedule !== false) {
    appendAdvisorSchedule(bubble, result.metrics?.commitment_schedule);
  }
}

async function loadAdvisor() {
  const messages = document.querySelector("#advisor-messages");
  if (!messages.children.length) {
    appendAdvisorMessage("assistant", "Olá! Posso avaliar uma compra, explicar entradas e saídas, listar vencimentos e sugerir cortes com base na sua base financeira.");
  }
  const badge = document.querySelector("#advisor-provider-status");
  try {
    const result = await api("/advisor/status");
    if (result.codex_ready) {
      badge.textContent = `Codex conectado • ${result.model}`;
      badge.className = "status-chip ok";
      badge.title = "O Codex explica os resultados calculados localmente";
    } else if (result.codex_configured && !result.codex_authenticated && !result.error) {
      badge.textContent = "Codex aguardando login";
      badge.className = "status-chip warn";
      badge.title = "Habilite o login por código de dispositivo no ChatGPT e execute scripts/setup-codex.sh";
    } else if (result.codex_configured) {
      badge.textContent = "Codex indisponível";
      badge.className = "status-chip warn";
      badge.title = result.error || "O serviço do Codex não está pronto";
    } else {
      badge.textContent = "Somente motor local";
      badge.className = "status-chip muted";
      badge.title = "O Codex ainda não foi configurado";
    }
  } catch (_) {
    badge.textContent = "Codex indisponível";
    badge.className = "status-chip warn";
  }
}

async function askAdvisor(message) {
  appendAdvisorMessage("user", message);
  const loading = appendAdvisorMessage("assistant", "Analisando seus dados...");
  try {
    const result = await api("/advisor/chat", {
      method: "POST",
      body: JSON.stringify({ message, history: state.advisorHistory.slice(-8) }),
    });
    renderAdvisorAnswer(loading, result);
    state.advisorHistory.push(
      { role: "user", content: message },
      { role: "assistant", content: result.answer.slice(0, 8000) },
    );
    state.advisorHistory = state.advisorHistory.slice(-8);
  } catch (error) {
    loading.textContent = error.message;
    loading.className = "advisor-message assistant error";
  }
  loading.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadUsers() {
  if (!state.user.is_admin) return navigate("dashboard");
  const items = await api("/users");
  document.querySelector("#users-table").innerHTML = items.length ? items.map((item) => `
    <tr><td><strong>${escapeHtml(item.name)}</strong>${item.is_current ? " <small>(você)</small>" : ""}</td><td>${escapeHtml(item.username)}</td><td>${item.is_admin ? "Administrador" : "Usuário da família"}</td><td><span class="status-chip ${item.active ? "ok" : "muted"}">${item.active ? "Ativo" : "Desativado"}</span></td><td class="right">${item.active && !item.is_current ? `<button class="danger-button deactivate-user" data-id="${escapeHtml(item.id)}">Desativar</button>` : ""}</td></tr>
  `).join("") : emptyRow(5, "Nenhum usuário cadastrado");
  document.querySelectorAll(".deactivate-user").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Desativar este acesso? O histórico financeiro será preservado.")) return;
    try { await api(`/users/${button.dataset.id}`, { method: "DELETE" }); await loadUsers(); toast("Acesso desativado"); }
    catch (error) { toast(error.message, true); }
  }));
}

function currentMonthKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function amountOrDash(value) {
  return value === null || value === undefined ? "—" : money.format(Number(value));
}

function severityLabel(severity) {
  return { block: "Block", critical: "Critical", review: "Review", warning: "Warning", info: "Info" }[severity] || severity;
}

function findingStatusLabel(statusValue) {
  return {
    open: "Aberto",
    acknowledged: "Reconhecido",
    resolved: "Resolvido",
    ignored: "Ignorado",
    false_positive: "Falso positivo",
    superseded: "Superado",
  }[statusValue] || statusValue;
}

// The Integrity screen and the global BLOCK banner only ever render fields
// already produced by the Financial Integrity Engine (status/score/trust
// gates/findings) -- see docs/FINANCIAL_RULES.md and
// docs/ARCHITECTURE.md principle 2. Nothing here recomputes a financial
// figure; the UI is a read/act surface over `/api/integrity/*` and
// `/api/monthly-closes/*`.
async function refreshIntegrityBanner() {
  const banner = document.querySelector("#integrity-banner");
  try {
    const status = await api("/integrity/status");
    if (status.status === "blocked") {
      document.querySelector("#integrity-banner-text").textContent =
        `Integridade financeira: BLOCK ativo (${status.open_findings || 0} finding(s) aberto(s)). Os números ainda podem ser corrigidos; nada foi escondido.`;
      banner.classList.remove("hidden");
      document.querySelector("#nav-integrity-badge").classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
      document.querySelector("#nav-integrity-badge").classList.add("hidden");
    }
  } catch (_) {
    // Best-effort: never let the banner check break navigation.
  }
}

function renderIntegritySummaryCards(status) {
  const scoreText = status.score === null || status.score === undefined ? "—" : `${Number(status.score).toFixed(1)}%`;
  const lastRun = status.last_run && status.last_run.completed_at
    ? dateFormat.format(new Date(status.last_run.completed_at))
    : "Nunca executado";
  document.querySelector("#integrity-summary-cards").innerHTML = `
    <div class="kpi ${status.status === "blocked" || status.status === "critical" ? "over-budget" : ""}">
      <span>Status consolidado</span>
      <strong><span class="status-chip severity-${status.status === "blocked" ? "block" : status.status === "critical" ? "critical" : status.status === "review_required" ? "review" : status.status === "attention" ? "warning" : status.status === "unknown" ? "info" : ""}">${escapeHtml(status.status)}</span></strong>
      <small>Score: ${scoreText} • Última execução: ${escapeHtml(lastRun)}</small>
    </div>
    <div class="kpi">
      <span>Confiança de projeção</span>
      <strong>${status.trusted_for_projection ? "Confiável" : "Não confiável"}</strong>
      <small>Gate determinístico de INV-005/006/018/022</small>
    </div>
    <div class="kpi">
      <span>Confiança de relatórios</span>
      <strong>${status.trusted_for_reports ? "Confiável" : "Não confiável"}</strong>
      <small>Gate determinístico de INV-019/020/022</small>
    </div>
    <div class="kpi">
      <span>Findings abertos</span>
      <strong>${status.open_findings || 0}</strong>
      <small>${Object.entries(status.open_findings_by_severity || {}).map(([key, value]) => `${severityLabel(key)}: ${value}`).join(" • ") || "Nenhum"}</small>
    </div>
  `;
}

async function loadIntegrity() {
  if (!document.querySelector("#close-period").value) {
    document.querySelector("#close-period").value = currentMonthKey();
  }
  const status = await api("/integrity/status");
  renderIntegritySummaryCards(status);
  await refreshIntegrityBanner();
  await Promise.all([
    loadMonthlyClose(),
    loadActiveCriticalFindings(),
    loadFindings(),
    loadReconciliations(),
  ]);
}

function findingCardHtml(item) {
  return `
    <article class="review-card" data-finding-id="${escapeHtml(item.id)}">
      <div class="review-icon">${item.severity === "block" || item.severity === "critical" ? "!" : "i"}</div>
      <div class="review-content">
        <h3>${escapeHtml(item.title)} <span class="status-chip severity-${escapeHtml(item.severity)}">${severityLabel(item.severity)}</span> <span class="status-chip muted">${findingStatusLabel(item.status)}</span></h3>
        <p>${escapeHtml(item.invariant_id)} • ${escapeHtml(item.period || "sem período")} • ${item.occurrence_count}x${item.last_seen_at ? ` • última vez ${dateFormat.format(new Date(item.last_seen_at))}` : ""}</p>
        <div class="review-controls">
          <button class="text-action view-finding-detail" data-id="${escapeHtml(item.id)}">Detalhe</button>
          ${item.status === "open" ? `<button class="text-action finding-action" data-id="${escapeHtml(item.id)}" data-action="acknowledge" data-label="reconhecer">Reconhecer</button>` : ""}
          ${item.status === "open" || item.status === "acknowledged" ? `
            <button class="text-action finding-action" data-id="${escapeHtml(item.id)}" data-action="resolve" data-label="resolver">Resolver</button>
            <button class="text-action finding-action" data-id="${escapeHtml(item.id)}" data-action="ignore" data-label="ignorar">Ignorar</button>
            <button class="text-action finding-action" data-id="${escapeHtml(item.id)}" data-action="false-positive" data-label="marcar falso positivo">Falso positivo</button>
          ` : ""}
        </div>
      </div>
    </article>
  `;
}

function bindFindingCardActions(container) {
  container.querySelectorAll(".view-finding-detail").forEach((button) => button.addEventListener("click", () => showFindingDetail(button.dataset.id)));
  container.querySelectorAll(".finding-action").forEach((button) => button.addEventListener("click", () => findingLifecycleAction(button.dataset.id, button.dataset.action, button.dataset.label)));
}

// Always visible, independent of the filtered/paginated list below: fetches
// every active (open/acknowledged) BLOCK and CRITICAL finding directly by
// severity so a material finding can never be pushed out of sight by the
// default page cap or by a status/severity filter someone left applied --
// see the engineering review on PR 7 ("UI pode esconder BLOCK/CRITICAL
// ativo pelo cap de 100").
async function loadActiveCriticalFindings() {
  const panel = document.querySelector("#findings-active-panel");
  const list = document.querySelector("#findings-active-list");
  const [blockData, criticalData] = await Promise.all([
    api("/integrity/findings?status=active&severity=block&limit=200"),
    api("/integrity/findings?status=active&severity=critical&limit=200"),
  ]);
  const seen = new Set();
  const items = [...blockData.items, ...criticalData.items].filter((item) => {
    if (seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
  if (!items.length) {
    panel.classList.add("hidden");
    list.innerHTML = "";
    return;
  }
  panel.classList.remove("hidden");
  list.innerHTML = items.map(findingCardHtml).join("");
  bindFindingCardActions(list);
}

function renderMonthlyClose(close) {
  const statusLabel = { open: "Aberto", review_required: "Revisão necessária", trusted: "Trusted" }[close.status] || close.status;
  const pending = close.pending;
  const reasons = [];
  if (!pending.eligible_for_trust) {
    if (!["healthy", "attention"].includes(pending.integrity_status)) {
      reasons.push(`status de integridade '${escapeHtml(pending.integrity_status)}'`);
    }
    if (!pending.current_snapshot_trusted_for_reports || !pending.current_snapshot_trusted_for_projection) {
      reasons.push("snapshot atual sem trusted_for_reports/trusted_for_projection");
    }
    if (!pending.current_snapshot_id) reasons.push("nenhum snapshot atual para o período");
  }
  document.querySelector("#close-status").innerHTML = `
    <div class="quality-item">
      <div><span class="quality-dot ${close.status === "trusted" ? "" : "warn"}"></span><div>
        <strong>${escapeHtml(statusLabel)}</strong>
        <small>${close.closed_at ? `Fechado em ${dateFormat.format(new Date(close.closed_at))}` : "Ainda não fechado"}${close.reopened_at ? ` • Reaberto em ${dateFormat.format(new Date(close.reopened_at))} (${escapeHtml(close.reason || "")})` : ""}</small>
      </div></div>
      <span class="status-chip ${close.status === "trusted" ? "ok" : "warn"}">${pending.open_findings || 0} finding(s) aberto(s)</span>
    </div>
    ${reasons.length ? `<p class="empty" style="text-align:left;padding:12px 0;">Pendências para trusted: ${reasons.join("; ")}.</p>` : ""}
  `;
}

async function loadMonthlyClose() {
  const period = document.querySelector("#close-period").value || currentMonthKey();
  const close = await api(`/monthly-closes/${period}`);
  renderMonthlyClose(close);
}

// The filtered/paginated view. `loadActiveCriticalFindings()` above is the
// one guaranteeing BLOCK/CRITICAL visibility -- this list's page cap is
// safe to keep small since it is no longer the only place a material
// finding can be seen or acted on.
let findingsPage = 1;
const FINDINGS_PAGE_SIZE = 20;

async function loadFindings(page = findingsPage) {
  findingsPage = Math.max(1, page);
  const params = new URLSearchParams();
  const statusFilter = document.querySelector("#finding-filter-status").value;
  const severityFilter = document.querySelector("#finding-filter-severity").value;
  const periodFilter = document.querySelector("#finding-filter-period").value;
  if (statusFilter) params.set("status", statusFilter);
  if (severityFilter) params.set("severity", severityFilter);
  if (periodFilter) params.set("period", periodFilter);
  params.set("page", String(findingsPage));
  params.set("limit", String(FINDINGS_PAGE_SIZE));
  const data = await api(`/integrity/findings?${params.toString()}`);
  const list = document.querySelector("#findings-list");
  list.innerHTML = data.items.length
    ? data.items.map(findingCardHtml).join("")
    : '<p class="empty">Nenhum finding para os filtros selecionados.</p>';
  bindFindingCardActions(list);

  const totalPages = Math.max(1, Math.ceil(data.total / data.limit));
  document.querySelector("#findings-page-info").textContent =
    data.total > 0 ? `Página ${data.page} de ${totalPages} • ${data.total} finding(s)` : "Nenhum finding";
  document.querySelector("#findings-page-prev").disabled = data.page <= 1;
  document.querySelector("#findings-page-next").disabled = data.page >= totalPages;
}

async function showFindingDetail(id) {
  const item = await api(`/integrity/findings/${id}`);
  const panel = document.querySelector("#finding-detail-panel");
  panel.classList.remove("hidden");
  document.querySelector("#finding-detail-body").innerHTML = `
    <div class="finding-detail-row"><dt>Problema</dt><dd>${escapeHtml(item.title)} — ${escapeHtml(item.message)}</dd></div>
    <div class="finding-detail-row"><dt>Invariant / regra</dt><dd>${escapeHtml(item.invariant_id)} • versão ${escapeHtml(item.financial_rules_version)}</dd></div>
    <div class="finding-detail-row"><dt>Severidade / status</dt><dd><span class="status-chip severity-${escapeHtml(item.severity)}">${severityLabel(item.severity)}</span> <span class="status-chip muted">${findingStatusLabel(item.status)}</span></dd></div>
    <div class="finding-detail-row"><dt>Esperado / encontrado</dt><dd>${amountOrDash(item.expected_amount)} / ${amountOrDash(item.actual_amount)}${item.difference_amount !== null ? ` (diferença ${amountOrDash(item.difference_amount)})` : ""}</dd></div>
    <div class="finding-detail-row"><dt>Entidade</dt><dd>${escapeHtml(item.entity_type)} • ${escapeHtml(item.entity_id || "—")} • ${escapeHtml(item.period || "sem período")}</dd></div>
    <div class="finding-detail-row"><dt>Lineage / trace</dt><dd>run ${escapeHtml(item.run_id)} • trace ${escapeHtml(item.trace_id)}</dd></div>
    <div class="finding-detail-row"><dt>Histórico</dt><dd>1ª ocorrência ${dateFormat.format(new Date(item.first_seen_at))} • última ${dateFormat.format(new Date(item.last_seen_at))} • ${item.occurrence_count}x</dd></div>
    <div class="finding-detail-row"><dt>Ação recomendada</dt><dd>${escapeHtml(item.recommended_action || "—")}</dd></div>
    ${item.acknowledged_at ? `<div class="finding-detail-row"><dt>Reconhecido</dt><dd>${dateFormat.format(new Date(item.acknowledged_at))} — ${escapeHtml(item.acknowledgement_reason || "")}</dd></div>` : ""}
    ${item.resolved_at ? `<div class="finding-detail-row"><dt>Resolução</dt><dd>${dateFormat.format(new Date(item.resolved_at))} — ${escapeHtml(item.resolution_reason || "")}</dd></div>` : ""}
    <p class="codex-note">Observação do Codex é opcional, consultiva e nunca altera este resultado determinístico -- ela nunca substitui o cálculo acima nem pode elevar sua severidade para BLOCK.</p>
  `;
}

async function findingLifecycleAction(id, action, label) {
  const reason = window.prompt(`Motivo para ${label} este finding (obrigatório):`);
  if (reason === null) return;
  if (reason.trim().length < 3) { toast("Motivo deve ter ao menos 3 caracteres", true); return; }
  try {
    await api(`/integrity/findings/${id}/${action}`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) });
    toast("Decisão registrada com motivo e trilha de auditoria");
    await loadFindings();
    await loadIntegrity();
    document.querySelector("#finding-detail-panel").classList.add("hidden");
  } catch (error) { toast(error.message, true); }
}

async function loadReconciliations() {
  const documents = await api("/imports");
  const recent = documents.slice(0, 20);
  const rows = await Promise.all(recent.map(async (item) => {
    try { return { item, reconciliation: await api(`/imports/${item.id}/reconciliation`) }; }
    catch (_) { return { item, reconciliation: null }; }
  }));
  document.querySelector("#reconciliation-table").innerHTML = rows.length ? rows.map(({ item, reconciliation }) => `
    <tr>
      <td data-label="Documento">${escapeHtml(item.name)}</td>
      <td data-label="Status"><span class="status-chip ${reconciliation && reconciliation.status === "reconciled" ? "ok" : reconciliation && reconciliation.status === "not_reconciled" ? "warn" : "muted"}">${escapeHtml(reconciliation ? reconciliation.status : "unknown")}</span></td>
      <td data-label="Declarado">${reconciliation ? amountOrDash(reconciliation.declared_total) : "—"}</td>
      <td data-label="Reconstruído">${reconciliation ? amountOrDash(reconciliation.reconstructed_total) : "—"}</td>
      <td data-label="Diferença">${reconciliation ? amountOrDash(reconciliation.difference) : "—"}</td>
    </tr>
  `).join("") : emptyRow(5, "Nenhum documento importado ainda");
}

function drawForecast(rows, floor) {
  const canvas = document.querySelector("#forecast-chart");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 700;
  const height = 250;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  if (!rows.length) return;
  const values = rows.flatMap((row) => [row.balance_delayed, floor]);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 1);
  const x = (index) => 42 + index * ((width - 62) / Math.max(1, rows.length - 1));
  const y = (value) => 18 + (max - value) * ((height - 48) / (max - min || 1));
  ctx.strokeStyle = "#dce4e9";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const py = 18 + i * ((height - 48) / 3);
    ctx.beginPath(); ctx.moveTo(42, py); ctx.lineTo(width - 18, py); ctx.stroke();
  }
  ctx.strokeStyle = "#b7791f";
  ctx.setLineDash([5, 5]);
  ctx.beginPath(); ctx.moveTo(42, y(floor)); ctx.lineTo(width - 18, y(floor)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = "#0f766e";
  ctx.lineWidth = 3;
  ctx.beginPath();
  rows.forEach((row, index) => { const px = x(index); const py = y(row.balance_delayed); if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); });
  ctx.stroke();
  ctx.fillStyle = "#0f766e";
  rows.forEach((row, index) => { ctx.beginPath(); ctx.arc(x(index), y(row.balance_delayed), 3, 0, Math.PI * 2); ctx.fill(); });
  ctx.fillStyle = "#667785"; ctx.font = "11px sans-serif";
  ctx.fillText(money.format(max), 0, 20); ctx.fillText(money.format(min), 0, height - 15);
}

document.querySelector("#setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { state.user = await api("/auth/setup", { method: "POST", body: JSON.stringify(formJson(event.target)) }); state.user = state.user.user; await showApp(); toast("Ambiente criado com segurança"); }
  catch (error) { toast(error.message, true); }
});
document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { state.user = await api("/auth/login", { method: "POST", body: JSON.stringify(formJson(event.target)) }); await showApp(); }
  catch (error) { toast(error.message, true); }
});
document.querySelector("#logout-button").addEventListener("click", async () => { await api("/auth/logout", { method: "POST" }); state.user = null; showAuth(true); });
document.querySelectorAll("#main-nav button").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
document.querySelector("#mobile-menu-toggle").addEventListener("click", () => {
  setMobileMenu(!document.querySelector(".sidebar").classList.contains("menu-open"));
});
document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => {
  if (button.dataset.go === "transactions") document.querySelector("#transaction-month").value = document.querySelector("#dashboard-month").value;
  navigate(button.dataset.go);
}));
document.querySelector("#dashboard-month").addEventListener("change", loadDashboard);
document.querySelector("#dashboard-prev-month").addEventListener("click", () => {
  const control = document.querySelector("#dashboard-month");
  control.value = shiftMonth(control.value || currentMonthKey(), -1);
  loadDashboard();
});
document.querySelector("#dashboard-next-month").addEventListener("click", () => {
  const control = document.querySelector("#dashboard-month");
  control.value = shiftMonth(control.value || currentMonthKey(), 1);
  loadDashboard();
});
document.querySelector("#dashboard-current-month").addEventListener("click", () => {
  document.querySelector("#dashboard-month").value = currentMonthKey();
  loadDashboard();
});
document.querySelector("#report-refresh").addEventListener("click", loadReports);
document.querySelector("#report-end-month").addEventListener("change", loadReports);
document.querySelector("#report-months").addEventListener("change", loadReports);
document.querySelector("#report-calendar-year").addEventListener("click", () => {
  const end = document.querySelector("#report-end-month");
  const selected = end.value || currentMonthKey();
  end.value = `${selected.slice(0, 4)}-12`;
  document.querySelector("#report-months").value = "12";
  loadReports();
});
document.querySelector("#report-print").addEventListener("click", () => window.print());
document.querySelector("#report-export-xlsx").addEventListener("click", () => downloadReportExport("xlsx").catch((error) => toast(error.message, true)));
document.querySelector("#report-export-pdf").addEventListener("click", () => downloadReportExport("pdf").catch((error) => toast(error.message, true)));
document.querySelector("#refresh-transactions").addEventListener("click", loadTransactions);
document.querySelector("#refresh-forecast").addEventListener("click", loadForecast);
document.querySelector("#refresh-income-entries").addEventListener("click", loadEntradas);
document.querySelector("#refresh-expense-entries").addEventListener("click", loadSaidas);
document.querySelector("#expense-entry-category").addEventListener("change", updateExpenseCustomCategoryField);
document.querySelector("#expense-entry-account").addEventListener("change", () => {
  updateExpenseCompetenceField();
  refreshExpenseInstallmentPreview();
});
document.querySelector("#expense-entry-form").elements.booked_at.addEventListener("change", updateExpenseCompetenceField);
["description", "amount", "booked_at", "installment_current", "installment_total", "competence"].forEach((field) => {
  document.querySelector("#expense-entry-form").elements[field].addEventListener("input", refreshExpenseInstallmentPreview);
});

document.querySelector("#capture-record").addEventListener("click", toggleAudioRecording);
document.querySelector("#capture-clear-audio").addEventListener("click", clearCaptureAudio);
document.querySelector("#capture-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  document.querySelector("#capture-file-label").textContent = file
    ? `${file.name} • ${Math.max(1, Math.round(file.size / 1024))} KB`
    : "Toque para escolher um arquivo de até 25 MB";
});
document.querySelector("#capture-refresh").addEventListener("click", loadCapture);
document.querySelector("#capture-preview-items").addEventListener("change", (event) => {
  if (!event.target.classList.contains("capture-category")) return;
  const custom = event.target.closest(".capture-item").querySelector(".capture-new-category");
  custom.classList.toggle("hidden", event.target.value !== "__other__");
  const input = custom.querySelector("input");
  input.required = event.target.value === "__other__";
  if (!input.required) input.value = "";
});

document.querySelector("#capture-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.captureRecorder?.state === "recording") {
    return toast("Pare a gravação antes de analisar", true);
  }
  const fileInput = document.querySelector("#capture-file");
  if (state.captureAudio && fileInput.files.length) {
    return toast("Envie o áudio ou o documento em uma captura; não os dois ao mesmo tempo", true);
  }
  const data = new FormData(event.target);
  if (state.captureAudio) {
    data.delete("file");
    data.append("file", new File([state.captureAudio], "lancamento.webm", { type: state.captureAudio.type || "audio/webm" }));
  } else if (!fileInput.files.length) {
    data.delete("file");
  }
  if (!String(data.get("text") || "").trim() && !state.captureAudio && !fileInput.files.length) {
    return toast("Escreva uma mensagem, grave um áudio ou escolha um arquivo", true);
  }
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = "Analisando com segurança...";
  try {
    const capture = await api("/captures/preview", { method: "POST", body: data });
    renderCapturePreview(capture);
    await loadCapture();
    toast(capture.items.length ? "Prévia pronta para conferência" : "A captura precisa de mais informações", !capture.items.length);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Analisar e preparar prévia";
  }
});

document.querySelector("#capture-confirm").addEventListener("click", async () => {
  if (!state.captureDraft) return;
  const items = capturePayload();
  const selected = items.filter((item) => item.selected);
  if (!selected.length) return toast("Selecione ao menos um item", true);
  const invalid = selected.find((item) => !item.description || item.amount <= 0
    || (item.kind === "transaction" && (!item.booked_at || !item.account_id || !item.movement_type))
    || (item.kind === "transaction" && !item.category_id && item.movement_type === "expense" && !item.category_name)
    || (item.kind === "obligation" && !item.due_date)
    || (item.kind === "payroll" && (!item.competence || !item.payment_date)));
  if (invalid) return toast("Preencha os campos obrigatórios dos itens selecionados", true);
  const largeConfirmation = confirmLargeTransactions(selected);
  if (!largeConfirmation.allowed) return toast("Captura não confirmada; nenhum lançamento foi criado", true);
  const button = document.querySelector("#capture-confirm");
  button.disabled = true;
  button.textContent = "Confirmando...";
  try {
    const result = await api(`/captures/${state.captureDraft.id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ items, confirmed_large_amount: largeConfirmation.confirmed }),
    });
    closeCapturePreview();
    document.querySelector("#capture-form").reset();
    document.querySelector("#capture-file-label").textContent = "Toque para escolher um arquivo de até 25 MB";
    clearCaptureAudio();
    await loadCapture();
    toast(`Captura confirmada${result.review_items ? `; ${result.review_items} item(ns) para revisão` : ""}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Confirmar itens selecionados";
  }
});

document.querySelector("#capture-cancel").addEventListener("click", async () => {
  if (!state.captureDraft) return closeCapturePreview();
  if (!window.confirm("Cancelar esta captura sem criar lançamentos?")) return;
  try {
    await api(`/captures/${state.captureDraft.id}`, { method: "DELETE" });
    closeCapturePreview();
    await loadCapture();
    toast("Captura cancelada; nenhum lançamento foi criado");
  } catch (error) {
    toast(error.message, true);
  }
});

document.querySelector("#transaction-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = formJson(event.target, ["amount"]);
    const largeConfirmation = confirmLargeTransactions([{ ...payload, kind: "transaction" }]);
    if (!largeConfirmation.allowed) return toast("Lançamento cancelado; use o Consultor para simulações", true);
    payload.confirmed_large_amount = largeConfirmation.confirmed;
    await api("/transactions", { method: "POST", body: JSON.stringify(payload) });
    const month = payload.booked_at.slice(0, 7);
    event.target.reset();
    event.target.elements.booked_at.value = currentDateKey();
    document.querySelector("#transaction-month").value = month;
    await loadTransactions();
    toast("Lançamento registrado");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#transfer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = formJson(event.target, ["amount"]);
    if (payload.from_account_id && payload.from_account_id === payload.to_account_id) {
      return toast("Conta de origem e destino devem ser diferentes", true);
    }
    const largeConfirmation = confirmLargeTransactions([{ ...payload, kind: "transaction" }]);
    if (!largeConfirmation.allowed) return toast("Transferência cancelada; use o Consultor para simulações", true);
    payload.confirmed_large_amount = largeConfirmation.confirmed;
    await api("/transfers", { method: "POST", body: JSON.stringify(payload) });
    const month = payload.booked_at.slice(0, 7);
    event.target.reset();
    event.target.elements.booked_at.value = currentDateKey();
    document.querySelector("#transferencias-month").value = month;
    await loadTransferencias();
    await loadDashboard();
    toast("Transferência registrada");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#refresh-transferencias").addEventListener("click", loadTransferencias);

document.querySelector("#pay-invoice-with-redemption").addEventListener("change", (event) => {
  document.querySelector("#pay-invoice-redemption-amount-field").classList.toggle("hidden", !event.target.checked);
  if (!event.target.checked) document.querySelector("#pay-invoice-redemption-amount").value = "";
});

document.querySelector("#pay-invoice-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  if (!form.elements.card_transaction_id.value) {
    return toast("Escolha uma fatura pendente em \"Faturas pendentes\" antes de registrar o pagamento", true);
  }
  const withRedemption = document.querySelector("#pay-invoice-with-redemption").checked;
  const redemptionAmount = Number(document.querySelector("#pay-invoice-redemption-amount").value || 0);
  if (withRedemption && redemptionAmount <= 0) {
    return toast("Informe o valor a resgatar do Privilège DI", true);
  }
  const payload = {
    card_transaction_id: form.elements.card_transaction_id.value,
    paying_account_id: form.elements.paying_account_id.value,
    amount: Number(form.elements.amount.value),
    booked_at: form.elements.booked_at.value,
    description: form.elements.description.value,
    confirmed: form.elements.confirmed.checked,
  };
  const amountsToConfirm = [{ amount: payload.amount, kind: "transaction" }];
  if (withRedemption) amountsToConfirm.push({ amount: redemptionAmount, kind: "transaction" });
  const largeConfirmation = confirmLargeTransactions(amountsToConfirm);
  if (!largeConfirmation.allowed) return toast("Pagamento cancelado; use o Consultor para simulações", true);
  payload.confirmed_large_amount = largeConfirmation.confirmed;
  try {
    // Fluxo combinado (`docs/GO_LIVE_MANUAL_UX_PLAN.md`): resgate e
    // pagamento são dois comandos canônicos independentes, chamados em
    // sequência -- nunca um único endpoint novo. Se o resgate falhar, o
    // pagamento nunca é enviado; se o resgate for confirmado e o pagamento
    // falhar depois, o resgate já registrado permanece como fato auditável
    // e o usuário pode repetir apenas o pagamento.
    if (withRedemption) {
      await api("/transactions", {
        method: "POST",
        body: JSON.stringify({
          booked_at: payload.booked_at,
          description: `Resgate do Privilège DI para pagar fatura: ${payload.description}`,
          amount: redemptionAmount,
          movement_type: "redemption",
          account_id: payload.paying_account_id,
          confirmed_large_amount: largeConfirmation.confirmed,
        }),
      });
    }
    await api("/card-payment-reconciliations/pay", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    document.querySelector("#pay-invoice-summary").value = "";
    document.querySelector("#pay-invoice-redemption-amount-field").classList.add("hidden");
    form.elements.booked_at.value = currentDateKey();
    await loadPayables();
    await loadDashboard();
    toast(withRedemption ? "Resgate e pagamento da fatura registrados" : "Pagamento da fatura registrado");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#refresh-payables").addEventListener("click", loadPayables);

document.querySelector("#income-entry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = formJson(event.target, ["amount"]);
    payload.movement_type = "income";
    const largeConfirmation = confirmLargeTransactions([{ ...payload, kind: "transaction" }]);
    if (!largeConfirmation.allowed) return toast("Lançamento cancelado; use o Consultor para simulações", true);
    payload.confirmed_large_amount = largeConfirmation.confirmed;
    await api("/transactions", { method: "POST", body: JSON.stringify(payload) });
    const month = payload.booked_at.slice(0, 7);
    event.target.reset();
    event.target.elements.booked_at.value = currentDateKey();
    document.querySelector("#income-entry-month").value = month;
    await loadEntradas();
    await loadDashboard();
    toast("Entrada registrada");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#expense-entry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = formJson(event.target, ["amount"]);
    payload.movement_type = "expense";
    if (payload.category_id === "__other__") payload.category_id = null;
    else payload.category_name = null;
    if (payload.installment_current != null) payload.installment_current = Number(payload.installment_current);
    if (payload.installment_total != null) payload.installment_total = Number(payload.installment_total);
    const largeConfirmation = confirmLargeTransactions([{ ...payload, kind: "transaction" }]);
    if (!largeConfirmation.allowed) return toast("Lançamento cancelado; use o Consultor para simulações", true);
    payload.confirmed_large_amount = largeConfirmation.confirmed;
    await api("/transactions", { method: "POST", body: JSON.stringify(payload) });
    const month = payload.booked_at.slice(0, 7);
    event.target.reset();
    event.target.elements.booked_at.value = currentDateKey();
    document.querySelector("#expense-entry-month").value = month;
    await loadCategories();
    updateExpenseCustomCategoryField();
    updateExpenseCompetenceField();
    await refreshExpenseInstallmentPreview();
    await loadSaidas();
    await loadDashboard();
    toast("Saída registrada");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await api("/accounts", { method: "POST", body: JSON.stringify(formJson(event.target)) }); event.target.reset(); event.target.elements.owner_label.value = "Família"; await loadAccounts(); toast("Conta cadastrada"); }
  catch (error) { toast(error.message, true); }
});
const batchStatusLabels = {
  imported: "Importado",
  imported_with_review: "Importado (revisão)",
  review_required: "Revisão necessária",
  rejected: "Rejeitado",
};

function batchStatusChipClass(status) {
  if (status === "imported") return "ok";
  if (status === "rejected") return "severity-critical";
  return "warn";
}

// Renders exactly the per-file fields the backend already computed
// (`POST /api/imports/batch`) -- this never parses, classifies, reconciles,
// deduplicates or sums financial values on its own; the summary counts by
// status are also backend-computed (`result.summary.by_status`), never
// recalculated here.
function renderBatchResults(result) {
  const panel = document.querySelector("#batch-results-panel");
  document.querySelector("#batch-results-table").innerHTML = result.results.map((item) => {
    const statusLabel = batchStatusLabels[item.status] || escapeHtml(item.status);
    const reconciliationStatus = item.reconciliation ? item.reconciliation.status : null;
    return `<tr>
      <td><strong>${escapeHtml(item.filename)}</strong></td>
      <td><span class="status-chip ${batchStatusChipClass(item.status)}">${statusLabel}</span></td>
      <td>${item.records ?? "—"}</td>
      <td>${item.review_items ?? "—"}</td>
      <td>${reconciliationStatus ? escapeHtml(reconciliationStatus) : "—"}</td>
      <td><small>${item.message ? escapeHtml(item.message) : "—"}</small></td>
    </tr>`;
  }).join("");
  panel.classList.remove("hidden");
}

document.querySelector("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const isPayroll = event.target.elements.document_type.value === "payroll";
  if (!isPayroll && !state.accounts.length) return toast("Cadastre uma conta antes de importar", true);
  const files = [...event.target.elements.file.files];
  if (!files.length) return toast("Selecione ao menos um arquivo", true);
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true; button.textContent = "Processando...";
  try {
    if (files.length === 1) {
      const result = await api("/imports", { method: "POST", body: new FormData(event.target) });
      event.target.reset();
      document.querySelector("#batch-results-panel").classList.add("hidden");
      await loadImports();
      toast(`${result.records} registros processados; ${result.review_items || 0} para revisão`);
    } else {
      const body = new FormData();
      const accountId = event.target.elements.account_id.value;
      if (accountId) body.append("account_id", accountId);
      body.append("document_type", event.target.elements.document_type.value);
      files.forEach((file) => body.append("files", file));
      const result = await api("/imports/batch", { method: "POST", body });
      event.target.reset();
      renderBatchResults(result);
      await loadImports();
      const counts = Object.entries(result.summary.by_status || {}).map(([status, count]) => `${count} ${batchStatusLabels[status] || status}`).join(", ");
      toast(`Lote com ${result.file_count} arquivo(s) processado: ${counts}`);
    }
  }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Importar e verificar"; }
});
document.querySelector("#import-document-type").addEventListener("change", (event) => {
  const account = document.querySelector("#import-account");
  const isPayroll = event.target.value === "payroll";
  account.disabled = isPayroll;
  account.required = !isPayroll;
  document.querySelector("#import-account-field").classList.toggle("muted-field", isPayroll);
});
document.querySelector("#commission-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await api("/commissions", { method: "POST", body: JSON.stringify(formJson(event.target, ["gross_amount", "tax_rate", "delay_days"])) }); event.target.reset(); event.target.elements.tax_rate.value = "0.06"; event.target.elements.delay_days.value = "60"; await loadIncome(); toast("Comissão adicionada com imposto calculado"); }
  catch (error) { toast(error.message, true); }
});
document.querySelector("#payroll-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await api("/payroll", { method: "POST", body: JSON.stringify(formJson(event.target, ["gross_amount", "deductions", "net_amount", "payroll_loan"])) }); event.target.reset(); await loadIncome(); toast("Registro de folha adicionado"); }
  catch (error) { toast(error.message, true); }
});
document.querySelector("#obligation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await api("/obligations", { method: "POST", body: JSON.stringify(formJson(event.target, ["amount", "recurrence_months", "occurrence_count"])) }); event.target.reset(); event.target.elements.recurrence_months.value = "0"; event.target.elements.occurrence_count.value = "1"; await loadForecast(); toast("Compromisso incluído na projeção"); }
  catch (error) { toast(error.message, true); }
});
document.querySelector("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = formJson(event.target);
  payload.is_admin = payload.is_admin === "true";
  try { await api("/users", { method: "POST", body: JSON.stringify(payload) }); event.target.reset(); await loadUsers(); toast("Acesso criado com sucesso"); }
  catch (error) { toast(error.message, true); }
});
document.querySelector("#advisor-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = event.target.elements.message;
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  await askAdvisor(message);
});
document.querySelectorAll(".advisor-suggestion").forEach((button) => button.addEventListener("click", async () => {
  await askAdvisor(button.textContent.trim());
}));
document.querySelector("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const numeric = ["monthly_salary_net", "monthly_cash_cap", "emergency_floor", "food_allowance", "meal_allowance_daily", "workdays_month", "investment_balance", "investment_gross_annual_rate", "investment_income_tax_rate"];
  try { await api("/profile", { method: "PUT", body: JSON.stringify(formJson(event.target, numeric)) }); toast("Premissas salvas"); await loadDashboard(); }
  catch (error) { toast(error.message, true); }
});
document.querySelectorAll('#integrity-banner [data-view="integrity"]').forEach((button) => button.addEventListener("click", () => navigate("integrity")));
document.querySelector("#integrity-run-full").addEventListener("click", async () => {
  try {
    await api("/integrity/runs", { method: "POST", body: JSON.stringify({ scope: "global" }) });
    toast("Auditoria completa executada");
    await loadIntegrity();
  } catch (error) { toast(error.message, true); }
});
document.querySelector("#close-period").addEventListener("change", () => loadMonthlyClose().catch((error) => toast(error.message, true)));
document.querySelector("#close-run").addEventListener("click", async () => {
  const period = document.querySelector("#close-period").value || currentMonthKey();
  try {
    await api(`/monthly-closes/${period}/run`, { method: "POST" });
    toast("Fechamento executado; revise os findings antes de confiar");
    await loadIntegrity();
  } catch (error) { toast(error.message, true); }
});
document.querySelector("#close-trust").addEventListener("click", async () => {
  const period = document.querySelector("#close-period").value || currentMonthKey();
  try {
    await api(`/monthly-closes/${period}/trust`, { method: "POST" });
    toast("Fechamento marcado como trusted");
    await loadIntegrity();
  } catch (error) { toast(error.message, true); }
});
document.querySelector("#close-reopen").addEventListener("click", async () => {
  const period = document.querySelector("#close-period").value || currentMonthKey();
  const reason = window.prompt("Motivo para reabrir o fechamento (obrigatório):");
  if (reason === null) return;
  if (reason.trim().length < 3) { toast("Motivo deve ter ao menos 3 caracteres", true); return; }
  try {
    await api(`/monthly-closes/${period}/reopen`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) });
    toast("Fechamento reaberto; snapshot e findings anteriores foram preservados");
    await loadIntegrity();
  } catch (error) { toast(error.message, true); }
});
document.querySelector("#finding-filter-apply").addEventListener("click", () => loadFindings(1).catch((error) => toast(error.message, true)));
document.querySelector("#findings-page-prev").addEventListener("click", () => loadFindings(findingsPage - 1).catch((error) => toast(error.message, true)));
document.querySelector("#findings-page-next").addEventListener("click", () => loadFindings(findingsPage + 1).catch((error) => toast(error.message, true)));
document.querySelector("#finding-detail-close").addEventListener("click", () => document.querySelector("#finding-detail-panel").classList.add("hidden"));

let responsiveTableFrame = null;
const responsiveTableObserver = new MutationObserver(() => {
  cancelAnimationFrame(responsiveTableFrame);
  responsiveTableFrame = requestAnimationFrame(() => enhanceResponsiveTables());
});
responsiveTableObserver.observe(document.querySelector("#app-shell"), { childList: true, subtree: true });
enhanceResponsiveTables();

window.addEventListener("resize", () => {
  if (window.innerWidth > 760) setMobileMenu(false);
  if (state.forecast.length) drawForecast(state.forecast, state.forecastFloor);
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") setMobileMenu(false); });
document.querySelector("#transaction-form").elements.booked_at.value = currentDateKey();
document.querySelector("#transfer-form").elements.booked_at.value = currentDateKey();
document.querySelector("#pay-invoice-form").elements.booked_at.value = currentDateKey();
document.querySelector("#income-entry-form").elements.booked_at.value = currentDateKey();
document.querySelector("#expense-entry-form").elements.booked_at.value = currentDateKey();
updateExpenseCustomCategoryField();
bootstrap();
