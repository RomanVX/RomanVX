'use strict';

/* ════════ Auth (hardcoded, no backend) ════════ */
const CREDS = { user: 'admin', pass: 'admin' };

const $ = id => document.getElementById(id);
const screens = { login: $('loginScreen'), cabinet: $('cabinetScreen'), app: $('appScreen') };

function show(name) {
  Object.entries(screens).forEach(([k, el]) => el.classList.toggle('hidden', k !== name));
}

function doLogin() {
  const u = $('loginUser').value.trim();
  const p = $('loginPass').value;
  if (u === CREDS.user && p === CREDS.pass) {
    localStorage.setItem('mp_auth', '1');
    $('loginError').textContent = '';
    show('cabinet');
  } else {
    $('loginError').textContent = 'Неверный логин или пароль';
  }
}

function enterCabinet() {
  localStorage.setItem('mp_cabinet', 'biomed');
  show('app');
  initApp();
}

function logoutCabinet() {
  localStorage.removeItem('mp_cabinet');
  show('cabinet');
}

/* ════════ Formatting ════════ */
const fmt = (n, d = 0) => n == null ? '—'
  : Number(n).toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });
const rub = n => fmt(Math.round(n)) + ' ₽';
const signed = n => (n >= 0 ? '+' : '') + fmt(Math.round(n));

/* ════════ Filters / params ════════ */
function toISO(d) { return d.toISOString().slice(0, 10); }
function initDates() {
  const to = new Date(), from = new Date();
  from.setDate(from.getDate() - 30);
  $('dateTo').value = toISO(to);
  $('dateFrom').value = toISO(from);
}
function params() {
  const p = new URLSearchParams();
  const f = $('dateFrom').value, t = $('dateTo').value;
  if (f && t) { p.set('date_from', f); p.set('date_to', t); } else { p.set('days', '30'); }
  if ($('brandSel').value) p.set('brand', $('brandSel').value);
  if ($('catSel').value) p.set('category', $('catSel').value);
  return p.toString();
}
async function api(path) {
  const r = await fetch('/api/dashboard' + path + '?' + params());
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

async function loadFilters() {
  try {
    const f = await api('/filters');
    const bs = $('brandSel'), cs = $('catSel');
    bs.innerHTML = '<option value="">Все бренды</option>' +
      f.brands.map(b => `<option>${b}</option>`).join('');
    cs.innerHTML = '<option value="">Все категории</option>' +
      f.categories.map(c => `<option>${c}</option>`).join('');
  } catch (e) { console.error('filters', e); }
}

/* ════════ Views state ════════ */
const dirty = { dashboard: true, products: true, stocks: true, supplies: true };
let current = 'dashboard';
let charts = {};
let sparks = [];

function switchView(name) {
  current = name;
  document.querySelectorAll('nav.tabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.view === name));
  ['dashboard', 'products', 'stocks', 'supplies'].forEach(v =>
    $('view-' + v).classList.toggle('hidden', v !== name));
  if (dirty[name]) loadView(name);
}

function loadView(name) {
  const fn = { dashboard: loadDashboard, products: loadProducts, stocks: loadStocks, supplies: loadSupplies }[name];
  fn();
}

function applyFilters() {
  Object.keys(dirty).forEach(k => dirty[k] = true);
  loadView(current);
  $('updated').textContent = 'Обновлено: ' + new Date().toLocaleTimeString('ru-RU');
}

/* ════════ Dashboard ════════ */
async function loadDashboard() {
  dirty.dashboard = false;
  try {
    const d = await api('/finance');
    renderCards(d.cards);
    renderStructure(d.structure);
    renderTop5(d.top_skus);
  } catch (e) { console.error('finance', e); }
}

function renderCards(cards) {
  $('cardsGrid').innerHTML = cards.map(c => {
    const up = c.delta >= 0;
    const good = c.invert ? !up : up;
    const arrow = up ? '↑' : '↓';
    const valStr = c.unit === '%' ? fmt(c.value, 1) + '%' : (c.unit === '₽' ? rub(c.value) : fmt(c.value));
    const prevStr = c.unit === '%' ? fmt(c.prev, 1) + '%' : (c.unit === '₽' ? rub(c.prev) : fmt(c.prev));
    return `
      <div class="metric">
        <div class="m-head"><span class="ic">${c.icon}</span>${c.title}</div>
        <div class="m-val">${valStr}</div>
        <div class="m-sec">${c.secondary}</div>
        <div class="m-foot">
          <span class="m-prev">было: ${prevStr}</span>
          <span class="delta ${good ? 'up' : 'down'}">${arrow} ${signed(c.delta)} (${fmt(Math.abs(c.delta_pct),1)}%)</span>
        </div>
      </div>`;
  }).join('');
}

function renderStructure(rows) {
  const ctx = $('structChart').getContext('2d');
  if (charts.struct) charts.struct.destroy();
  charts.struct = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.label),
      datasets: [{
        data: rows.map(r => r.value),
        backgroundColor: rows.map(r => r.color),
        borderRadius: 6,
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => ' ' + rub(c.raw) } },
      },
      scales: {
        x: { ticks: { color: '#888', callback: v => fmt(v / 1000) + 'k' }, grid: { color: '#1e1e1e' } },
        y: { ticks: { color: '#bbb', font: { size: 11 } }, grid: { display: false } },
      },
    },
  });
}

function renderTop5(rows) {
  $('top5').innerHTML = rows.map(r => `
    <div class="t5-row">
      <div>
        <div class="t5-art">${r.supplierArticle || r.nmId}</div>
        <div class="t5-name">${r.subject}</div>
      </div>
      <div class="t5-val">${rub(r.total_revenue)}</div>
    </div>`).join('') || '<div class="t5-name">Нет данных</div>';
}

/* ════════ Products ════════ */
let prodData = [], prodSort = {};
async function loadProducts() {
  dirty.products = false;
  try {
    prodData = await api('/products');
    renderProducts(prodData);
  } catch (e) { console.error('products', e); }
}
function abcBadge(v) { return `<span class="badge abc-${v}">${v}</span>`; }
function renderProducts(rows) {
  $('prodBody').innerHTML = rows.map(r => `
    <tr>
      <td><div class="prod-cell">
        <div class="prod-thumb">${(r.subject || '?').slice(0,2).toUpperCase()}</div>
        <div class="prod-meta"><b title="${r.subject}">${r.subject}</b><span>${r.supplierArticle}</span></div>
      </div></td>
      <td>${rub(r.realization)}</td>
      <td>${rub(r.sales_after_spp)}</td>
      <td>${rub(r.for_pay)}</td>
      <td class="${r.profit >= 0 ? 'pos' : 'neg'}">${rub(r.profit)}</td>
      <td class="${r.margin >= 0 ? 'pos' : 'neg'}">${fmt(r.margin,1)}%</td>
      <td>${fmt(r.roi,1)}%</td>
      <td>${fmt(r.drr,1)}%</td>
      <td>${fmt(r.buyout,1)}%</td>
      <td>${fmt(r.orders)}</td>
      <td>${fmt(r.sold)}</td>
      <td>${fmt(r.returns)}</td>
      <td>${abcBadge(r.abc_rev)}</td>
      <td>${abcBadge(r.abc_profit)}</td>
    </tr>`).join('');
}
function sortProducts(col) {
  const asc = prodSort[col] = !prodSort[col];
  const sorted = [...prodData].sort((a, b) => {
    const va = a[col], vb = b[col];
    if (typeof va === 'number') return asc ? va - vb : vb - va;
    return asc ? String(va).localeCompare(String(vb), 'ru') : String(vb).localeCompare(String(va), 'ru');
  });
  renderProducts(sorted);
}
function searchProducts(q) {
  q = q.trim().toLowerCase();
  renderProducts(prodData.filter(r =>
    !q || String(r.supplierArticle).toLowerCase().includes(q) || (r.subject || '').toLowerCase().includes(q)));
}

/* ════════ Stocks / warehouses ════════ */
async function loadStocks() {
  dirty.stocks = false;
  try {
    const whs = await api('/warehouses');
    sparks.forEach(c => c.destroy()); sparks = [];
    $('whGrid').innerHTML = whs.map((w, i) => `
      <div class="wh-card">
        <div class="wh-top">
          <span class="wh-name">${w.warehouse}</span>
          <span class="wh-status ${w.status_ok ? 'ok' : 'bad'}">${w.status_ok ? 'Всё хорошо' : 'Срочно — пополнить'}</span>
        </div>
        <div class="wh-metrics">
          <div class="wm"><small>Продаж/день</small><b>${fmt(w.per_day,1)}</b></div>
          <div class="wm"><small>Остаток, шт</small><b>${fmt(w.stock_qty)}</b></div>
          <div class="wm"><small>Покрытие, дней</small><b>${w.coverage_days >= 999 ? '∞' : fmt(w.coverage_days,1)}</b></div>
          <div class="wm"><small>Выкуп / Возвраты</small><b>${fmt(w.buyout,0)}% / ${fmt(w.returns_pct,1)}%</b></div>
        </div>
        <canvas class="spark" id="spark${i}"></canvas>
      </div>`).join('');
    whs.forEach((w, i) => {
      const ctx = $('spark' + i).getContext('2d');
      sparks.push(new Chart(ctx, {
        type: 'line',
        data: { labels: w.trend.map((_, k) => k), datasets: [{
          data: w.trend, borderColor: w.status_ok ? '#2ecc71' : '#e74c3c',
          backgroundColor: 'transparent', pointRadius: 0, borderWidth: 2, tension: .4 }] },
        options: { plugins: { legend: { display: false } },
          scales: { x: { display: false }, y: { display: false } } },
      }));
    });
  } catch (e) { console.error('warehouses', e); }
}

/* ════════ Supplies ════════ */
let supData = [], supSort = {};
const PRIO = { urgent: '🔴 срочно', planned: '🟡 плановый', ok: '🟢 ок' };
async function loadSupplies() {
  dirty.supplies = false;
  try {
    supData = await api('/supplies');
    renderSupplies(supData);
  } catch (e) { console.error('supplies', e); }
}
function renderSupplies(rows) {
  $('supBody').innerHTML = rows.map(r => `
    <tr>
      <td><div class="prod-cell">
        <div class="prod-thumb">${(r.subject || '?').slice(0,2).toUpperCase()}</div>
        <div class="prod-meta"><b title="${r.subject}">${r.subject}</b><span>${r.supplierArticle}</span></div>
      </div></td>
      <td>${fmt(r.stock_qty)}</td>
      <td>${fmt(r.avg_daily_sales,2)}</td>
      <td>${r.coverage_days >= 999 ? '∞' : fmt(r.coverage_days,1)}</td>
      <td class="${r.need_30d>0?'need-hi':''}">${fmt(r.need_30d)}</td>
      <td>${fmt(r.need_60d)}</td>
      <td>${fmt(r.need_90d)}</td>
      <td class="prio ${r.priority}">${PRIO[r.priority]}</td>
    </tr>`).join('');
}
function sortSupplies(col) {
  const asc = supSort[col] = !supSort[col];
  const sorted = [...supData].sort((a, b) => {
    const va = a[col], vb = b[col];
    if (typeof va === 'number') return asc ? va - vb : vb - va;
    return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
  });
  renderSupplies(sorted);
}

/* ════════ Init ════════ */
function initApp() {
  initDates();
  loadFilters();
  switchView('dashboard');
}

document.addEventListener('DOMContentLoaded', () => {
  // login
  $('loginBtn').addEventListener('click', doLogin);
  [$('loginUser'), $('loginPass')].forEach(el =>
    el.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); }));
  $('cabBiomed').addEventListener('click', enterCabinet);
  $('changeCab').addEventListener('click', logoutCabinet);

  // nav + filters
  document.querySelectorAll('nav.tabs button').forEach(b =>
    b.addEventListener('click', () => switchView(b.dataset.view)));
  $('applyBtn').addEventListener('click', applyFilters);

  // table sorting
  document.querySelectorAll('#prodTable thead th').forEach(th =>
    th.addEventListener('click', () => sortProducts(th.dataset.col)));
  document.querySelectorAll('#supTable thead th').forEach(th =>
    th.addEventListener('click', () => sortSupplies(th.dataset.col)));
  $('prodSearch').addEventListener('input', e => searchProducts(e.target.value));

  // restore session
  if (localStorage.getItem('mp_auth') === '1') {
    if (localStorage.getItem('mp_cabinet')) { show('app'); initApp(); }
    else show('cabinet');
  } else {
    show('login');
  }
});
