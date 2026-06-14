'use strict';

const API = '';
let charts = {};
let sortState = {};
const dirty = { finance: true, products: true, stocks: true, supplies: true, unitec: true };
let currentTab = 'finance';
let prodAllData = [];

// ── Auth ──────────────────────────────────────────────────────────────────────

const CREDS = { user: 'admin', pass: 'admin' };

function showOverlay(name) {
  document.getElementById('loginOverlay').style.display   = name === 'login'   ? 'flex' : 'none';
  document.getElementById('cabinetOverlay').style.display = name === 'cabinet' ? 'flex' : 'none';
  const showApp = name === 'app';
  document.getElementById('mainNav').style.display     = showApp ? 'flex'  : 'none';
  document.getElementById('mainContent').style.display = showApp ? 'block' : 'none';
}

function doLogin() {
  const u = document.getElementById('loginUser').value.trim();
  const p = document.getElementById('loginPass').value;
  if (u === CREDS.user && p === CREDS.pass) {
    localStorage.setItem('mp_auth', '1');
    document.getElementById('loginError').textContent = '';
    showOverlay('cabinet');
  } else {
    document.getElementById('loginError').textContent = 'Неверный логин или пароль';
  }
}

function enterCabinet() {
  localStorage.setItem('mp_cabinet', 'biomed');
  showOverlay('app');
  initDashboard();
}

function changeCabinet() {
  localStorage.removeItem('mp_cabinet');
  showOverlay('cabinet');
}

// ── Date helpers ──────────────────────────────────────────────────────────────

function toISO(d) { return d.toISOString().slice(0, 10); }

function initDates(daysBack = 30) {
  const to = new Date();
  const from = new Date(to);
  from.setDate(from.getDate() - daysBack);
  document.getElementById('dateTo').value   = toISO(to);
  document.getElementById('dateFrom').value = toISO(from);
}

function getDateParams() {
  const from = document.getElementById('dateFrom').value;
  const to   = document.getElementById('dateTo').value;
  return from && to ? `date_from=${from}&date_to=${to}` : `days=30`;
}

function getParams() {
  let p = getDateParams();
  const brand = document.getElementById('brandFilter').value;
  const cat   = document.getElementById('catFilter').value;
  if (brand) p += `&brand=${encodeURIComponent(brand)}`;
  if (cat)   p += `&category=${encodeURIComponent(cat)}`;
  return p;
}

// ── Fetch ─────────────────────────────────────────────────────────────────────

async function fetchJSON(path) {
  const r = await fetch(`${API}${path}?${getParams()}`);
  if (!r.ok) {
    let detail = `${r.status}`;
    try { const body = await r.json(); detail = body.detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

function fmt(n, dec = 0) {
  if (n == null) return '—';
  return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: dec, maximumFractionDigits: dec });
}
function fmtRub(n) { return fmt(n) + ' ₽'; }

// ── Tab switching ─────────────────────────────────────────────────────────────

function switchTab(name, linkEl) {
  document.querySelectorAll('#mainTabs .nav-link').forEach(a => a.classList.remove('active'));
  if (linkEl) linkEl.classList.add('active');
  ['finance', 'products', 'stocks', 'supplies', 'unitec'].forEach(t => {
    document.getElementById('pane-' + t).style.display = t === name ? 'block' : 'none';
  });
  currentTab = name;
  if (dirty[name]) {
    dirty[name] = false;
    ({ finance: loadFinance, products: loadProducts, stocks: loadStocks,
       supplies: loadSupplies, unitec: loadUnitEc })[name]();
  }
}

function markAllDirty() {
  Object.keys(dirty).forEach(k => dirty[k] = true);
}

function loadAll() {
  document.getElementById('lastUpdated').textContent = 'Загрузка…';
  markAllDirty();
  switchTab(currentTab);
}

// ── Filters ───────────────────────────────────────────────────────────────────

async function loadFilters() {
  try {
    const d = await fetchJSON('/api/dashboard/filters');
    const bSel = document.getElementById('brandFilter');
    const cSel = document.getElementById('catFilter');
    const bVal = bSel.value, cVal = cSel.value;
    bSel.innerHTML = '<option value="">Все бренды</option>' +
      d.brands.map(b => `<option${b === bVal ? ' selected' : ''}>${b}</option>`).join('');
    cSel.innerHTML = '<option value="">Все категории</option>' +
      d.categories.map(c => `<option${c === cVal ? ' selected' : ''}>${c}</option>`).join('');
  } catch (e) { console.warn('filters', e); }
}

// ── Finance / Dashboard ───────────────────────────────────────────────────────

async function loadFinance() {
  try {
    const d = await fetchJSON('/api/dashboard/finance');
    renderCards(d.cards);
    renderStructure(d.structure);
    renderTop5(d.top_skus);
    await loadSalesChart();
    document.getElementById('lastUpdated').textContent = 'Обновлено: ' + new Date().toLocaleTimeString('ru-RU');
  } catch (e) {
    console.error('finance', e);
    document.getElementById('cardsGrid').innerHTML =
      `<div class="col-12 text-danger text-center py-3">Ошибка загрузки: ${e.message}</div>`;
  }
}

function renderCards(cards) {
  const grid = document.getElementById('cardsGrid');
  grid.innerHTML = cards.map(c => {
    const up = c.delta >= 0;
    const colorCls = c.invert ? (up ? 'down' : 'up') : (up ? 'up' : 'down');
    const arrow = up ? '↑' : '↓';
    const valStr  = c.unit === '%' ? fmt(c.value, 1)  + '%' : fmtRub(c.value);
    const prevStr = c.unit === '%' ? fmt(c.prev, 1)   + '%' : fmtRub(c.prev);
    const dAbs = Math.abs(c.delta);
    const deltaStr = c.unit === '%' ? fmt(dAbs, 1) + '%' : fmtRub(dAbs);
    return `<div class="col-6 col-md-4 col-xl-3">
      <div class="metric-card">
        <div class="mc-head">${c.icon} ${c.title}</div>
        <div class="mc-val">${valStr}</div>
        <div class="d-flex justify-content-between align-items-end mt-1 gap-1">
          <span class="mc-prev">${prevStr}</span>
          <span class="mc-delta ${colorCls}">${arrow} ${deltaStr}&nbsp;(${fmt(Math.abs(c.delta_pct), 1)}%)</span>
        </div>
        ${c.secondary ? `<div class="mc-sub">${c.secondary}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

function renderStructure(rows) {
  const ctx = document.getElementById('structureChart').getContext('2d');
  if (charts.structure) charts.structure.destroy();
  charts.structure = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.label),
      datasets: [{ data: rows.map(r => Math.abs(r.value)), backgroundColor: rows.map(r => r.color), borderRadius: 4 }],
    },
    options: {
      indexAxis: 'y', responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => ' ' + fmtRub(ctx.raw) } },
      },
      scales: {
        x: { ticks: { color: '#64748b', callback: v => fmt(v) }, grid: { color: '#1e2235' } },
        y: { ticks: { color: '#94a3b8', font: { size: 11 } } },
      },
    },
  });
}

function renderTop5(items) {
  const el = document.getElementById('top5List');
  if (!items || !items.length) { el.innerHTML = '<p class="text-secondary text-center py-3">Нет данных</p>'; return; }
  const max = items[0].total_revenue || 1;
  el.innerHTML = items.map((r, i) => {
    const pct  = Math.round(r.total_revenue / max * 100);
    const art  = r.supplierArticle || r.nmId;
    const name = (r.subject || String(art)).slice(0, 24);
    return `<div class="mb-3">
      <div class="d-flex justify-content-between mb-1">
        <span style="font-size:12px;color:#e2e8f0">${i + 1}. ${name}</span>
        <span style="font-size:12px;color:#c9a84c;white-space:nowrap">${fmtRub(r.total_revenue)}</span>
      </div>
      <div class="progress" style="height:4px">
        <div class="progress-bar" style="width:${pct}%;background:#c9a84c"></div>
      </div>
    </div>`;
  }).join('');
}

// ── Sales chart ───────────────────────────────────────────────────────────────

async function loadSalesChart() {
  try {
    const data = await fetchJSON('/api/dashboard/sales-dynamics');
    const ctx = document.getElementById('salesChart').getContext('2d');
    if (charts.sales) charts.sales.destroy();
    charts.sales = new Chart(ctx, {
      data: {
        labels: data.map(r => r.date),
        datasets: [
          {
            type: 'bar', label: 'Выручка ₽', data: data.map(r => r.revenue),
            backgroundColor: 'rgba(201,168,76,0.45)', borderColor: 'rgba(201,168,76,0.9)',
            borderWidth: 1, yAxisID: 'y',
          },
          {
            type: 'line', label: 'Заказы', data: data.map(r => r.orders_count),
            borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,0.1)',
            pointRadius: 2, tension: 0.4, yAxisID: 'y1',
          },
          {
            type: 'line', label: 'Продажи, шт', data: data.map(r => r.sales_count),
            borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,0.1)',
            pointRadius: 2, tension: 0.4, borderDash: [4, 3], yAxisID: 'y1',
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { color: '#94a3b8' } } },
        scales: {
          x:  { ticks: { color: '#64748b', maxRotation: 45 }, grid: { color: '#1e2235' } },
          y:  { position: 'left',  ticks: { color: '#94a3b8', callback: v => fmt(v) + ' ₽' }, grid: { color: '#1e2235' } },
          y1: { position: 'right', ticks: { color: '#38bdf8' }, grid: { drawOnChartArea: false } },
        },
      },
    });
  } catch (e) { console.error('salesChart', e); }
}

// ── Products ──────────────────────────────────────────────────────────────────

async function loadProducts() {
  const tbody = document.getElementById('prodBody');
  tbody.innerHTML = '<tr><td colspan="15" class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span></td></tr>';
  try {
    prodAllData = await fetchJSON('/api/dashboard/products');
    renderProdTable(prodAllData);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="15" class="text-danger text-center py-3">Ошибка: ${e.message}</td></tr>`;
  }
}

function abcBadge(v) {
  const cls = { A: 'badge-abc-A', B: 'badge-abc-B', C: 'badge-abc-C' }[v] || '';
  return `<span class="badge ${cls}">${v}</span>`;
}

function prodRowFn(r) {
  const mc = r.margin < 0 ? 'text-danger' : r.margin > 20 ? 'text-success' : '';
  const rc = r.roi < 0 ? 'text-danger' : r.roi > 50 ? 'text-success' : '';
  const bc = r.buyout < 40 ? 'text-danger' : r.buyout > 70 ? 'text-success' : '';
  return `
    <td>${r.supplierArticle || r.nmId}</td>
    <td class="text-truncate" style="max-width:180px" title="${r.subject}">${r.subject}</td>
    <td>${fmtRub(r.realization)}</td>
    <td>${fmtRub(r.sales_after_spp)}</td>
    <td>${fmtRub(r.for_pay)}</td>
    <td>${fmtRub(r.profit)}</td>
    <td class="${mc}">${fmt(r.margin, 1)}%</td>
    <td class="${rc}">${fmt(r.roi, 1)}%</td>
    <td>${fmt(r.drr, 1)}%</td>
    <td class="${bc}">${fmt(r.buyout, 1)}%</td>
    <td>${fmt(r.orders)}</td>
    <td>${fmt(r.sold)}</td>
    <td>${fmt(r.returns)}</td>
    <td>${abcBadge(r.abc_rev)}</td>
    <td>${abcBadge(r.abc_profit)}</td>`;
}

function renderProdTable(data) {
  const tbody = document.getElementById('prodBody');
  tbody._data  = data;
  tbody._rowFn = prodRowFn;
  tbody.innerHTML = data.length
    ? data.map(r => `<tr>${prodRowFn(r)}</tr>`).join('')
    : '<tr><td colspan="15" class="text-secondary text-center py-3">Нет данных</td></tr>';
  initSortable(document.getElementById('prodTable'));
}

// ── Stocks / Warehouses ───────────────────────────────────────────────────────

async function loadStocks() {
  const grid = document.getElementById('whGrid');
  grid.innerHTML = '<div class="col-12 text-center text-secondary py-4"><span class="spinner-border"></span></div>';
  try {
    const data = await fetchJSON('/api/dashboard/warehouses');
    renderWhGrid(data);
  } catch (e) {
    grid.innerHTML = `<div class="col-12 text-danger text-center py-3">Ошибка: ${e.message}</div>`;
  }
}

let _sparkCharts = {};

function _destroySparks() {
  Object.values(_sparkCharts).forEach(c => { try { c.destroy(); } catch {} });
  _sparkCharts = {};
}

function renderWhGrid(data) {
  _destroySparks();
  const grid = document.getElementById('whGrid');
  if (!data.length) {
    grid.innerHTML = '<div class="col-12 text-secondary text-center py-4">Нет данных</div>';
    return;
  }
  grid.innerHTML = data.map(wh => {
    const covCls = wh.coverage_days < 14 ? 'text-danger' : wh.coverage_days < 30 ? 'text-warning' : 'text-success';
    const cov    = wh.coverage_days >= 999 ? '∞' : fmt(wh.coverage_days, 1);
    const sid    = 'spark_' + wh.warehouse.replace(/[^a-zA-Z0-9]/g, '_');
    return `<div class="col-12 col-sm-6 col-xl-4">
      <div class="wh-card ${wh.status_ok ? 'wh-ok' : 'wh-low'}">
        <div class="d-flex justify-content-between align-items-start">
          <div>
            <div class="wh-name">${wh.warehouse}</div>
            <div class="wh-meta">
              <span class="me-2">📦 ${fmt(wh.stock_qty)} шт</span>
              <span>📈 ${fmt(wh.per_day, 1)}/день</span>
            </div>
          </div>
          <div class="text-end">
            <div class="wm ${covCls}">Покрытие: ${cov} д</div>
            <div class="wm text-secondary">Выкуп: ${fmt(wh.buyout, 1)}%</div>
            <div class="wm text-secondary">Возвраты: ${fmt(wh.returns_pct, 1)}%</div>
          </div>
        </div>
        <canvas id="${sid}" class="spark" height="40"></canvas>
      </div>
    </div>`;
  }).join('');

  data.forEach(wh => {
    const sid = 'spark_' + wh.warehouse.replace(/[^a-zA-Z0-9]/g, '_');
    const canvas = document.getElementById(sid);
    if (!canvas) return;
    const existing = Chart.getChart(canvas);
    if (existing) existing.destroy();
    _sparkCharts[sid] = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: wh.trend.map((_, i) => i),
        datasets: [{
          data: wh.trend,
          borderColor: wh.status_ok ? '#4ade80' : '#f87171',
          backgroundColor: wh.status_ok ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
          borderWidth: 2, pointRadius: 0, tension: 0.3, fill: true,
        }],
      },
      options: {
        responsive: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } },
      },
    });
  });
}

// ── Supplies ──────────────────────────────────────────────────────────────────

async function loadSupplies() {
  const tbody = document.getElementById('supBody');
  tbody.innerHTML = '<tr><td colspan="9" class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span></td></tr>';
  try {
    const data = await fetchJSON('/api/dashboard/supplies');
    renderSupTable(data);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="text-danger text-center py-3">Ошибка: ${e.message}</td></tr>`;
  }
}

function prioBadge(p) {
  return {
    urgent:  '<span class="prio-urgent">🔴 срочно</span>',
    planned: '<span class="prio-planned">🟡 плановый</span>',
    ok:      '<span class="prio-ok">🟢 ок</span>',
  }[p] || p;
}

function supRowFn(r) {
  const need = v => v > 0 ? `<span class="need-badge">${fmt(v)}</span>` : `<span class="text-secondary">0</span>`;
  const covCls = r.coverage_days < 14 ? 'coverage-low' : r.coverage_days < 30 ? 'coverage-med' : 'coverage-ok';
  const cov    = r.coverage_days >= 999 ? '∞' : fmt(r.coverage_days, 1);
  return `
    <td>${r.supplierArticle || r.nmId}</td>
    <td class="text-truncate" style="max-width:200px" title="${r.subject}">${r.subject}</td>
    <td>${fmt(r.stock_qty)}</td>
    <td>${fmt(r.avg_daily_sales, 2)}</td>
    <td class="${covCls}">${cov}</td>
    <td>${need(r.need_30d)}</td>
    <td>${need(r.need_60d)}</td>
    <td>${need(r.need_90d)}</td>
    <td>${prioBadge(r.priority)}</td>`;
}

function renderSupTable(data) {
  const tbody = document.getElementById('supBody');
  tbody._data  = data;
  tbody._rowFn = supRowFn;
  tbody.innerHTML = data.length
    ? data.map(r => `<tr>${supRowFn(r)}</tr>`).join('')
    : '<tr><td colspan="9" class="text-secondary text-center py-3">Нет данных</td></tr>';
  initSortable(document.getElementById('supTable'));
}

// ── Unit economics ────────────────────────────────────────────────────────────

async function loadUnitEc() {
  const tbody = document.getElementById('unitecBody');
  tbody.innerHTML = '<tr><td colspan="13" class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span></td></tr>';
  try {
    const d = await fetchJSON('/api/dashboard/unit-economics');
    document.getElementById('unitecCostBadge').textContent =
      d.costs_loaded ? `✓ себестоимость из файла (${d.costs_loaded} арт.)` : 'себестоимость оценочная';
    renderUnitecTable(d.rows);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="13" class="text-danger text-center py-3">Ошибка: ${e.message}</td></tr>`;
  }
}

function unitecRowFn(r) {
  const pc = r.profit_per < 0 ? 'text-danger' : r.profit_per > 0 ? 'text-success' : '';
  const mc = r.margin < 0 ? 'text-danger' : r.margin > 20 ? 'text-success' : '';
  const rc = r.roi < 0 ? 'text-danger' : r.roi > 50 ? 'text-success' : '';
  const src = r.cost_source === 'file'
    ? '<i class="bi bi-file-earmark-check text-success" title="Из файла"></i>'
    : '<i class="bi bi-calculator text-secondary" title="Оценочно"></i>';
  return `
    <td>${r.supplierArticle || r.nmId}</td>
    <td class="text-truncate" style="max-width:160px" title="${r.subject}">${r.subject}</td>
    <td>${fmt(r.sold)}</td>
    <td>${fmtRub(r.avg_price)}</td>
    <td>${fmtRub(r.cost_per_unit)} ${src}</td>
    <td>${fmtRub(r.commission_per)}</td>
    <td>${fmtRub(r.logistics_per)}</td>
    <td>${fmtRub(r.storage_per)}</td>
    <td>${fmtRub(r.ad_per)}</td>
    <td>${fmtRub(r.tax_per)}</td>
    <td class="${pc} fw-bold">${fmtRub(r.profit_per)}</td>
    <td class="${mc}">${fmt(r.margin, 1)}%</td>
    <td class="${rc}">${fmt(r.roi, 1)}%</td>`;
}

function renderUnitecTable(data) {
  const tbody = document.getElementById('unitecBody');
  tbody._data  = data;
  tbody._rowFn = unitecRowFn;
  tbody.innerHTML = data.length
    ? data.map(r => `<tr>${unitecRowFn(r)}</tr>`).join('')
    : '<tr><td colspan="13" class="text-secondary text-center py-3">Нет данных</td></tr>';
  initSortable(document.getElementById('unitecTable'));
}

// ── Cost file upload ──────────────────────────────────────────────────────────

async function uploadCosts(input) {
  const file = input.files[0];
  if (!file) return;
  const status = document.getElementById('costStatus');
  status.textContent = 'Загрузка…';
  status.style.color = '#94a3b8';
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/upload/costs', { method: 'POST', body: fd });
    if (!r.ok) {
      const err = await r.json();
      throw new Error(err.detail || r.status);
    }
    const d = await r.json();
    status.textContent = `✓ Загружено ${d.loaded} артикулов`;
    status.style.color = '#4ade80';
    // mark unit-ec dirty so it reloads with new costs
    dirty.unitec = true;
    if (currentTab === 'unitec') { dirty.unitec = false; loadUnitEc(); }
  } catch (e) {
    status.textContent = `Ошибка: ${e.message}`;
    status.style.color = '#f87171';
  }
  input.value = '';
}

// ── Table helpers ─────────────────────────────────────────────────────────────

function initSortable(tableEl) {
  if (!tableEl) return;
  tableEl.querySelectorAll('thead th[data-col]').forEach(th => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      const tbody = tableEl.querySelector('tbody');
      if (!tbody || !tbody._data) return;
      const key = tbody.id + col;
      const asc = sortState[key] === false;
      sortState[key] = asc;
      const sorted = [...tbody._data].sort((a, b) => {
        const va = a[col], vb = b[col];
        if (typeof va === 'number') return asc ? va - vb : vb - va;
        return asc ? String(va).localeCompare(String(vb), 'ru') : String(vb).localeCompare(String(va), 'ru');
      });
      tbody._data = sorted;
      tbody.innerHTML = sorted.map(r => `<tr>${tbody._rowFn(r)}</tr>`).join('');
    });
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

function initDashboard() {
  initDates(30);

  document.getElementById('daysSelect').addEventListener('change', e => {
    initDates(parseInt(e.target.value, 10));
    loadAll();
  });

  ['dateFrom', 'dateTo'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => {
      document.getElementById('daysSelect').value = '';
      loadAll();
    });
  });

  document.getElementById('brandFilter').addEventListener('change', () => { markAllDirty(); switchTab(currentTab); });
  document.getElementById('catFilter').addEventListener('change',   () => { markAllDirty(); switchTab(currentTab); });

  document.getElementById('prodSearch').addEventListener('input', e => {
    const q = e.target.value.trim().toLowerCase();
    const filtered = q
      ? prodAllData.filter(r =>
          (r.supplierArticle || '').toLowerCase().includes(q) ||
          (r.subject || '').toLowerCase().includes(q))
      : prodAllData;
    renderProdTable(filtered);
  });

  loadFilters();
  switchTab('finance', document.querySelector('#mainTabs .nav-link'));
  setInterval(() => { markAllDirty(); switchTab(currentTab); }, 5 * 60 * 1000);
}

document.addEventListener('DOMContentLoaded', () => {
  ['loginUser', 'loginPass'].forEach(id =>
    document.getElementById(id).addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); })
  );

  if (localStorage.getItem('mp_auth') === '1') {
    if (localStorage.getItem('mp_cabinet')) {
      showOverlay('app');
      initDashboard();
    } else {
      showOverlay('cabinet');
    }
  } else {
    showOverlay('login');
  }
});
