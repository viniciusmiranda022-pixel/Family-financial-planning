const state = {
  user: null,
  accounts: [],
  categories: [],
  forecast: [],
  forecastFloor: 0,
  captureDraft: null,
  captureAudio: null,
  captureRecorder: null,
  advisorHistory: [],
};
const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const dateFormat = new Intl.DateTimeFormat("pt-BR", { timeZone: "UTC" });
const monthFormat = new Intl.DateTimeFormat("pt-BR", { month: "long", year: "numeric", timeZone: "UTC" });
const accountTypeLabels = { checking: "Conta corrente", credit_card: "Cartão de crédito", investment: "Investimento", cash: "Dinheiro", other: "Outros" };
const systemCategories = new Set(["Conciliação", "Transferência patrimonial", "Transferência interna", "Repasses a confirmar", "Reembolsos e estornos", "Receitas", "Revisar"]);
const pageNames = {
  dashboard: "Visão geral",
  capture: "Lançar agora",
  imports: "Importações",
  transactions: "Lançamentos",
  reviews: "Revisar",
  income: "Rendas",
  planning: "Planejamento",
  advisor: "Consultor financeiro",
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
    const detail = typeof payload.detail === "string" ? payload.detail : "Não foi possível concluir a operação";
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

function showAuth(configured) {
  document.querySelector("#app-shell").classList.add("hidden");
  document.querySelector("#auth-shell").classList.remove("hidden");
  document.querySelector("#login-form").classList.toggle("hidden", !configured);
  document.querySelector("#setup-form").classList.toggle("hidden", configured);
}

async function showApp() {
  document.querySelector("#auth-shell").classList.add("hidden");
  document.querySelector("#app-shell").classList.remove("hidden");
  document.querySelector("#current-user").textContent = state.user.name;
  document.querySelector("#nav-users").classList.toggle("hidden", !state.user.is_admin);
  await Promise.all([loadAccounts(), loadCategories()]);
  await navigate("dashboard");
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
  document.querySelectorAll(".view").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll("#main-nav button").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  document.querySelector(`#view-${view}`).classList.add("active");
  document.querySelector("#page-title").textContent = pageNames[view];
  const loaders = {
    dashboard: loadDashboard,
    capture: loadCapture,
    imports: loadImports,
    transactions: loadTransactions,
    reviews: loadReviews,
    income: loadIncome,
    planning: loadForecast,
    advisor: loadAdvisor,
    users: loadUsers,
    settings: loadProfile,
  };
  try { await loaders[view]?.(); } catch (error) { toast(error.message, true); }
}

async function loadAccounts() {
  state.accounts = await api("/accounts");
  const select = document.querySelector("#import-account");
  select.innerHTML = state.accounts.length
    ? state.accounts.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("")
    : '<option value="">Cadastre uma conta primeiro</option>';
  const transactionSelect = document.querySelector("#transaction-account");
  transactionSelect.innerHTML = state.accounts.length
    ? state.accounts.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("")
    : '<option value="">Cadastre uma conta primeiro</option>';
  const captureSelect = document.querySelector("#capture-account");
  if (captureSelect) {
    const selected = captureSelect.value;
    captureSelect.innerHTML = '<option value="">Escolher na prévia</option>' + state.accounts.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} • ${escapeHtml(item.owner_label)}</option>`).join("");
    if ([...captureSelect.options].some((option) => option.value === selected)) captureSelect.value = selected;
  }
}

async function loadCategories() {
  state.categories = await api("/categories");
  document.querySelector("#transaction-category").innerHTML = manualCategoryOptions();
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

async function loadDashboard() {
  const monthControl = document.querySelector("#dashboard-month");
  if (!monthControl.value) monthControl.value = currentMonthKey();
  const selectedMonth = monthControl.value;
  const [summary, transactions, cutPlan] = await Promise.all([
    api(`/dashboard?month=${encodeURIComponent(selectedMonth)}`),
    api(`/transactions?limit=8&month=${encodeURIComponent(selectedMonth)}`),
    api(`/cut-plan?month=${encodeURIComponent(selectedMonth)}`),
  ]);
  const selectedLabel = monthLabel(summary.month);
  monthControl.value = summary.month;
  document.querySelector("#dashboard-period-label").textContent = selectedLabel;
  document.querySelector("#monthly-categories-title").textContent = `Gastos de ${selectedLabel} por categoria`;
  document.querySelector("#kpi-investment").textContent = money.format(summary.investment_balance);
  document.querySelector("#kpi-cash-in").textContent = money.format(summary.cash_in);
  document.querySelector("#kpi-cash-out").textContent = money.format(summary.cash_out);
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

function updateCustomCategoryField() {
  const select = document.querySelector("#transaction-category");
  const field = document.querySelector("#transaction-custom-category-field");
  const input = document.querySelector("#transaction-custom-category");
  const custom = select.value === "__other__" && !select.disabled;
  field.classList.toggle("hidden", !custom);
  input.disabled = !custom;
  input.required = custom;
}

function updateManualTransactionFields() {
  const movementType = document.querySelector("#transaction-movement-type").value;
  const category = document.querySelector("#transaction-category");
  const categoryField = document.querySelector("#transaction-category-field");
  const needsCategory = movementType === "expense";
  category.disabled = !needsCategory;
  category.required = needsCategory;
  categoryField.classList.toggle("muted-field", !needsCategory);
  updateCustomCategoryField();
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
    loading.textContent = result.answer;
    loading.className = `advisor-message assistant ${result.status}`;
    loading.dataset.source = result.provider === "codex" ? "Explicado pelo Codex" : "Motor financeiro local";
    state.advisorHistory.push(
      { role: "user", content: message },
      { role: "assistant", content: result.answer },
    );
    state.advisorHistory = state.advisorHistory.slice(-8);
  } catch (error) {
    loading.textContent = error.message;
    loading.className = "advisor-message assistant error";
  }
  loading.scrollIntoView({ behavior: "smooth", block: "end" });
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
document.querySelector("#refresh-transactions").addEventListener("click", loadTransactions);
document.querySelector("#refresh-forecast").addEventListener("click", loadForecast);
document.querySelector("#transaction-movement-type").addEventListener("change", updateManualTransactionFields);
document.querySelector("#transaction-category").addEventListener("change", updateCustomCategoryField);

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
  const button = document.querySelector("#capture-confirm");
  button.disabled = true;
  button.textContent = "Confirmando...";
  try {
    const result = await api(`/captures/${state.captureDraft.id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ items }),
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
    if (payload.category_id === "__other__") payload.category_id = null;
    else payload.category_name = null;
    await api("/transactions", { method: "POST", body: JSON.stringify(payload) });
    const month = payload.booked_at.slice(0, 7);
    event.target.reset();
    event.target.elements.booked_at.value = currentDateKey();
    document.querySelector("#transaction-month").value = month;
    await loadCategories();
    updateManualTransactionFields();
    await loadTransactions();
    toast("Lançamento registrado");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("#account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await api("/accounts", { method: "POST", body: JSON.stringify(formJson(event.target)) }); event.target.reset(); event.target.elements.owner_label.value = "Família"; await loadAccounts(); toast("Conta cadastrada"); }
  catch (error) { toast(error.message, true); }
});
document.querySelector("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const isPayroll = event.target.elements.document_type.value === "payroll";
  if (!isPayroll && !state.accounts.length) return toast("Cadastre uma conta antes de importar", true);
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true; button.textContent = "Processando...";
  try { const result = await api("/imports", { method: "POST", body: new FormData(event.target) }); event.target.reset(); await loadImports(); toast(`${result.records} registros processados; ${result.review_items || 0} para revisão`); }
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

window.addEventListener("resize", () => { if (state.forecast.length) drawForecast(state.forecast, state.forecastFloor); });
document.querySelector("#transaction-form").elements.booked_at.value = currentDateKey();
updateManualTransactionFields();
bootstrap();
