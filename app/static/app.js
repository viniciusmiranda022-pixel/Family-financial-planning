const state = { user: null, accounts: [], categories: [], forecast: [], forecastFloor: 0 };
const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const dateFormat = new Intl.DateTimeFormat("pt-BR", { timeZone: "UTC" });
const monthFormat = new Intl.DateTimeFormat("pt-BR", { month: "long", year: "numeric", timeZone: "UTC" });
const pageNames = {
  dashboard: "Visão geral",
  imports: "Importações",
  transactions: "Lançamentos",
  reviews: "Revisar",
  income: "Rendas",
  planning: "Planejamento",
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
    imports: loadImports,
    transactions: loadTransactions,
    reviews: loadReviews,
    income: loadIncome,
    planning: loadForecast,
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
}

async function loadCategories() {
  state.categories = await api("/categories");
}

function emptyRow(columns, text = "Nenhum registro encontrado") {
  return `<tr><td colspan="${columns}" class="empty">${escapeHtml(text)}</td></tr>`;
}

function currentMonthKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
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
  document.querySelector("#kpi-spending").textContent = money.format(summary.spending);
  document.querySelector("#kpi-cap-caption").textContent = `de ${money.format(summary.cash_cap)} em ${selectedLabel}`;
  document.querySelector("#kpi-remaining").textContent = money.format(summary.remaining_cap);
  document.querySelector("#kpi-reviews").textContent = summary.review_count;
  document.querySelector("#nav-review-count").textContent = summary.review_count;
  document.querySelector("#food-benefits").textContent = money.format(summary.food_benefits);
  const percent = summary.cash_cap > 0 ? Math.round((summary.spending / summary.cash_cap) * 100) : 0;
  const width = Math.min(percent, 100);
  const bar = document.querySelector("#budget-progress");
  bar.style.width = `${width}%`;
  bar.classList.toggle("over", percent > 100);
  document.querySelector("#budget-percent").textContent = `${percent}%`;
  document.querySelector("#budget-used").textContent = `${money.format(summary.spending)} usados`;
  document.querySelector("#budget-total").textContent = `${money.format(summary.cash_cap)} de teto`;
  const qualityItems = [
    [summary.review_count === 0, "Fila de revisão", summary.review_count === 0 ? "Sem pendências" : `${summary.review_count} itens`],
    [state.accounts.length > 0, "Contas cadastradas", state.accounts.length ? `${state.accounts.length} fontes` : "Cadastre a primeira"],
    [summary.cash_cap > 0, "Perfil financeiro", summary.cash_cap > 0 ? "Premissas configuradas" : "Configuração pendente"],
  ];
  if (summary.duplicates_ignored > 0) qualityItems.unshift([true, "Consolidação automática", `${summary.duplicates_ignored} cópia(s) ignorada(s) no mês`]);
  document.querySelector("#quality-list").innerHTML = qualityItems.map(([ok, title, detail]) => `<div class="quality-item"><div><span class="quality-dot${ok ? "" : " warn"}"></span><strong>${escapeHtml(title)}</strong></div><small>${escapeHtml(detail)}</small></div>`).join("");
  const duplicateNote = document.querySelector("#monthly-duplicates-note");
  duplicateNote.classList.toggle("hidden", summary.duplicates_ignored === 0);
  duplicateNote.textContent = summary.duplicates_ignored > 0
    ? `${summary.duplicates_ignored} lançamento(s) repetido(s) entre a planilha consolidada e importações anteriores foram desconsiderados.`
    : "";
  document.querySelector("#monthly-categories-table").innerHTML = summary.category_spending.length
    ? summary.category_spending.map((item) => {
      const share = summary.spending > 0 ? Math.round((item.amount / summary.spending) * 100) : 0;
      return `<tr><td><strong>${escapeHtml(item.category)}</strong></td><td class="right">${share}%</td><td class="right amount-expense">${money.format(item.amount)}</td></tr>`;
    }).join("")
    : emptyRow(3, `Nenhum gasto considerado em ${selectedLabel}`);
  document.querySelector("#cut-plan-savings").textContent = money.format(cutPlan.potential_monthly_savings);
  document.querySelector("#cut-plan-period").textContent = cutPlan.covered_months
    ? `${cutPlan.covered_months} mês(es) com dados entre ${dateFormat.format(new Date(`${cutPlan.window_start}T00:00:00Z`))} e ${dateFormat.format(new Date(`${cutPlan.window_end}T00:00:00Z`))}`
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

async function loadTransactions() {
  const month = document.querySelector("#transaction-month").value;
  const items = await api(`/transactions?limit=500${month ? `&month=${month}` : ""}`);
  document.querySelector("#transactions-table").innerHTML = items.length ? items.map((item) => `
    <tr data-id="${escapeHtml(item.id)}">
      <td>${dateFormat.format(new Date(`${item.date}T00:00:00Z`))}</td>
      <td><strong>${escapeHtml(item.description)}</strong>${item.installment ? `<br><small>Parcela ${escapeHtml(item.installment)}</small>` : ""}</td>
      <td><select class="category-select" data-id="${escapeHtml(item.id)}">${categoryOptions(item.category_id)}</select></td>
      <td>${escapeHtml(item.owner)}</td><td>${escapeHtml(item.account)}</td>
      <td>${item.possible_duplicate ? '<span class="status-chip warn">Possível duplicidade</span>' : item.excluded ? '<span class="status-chip muted">Excluído do cálculo</span>' : '<span class="status-chip ok">Considerado</span>'}</td>
      <td class="right ${item.amount < 0 ? "amount-expense" : "amount-income"}">${money.format(item.amount)}</td>
    </tr>
  `).join("") : emptyRow(7);
  document.querySelectorAll(".category-select").forEach((select) => select.addEventListener("change", async (event) => {
    try {
      await api(`/transactions/${event.target.dataset.id}`, { method: "PATCH", body: JSON.stringify({ category_id: event.target.value, reviewed: true }) });
      toast("Categoria atualizada e item revisado");
    } catch (error) { toast(error.message, true); }
  }));
}

async function loadReviews() {
  const items = await api("/reviews");
  document.querySelector("#nav-review-count").textContent = items.length;
  document.querySelector("#reviews-list").innerHTML = items.length ? items.map((item) => `
    <article class="review-card"><div class="review-icon">!</div><div><h3>${escapeHtml(item.description)}</h3><p>${escapeHtml(item.details || item.reason)}${item.amount !== null ? ` • ${money.format(item.amount)}` : ""}</p></div><button class="secondary resolve-review" data-id="${escapeHtml(item.id)}">Marcar como resolvido</button></article>
  `).join("") : '<div class="empty">Nenhuma pendência. Todos os lançamentos estão conciliados.</div>';
  document.querySelectorAll(".resolve-review").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/reviews/${button.dataset.id}/resolve`, { method: "POST" }); await loadReviews(); toast("Pendência resolvida"); }
    catch (error) { toast(error.message, true); }
  }));
}

async function loadIncome() {
  const [commissions, payroll] = await Promise.all([api("/commissions"), api("/payroll")]);
  document.querySelector("#commissions-table").innerHTML = commissions.length ? commissions.map((item) => `
    <tr><td>${dateFormat.format(new Date(`${item.expected_date}T00:00:00Z`))}</td><td>${escapeHtml(item.description)}</td><td class="right">${money.format(item.gross)}</td><td class="right amount-expense">${money.format(item.tax)}</td><td class="right amount-income">${money.format(item.net)}</td></tr>
  `).join("") : emptyRow(5, "Nenhuma comissão cadastrada");
  const kindLabels = { regular: "Salário", "13_first": "1ª do 13º", "13_second": "2ª do 13º", vacation_extra: "Férias adicionais", other: "Outro" };
  document.querySelector("#payroll-table").innerHTML = payroll.length ? payroll.map((item) => `
    <tr><td>${dateFormat.format(new Date(`${item.payment_date}T00:00:00Z`))}</td><td>${escapeHtml(item.person_name)}</td><td>${kindLabels[item.kind] || escapeHtml(item.kind)}</td><td class="right amount-income">${money.format(item.net)}</td></tr>
  `).join("") : emptyRow(4, "Nenhum holerite cadastrado");
}

async function loadProfile() {
  const profile = await api("/profile");
  const form = document.querySelector("#profile-form");
  Object.entries(profile).forEach(([key, value]) => { if (form.elements[key] && value !== null) form.elements[key].value = value; });
}

async function loadForecast() {
  const data = await api("/forecast");
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
  drawForecast(data.rows, data.summary.emergency_floor);
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
document.querySelector("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const numeric = ["monthly_salary_net", "monthly_cash_cap", "emergency_floor", "food_allowance", "meal_allowance_daily", "workdays_month", "investment_balance", "investment_gross_annual_rate", "investment_income_tax_rate"];
  try { await api("/profile", { method: "PUT", body: JSON.stringify(formJson(event.target, numeric)) }); toast("Premissas salvas"); await loadDashboard(); }
  catch (error) { toast(error.message, true); }
});

window.addEventListener("resize", () => { if (state.forecast.length) drawForecast(state.forecast, state.forecastFloor); });
bootstrap();
