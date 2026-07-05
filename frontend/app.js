'use strict';

// ── Тема (тёмная/светлая) ─────────────────────────────────────────────────────
// Атрибут data-theme ставится ещё в <head> (до отрисовки), здесь только читаем.
// Canvas-графики не понимают CSS-переменные, поэтому для Chart.js — сырые hex,
// подобранные под активную тему. Переключение перерисовывает страницу целиком.
const _THEME = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
const CHART_C = _THEME === 'light'
  ? { tick: '#5b6472', tick2: '#8a93a5', grid: '#e2e5ee', legend: '#3d4454', gridSoft: 'rgba(20,25,45,.08)', pos: '#16a34a' }
  : { tick: '#94a3b8', tick2: '#64748b', grid: '#1e2235', legend: '#cbd5e1', gridSoft: 'rgba(255,255,255,.06)', pos: '#4ade80' };

function toggleTheme() {
  const next = _THEME === 'light' ? 'dark' : 'light';
  localStorage.setItem('mp_theme', next);
  location.reload();   // графики на canvas требуют полной перерисовки с новой палитрой
}

function _initThemeBtn() {
  const btn = document.getElementById('themeBtn');
  if (btn) btn.innerHTML = _THEME === 'light' ? '<i class="bi bi-moon-stars"></i>' : '<i class="bi bi-sun"></i>';
  if (window.Chart) Chart.defaults.color = CHART_C.tick;
}
document.addEventListener('DOMContentLoaded', _initThemeBtn);

const API = '';
let charts = {};
let sortState = {};
const dirty = { salesan: true, stocks: true, reviews: true, history: true, finance: true, unit: true };
let _advertData = [];
let currentTab = 'finance';
let prodAllData = [];

// ── Группировка по брендам + спец-разбивки (фисты / спреи для минета) ──────────
const BRAND_ORDER = ['Джага', 'Satisfucktion', 'Aloe'];
const SUBGROUPS = [
  { name: 'Фисты',             skus: ['BMN-0013', 'BMN-0028', 'BMN-0035', 'BMN-0036', 'ST-07'] },
  { name: 'Спреи для минета',  skus: ['BMN-0115', 'BMN-0116', 'BMN-0110'] },
];
const GROUP_ORDER = ['Фисты', 'Aloe', 'Спреи для минета', 'Satisfucktion', 'Джага', 'Прочее'];

function articleGroup(r) {
  const sku = r.supplierArticle || r.sku || '';
  for (const sg of SUBGROUPS) if (sg.skus.includes(sku)) return sg.name;
  return BRAND_ORDER.includes(r.brand) ? r.brand : 'Прочее';
}

// Среднее продаж/день по платформам (для сортировки в остатках)
function avgPerDay(r) {
  const vals = [r.wb_per_day, r.oz_per_day, r.ym_per_day].map(v => +v || 0);
  return (vals[0] + vals[1] + vals[2]) / 3;
}

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
  const fromEl = document.getElementById('dateFrom');
  const toEl   = document.getElementById('dateTo');
  const from = fromEl && fromEl.value;
  const to   = toEl && toEl.value;
  return from && to ? `date_from=${from}&date_to=${to}` : `days=30`;
}

function getParams() {
  let p = getDateParams();
  const bEl = document.getElementById('brandFilter');
  const cEl = document.getElementById('catFilter');
  if (bEl && bEl.value) p += `&brand=${encodeURIComponent(bEl.value)}`;
  if (cEl && cEl.value) p += `&category=${encodeURIComponent(cEl.value)}`;
  return p;
}

// ── Fetch ─────────────────────────────────────────────────────────────────────

async function fetchJSON(path, timeoutMs = 60000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let r;
  try {
    r = await fetch(`${API}${path}?${getParams()}`, { signal: ctrl.signal });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('Таймаут запроса (' + Math.round(timeoutMs/1000) + 'с)');
    throw e;
  }
  clearTimeout(timer);
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
function skuName(r) {
  const art = r.supplierArticle || r.nmId;
  return r.subject && r.subject !== art ? `${art} <span class="text-secondary small">${r.subject}</span>` : String(art);
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function switchTab(name, linkEl) {
  document.querySelectorAll('#mainTabs .nav-link').forEach(a => a.classList.remove('active'));
  if (linkEl) linkEl.classList.add('active');
  ['salesan', 'stocks', 'reviews', 'history', 'finance', 'unit'].forEach(t => {
    const el = document.getElementById('pane-' + t);
    if (el) el.style.display = t === name ? 'block' : 'none';
  });
  currentTab = name;
  if (dirty[name]) {
    dirty[name] = false;
    ({ salesan: loadSalesAnalytics, stocks: loadStocks,
       reviews: loadReviews, history: loadHistory, finance: loadFinance,
       unit: loadUnitEconomics })[name]();
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
        x: { ticks: { color: CHART_C.tick2, callback: v => fmt(v) }, grid: { color: CHART_C.grid } },
        y: { ticks: { color: CHART_C.tick, font: { size: 11 } } },
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
        <span style="font-size:12px;color:var(--val-soft)">${i + 1}. ${name}</span>
        <span style="font-size:12px;color:var(--gold);white-space:nowrap">${fmtRub(r.total_revenue)}</span>
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
            borderColor: CHART_C.pos, backgroundColor: 'rgba(74,222,128,0.1)',
            pointRadius: 2, tension: 0.4, borderDash: [4, 3], yAxisID: 'y1',
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { color: CHART_C.tick } } },
        scales: {
          x:  { ticks: { color: CHART_C.tick2, maxRotation: 45 }, grid: { color: CHART_C.grid } },
          y:  { position: 'left',  ticks: { color: CHART_C.tick, callback: v => fmt(v) + ' ₽' }, grid: { color: CHART_C.grid } },
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
    <td class="text-truncate" style="max-width:180px" title="${r.subject || ''}">${r.subject || '—'}</td>
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

// ── Stocks table ─────────────────────────────────────────────────────────────

let _stocksData = [];
let _stocksSortCol = 'days_to_oos';
let _stocksSortAsc = true;

async function loadRecommendations(forceRefresh = false) {
  const modal = new bootstrap.Modal(document.getElementById('recoModal'));
  const body = document.getElementById('recoModalBody');
  const ts = document.getElementById('recoGeneratedAt');
  body.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border"></span><p class="mt-2 small">Анализирую данные...</p></div>';
  ts.textContent = '';
  modal.show();
  try {
    const url = '/api/dashboard/supply-recommendations' + (forceRefresh ? '?refresh=1' : '');
    if (forceRefresh) await fetchJSON('/api/dashboard/supply-recommendations/invalidate', {method:'POST'}).catch(()=>{});
    const data = await fetchJSON(url);
    // Простое markdown → HTML: **bold**, заголовки, переносы
    const html = (data.text || '')
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
      .replace(/^#{1,3} (.+)$/gm,'<strong style="font-size:15px">$1</strong>')
      .replace(/\n/g,'<br>');
    body.innerHTML = html;
    ts.textContent = data.generated_at ? `Сформировано: ${data.generated_at}` : '';
  } catch (e) {
    body.innerHTML = `<div class="text-danger">Ошибка: ${e.message}</div>`;
  }
}

async function loadStocks() {
  const wrap = document.getElementById('stocksTableWrap');
  wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border"></span></div>';
  try {
    _stocksData = await fetchJSON('/api/dashboard/stocks_table');
    renderStocksTable();
  } catch (e) {
    wrap.innerHTML = `<div class="text-danger text-center py-3">Ошибка: ${e.message}</div>`;
  }
}

function exportStocksExcel() {
  // сервер отдаёт готовый .xlsx с Content-Disposition: attachment
  window.location.href = `${API}/api/dashboard/stocks_export`;
}

function _stocksSort(col) {
  if (_stocksSortCol === col) {
    _stocksSortAsc = !_stocksSortAsc;
  } else {
    _stocksSortCol = col;
    _stocksSortAsc = (col === 'days_to_oos' || col === 'supplierArticle' || col === 'subject');
  }
  renderStocksTable();
}

function renderStocksTable() {
  const wrap = document.getElementById('stocksTableWrap');
  if (!_stocksData.length) {
    wrap.innerHTML = '<div class="text-secondary text-center py-4">Нет данных</div>';
    return;
  }

  const STATUS_ORD  = { red: 0, yellow: 1, green: 2 };
  const STATUS_ICON = { red: '🔴', yellow: '🟡', green: '🟢' };
  const STATUS_CLS  = { red: 'text-danger fw-bold', yellow: 'text-warning', green: 'text-success' };

  const col = _stocksSortCol;
  const asc = _stocksSortAsc;

  const sorted = [..._stocksData].sort((a, b) => {
    if (col === 'status') {
      const d = STATUS_ORD[a.status] - STATUS_ORD[b.status];
      return asc ? d : -d;
    }
    const av = a[col] ?? '', bv = b[col] ?? '';
    const d = typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv), 'ru');
    return asc ? d : -d;
  });

  // Group by brand — preserve BRAND_ORDER, unknown → "Прочее"
  // Within each group: sort by wb_per_day desc, fallback oz_per_day desc
  const groupMap = {};
  sorted.forEach(r => {
    const g = articleGroup(r);
    (groupMap[g] = groupMap[g] || []).push(r);
  });
  Object.values(groupMap).forEach(rows => {
    rows.sort((a, b) => avgPerDay(b) - avgPerDay(a));
  });
  const orderedGroups = GROUP_ORDER
    .filter(g => groupMap[g])
    .map(g => [g, groupMap[g]]);

  function thSort(c, label, tip) {
    const active = _stocksSortCol === c;
    const arrow  = active ? (_stocksSortAsc ? ' ↑' : ' ↓') : '';
    const t      = tip ? ` title="${tip}"` : '';
    return `<th style="cursor:pointer;white-space:nowrap"${t} onclick="_stocksSort('${c}')">${label}${arrow}</th>`;
  }

  function cell(qty, perDay, days) {
    const st  = days <= 20 ? 'red' : days <= 45 ? 'yellow' : 'green';
    const oos = days >= 999
      ? '<span class="text-secondary">∞</span>'
      : `<span class="${STATUS_CLS[st]}">${days}</span>`;
    const q = qty > 0
      ? `<span style="color:var(--val);font-weight:600">${fmt(qty)}</span>`
      : '<span class="text-secondary">—</span>';
    const v = perDay > 0
      ? `<span style="color:var(--val);font-weight:600">${fmt(perDay, 1)}</span>`
      : '<span class="text-secondary">—</span>';
    return `<td class="text-end" style="border-left:2px solid var(--sep)">${q}</td><td class="text-end">${v}</td><td class="text-end">${oos}</td>`;
  }

  const header = `<thead class="sticky-top">
    <tr style="background:var(--t-row)">
      ${thSort('supplierArticle','Артикул')}
      ${thSort('name','Название')}
      ${thSort('brand','Бренд')}
      <th class="text-center" colspan="3" style="background:#a21caf;color:white;border-left:2px solid var(--sep)">WB</th>
      <th class="text-center" colspan="3" style="background:#1d4ed8;color:white;border-left:2px solid var(--sep)">OZON</th>
      <th class="text-center" colspan="3" style="background:#854d0e;color:white;border-left:2px solid var(--sep)">YM</th>
    </tr>
    <tr class="small" style="background:var(--surface-2);color:var(--muted)">
      <th></th><th></th><th></th>
      <th class="text-end" style="border-left:2px solid var(--sep)" title="Остаток WB (quantity)">Ост</th>
      <th class="text-end" title="Продаж/день WB">Пр/д</th>
      <th class="text-end" title="Дней до OOS">Дней</th>
      <th class="text-end" style="border-left:2px solid var(--sep)" title="Остаток Ozon">Ост</th>
      <th class="text-end" title="Продаж/день Ozon">Пр/д</th>
      <th class="text-end" title="Дней до OOS">Дней</th>
      <th class="text-end" style="border-left:2px solid var(--sep)" title="Остаток YM">Ост</th>
      <th class="text-end" title="Продаж/день YM">Пр/д</th>
      <th class="text-end" title="Дней до OOS">Дней</th>
    </tr>
  </thead>`;

  const bodyRows = orderedGroups.map(([grp, rows]) => {
    const grpRow = `<tr class="table-secondary">
      <td colspan="12"><strong>${grp}</strong> <span class="text-secondary small">(${rows.length} арт.)</span></td>
    </tr>`;
    const itemRows = rows.map(r => `<tr style="font-size:14px">
      <td><code style="color:var(--val-soft)">${r.supplierArticle}</code></td>
      <td style="color:var(--val)">${r.name}</td>
      <td class="text-secondary small">${r.brand}</td>
      ${cell(r.wb_qty, r.wb_per_day, r.wb_days)}
      ${cell(r.oz_qty, r.oz_per_day, r.oz_days)}
      ${cell(r.ym_qty, r.ym_per_day, r.ym_days)}
    </tr>`).join('');
    return grpRow + itemRows;
  }).join('');

  wrap.innerHTML = `<div class="table-responsive">
    <table class="table table-sm table-hover align-middle mb-0">
      ${header}
      <tbody>${bodyRows}</tbody>
    </table>
  </div>`;
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
  tbody.innerHTML = '<tr><td colspan="16" class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span></td></tr>';
  try {
    const d = await fetchJSON('/api/dashboard/unit-economics');
    const costBadge = document.getElementById('unitecCostBadge');
    const srcBadge  = document.getElementById('unitecSourceBadge');
    costBadge.textContent = d.costs_loaded
      ? `✓ себестоимость из файла (${d.costs_loaded} арт.)`
      : 'себестоимость не загружена';
    if (d.source === 'report') {
      srcBadge.textContent = 'данные из финотчёта WB';
      srcBadge.style.display = '';
    } else {
      srcBadge.style.display = 'none';
    }
    renderUnitecTable(d.rows);
    loadMonthlyPivot();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="16" class="text-danger text-center py-3">Ошибка: ${e.message}</td></tr>`;
  }
}

function unitecRowFn(r) {
  // supports both real report rows and estimated rows
  const profit  = r.profit  ?? r.profit_per  ?? 0;
  const margin  = r.margin  ?? 0;
  const roi     = r.roi     ?? 0;
  const pc = profit < 0 ? 'text-danger fw-bold' : profit > 0 ? 'text-success fw-bold' : 'fw-bold';
  const mc = margin < 0 ? 'text-danger' : margin > 20 ? 'text-success' : '';
  const rc = roi < 0 ? 'text-danger' : roi > 50 ? 'text-success' : '';

  // cost source icon
  let costCell;
  if (r.cost_source === 'file') {
    costCell = `${fmtRub(r.cost_per_unit)} <i class="bi bi-file-earmark-check text-success" title="Из файла"></i>`;
  } else if (r.cost_source === 'missing') {
    costCell = `<span class="text-danger">— нет</span>`;
  } else {
    costCell = `${fmtRub(r.cost_per_unit)} <i class="bi bi-calculator text-secondary" title="Оценочно"></i>`;
  }

  const name = r.name || r.subject || '—';
  const qty  = r.sale_qty ?? r.sold ?? 0;
  const buyRub = r.buy_rub ?? 0;

  if (r.buy_rub !== undefined) {
    // Real report row
    return `
      <td>${r.supplierArticle}</td>
      <td class="text-truncate" style="max-width:200px" title="${name}">${name}</td>
      <td>${fmt(qty)}<small class="text-secondary"> (-${fmt(r.return_qty??0)})</small></td>
      <td>${fmtRub(buyRub)}</td>
      <td>${costCell}</td>
      <td>${fmtRub(r.cost_total)}</td>
      <td>${fmtRub(r.logistics)}</td>
      <td>${fmtRub(r.commission)}</td>
      <td>${fmtRub(r.acquiring)}</td>
      <td>${fmtRub(r.storage)}</td>
      <td>${r.penalty ? fmtRub(r.penalty) : '—'}</td>
      <td>${r.deduction ? fmtRub(r.deduction) : '—'}</td>
      <td>${fmtRub(r.tax)}</td>
      <td class="${pc}">${fmtRub(profit)}</td>
      <td class="${mc}">${fmt(margin, 1)}%</td>
      <td class="${rc}">${r.cost_total ? fmt(roi, 1) + '%' : '—'}</td>`;
  } else {
    // Estimated (fallback) row
    return `
      <td>${r.supplierArticle || r.nmId}</td>
      <td class="text-truncate" style="max-width:200px" title="${r.subject||''}">${r.subject||'—'}</td>
      <td>${fmt(r.sold)}</td>
      <td>${fmtRub(r.avg_price)}</td>
      <td>${costCell}</td>
      <td>—</td>
      <td>${fmtRub(r.logistics_per)}</td>
      <td>${fmtRub(r.commission_per)}</td>
      <td>—</td>
      <td>${fmtRub(r.storage_per)}</td>
      <td>—</td>
      <td>—</td>
      <td>${fmtRub(r.tax_per)}</td>
      <td class="${pc}">${fmtRub(r.profit_per)}</td>
      <td class="${mc}">${fmt(margin, 1)}%</td>
      <td class="${rc}">${fmt(roi, 1)}%</td>`;
  }
}

function renderUnitecTable(data) {
  const tbody = document.getElementById('unitecBody');
  tbody._data  = data;
  tbody._rowFn = unitecRowFn;
  tbody.innerHTML = data.length
    ? data.map(r => `<tr>${unitecRowFn(r)}</tr>`).join('')
    : '<tr><td colspan="16" class="text-secondary text-center py-3">Нет данных</td></tr>';
  initSortable(document.getElementById('unitecTable'));
}

async function loadMonthlyPivot() {
  const tbody = document.getElementById('pivotBody');
  if (!tbody) return;
  try {
    const d = await fetchJSON('/api/dashboard/monthly-pivot');
    renderMonthlyPivot(d.rows);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="14" class="text-danger text-center py-3">Ошибка свода: ${e.message}</td></tr>`;
  }
}

function pivotRowFn(r) {
  const pc = r.profit < 0 ? 'text-danger fw-bold' : 'text-success fw-bold';
  const mc = r.margin < 0 ? 'text-danger' : r.margin > 20 ? 'text-success' : '';
  return `
    <td class="fw-semibold">${r.label}</td>
    <td>${fmt(r.sale_qty)}</td>
    <td class="text-secondary">${fmt(r.return_qty)}</td>
    <td>${fmtRub(r.buy_rub)}</td>
    <td class="text-secondary">${fmtRub(r.logistics)}</td>
    <td class="text-secondary">${fmtRub(r.commission)}</td>
    <td class="text-secondary">${fmtRub(r.acquiring)}</td>
    <td class="text-secondary">${fmtRub(r.storage)}</td>
    <td class="text-secondary">${r.penalty ? fmtRub(r.penalty) : '—'}</td>
    <td class="text-secondary">${r.deduction ? fmtRub(r.deduction) : '—'}</td>
    <td>${fmtRub(r.for_pay)}</td>
    <td class="text-secondary">${fmtRub(r.tax)}</td>
    <td class="${pc}">${fmtRub(r.profit)}</td>
    <td class="${mc}">${fmt(r.margin, 1)}%</td>`;
}

function renderMonthlyPivot(data) {
  const tbody = document.getElementById('pivotBody');
  if (!tbody) return;
  tbody._data  = data;
  tbody._rowFn = pivotRowFn;
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="14" class="text-secondary text-center py-3">Нет данных за период</td></tr>';
    return;
  }
  // Add totals row
  const totals = data.reduce((t, r) => {
    ['sale_qty','return_qty','buy_rub','logistics','commission','acquiring',
     'storage','penalty','deduction','for_pay','tax','profit'].forEach(k => t[k] = (t[k]||0) + (r[k]||0));
    return t;
  }, {});
  totals.label = 'ИТОГО';
  totals.margin = totals.buy_rub ? Math.round(totals.profit / totals.buy_rub * 1000) / 10 : 0;

  tbody.innerHTML = data.map(r => `<tr>${pivotRowFn(r)}</tr>`).join('') +
    `<tr class="table-active fw-bold">${pivotRowFn(totals)}</tr>`;
  initSortable(document.getElementById('pivotTable'));
}

// ── Cost file upload ──────────────────────────────────────────────────────────

async function uploadCosts(input) {
  const file = input.files[0];
  if (!file) return;
  const status = document.getElementById('costStatus');
  status.textContent = 'Загрузка…';
  status.style.color = CHART_C.tick;
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
    status.style.color = 'var(--pos)';
    // mark unit-ec dirty so it reloads with new costs
    dirty.unitec = true;
    if (currentTab === 'unitec') { dirty.unitec = false; loadUnitEc(); }
  } catch (e) {
    status.textContent = `Ошибка: ${e.message}`;
    status.style.color = 'var(--neg)';
  }
  input.value = '';
}

// ── Продвижение (Advert) ──────────────────────────────────────────────────────

async function loadAdvert() {
  document.getElementById('advertCards').innerHTML =
    '<div class="col-12 text-center text-secondary py-4"><span class="spinner-border"></span></div>';
  document.getElementById('advertBody').innerHTML =
    '<tr><td colspan="13" class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span></td></tr>';
  try {
    const d = await fetchJSON('/api/advert/campaigns');
    if (d.mock) {
      const hint = d.hint || 'Задайте WB_ADVERT_KEY в переменных окружения Render';
      document.getElementById('advertCards').innerHTML =
        `<div class="col-12 py-3">
          <div class="alert alert-warning mb-0">
            <i class="bi bi-key-fill me-2"></i><strong>Нет ключа для Advert API.</strong><br>
            Для доступа к рекламным кампаниям нужен отдельный токен WB.<br>
            Добавьте переменную <code>WB_ADVERT_KEY</code> в Render → Environment Variables.<br>
            <small class="text-muted">Получить токен: WB личный кабинет → Настройки → Доступ к API → Реклама</small>
          </div>
        </div>`;
      document.getElementById('advertBody').innerHTML =
        '<tr><td colspan="13" class="text-secondary text-center py-3">—</td></tr>';
      return;
    }
    _advertData = d.campaigns || [];
    renderAdvertSummary(_advertData);
    renderAdvertTable(_advertData);
  } catch (e) {
    document.getElementById('advertCards').innerHTML =
      `<div class="col-12 text-danger text-center py-3">Ошибка загрузки: ${e.message}</div>`;
    document.getElementById('advertBody').innerHTML =
      `<tr><td colspan="13" class="text-danger text-center py-3">—</td></tr>`;
  }
}

function renderAdvertSummary(data) {
  const active  = data.filter(c => c.status_code === 7);
  const totViews  = data.reduce((s, c) => s + c.views,  0);
  const totClicks = data.reduce((s, c) => s + c.clicks, 0);
  const totSpend  = data.reduce((s, c) => s + c.spend,  0);
  const totOrders = data.reduce((s, c) => s + c.orders, 0);
  const totRev    = data.reduce((s, c) => s + c.revenue,0);
  const avgCtr    = totViews  ? totClicks / totViews  * 100 : 0;
  const avgCpc    = totClicks ? totSpend  / totClicks        : 0;
  const avgCpo    = totOrders ? totSpend  / totOrders        : 0;
  const avgDrr    = totRev    ? totSpend  / totRev    * 100  : 0;

  const cards = [
    { icon: '📢', title: 'Активных кампаний', value: active.length,  unit: 'шт', prev: data.length,  prevLabel: 'всего' },
    { icon: '👁', title: 'Показы',             value: totViews,       unit: 'шт' },
    { icon: '🖱', title: 'Клики',              value: totClicks,      unit: 'шт' },
    { icon: '📊', title: 'CTR',                value: avgCtr,         unit: '%',  dec: 2 },
    { icon: '💰', title: 'Расход',             value: totSpend,       unit: '₽' },
    { icon: '🛒', title: 'Заказы с рекламы',   value: totOrders,      unit: 'шт' },
    { icon: '📦', title: 'Выручка с рекламы',  value: totRev,         unit: '₽' },
    { icon: '🎯', title: 'ДРР',                value: avgDrr,         unit: '%',  dec: 1 },
    { icon: '💳', title: 'CPC (ср.)',           value: avgCpc,         unit: '₽',  dec: 2 },
    { icon: '📬', title: 'CPO (ср.)',           value: avgCpo,         unit: '₽',  dec: 0 },
  ];

  document.getElementById('advertCards').innerHTML = cards.map(c => {
    const val = c.unit === '₽' ? fmtRub(c.value)
              : c.unit === '%'  ? fmt(c.value, c.dec ?? 1) + '%'
              : fmt(c.value);
    const sub = c.prevLabel ? `<div class="mc-sub">${fmt(c.prev)} ${c.prevLabel}</div>` : '';
    return `<div class="col-6 col-md-4 col-xl-2">
      <div class="metric-card">
        <div class="mc-head">${c.icon} ${c.title}</div>
        <div class="mc-val">${val}</div>
        ${sub}
      </div>
    </div>`;
  }).join('');
}

function advertStatusBadge(status, code) {
  const cls = code === 7 ? 'success' : code === 8 ? 'warning' : code === 9 ? 'secondary' : 'info';
  return `<span class="badge bg-${cls}">${status}</span>`;
}

function advertTypeBadge(type) {
  const cls = type === 'Автокампания' ? 'primary' : type.includes('Поиск') ? 'info' : 'secondary';
  return `<span class="badge bg-${cls} text-dark">${type}</span>`;
}

function advertRowFn(r) {
  const drrCls = r.drr > 20 ? 'text-danger' : r.drr > 10 ? 'text-warning' : r.drr > 0 ? 'text-success' : '';
  return `
    <td class="text-truncate" style="max-width:200px" title="${r.name}">${r.name}</td>
    <td>${advertTypeBadge(r.type)}</td>
    <td>${advertStatusBadge(r.status, r.status_code)}</td>
    <td>${fmt(r.views)}</td>
    <td>${fmt(r.clicks)}</td>
    <td>${fmt(r.ctr, 2)}%</td>
    <td>${fmtRub(r.cpc)}</td>
    <td class="fw-bold">${fmtRub(r.spend)}</td>
    <td>${fmt(r.orders)}</td>
    <td>${fmtRub(r.revenue)}</td>
    <td>${fmt(r.cr, 2)}%</td>
    <td>${r.cpo ? fmtRub(r.cpo) : '—'}</td>
    <td class="${drrCls}">${r.drr ? fmt(r.drr, 1) + '%' : '—'}</td>`;
}

function renderAdvertTable(data) {
  const tbody = document.getElementById('advertBody');
  tbody._data  = data;
  tbody._rowFn = advertRowFn;
  tbody.innerHTML = data.length
    ? data.map(r => `<tr>${advertRowFn(r)}</tr>`).join('')
    : '<tr><td colspan="13" class="text-secondary text-center py-3">Нет рекламных кампаний</td></tr>';
  initSortable(document.getElementById('advertTable'));
}

function filterAdvertTable() {
  const status = document.getElementById('advertStatusFilter').value;
  const type   = document.getElementById('advertTypeFilter').value;
  const q      = document.getElementById('advertSearch').value.trim().toLowerCase();
  const filtered = _advertData.filter(c =>
    (!status || c.status === status) &&
    (!type   || c.type === type) &&
    (!q      || c.name.toLowerCase().includes(q))
  );
  renderAdvertTable(filtered);
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

// ── Sales Analytics ───────────────────────────────────────────────────────────

let _wsData = null;
// ── Заказы по неделям с разбивкой по SKU ─────────────────────────────────────

let _ordersData = null;
let _ordersMp = 'WB';
let _ordersCompact = false;   // true → показываем только текущую и прошлую неделю

const _MP_COLORS = { WB: '#c026d3', OZON: '#3b82f6', YM: '#b45309', TOTAL: '#059669' };
const _MP_LABEL  = { WB: 'WB (Wildberries)', OZON: 'Ozon', YM: 'Яндекс Маркет', TOTAL: 'Все площадки' };

async function loadSalesAnalytics() {
  await loadOrders();
}

function setOrdersMp(mp) {
  _ordersMp = mp;
  ['WB','OZON','YM','TOTAL'].forEach(m => {
    const el = document.getElementById('ordMp' + m);
    if (el) el.classList.toggle('active', m === mp);
  });
  renderOrdersTable();
}

async function reloadOrders() {
  await fetch(`${API}/api/dashboard/weekly_orders/invalidate`, { method: 'POST' }).catch(() => {});
  _ordersData = null;
  await loadOrders();
}

async function loadOrders() {
  const tbl = document.getElementById('ordersTable');
  if (tbl) tbl.innerHTML =
    '<tr><td class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span> Загрузка…</td></tr>';
  try {
    _ordersData = await fetchJSON('/api/dashboard/weekly_orders');
    renderOrdersTable();
    loadOrdersMonthly();  // подгружаем месячную разбивку параллельно
  } catch (e) {
    if (tbl) tbl.innerHTML = `<tr><td class="text-danger py-3">Ошибка: ${e.message}</td></tr>`;
  }
}

let _monthlyData = null;

async function loadOrdersMonthly() {
  const wrap = document.getElementById('ordersMonthlyWrap');
  if (!wrap) return;
  renderOrdersMonthly();
}

function renderOrdersMonthly() {
  const wrap = document.getElementById('ordersMonthlyWrap');
  if (!wrap || !_ordersData) return;

  // Считаем месяцы из уже загруженных weekly_orders — без доп. запроса
  // weeks приходят как "27 мая – 2 июн", берём начальную дату из заголовков
  const weeks = _ordersData.weeks || [];
  if (!weeks.length) return;

  // Парсим метки недель в месяц (берём первые 3 символа названия месяца из конца)
  // Формат из бэкенда: "27 мая – 2 июн"  или  "30 июн – 6 июл"
  function weekToMonthKey(label) {
    // Формат бэкенда: "27.05 - 02.06"
    // берём правую часть (конец недели), извлекаем месяц
    const parts = label.split('-');
    const end = (parts[parts.length - 1] || parts[0]).trim();
    // ожидаем "DD.MM"
    const m = end.match(/(\d{2})\.(\d{2})/);
    if (!m) return null;
    const month = parseInt(m[2], 10);  // 1-12
    const now = new Date();
    let year = now.getFullYear();
    // если месяц конца недели впереди текущего — это прошлый год
    if (month > now.getMonth() + 1) year--;
    return `${year}-${String(month).padStart(2,'0')}`;
  }

  const MPs = [
    { key: 'WB',   label: 'WB',   color: '#c026d3' },
    { key: 'OZON', label: 'Ozon', color: '#3b82f6' },
    { key: 'YM',   label: 'ЯМ',   color: '#b45309' },
  ];

  // Собираем по месяцам
  const monthKeys = [];
  const monthMap = {};   // monthKey → index
  weeks.forEach((w, wi) => {
    const mk = weekToMonthKey(w);
    if (!mk) return;
    if (!(mk in monthMap)) { monthMap[mk] = monthKeys.length; monthKeys.push(mk); }
  });
  if (!monthKeys.length) return;

  const nm = monthKeys.length;
  const data = {};
  MPs.forEach(({ key }) => {
    data[key] = { rub: Array(nm).fill(0), qty: Array(nm).fill(0),
                  cancel_rub: Array(nm).fill(0) };
    const block = _ordersData[key];
    if (!block) return;
    (block.skus || []).forEach(s => {
      weeks.forEach((w, wi) => {
        const mk = weekToMonthKey(w);
        if (mk === null || !(mk in monthMap)) return;
        const mi = monthMap[mk];
        data[key].rub[mi]        += s.rub[wi] || 0;
        data[key].qty[mi]        += s.qty[wi] || 0;
        if (s.cancel_rub) data[key].cancel_rub[mi] += s.cancel_rub[wi] || 0;
      });
    });
  });

  // Итого
  data['total'] = { rub: Array(nm).fill(0), qty: Array(nm).fill(0), cancel_rub: Array(nm).fill(0) };
  MPs.forEach(({ key }) => {
    data[key].rub.forEach((v,i) => { data['total'].rub[i] += v; });
    data[key].cancel_rub.forEach((v,i) => { data['total'].cancel_rub[i] += v; });
  });

  const RU_MONTH_NAMES = ['Янв','Фев','Мар','Апр','Май','Июн','Июл','Авг','Сен','Окт','Ноя','Дек'];
  const monthLabels = monthKeys.map(mk => {
    const [y, m] = mk.split('-');
    return RU_MONTH_NAMES[parseInt(m)-1] + ' ' + y.slice(2);
  });

  const allMPs = [...MPs, { key: 'total', label: 'Итого', color: '#059669' }];

  let html = `<div class="card border-0 bg-card mt-2">
    <div class="card-header bg-transparent border-0 py-2 d-flex align-items-center gap-2">
      <span class="fw-semibold small">📅 По месяцам</span>
    </div>
    <div class="card-body p-0"><div class="table-responsive">
    <table class="table table-sm align-middle mb-0 text-nowrap" style="font-size:0.8rem">
    <thead><tr>
      <th style="min-width:80px;position:sticky;left:0;background:var(--t-sticky)">Площадка</th>`;
  monthLabels.forEach(m => { html += `<th class="text-end" style="${_WEEK_SEP}">${m}</th>`; });
  html += `</tr></thead><tbody>`;

  allMPs.forEach(({ key, label, color }) => {
    const d = data[key];
    if (!d) return;
    const isTotal = key === 'total';
    const bg = isTotal ? 'background:var(--chip-bg);color:var(--chip-ink);' : '';
    const stickyBg = isTotal ? 'var(--chip-bg)' : 'var(--t-sticky)';
    html += `<tr>`;
    html += `<td class="fw-semibold" style="${bg}position:sticky;left:0;background:${stickyBg}">` +
            `<span style="color:${color}">${label}</span></td>`;
    d.rub.forEach((v, i) => {
      const pct = v > 0 ? Math.round((v - d.cancel_rub[i]) / v * 100) : null;
      const buyoutHtml = pct !== null && pct < 100
        ? `<div style="font-size:0.6rem;color:var(--pos-strong);line-height:1.1">✓${pct}%</div>` : '';
      html += `<td class="text-end" style="${_WEEK_SEP}${bg}">${v ? fmtRub(v) : '<span class="text-muted">—</span>'}${buyoutHtml}</td>`;
    });
    html += `</tr>`;
  });

  html += `</tbody></table></div></div></div>`;
  wrap.innerHTML = html;
}

async function loadSalesAnalysis(refresh = false) {
  const modal = new bootstrap.Modal(document.getElementById('salesAnalysisModal'));
  const body = document.getElementById('salesAnalysisBody');
  const at = document.getElementById('salesAnalysisAt');
  body.innerHTML = '<div class="text-center py-4"><span class="spinner-border"></span></div>';
  modal.show();
  try {
    const url = '/api/dashboard/sales_analysis' + (refresh ? '?refresh=true' : '');
    const d = await fetchJSON(url);
    body.textContent = d.text;
    if (at) at.textContent = d.generated_at || '';
  } catch (e) {
    body.innerHTML = `<div class="text-danger">Ошибка: ${e.message}</div>`;
  }
}

// стрелка динамики week-over-week (как в остатках): ▲ рост / ▼ падение
function _dynArrow(cur, prev) {
  if (!prev || !cur) return '';
  const diff = cur - prev;
  if (Math.abs(diff) < 0.005 * Math.max(cur, prev)) return '';  // <0.5% — без стрелки
  const pct = Math.round(Math.abs(diff) / prev * 100);
  if (diff > 0) return `<span style="color:var(--pos);font-size:0.68rem;margin-left:3px">▲${pct}%</span>`;
  return `<span style="color:var(--neg);font-size:0.68rem;margin-left:3px">▼${pct}%</span>`;
}

const _WEEK_SEP = 'border-left:2px solid var(--sep-strong);';   // вертикальная линия между неделями

// ячейка ₽ с динамикой относительно предыдущей недели
function _rubCell(arr, i, cls, extra = '') {
  const v = arr[i];
  const st = `white-space:nowrap;${extra}`;
  if (!v) return `<td class="text-end ${cls}" style="${st}"><span class="text-muted">—</span></td>`;
  const prev = i > 0 ? arr[i - 1] : 0;
  return `<td class="text-end ${cls}" style="${st}">${fmtRub(v)}${_dynArrow(v, prev)}</td>`;
}

function _qtyCell(v, cls, extra = '') {
  return `<td class="text-end ${cls}" style="${extra}">${v ? fmt(v) : '<span class="text-muted">—</span>'}</td>`;
}

// индексы недель для показа: все, либо только прошлая+текущая
function _visWeeks(n) {
  if (_ordersCompact) return [n - 2, n - 1].filter(i => i >= 0);
  return Array.from({ length: n }, (_, i) => i);
}

function toggleFinMonth(grpId) {
  const rows = document.querySelectorAll(`.fin-week-${grpId}`);
  const arr  = document.getElementById('arr-' + grpId);
  const expanded = arr && arr.textContent === '▼';
  rows.forEach(r => { r.style.display = expanded ? 'none' : ''; });
  if (arr) arr.textContent = expanded ? '▶' : '▼';
}

function toggleGrpRows(grpId) {
  const rows = document.querySelectorAll(`[data-grp="${grpId}"]`);
  const arr  = document.getElementById('arr-' + grpId);
  const expanded = arr && arr.textContent === '▼';
  rows.forEach(r => { r.style.display = expanded ? 'none' : ''; });
  if (arr) arr.textContent = expanded ? '▶' : '▼';
}

function toggleOrdersCompact() {
  _ordersCompact = !_ordersCompact;
  const b = document.getElementById('ordCompactBtn');
  if (b) b.innerHTML = _ordersCompact
    ? '<i class="bi bi-arrows-angle-expand"></i> Все недели'
    : '<i class="bi bi-arrows-angle-contract"></i> Текущая + прошлая';
  renderOrdersTable();
}

// цвета групп для бубликов (совпадают с GROUP_ORDER)
const _GROUP_COLORS = {
  'Фисты':            '#ef4444',
  'Aloe':             '#22c55e',
  'Спреи для минета': '#f59e0b',
  'Satisfucktion':    '#ec4899',
  'Джага':            '#8b5cf6',
  'Прочее':           CHART_C.tick2,
};
function _groupColor(g) { return _GROUP_COLORS[g] || CHART_C.tick2; }

// разбивка выбранной площадки по группам (как в остатках)
function _ordersGrouped() {
  const d = _ordersData;
  if (!d) return null;
  const mp = _ordersMp;
  const weeks = d.weeks;
  const n = weeks.length;

  let block;
  if (mp === 'TOTAL') {
    // Объединяем SKU из всех трёх площадок по внутреннему артикулу
    const merged = {};
    ['WB','OZON','YM'].forEach(m => {
      (d[m]?.skus || []).forEach(s => {
        if (!merged[s.sku]) {
          merged[s.sku] = { ...s, rub: [...s.rub], qty: [...s.qty],
            cancel_rub: s.cancel_rub ? [...s.cancel_rub] : Array(n).fill(0),
            cancel_qty: s.cancel_qty ? [...s.cancel_qty] : Array(n).fill(0) };
        } else {
          s.rub.forEach((v,i) => { merged[s.sku].rub[i] += v; });
          s.qty.forEach((v,i) => { merged[s.sku].qty[i] += v; });
          if (s.cancel_rub) s.cancel_rub.forEach((v,i) => { merged[s.sku].cancel_rub[i] += v; });
          if (s.cancel_qty) s.cancel_qty.forEach((v,i) => { merged[s.sku].cancel_qty[i] += v; });
        }
      });
    });
    const mergedSkus = Object.values(merged);
    const totRub = Array(n).fill(0), totQty = Array(n).fill(0);
    mergedSkus.forEach(s => { s.rub.forEach((v,i) => totRub[i]+=v); s.qty.forEach((v,i) => totQty[i]+=v); });
    block = { skus: mergedSkus, total_rub: totRub, total_qty: totQty };
  } else {
    block = d[mp];
    if (!block) return null;
  }

  const skus = (block.skus || []).filter(s => s.rub.some(v => v) || s.qty.some(v => v));
  const groupMap = {};
  skus.forEach(s => {
    const g = articleGroup(s);
    (groupMap[g] = groupMap[g] || []).push(s);
  });
  Object.values(groupMap).forEach(rows =>
    rows.sort((a, b) => b.rub.reduce((x,y)=>x+y,0) - a.rub.reduce((x,y)=>x+y,0)));
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  return { mp, block, weeks, n, col: _MP_COLORS[mp], skus, orderedGroups };
}

function _ordersTableHTML() {
  const g = _ordersGrouped();
  if (!g) return '';
  const { mp, block, weeks, n, col, skus, orderedGroups } = g;
  const vis = _visWeeks(n);

  // thead: Артикул/Название | week1 ₽/шт | week2 ₽/шт | ...
  let thead = '<thead class="sticky-top"><tr>'
    + '<th rowspan="2" style="min-width:200px;vertical-align:bottom">Артикул / Название</th>';
  vis.forEach(i => {
    thead += `<th class="text-end" colspan="2" style="white-space:nowrap;${_WEEK_SEP}">${weeks[i]}</th>`;
  });
  thead += '</tr><tr>';
  vis.forEach(() => {
    thead += `<th class="text-end text-secondary" style="font-size:0.72rem;min-width:78px;${_WEEK_SEP}">₽</th>`
           + '<th class="text-end text-secondary" style="font-size:0.72rem;min-width:38px">шт</th>';
  });
  thead += '</tr></thead>';

  const totalRub = block.total_rub;
  const totalQty = block.total_qty;

  let tbody = '<tbody>';

  // итоговая строка площадки
  tbody += `<tr data-row="mp" style="background:var(--t-mp-row)">`;
  tbody += `<td class="fw-bold" style="color:${col}">${_MP_LABEL[mp]}</td>`;
  vis.forEach(i => {
    tbody += _rubCell(totalRub, i, 'fw-semibold', _WEEK_SEP);
    tbody += _qtyCell(totalQty[i], 'fw-semibold');
  });
  tbody += '</tr>';

  orderedGroups.forEach(([grp, grpSkus], gi) => {
    // промежуточный итог по группе (включая отмены)
    const bRub       = Array(n).fill(0);
    const bQty       = Array(n).fill(0);
    const bCancelRub = Array(n).fill(0);
    grpSkus.forEach(s => {
      s.rub.forEach((v, i) => { bRub[i] += v; });
      s.qty.forEach((v, i) => { bQty[i] += v; });
      if (s.cancel_rub) s.cancel_rub.forEach((v, i) => { bCancelRub[i] += v; });
    });

    const grpId = `grp-${mp}-${gi}`;
    const GRP_BG = 'background:var(--chip-bg);color:var(--chip-ink);';

    // Строка-заголовок группы со стрелкой сворачивания
    tbody += `<tr data-row="grp" style="border-top:2px solid var(--sep-strong);cursor:pointer" onclick="toggleGrpRows('${grpId}')">`;
    tbody += `<td class="fw-semibold ps-2" style="${GRP_BG}padding:6px 8px">`
           + `<span id="arr-${grpId}" style="display:inline-block;width:14px;font-size:0.8rem;color:var(--muted)">▶</span>`
           + `<span class="me-1" style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${_groupColor(grp)}"></span>`
           + `<strong>${grp}</strong> <span class="small" style="color:var(--muted)">(${grpSkus.length} арт.)</span></td>`;

    // Ячейки по неделям с % выкупа под суммой
    vis.forEach(i => {
      const v    = bRub[i];
      const prev = i > 0 ? bRub[i - 1] : 0;
      const pct  = v > 0 ? Math.round((v - bCancelRub[i]) / v * 100) : null;
      const dynHtml   = v ? _dynArrow(v, prev) : '';
      const buyoutHtml = pct !== null
        ? `<div style="font-size:0.6rem;color:var(--pos-strong);line-height:1.2;margin-top:1px">✓${pct}% выкуп</div>`
        : '';
      const cellVal = v
        ? `${fmtRub(v)}${dynHtml}${buyoutHtml}`
        : `<span class="text-muted">—</span>`;
      tbody += `<td class="text-end small fw-semibold" style="${_WEEK_SEP + GRP_BG}white-space:nowrap;padding:4px 6px">${cellVal}</td>`;
      tbody += _qtyCell(bQty[i], 'small fw-semibold', GRP_BG);
    });
    tbody += '</tr>';

    // строки SKU — по умолчанию скрыты
    grpSkus.forEach(s => {
      tbody += `<tr data-row="sku" data-grp="${grpId}" style="display:none;background:var(--t-sku-row)">`;
      tbody += `<td class="ps-4 small" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">`
             + `<span class="badge me-1" style="background:${col}22;color:${col};font-size:10px">${s.sku}</span>`
             + `<span class="text-muted">${s.name || s.sku}</span></td>`;
      vis.forEach(i => {
        tbody += _rubCell(s.rub, i, 'small', _WEEK_SEP);
        tbody += _qtyCell(s.qty[i], 'small');
      });
      tbody += '</tr>';
    });
  });

  // ── tfoot: бублики доли групп по неделям + % справа ──────────────────────
  let tfoot = '';
  if (orderedGroups.length) {
    const labels = orderedGroups.map(([grp]) => grp);
    const colors = labels.map(_groupColor);
    // legend в первой ячейке (залипает слева)
    const legendHTML = labels.map((l, i) =>
      `<div style="white-space:nowrap;font-size:0.74rem;color:var(--val-soft);margin-bottom:3px">`
      + `<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${colors[i]};margin-right:5px"></span>${l}</div>`
    ).join('');
    tfoot = `<tfoot><tr>`
      + `<td style="position:sticky;left:0;background:var(--t-legend);padding:10px 8px;vertical-align:middle">${legendHTML}</td>`;
    vis.forEach(i => {
      // доли групп этой недели
      const sums = orderedGroups.map(([, sk]) => sk.reduce((a, s) => a + (s.rub[i] || 0), 0));
      const tot = sums.reduce((a, b) => a + b, 0);
      const pctItems = labels.map((l, gi) => ({ l, gi, p: tot ? Math.round(sums[gi] / tot * 100) : 0 }))
        .filter(x => x.p > 0)
        .sort((a, b) => b.p - a.p);
      const pctHTML = pctItems.map(({ gi, p }) =>
        `<div style="white-space:nowrap;font-size:0.68rem;color:var(--ink-2);line-height:1.35">`
        + `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${colors[gi]};margin-right:4px"></span>${p}%</div>`
      ).join('');
      tfoot += `<td colspan="2" style="background:var(--t-legend);padding:6px 4px;${_WEEK_SEP}">`
             + `<div style="display:flex;align-items:center;justify-content:center;gap:6px">`
             + `<canvas id="ordDonutW${i}" width="96" height="96"></canvas>`
             + `<div>${pctHTML}</div></div></td>`;
    });
    tfoot += `</tr></tfoot>`;
  }

  return thead + tbody + tfoot;
}

function renderOrdersTable() {
  const tbl = document.getElementById('ordersTable');
  if (tbl) tbl.innerHTML = _ordersTableHTML();
  _renderTfootDonuts();
}

// рендерит Chart.js бублики в canvas-ы внутри tfoot (уже вставленного в DOM)
function _renderTfootDonuts() {
  (charts.ordersDonuts || []).forEach(c => { try { c.destroy(); } catch (e) {} });
  charts.ordersDonuts = [];

  const g = _ordersGrouped();
  if (!g || !g.orderedGroups.length) return;
  const { n, orderedGroups } = g;

  const labels = orderedGroups.map(([grp]) => grp);
  const colors = labels.map(_groupColor);

  _visWeeks(n).forEach(i => {
    const canvas = document.getElementById('ordDonutW' + i);
    if (!canvas) return;
    const dataArr = orderedGroups.map(([, sk]) => sk.reduce((a, s) => a + (s.rub[i] || 0), 0));
    const sum = dataArr.reduce((a, b) => a + b, 0);
    const c = new Chart(canvas.getContext('2d'), {
      type: 'doughnut',
      data: { labels, datasets: [{ data: dataArr, backgroundColor: colors, borderWidth: 0 }] },
      options: {
        responsive: false, cutout: '60%',
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: false,
            external({ chart, tooltip }) {
              let el = document.getElementById('ordDonutTooltip');
              if (!el) {
                el = document.createElement('div');
                el.id = 'ordDonutTooltip';
                el.style.cssText = 'position:fixed;background:var(--surface-3);color:var(--val-soft);border:1px solid var(--sep-strong);border-radius:6px;padding:6px 10px;font-size:0.78rem;pointer-events:none;z-index:9999;white-space:nowrap;transition:opacity .1s';
                document.body.appendChild(el);
              }
              if (tooltip.opacity === 0) { el.style.opacity = '0'; return; }
              const item = tooltip.dataPoints?.[0];
              if (!item) return;
              const pct = sum ? Math.round(item.raw / sum * 100) : 0;
              el.innerHTML = `<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${item.dataset.backgroundColor[item.dataIndex]};margin-right:5px"></span>`
                + `<b>${item.label}</b>: ${fmtRub(item.raw)} (${pct}%)`;
              const pos = chart.canvas.getBoundingClientRect();
              el.style.left = (pos.left + tooltip.caretX + 12) + 'px';
              el.style.top  = (pos.top  + tooltip.caretY - 10) + 'px';
              el.style.opacity = '1';
            },
          },
        },
      },
    });
    charts.ordersDonuts.push(c);
  });
}

function _fmtShort(v) {
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1).replace('.0', '') + ' млн';
  if (Math.abs(v) >= 1e3) return Math.round(v / 1e3) + ' тыс';
  return Math.round(v).toString();
}

// ── Накопленная история продаж ────────────────────────────────────────────────

async function loadHistory() {
  const meta = document.getElementById('histMeta');
  meta.textContent = 'Загрузка…';
  try {
    const from = document.getElementById('histFrom').value;
    const to   = document.getElementById('histTo').value;
    const qs = new URLSearchParams();
    if (from) qs.set('date_from', from);
    if (to)   qs.set('date_to', to);
    const d = await fetchJSON('/api/dashboard/sales_history?' + qs.toString());
    renderHistory(d);
  } catch (e) {
    console.error('history', e);
    meta.textContent = 'Ошибка загрузки';
  }
}

const _PLAT_COLORS = { wb: '#c026d3', ozon: '#3b82f6', ym: '#b45309' };
const _PLAT_NAMES  = { wb: 'WB', ozon: 'Ozon', ym: 'YM' };

function renderHistory(d) {
  const meta = document.getElementById('histMeta');
  if (d.stored_from) {
    meta.textContent = `накоплено ${d.days_stored} дн. (${d.stored_from} — ${d.stored_to})`;
  } else {
    meta.textContent = 'данных пока нет — накопление началось, зайдите позже';
  }

  // Карточки итогов по площадкам
  const order = ['wb', 'ozon', 'ym'];
  const bp = {};
  Object.entries(d.by_platform || {}).forEach(([k, v]) => { bp[k.toLowerCase()] = v; });
  let totalRub = 0, totalQty = 0;
  const cards = order.filter(k => bp[k]).map(k => {
    const v = bp[k]; totalRub += v.revenue; totalQty += v.qty;
    return `<div class="col-6 col-md-3"><div class="card h-100"><div class="card-body">
      <div class="small text-muted">${_PLAT_NAMES[k]}</div>
      <div class="h5 mb-0" style="color:${_PLAT_COLORS[k]}">${fmtRub(v.revenue)}</div>
      <div class="small text-muted">${fmt(v.qty)} шт.</div>
    </div></div></div>`;
  });
  cards.push(`<div class="col-6 col-md-3"><div class="card h-100 border-success"><div class="card-body">
    <div class="small text-muted">Всего</div>
    <div class="h5 mb-0 text-success">${fmtRub(totalRub)}</div>
    <div class="small text-muted">${fmt(totalQty)} шт.</div>
  </div></div></div>`);
  document.getElementById('histCards').innerHTML = cards.join('');

  // График выручки по дням (stacked по площадкам)
  const daily = d.daily || [];
  const labels = daily.map(r => r.date);
  const datasets = order.map(k => ({
    label: _PLAT_NAMES[k],
    data: daily.map(r => r[k] || 0),
    backgroundColor: _PLAT_COLORS[k],
    borderRadius: 3,
    stack: 'rev',
  }));
  const ctx = document.getElementById('histChart').getContext('2d');
  if (charts.history) charts.history.destroy();
  charts.history = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: CHART_C.legend, usePointStyle: true, pointStyle: 'rectRounded' } },
        tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtRub(c.raw)}` } },
        datalabels: false,
      },
      scales: {
        x: { stacked: true, ticks: { color: CHART_C.tick, maxRotation: 90, font: { size: 10 } }, grid: { display: false } },
        y: { stacked: true, ticks: { color: CHART_C.tick, callback: v => _fmtShort(v) }, grid: { color: CHART_C.gridSoft }, beginAtZero: true },
      },
    },
  });
}


// ── Финансы / P&L ────────────────────────────────────────────────────────────

let _financeData = { WB: null, OZON: null, YM: null, TOTAL: null };
let _financeMp = 'WB';
let _wbPnlPolling = false;
let _finCompact = false;

function toggleFinCompact() {
  _finCompact = !_finCompact;
  const btn = document.getElementById('finCompactBtn');
  if (btn) btn.classList.toggle('active', _finCompact);
  renderFinanceTable();
}

function setFinanceMp(mp) {
  _financeMp = mp;
  ['WB','OZON','YM','TOTAL'].forEach(m => {
    const el = document.getElementById('finMp' + m);
    if (el) el.classList.toggle('active', m === mp);
  });
  renderFinanceTable();
}

async function loadFinance() {
  const wrap = document.getElementById('financeTableWrap');
  if (wrap) wrap.innerHTML = '<div class="text-center text-secondary py-5"><span class="spinner-border me-2"></span>Загрузка P&L… (может занять до 2 мин)</div>';
  await loadFinanceMp('WB');
  renderFinanceTable();
  // остальные площадки подтягиваем в фоне, чтобы переключение было мгновенным
  loadFinanceMp('OZON');
  loadFinanceMp('YM');
}

async function reloadFinance() {
  _financeData = { WB: null, OZON: null, YM: null, TOTAL: null };
  await fetch('/api/finance/wb/reports/invalidate', { method: 'POST' }).catch(() => {});
  await loadFinance();
}

const _FIN_URLS = {
  WB:   '/api/finance/wb/pnl',
  OZON: '/api/finance/ozon/pnl',
  YM:   '/api/finance/ym/pnl',
};

let _manualCosts = null;   // {items: [{id, mk, label, amount}]}

async function loadFinanceMp(mp) {
  if (_financeData[mp]) return;
  try {
    if (mp === 'TOTAL') {
      const jobs = [loadFinanceMp('WB'), loadFinanceMp('OZON'), loadFinanceMp('YM')];
      if (!_manualCosts) jobs.push(fetchJSON('/api/finance/manual_costs').then(d => { _manualCosts = d; }));
      await Promise.all(jobs);
      _financeData.TOTAL = buildFinanceTotal();
    } else {
      _financeData[mp] = await fetchJSON(_FIN_URLS[mp], 120000);
    }
  } catch (e) {
    _financeData[mp] = { rows: [], months: [], error: e.message };
  }
}

// Тотал: выручка и затраты в разрезе площадок + ручные статьи + фин. итог
function buildFinanceTotal() {
  const PLATS = [['WB', 'WB'], ['OZON', 'Ozon'], ['YM', 'ЯМ']];
  const loaded = PLATS.map(([k, label]) => [k, label, _financeData[k]])
    .filter(([, , d]) => d && (d.months || []).length && d.source !== 'weekly'); // WB только с точными данными
  if (!loaded.length) return { rows: [], months: [], message: '⏳ Точные данные площадок ещё собираются — Тотал появится через минуту' };

  const monthLabels = {};
  loaded.forEach(([, , d]) => d.months.forEach(m => { monthLabels[m.key] = m.label; }));
  const monthKeys = Object.keys(monthLabels).sort().reverse();

  const rowVals = (d, key) => {
    const row = (d.rows || []).find(r => r.key === key);
    return row && row.values ? row.values : {};
  };
  const sumAll = key => {
    const vals = {};
    monthKeys.forEach(mk => {
      vals[mk] = loaded.reduce((a, [, , d]) => a + (rowVals(d, key)[mk] || 0), 0);
    });
    return vals;
  };

  const revenue = sumAll('retailAmount');
  const payout  = sumAll('bankPayment');
  const cogs    = sumAll('cogs');    // уже отрицательные
  const gross   = sumAll('gross');

  const rows = [
    { key: 'retailAmount', label: '📦 Выручка (все площадки)', style: 'header', formula: 'direct', values: revenue },
  ];
  // выручка в разрезе площадок — справочно
  loaded.forEach(([k, label, d]) => {
    const rv = rowVals(d, 'retailAmount');
    rows.push({ key: 'rev_' + k, label: `      ↳ ${label}`, style: 'note', formula: 'info',
                values: Object.fromEntries(monthKeys.map(mk => [mk, rv[mk] || 0])) });
  });
  // затраты каждой площадки = к перечислению − выручка (отрицательное)
  loaded.forEach(([k, label, d]) => {
    const rv = rowVals(d, 'retailAmount'), po = rowVals(d, 'bankPayment');
    rows.push({ key: 'cost_' + k, label: `  − Затраты ${label}`, style: 'cost', formula: 'direct',
                values: Object.fromEntries(monthKeys.map(mk => [mk, (po[mk] || 0) - (rv[mk] || 0)])) });
  });
  rows.push({ key: 'bankPayment', label: '💳 К перечислению', style: 'subtotal', formula: 'direct', values: payout });
  rows.push({ key: 'cogs', label: '  − Себестоимость', style: 'cost', formula: 'direct', values: cogs });
  rows.push({ key: 'gross', label: '✅ Валовая прибыль', style: 'total', formula: 'direct', values: gross });
  const pct = {};
  monthKeys.forEach(mk => { pct[mk] = revenue[mk] ? Math.round(gross[mk] / revenue[mk] * 100) : 0; });
  rows.push({ key: 'gross_pct', label: '   Маржа %', style: 'pct', formula: 'gross_pct', values: pct });

  // ручные статьи затрат (сгруппированы по названию)
  const items = (_manualCosts && _manualCosts.items) || [];
  const byLabel = {};
  items.forEach(it => {
    if (!monthLabels[it.mk]) return;
    (byLabel[it.label] = byLabel[it.label] || {})[it.mk] =
      (byLabel[it.label][it.mk] || 0) + it.amount;
  });
  const manualSum = {};
  monthKeys.forEach(mk => { manualSum[mk] = 0; });
  Object.entries(byLabel).forEach(([label, vals]) => {
    rows.push({ key: 'man_' + label, label: `  − ${label}`, style: 'cost', formula: 'direct',
                values: Object.fromEntries(monthKeys.map(mk => [mk, -(vals[mk] || 0)])) });
    monthKeys.forEach(mk => { manualSum[mk] += vals[mk] || 0; });
  });
  const net = {};
  monthKeys.forEach(mk => { net[mk] = (gross[mk] || 0) - manualSum[mk]; });
  rows.push({ key: 'net', label: '🏁 Финансовый итог месяца', style: 'total', formula: 'direct', values: net });
  const netPct = {};
  monthKeys.forEach(mk => { netPct[mk] = revenue[mk] ? Math.round(net[mk] / revenue[mk] * 100) : 0; });
  rows.push({ key: 'net_pct', label: '   Итоговая маржа %', style: 'pct', formula: 'gross_pct', values: netPct });

  return {
    months: monthKeys.map(mk => ({ key: mk, label: monthLabels[mk] })),
    rows,
    fetched_at: loaded[0][2].fetched_at || '',
  };
}

// ── Ручные статьи затрат (Тотал) ──────────────────────────────────────────────

async function addManualCost() {
  const mk = document.getElementById('manCostMonth')?.value;
  const label = (document.getElementById('manCostLabel')?.value || '').trim();
  const amount = parseFloat(document.getElementById('manCostAmount')?.value || '0');
  if (!mk || !label || !amount) return;
  await fetch('/api/finance/manual_costs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mk, label, amount }),
  });
  _manualCosts = null;
  _financeData.TOTAL = null;
  renderFinanceTable();
}

async function delManualCost(id) {
  await fetch(`/api/finance/manual_costs/${id}`, { method: 'DELETE' });
  _manualCosts = null;
  _financeData.TOTAL = null;
  renderFinanceTable();
}

function manualCostsPanel(months) {
  const items = ((_manualCosts && _manualCosts.items) || [])
    .slice().sort((a, b) => b.mk.localeCompare(a.mk) || a.label.localeCompare(b.label));
  const monthOpts = [...months].sort((a, b) => b.key.localeCompare(a.key))
    .map(m => `<option value="${m.key}">${m.label}</option>`).join('');
  const mLabel = mk => (months.find(m => m.key === mk) || {}).label || mk;
  let list = '';
  if (items.length) {
    list = `<div class="mt-2 d-flex flex-column gap-1">` + items.map(it => `
      <div class="d-flex align-items-center gap-2 small">
        <span class="text-secondary" style="min-width:80px">${mLabel(it.mk)}</span>
        <span style="min-width:220px">${esc(it.label)}</span>
        <span style="color:var(--neg)">−${fmtRub(it.amount)}</span>
        <button class="btn btn-sm btn-outline-danger py-0 px-1" style="font-size:.7rem;line-height:1.2"
                onclick="delManualCost(${it.id})" title="Удалить">✕</button>
      </div>`).join('') + `</div>`;
  }
  return `
  <div class="card bg-card mt-3 p-3">
    <div class="fw-semibold mb-2" style="color:var(--ink)">➕ Ручные статьи затрат <span class="text-secondary small fw-normal">(аренда, зарплаты, фф и т.д. — вычитаются из валовой в «Финансовый итог месяца»)</span></div>
    <div class="d-flex gap-2 flex-wrap align-items-center">
      <select id="manCostMonth" class="form-select form-select-sm bg-dark text-white border-secondary" style="width:130px">${monthOpts}</select>
      <input id="manCostLabel" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:260px" placeholder="Название (напр. Аренда склада)">
      <input id="manCostAmount" type="number" min="0" step="100" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:130px" placeholder="Сумма ₽">
      <button class="btn btn-sm btn-outline-success" onclick="addManualCost()">Добавить</button>
    </div>
    ${list}
  </div>`;
}

function renderFinanceTable() {
  const wrap = document.getElementById('financeTableWrap');
  if (!wrap) return;

  const mp = _financeMp;
  if (!_financeData[mp]) {
    wrap.innerHTML = '<div class="text-center text-secondary py-5"><span class="spinner-border me-2"></span>Загрузка P&L…</div>';
    loadFinanceMp(mp).then(() => renderFinanceTable());
    return;
  }

  const d = _financeData[mp];
  if (d.message) {
    wrap.innerHTML = `<div class="alert alert-info mt-3">${d.message}</div>`;
    if (d.message.includes('⏳')) {
      // отчёт собирается на бэке — перепроверяем через 20с
      setTimeout(() => {
        _financeData[mp] = null;
        _financeData.TOTAL = null;
        if (_financeMp === mp) renderFinanceTable(); else loadFinanceMp(mp);
      }, 20000);
    }
    return;
  }
  if (d.error)   { wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${d.error}</div>`; return; }

  // WB: пока точный (детальный) отчёт не собран — не показываем приблизительные
  // недельные цифры вовсе: либо правильные данные, либо экран загрузки
  if (mp === 'WB' && d.source === 'weekly') {
    if (d.detail_error) {
      wrap.innerHTML = `<div class="alert alert-danger mt-3">⚠ Не удалось загрузить детальный отчёт WB: ${d.detail_error}<br>
        <span class="small text-secondary">Повторная попытка выполняется автоматически.</span></div>`;
    } else {
      wrap.innerHTML = `<div class="text-center text-secondary py-5">
        <div class="spinner-border mb-3" style="color:#c026d3"></div>
        <div class="fw-semibold text-white mb-1">Собираем точный отчёт WB</div>
        <div class="small">Загружаем детализацию по датам операций (обычно 1-2 минуты).<br>
        Страница обновится автоматически.</div></div>`;
    }
    if (!_wbPnlPolling) {
      _wbPnlPolling = true;
      setTimeout(() => {
        _wbPnlPolling = false;
        _financeData.WB = null;
        _financeData.TOTAL = null;
        if (_financeMp === 'WB') renderFinanceTable(); else loadFinanceMp('WB');
      }, 20000);
    }
    return;
  }

  const rows = d.rows || [];
  // Хронология слева направо (как в заказах): старые месяцы слева, свежие справа
  let months = [...(d.months || [])].sort((a, b) => a.key.localeCompare(b.key));
  if (_finCompact && months.length > 2) months = months.slice(-2);
  if (!months.length) { wrap.innerHTML = '<div class="text-secondary text-center py-4">Данных нет</div>'; return; }

  const MP_COLOR = { WB: '#c026d3', OZON: '#3b82f6', YM: '#b45309', TOTAL: '#059669' };
  const col = MP_COLOR[mp] || '#c026d3';
  const SEP = 'border-left:2px solid var(--sep);';

  // Стили строк — читаемо, как в заказах: белые жирные цифры на ключевых строках
  const ROW_STYLE = {
    header:   `background:var(--fin-header)`,
    cost:     `background:var(--fin-cost)`,
    note:     `background:transparent;font-style:italic`,
    subtotal: `background:var(--fin-subtotal);border-top:2px solid var(--sep-strong)`,
    total:    `background:var(--fin-total);border-top:2px solid var(--pos-strong)`,
    pct:      `background:var(--fin-pct);font-style:italic`,
    normal:   `background:var(--t-row)`,
  };

  function fmtCell(key, val, style) {
    if (key === 'gross_pct' || style === 'pct') {
      const clr = val >= 20 ? 'var(--pos)' : val >= 10 ? 'var(--warn-c)' : 'var(--neg)';
      return `<span style="color:${clr};font-weight:700">${val}%</span>`;
    }
    if (val === 0) return `<span class="text-muted small">—</span>`;
    if (key === 'advert_bonus') {
      return `<span style="color:var(--pos-soft)">${fmtRub(Math.abs(val))}</span>`;
    }
    if (key === 'advert_balance') {
      return `<span style="color:var(--dim)">${fmtRub(Math.abs(val))}</span>`;
    }
    // ключевые строки — белым жирным, затраты — светлым с красным минусом
    const emphasized = style === 'header' || style === 'subtotal' || style === 'total';
    if (emphasized) {
      const c = key === 'gross' || key === 'bankPayment' ? 'var(--pos)' : 'var(--val)';
      return `<span style="color:${c};font-weight:700">${val < 0 ? '−' : ''}${fmtRub(Math.abs(val))}</span>`;
    }
    if (val < 0) {
      return `<span style="color:var(--ink)"><span style="color:var(--neg)">−</span>${fmtRub(Math.abs(val))}</span>`;
    }
    return `<span style="color:var(--ink)">${fmtRub(val)}</span>`;
  }

  // Итоговая колонка — по видимым месяцам
  function rowTotal(row) {
    if (row.formula === 'gross_pct') return null; // % не суммируется
    return months.reduce((a, m) => a + (row.values[m.key] || 0), 0);
  }

  let html = `<div class="table-responsive"><table class="table table-sm align-middle mb-0 text-nowrap" style="font-size:0.85rem">`;

  // thead
  html += `<thead><tr>
    <th style="min-width:230px;position:sticky;left:0;background:var(--t-sticky);z-index:2">Статья</th>`;
  months.forEach(m => {
    html += `<th class="text-end" style="${SEP}min-width:112px">${m.label}</th>`;
  });
  html += `<th class="text-end" style="${SEP}min-width:124px;color:${col}">Итого</th>`;
  html += `</tr></thead><tbody>`;

  rows.forEach(row => {
    const st = ROW_STYLE[row.style] || ROW_STYLE.normal;
    const stickyBg = (row.style === 'total') ? 'var(--fin-total)'
                   : (row.style === 'subtotal') ? 'var(--fin-subtotal)'
                   : (row.style === 'header') ? 'var(--fin-header)' : 'var(--t-sticky)';
    const labelStyle = (row.style === 'note') ? 'color:var(--pos-soft);font-size:0.78rem' :
                       (row.style === 'cost') ? 'color:var(--ink-2)' : 'color:var(--val);font-weight:600';
    html += `<tr style="${st}">`;
    html += `<td style="position:sticky;left:0;background:${stickyBg};padding:6px 12px;${labelStyle}">${row.label}</td>`;
    months.forEach(m => {
      const v = row.values[m.key] ?? 0;
      html += `<td class="text-end" style="${SEP}padding:5px 12px">${fmtCell(row.key, v, row.style)}</td>`;
    });
    // итоговая колонка
    const tot = rowTotal(row);
    if (tot !== null) {
      html += `<td class="text-end" style="${SEP}padding:5px 12px;background:rgba(201,168,76,.04)">${fmtCell(row.key, tot, row.style === 'cost' ? 'cost' : row.style)}</td>`;
    } else {
      // маржа итого считается от суммарных значений
      const gross = rows.find(r => r.key === (row.key === 'net_pct' ? 'net' : 'gross'));
      const retail = rows.find(r => r.key === 'retailAmount');
      const gTot = gross ? rowTotal(gross) : 0;
      const rTot = retail ? rowTotal(retail) : 0;
      const pct = rTot > 0 ? Math.round(gTot / rTot * 100) : 0;
      const clr = pct >= 20 ? 'var(--pos)' : pct >= 10 ? 'var(--warn-c)' : 'var(--neg)';
      html += `<td class="text-end fw-bold" style="${SEP}padding:5px 12px"><span style="color:${clr}">${pct}%</span></td>`;
    }
    html += `</tr>`;
  });

  html += `</tbody></table></div>`;
  if (!d.cogs_loaded) {
    html += `<div class="text-warning small mt-2">⚠ Себестоимость не загружена — строка COGS показывает 0. Загрузите справочник через раздел загрузки.</div>`;
  }
  if (mp === 'WB' && d.detail_upto) {
    const tail = d.tail_days
      ? ` Продажи после этой даты (${d.tail_days} дн.) добавлены из оперативных данных — комиссия и логистика по ним уточнятся после формирования недельного отчёта WB.`
      : '';
    html += `<div class="text-secondary small mt-2">Отчёт реализации WB сформирован по ${d.detail_upto}.${tail}</div>`;
  }
  if (d.fetched_at) html += `<div class="text-secondary text-end small mt-1">Обновлено: ${d.fetched_at}</div>`;
  if (mp === 'TOTAL') html += manualCostsPanel(d.months || []);
  wrap.innerHTML = html;
}

// ── Юнит-экономика WB ─────────────────────────────────────────────────────────

let _unitData = null;
let _unitMetric = 'gross';
let _unitMp = 'WB';                            // WB | OZON
const _unitDataByMp = {};                      // кэш ответов по площадке

function setUnitMp(mp) {
  if (_unitMp === mp) return;
  _unitMp = mp;
  _unitMonth = null;
  _unitData = _unitDataByMp[mp] || null;
  ['WB', 'OZON', 'YM'].forEach(k => {
    const el = document.getElementById('unitMp' + k);
    if (el) el.classList.toggle('active', k === mp);
  });
  loadUnitEconomics();
}
let _unitMode = 'month';                       // month — как в Excel; dyn — динамика
let _unitMonth = null;                         // выбранный месяц или 'ALL'
let _unitTax = parseFloat(localStorage.getItem('unit_tax_profit_pct') || '0');
const _unitExpanded = new Set();

function setUnitTax(v) {
  _unitTax = Math.max(0, parseFloat(v) || 0);
  localStorage.setItem('unit_tax_profit_pct', String(_unitTax));
  renderUnitTable();
}

function toggleUnitMode() {
  _unitMode = _unitMode === 'month' ? 'dyn' : 'month';
  const btn = document.getElementById('unitModeBtn');
  if (btn) btn.textContent = _unitMode === 'month' ? '📈 Динамика по месяцам' : '📋 Помесячная таблица';
  renderUnitTable();
}

function setUnitMonth(mk) {
  _unitMonth = mk;
  renderUnitTable();
}

const UNIT_METRICS = [
  ['gross',   'Валовая прибыль'],
  ['margin',  'Маржа %'],
  ['revenue', 'Выручка'],
  ['qty',     'Штуки'],
  ['advert',  'Продвижение'],
  ['cogs',    'Себестоимость'],
];

// строки развёрнутого P&L артикула
const UNIT_PNL_ROWS = [
  ['revenue',    'Выручка (до СПП)',        'var(--val)'],
  ['commission', '− Комиссия и эквайринг',  'var(--neg)'],
  ['delivery',   '− Логистика',             'var(--neg)'],
  ['storage',    '− Хранение',              'var(--neg)'],
  ['penalty',    '− Штрафы',                'var(--warn-c)'],
  ['deductions', '− Удержания (распр.)',    'var(--warn-c)'],
  ['advert',     '− Продвижение (распр.)',  '#c084fc'],
  ['payout',     'К перечислению',          'var(--pos)'],
  ['cogs',       '− Себестоимость',         'var(--neg)'],
  ['gross',      'Валовая прибыль',         'var(--pos)'],
  ['margin',     'Маржа %',                 'var(--pos)'],
  ['qty',        'Штук',                    'var(--ink-2)'],
];

async function loadUnitEconomics() {
  const wrap = document.getElementById('unitTableWrap');
  if (_unitData) { renderUnitTable(); return; }
  if (wrap) wrap.innerHTML = '<div class="text-center text-secondary py-5"><span class="spinner-border me-2"></span>Собираем юнит-экономику…</div>';
  const mp = _unitMp;
  try {
    const url = { OZON: '/api/finance/ozon/unit', YM: '/api/finance/ym/unit' }[mp] || '/api/finance/wb/unit';
    const d = await fetchJSON(url, 120000);
    if (mp !== _unitMp) return;   // пока грузили — переключили площадку
    if (d.message) {
      if (wrap) wrap.innerHTML = `<div class="alert alert-info mt-3">${d.message}</div>`;
      setTimeout(() => { if (currentTab === 'unit' && mp === _unitMp) loadUnitEconomics(); }, 20000);
      return;
    }
    _unitData = _unitDataByMp[mp] = d;
    renderUnitTable();
  } catch (e) {
    if (wrap) wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${e.message}</div>`;
  }
}

function setUnitMetric(m) {
  _unitMetric = m;
  renderUnitTable();
}

function toggleUnitSku(sku) {
  if (_unitExpanded.has(sku)) _unitExpanded.delete(sku); else _unitExpanded.add(sku);
  renderUnitTable();
}

function _unitFmt(key, v) {
  if (v == null) return '<span class="text-muted small">—</span>';
  if (key === 'margin') {
    const clr = v >= 20 ? 'var(--pos)' : v >= 10 ? 'var(--warn-c)' : 'var(--neg)';
    return `<span style="color:${clr};font-weight:600">${v}%</span>`;
  }
  if (key === 'qty') return `<span style="color:var(--ink)">${fmt(v)}</span>`;
  if (v === 0) return '<span class="text-muted small">—</span>';
  const isCost = ['commission','delivery','storage','acceptance','penalty','advert','deductions','cogs'].includes(key);
  if (isCost) return `<span style="color:var(--ink)"><span style="color:var(--neg)">−</span>${fmtRub(Math.abs(v))}</span>`;
  const clr = (key === 'gross' || key === 'payout') ? (v >= 0 ? 'var(--pos)' : 'var(--neg)') : 'var(--val)';
  return `<span style="color:${clr};font-weight:600">${v < 0 ? '−' : ''}${fmtRub(Math.abs(v))}</span>`;
}

function _momBadge(key, cur, prev) {
  if (prev == null || cur == null) return '';
  if (key === 'margin') {
    const d = cur - prev;
    if (!d) return '';
    const up = d > 0;
    return ` <span class="small" style="color:${up ? 'var(--pos)' : 'var(--neg)'}">${up ? '▲' : '▼'}${Math.abs(d)}пп</span>`;
  }
  if (!prev) return '';
  const pct = Math.round((cur - prev) / Math.abs(prev) * 100);
  if (!pct) return '';
  // для затрат рост — плохо (красный)
  const isCost = ['advert','cogs','commission','delivery','deductions'].includes(key);
  const up = pct > 0;
  const good = isCost ? !up : up;
  return ` <span class="small" style="color:${good ? 'var(--pos)' : 'var(--neg)'}">${up ? '▲' : '▼'}${Math.abs(pct)}%</span>`;
}

function renderUnitTable() {
  const wrap = document.getElementById('unitTableWrap');
  if (!wrap || !_unitData) return;

  const taxInput = document.getElementById('unitTaxPct');
  if (taxInput && taxInput.value === '' && _unitTax > 0) taxInput.value = _unitTax;

  // пилюли месяцев (для режима «месяц»)
  const mBox = document.getElementById('unitMonthBtns');
  if (mBox) {
    const months = _unitData.months || [];
    if (!_unitMonth) _unitMonth = months.length ? months[months.length - 1].key : 'ALL';
    if (_unitMode === 'month') {
      mBox.innerHTML = months.map(m =>
        `<button class="btn btn-sm btn-outline-info ${_unitMonth === m.key ? 'active' : ''}"
                 onclick="setUnitMonth('${m.key}')">${m.label}</button>`).join('') +
        `<button class="btn btn-sm btn-outline-info ${_unitMonth === 'ALL' ? 'active' : ''}"
                 onclick="setUnitMonth('ALL')">Σ Период</button>`;
    } else {
      mBox.innerHTML = '';
    }
  }

  if (_unitMode === 'month') { renderUnitMonth(); return; }
  renderUnitDynamics();
}

// ── Режим «месяц»: формат управленческой таблицы (как Excel владельца) ────────

function _unitCellSum(cells, months) {
  // суммирует ячейки выбранных месяцев в один объект
  const out = {};
  months.forEach(m => {
    const c = cells[m.key];
    if (!c) return;
    Object.keys(c).forEach(k => { out[k] = (out[k] || 0) + c[k]; });
  });
  if (out.revenue) out.margin = Math.round((out.gross || 0) / out.revenue * 100);
  return out;
}

function renderUnitMonth() {
  const wrap = document.getElementById('unitTableWrap');
  const months = _unitData.months || [];
  const selMonths = _unitMonth === 'ALL' ? months : months.filter(m => m.key === _unitMonth);
  const skus = _unitData.skus || [];
  const totalsCell = _unitCellSum(_unitData.totals || {}, selMonths);

  const isOz = _unitMp === 'OZON';
  const isYm = _unitMp === 'YM';
  const COLS = [
    ['nm',         isOz ? 'SKU Ozon' : isYm ? 'SKU' : 'Артикул WB'],
    ['name',       'Название'],
    ['unitCost',   'Себес. ед.'],
    ['qty',        'Шт'],
    ['revenue',    'Выкупы, ₽'],
    ['cogs',       'Себес., ₽'],
    ['delivery',   (isOz || isYm) ? 'Доставка' : 'Логистика'],
    ['storage',    isOz ? 'Услуги FBO' : 'Хранение'],
    ['commission', isOz ? 'Комиссия Ozon' : isYm ? 'Комиссия ЯМ' : 'Комиссия WB'],
    ['acquiring',  'Эквайринг'],
    ['advert',     'Продвижение'],
    ['other',      'Удерж./проч.'],
    ['tax',        _unitTax > 0 ? `Налог ${_unitTax}% с приб.` : 'Налог (—)'],
    ['profit',     'Прибыль/убыток'],
    ['roi',        'ROI %'],
  ];

  function derive(c) {
    if (!c || !Object.keys(c).length) return null;
    const other = (c.deductions || 0) + (c.penalty || 0) + (c.acceptance || 0);
    // налог — с прибыли (доходы-расходы), только с положительной
    const tax = _unitTax > 0 ? Math.max(c.gross || 0, 0) * _unitTax / 100 : 0;
    const profit = (c.gross || 0) - tax;
    const roi = (c.cogs || 0) > 0 ? Math.round(profit / c.cogs * 100) : null;
    return { ...c, other, tax, profit, roi };
  }

  function cell(key, d, r) {
    if (key === 'nm')   return `<td style="padding:5px 10px"><code style="color:var(--dim)">${r?.nmId || '—'}</code></td>`;
    if (key === 'name') return `<td style="padding:5px 10px;max-width:340px;overflow:hidden;text-overflow:ellipsis">
        <code style="color:var(--val-soft)">${r?.sku || ''}</code> <span class="text-secondary small">${r?.name || ''}</span></td>`;
    if (key === 'unitCost') {
      const v = r?.unitCost;
      return `<td class="text-end" style="padding:5px 10px;background:rgba(201,168,76,.05)">${v ? fmtRub(v) : '—'}</td>`;
    }
    if (!d) return `<td class="text-end" style="padding:5px 10px"><span class="text-muted small">—</span></td>`;
    const v = d[key];
    if (key === 'qty') return `<td class="text-end" style="padding:5px 10px;color:var(--ink)">${fmt(v || 0)}</td>`;
    if (key === 'roi') {
      if (v == null) return `<td class="text-end" style="padding:5px 10px"><span class="text-muted small">—</span></td>`;
      const clr = v >= 250 ? 'var(--pos-bright)' : v >= 150 ? 'var(--pos)' : v >= 80 ? 'var(--warn-c)' : 'var(--neg)';
      const bg = v >= 150 ? 'rgba(34,197,94,.12)' : v >= 80 ? 'rgba(251,191,36,.08)' : 'rgba(248,113,113,.10)';
      return `<td class="text-end" style="padding:5px 10px;background:${bg}"><span style="color:${clr};font-weight:700">${fmt(v)}%</span></td>`;
    }
    if (key === 'profit') {
      const clr = v >= 0 ? 'var(--pos)' : 'var(--neg)';
      return `<td class="text-end" style="padding:5px 10px"><span style="color:${clr};font-weight:700">${v < 0 ? '−' : ''}${fmtRub(Math.abs(Math.round(v)))}</span></td>`;
    }
    if (key === 'revenue') return `<td class="text-end" style="padding:5px 10px"><span style="color:var(--val);font-weight:600">${fmtRub(Math.round(v || 0))}</span></td>`;
    // затратные колонки
    if (!v) return `<td class="text-end" style="padding:5px 10px"><span class="text-muted small">—</span></td>`;
    return `<td class="text-end" style="padding:5px 10px;color:var(--ink)"><span style="color:var(--neg)">−</span>${fmtRub(Math.abs(Math.round(v)))}</td>`;
  }

  // группировка как в заказах/остатках
  const groupMap = {};
  skus.forEach(r => {
    const c = derive(_unitCellSum(r.months, selMonths));
    if (!c || (!c.revenue && !c.qty)) return;
    const g = articleGroup({ supplierArticle: r.sku, brand: r.brand });
    (groupMap[g] = groupMap[g] || []).push({ r, c });
  });
  Object.values(groupMap).forEach(list => list.sort((a, b) => (b.c.revenue || 0) - (a.c.revenue || 0)));
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  let html = `<div class="table-responsive"><table class="table table-sm align-middle mb-0 text-nowrap" style="font-size:0.83rem">`;
  html += `<thead><tr>` + COLS.map(([k, label], i) =>
    `<th class="${i > 1 ? 'text-end' : ''}" style="${i > 2 ? 'border-left:1px solid var(--sep);' : ''}min-width:${k === 'name' ? '280' : k === 'nm' ? '110' : '96'}px">${label}</th>`).join('') + `</tr></thead><tbody>`;

  // ИТОГО (сходится со вкладкой Финансы)
  const T = derive(totalsCell);
  html += `<tr style="background:var(--fin-total);border-bottom:2px solid var(--pos-strong);font-weight:700">
    <td style="padding:6px 10px;color:var(--val)" colspan="2">ИТОГО ${isOz ? 'Ozon' : isYm ? 'ЯМ' : 'WB'}</td>
    <td></td>${COLS.slice(3).map(([k]) => cell(k, T, null)).join('')}</tr>`;

  orderedGroups.forEach(([gname, list]) => {
    const G = derive(list.reduce((acc, { c }) => {
      Object.keys(c).forEach(k => { if (typeof c[k] === 'number') acc[k] = (acc[k] || 0) + c[k]; });
      return acc;
    }, {}));
    if (G) { G.roi = G.cogs > 0 ? Math.round(G.profit / G.cogs * 100) : null; }
    html += `<tr style="background:var(--surface-3);font-weight:600">
      <td colspan="3" style="padding:6px 10px;color:var(--val)">${gname} <span class="text-secondary small">(${list.length} арт.)</span></td>
      ${COLS.slice(3).map(([k]) => cell(k, G, null)).join('')}</tr>`;
    list.forEach(({ r, c }) => {
      html += `<tr style="background:var(--t-row)">${COLS.map(([k]) => cell(k, c, r)).join('')}</tr>`;
    });
  });

  html += `</tbody></table></div>`;
  const preciseMks = _unitData.advert_precise_months || [];
  const advNote = isYm
    ? 'Все статьи — из «Отчёта по стоимости услуг» YM (построчно по SKU); услуги без SKU (буст показов, поставки) — по доле выручки'
    : isOz
    ? 'Все статьи — из детализации начислений Ozon; начисления без привязки к товару (промо, размещение FBO) — по доле выручки'
    : ((_unitMonth !== 'ALL' && preciseMks.includes(_unitMonth))
        ? 'Продвижение — точная раскладка по артикулам из статистики кампаний WB (fullstats)'
        : 'Продвижение — по доле выручки' + (_unitData.advert_building ? ' (точная раскладка по кампаниям собирается в фоне, ~5-10 мин)' : ''));
  if ((_unitData.months_pending || []).length) {
    html += `<div class="text-info small mt-2">⏳ Месяцы ${_unitData.months_pending.join(', ')} появятся по мере сборки ${isOz ? 'SKU-разреза начислений Ozon' : 'раскладки рекламы (по минуте на месяц)'}. Страница обновится сама.</div>`;
    if (!window._unitPendingTimer) {
      window._unitPendingTimer = setTimeout(() => {
        window._unitPendingTimer = null;
        _unitData = null;
        delete _unitDataByMp[_unitMp];
        if (currentTab === 'unit') loadUnitEconomics();
      }, 30000);
    }
  }
  const taxNote = _unitTax > 0 ? `налог — ${_unitTax}% с прибыли (доходы−расходы)` : 'налог не задан (введите % сверху)';
  html += `<div class="text-secondary small mt-2">${advNote}; ${(isOz || isYm) ? '' : 'удержания — по доле выручки; '}${taxNote}; ROI = прибыль / себестоимость.` +
          (_unitData.detail_upto ? ` Отчёт реализации — по ${_unitData.detail_upto}.` : '') + `</div>`;
  wrap.innerHTML = html;
}

// ── Режим «динамика»: SKU × месяцы по выбранной метрике ───────────────────────

function renderUnitDynamics() {
  const wrap = document.getElementById('unitTableWrap');
  // кнопки метрик — инлайн над таблицей
  let metricBtns = `<div class="d-flex gap-1 flex-wrap mb-2">` + UNIT_METRICS.map(([k, label]) =>
    `<button class="btn btn-sm btn-outline-info ${k === _unitMetric ? 'active' : ''}" onclick="setUnitMetric('${k}')">${label}</button>`).join('') + `</div>`;

  const months = _unitData.months || [];
  const skus = _unitData.skus || [];
  const totals = _unitData.totals || {};
  const SEP = 'border-left:2px solid var(--sep);';

  let html = `<div class="table-responsive"><table class="table table-sm align-middle mb-0 text-nowrap" style="font-size:0.85rem">`;
  html += `<thead><tr>
    <th style="min-width:250px;position:sticky;left:0;background:var(--t-sticky);z-index:2">Артикул</th>`;
  months.forEach(m => { html += `<th class="text-end" style="${SEP}min-width:120px">${m.label}</th>`; });
  html += `<th class="text-end" style="${SEP}min-width:124px;color:#c026d3">Итого</th></tr></thead><tbody>`;

  function metricTotal(cells, key) {
    if (key === 'margin') {
      const g = months.reduce((a, m) => a + (cells[m.key]?.gross || 0), 0);
      const r = months.reduce((a, m) => a + (cells[m.key]?.revenue || 0), 0);
      return r > 0 ? Math.round(g / r * 100) : null;
    }
    return months.reduce((a, m) => a + (cells[m.key]?.[key] || 0), 0);
  }

  function rowCells(cells, emphasize) {
    let out = '';
    months.forEach((m, i) => {
      const cur = cells[m.key]?.[_unitMetric];
      const prev = i > 0 ? cells[months[i - 1].key]?.[_unitMetric] : null;
      out += `<td class="text-end" style="${SEP}padding:5px 12px">${_unitFmt(_unitMetric, cur)}${_momBadge(_unitMetric, cur, prev)}</td>`;
    });
    const tot = metricTotal(cells, _unitMetric);
    out += `<td class="text-end" style="${SEP}padding:5px 12px;background:rgba(201,168,76,.04)">${_unitFmt(_unitMetric, tot)}</td>`;
    return out;
  }

  // ИТОГО (сходится со вкладкой Финансы)
  html += `<tr style="background:var(--fin-total);border-bottom:2px solid var(--pos-strong)">
    <td style="position:sticky;left:0;background:var(--fin-total);padding:6px 12px;color:var(--val);font-weight:700">ИТОГО ${_unitMp === 'OZON' ? 'Ozon' : _unitMp === 'YM' ? 'ЯМ' : 'WB'}</td>
    ${rowCells(totals, true)}</tr>`;

  skus.forEach(r => {
    const expanded = _unitExpanded.has(r.sku);
    html += `<tr style="background:var(--t-row);cursor:pointer" onclick="toggleUnitSku('${r.sku}')">
      <td style="position:sticky;left:0;background:var(--t-sticky);padding:6px 12px">
        <span style="color:var(--dim)">${expanded ? '▼' : '▶'}</span>
        <code style="color:var(--val-soft)">${r.sku}</code>
        <span class="text-secondary small">${r.name || ''}</span></td>
      ${rowCells(r.months)}</tr>`;
    if (expanded) {
      UNIT_PNL_ROWS.forEach(([key, label, clr]) => {
        html += `<tr style="background:var(--fin-cost);font-size:0.8rem">
          <td style="position:sticky;left:0;background:var(--t-detail);padding:3px 12px 3px 34px;color:${clr}">${label}</td>`;
        months.forEach(m => {
          html += `<td class="text-end" style="${SEP}padding:3px 12px">${_unitFmt(key, r.months[m.key]?.[key])}</td>`;
        });
        const tot = key === 'margin'
          ? (() => { const g = months.reduce((a, m) => a + (r.months[m.key]?.gross || 0), 0);
                     const rv = months.reduce((a, m) => a + (r.months[m.key]?.revenue || 0), 0);
                     return rv > 0 ? Math.round(g / rv * 100) : null; })()
          : months.reduce((a, m) => a + (r.months[m.key]?.[key] || 0), 0);
        html += `<td class="text-end" style="${SEP}padding:3px 12px">${_unitFmt(key, tot)}</td></tr>`;
      });
    }
  });

  html += `</tbody></table></div>`;
  if (_unitData.detail_upto) {
    html += `<div class="text-secondary small mt-2">Продвижение и удержания распределены по SKU пропорционально доле выручки месяца. Отчёт реализации — по ${_unitData.detail_upto}.</div>`;
  }
  wrap.innerHTML = metricBtns + html;
}

// ── Init ──────────────────────────────────────────────────────────────────────

function initDashboard() {
  switchTab('salesan', document.querySelector('#mainTabs .nav-link'));
  setInterval(() => { markAllDirty(); switchTab(currentTab); }, 30 * 60 * 1000);

  // Preload all other tabs in background so switching feels instant
  const bgTabs = [
    { name: 'stocks',  fn: loadStocks },
    { name: 'history', fn: loadHistory },
    { name: 'finance', fn: loadFinance },
    { name: 'reviews', fn: loadReviews },
  ];
  bgTabs.forEach(({ name, fn }, i) => {
    setTimeout(() => {
      if (dirty[name]) { dirty[name] = false; fn(); }
    }, (i + 1) * 2000); // stagger: 2s, 4s, 6s, 8s
  });
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


// === REVIEWS TAB (auto-fetch from WB / Ozon / YM APIs) ===
let _allReviews = [];
let _statsData = {};
let _dynData = {};
let _drafts = {};
let _reviewsSig = '';      // сигнатура данных — чтобы не перерисовывать без изменений
let _reviewsPolling = false;

function _reviewsSignature(data) {
  const s = data.stats || {};
  return `${(data.reviews || []).length}|${JSON.stringify(s)}|${Object.keys(data.drafts || {}).length}`;
}

async function _fetchReviewsData() {
  const res = await fetch(`${API}/api/reviews/data?limit=5000`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function _applyReviewsData(data) {
  _allReviews = data.reviews || [];
  _statsData = data.stats || {};
  _dynData = data.dynamics || {};
  _drafts = data.drafts || {};
  renderRatingsTable(data.ratings || {});
  renderStats(_statsData);
  populateDynFilter(data.ratings || {});
  renderRatingDynamicsFiltered();
  renderReviewsFeed();
  populateRatingCalc((data.ratings || {}).articles || [], (data.ratings || {}).groups || []);
}

async function loadReviews() {
  const feedEl = document.getElementById('reviewsFeed');
  if (feedEl && !_allReviews.length) feedEl.innerHTML = '<p class="text-secondary">Загружаем отзывы...</p>';
  try {
    const data = await _fetchReviewsData();
    _reviewsSig = _reviewsSignature(data);
    _applyReviewsData(data);
  } catch (e) {
    console.error('loadReviews', e);
    if (feedEl) feedEl.innerHTML = `<p class="text-danger">Ошибка: ${e.message}</p>`;
  }
  // /data сам запускает фоновую синхронизацию на бэке (TTL 15 мин) —
  // поллим результат и перерисовываем, когда придут новые данные
  pollReviewsUpdates();
}

async function pollReviewsUpdates(times = 8, intervalMs = 7000) {
  if (_reviewsPolling) return;
  _reviewsPolling = true;
  const note = document.getElementById('reviewsSyncNote');
  if (note) note.textContent = '⏳ синхронизация с площадками…';
  try {
    for (let i = 0; i < times; i++) {
      await new Promise(r => setTimeout(r, intervalMs));
      try {
        const data = await _fetchReviewsData();
        const sig = _reviewsSignature(data);
        if (sig !== _reviewsSig) {
          _reviewsSig = sig;
          _applyReviewsData(data);
        }
      } catch (e) { console.error('pollReviews', e); }
    }
  } finally {
    _reviewsPolling = false;
    if (note) note.textContent = '';
  }
}

async function forceRefreshReviews() {
  const btn = document.getElementById('refreshReviewsBtn');
  if (btn) btn.textContent = 'Обновляем...';
  try {
    // бэк запускает обновление в фоне и сразу отвечает; результат подхватит поллинг
    await fetch(`${API}/api/reviews/refresh?force=true`, { method: 'POST' });
    await pollReviewsUpdates();
  } catch (e) { console.error('refreshReviews', e); }
  if (btn) btn.textContent = '🔄 Обновить';
}

// ── Калькулятор рейтинга: сколько 5★ нужно до цели ───────────────────────────

let _ratingsArticles = [];
let _ratingsGroups = [];

function populateRatingCalc(articles, groups) {
  _ratingsArticles = articles || [];
  _ratingsGroups = groups || [];
  const sel = document.getElementById('calcSku');
  if (!sel) return;
  const cur = sel.value;
  let html = '<option value="">— выберите артикул или группу —</option>';
  if (_ratingsGroups.length) {
    html += '<optgroup label="Группы">' +
      _ratingsGroups.map(g => `<option value="grp:${g.group}">📦 ${g.group}</option>`).join('') +
      '</optgroup>';
  }
  html += '<optgroup label="Артикулы">' +
    _ratingsArticles.map(a => {
      const label = a.name && a.name !== a.sku ? `${a.sku} — ${a.name}` : a.sku;
      return `<option value="${a.sku}">${label}</option>`;
    }).join('') + '</optgroup>';
  sel.innerHTML = html;
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  renderRatingCalc();
}

function renderRatingCalc() {
  const out = document.getElementById('ratingCalcOut');
  if (!out) return;
  const val  = (document.getElementById('calcSku')  || {}).value || '';
  const plat = (document.getElementById('calcPlatform') || {}).value || 'wb';
  let art, entityLabel;
  if (val.startsWith('grp:')) {
    const gname = val.slice(4);
    art = _ratingsGroups.find(g => g.group === gname);
    entityLabel = `группе «${gname}»`;
  } else {
    art = _ratingsArticles.find(a => a.sku === val);
    entityLabel = 'этому артикулу';
  }
  if (!art) {
    out.innerHTML = '<span class="text-secondary">Выберите артикул или группу, чтобы рассчитать</span>';
    return;
  }
  const n = art[plat + '_cnt'] || 0;
  const s = art[plat + '_sum'] || 0;
  const PLAT_NAME = { wb: 'WB', ozon: 'Ozon', ym: 'ЯМ' };
  if (!n) {
    out.innerHTML = `<span class="text-secondary">По ${entityLabel} нет оценок на ${PLAT_NAME[plat]}</span>`;
    return;
  }
  const cur = s / n;
  // Клиент видит рейтинг с одной цифрой после запятой: 4.551 показывается как 4.6.
  // Значит цель «4.8» достигнута уже при среднем ≥ 4.75 (порог округления).
  const shown = Math.round(cur * 10) / 10;

  const targets = [4.5, 4.6, 4.7, 4.8, 4.9];
  let rows = '';
  targets.forEach(t => {
    const threshold = t - 0.05;  // минимальное среднее, при котором показывается t
    let needHtml, afterHtml;
    if (cur >= threshold) {
      needHtml  = '<span class="text-success">✓ достигнуто</span>';
      afterHtml = `<span class="text-secondary">показывается ${shown.toFixed(1)}★</span>`;
    } else {
      const need = Math.ceil((threshold * n - s) / (5 - threshold));
      const after = (s + 5 * need) / (n + need);
      needHtml  = `<span class="fw-bold" style="color:var(--pos)">+${fmt(need)}</span> оценок 5★`;
      afterHtml = `<span class="text-secondary">среднее станет ${after.toFixed(3)} → покажет ${t.toFixed(1)}★</span>`;
    }
    rows += `<tr>
      <td class="fw-semibold">${t.toFixed(1)}★</td>
      <td>${needHtml}</td>
      <td>${afterHtml}</td>
    </tr>`;
  });

  // Влияние одной плохой оценки
  const drop1 = (s + 1) / (n + 1);
  const drop1Shown = Math.round(drop1 * 10) / 10;

  out.innerHTML = `
    <div class="mb-2">
      Текущий рейтинг на <b>${PLAT_NAME[plat]}</b>:
      <span class="fw-bold" style="color:var(--warn-c);font-size:1.1rem">${shown.toFixed(1)}★</span>
      <span class="text-secondary">(точное среднее ${cur.toFixed(3)}, ${fmt(n)} оценок)</span>
    </div>
    <div class="table-responsive">
      <table class="table table-sm table-dark align-middle mb-2" style="max-width:680px">
        <thead><tr class="text-secondary small">
          <th>Цель (на витрине)</th><th>Сколько нужно</th><th>Результат</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="text-secondary small">
      Расчёт учитывает округление витрины: например, для «4.8★» достаточно среднего 4.75.
      ⚠ Одна новая оценка 1★ уронит среднее до <b>${drop1.toFixed(3)}</b>
      (на витрине — ${drop1Shown.toFixed(1)}★).
      Площадки могут учитывать только недавние оценки, поэтому цифра в карточке может отличаться.
    </div>`;
}

async function analyzeStyle() {
  const btn = document.getElementById('analyzeStyleBtn');
  const out = document.getElementById('styleGuide');
  if (btn) { btn.disabled = true; btn.textContent = '🧠 Анализирую…'; }
  if (out) out.innerHTML = '<div class="text-secondary small">Изучаю наши прошлые ответы…</div>';
  try {
    const res = await fetch(`${API}/api/reviews/analyze-style?platform=WB&sample=300`, { method: 'POST' });
    const g = await res.json();
    renderStyleGuide(g);
  } catch (e) {
    if (out) out.innerHTML = `<div class="text-danger small">Ошибка: ${e.message}</div>`;
  }
  if (btn) { btn.disabled = false; btn.textContent = '🧠 Анализ стиля ответов'; }
}

function renderStyleGuide(g) {
  const out = document.getElementById('styleGuide');
  if (!out) return;
  if (g.error) {
    out.innerHTML = `<div class="alert alert-warning py-2 small mb-0">${g.error}</div>`;
    return;
  }
  const list = arr => (arr || []).map(x => `<li>${x}</li>`).join('');
  const m = g._meta || {};
  out.innerHTML = `
    <div class="card bg-card border-0">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <h6 class="mb-0">Стиль наших ответов <span class="text-secondary small">(${m.platform || 'WB'}, проанализировано ${m.analyzed || 0})</span></h6>
        </div>
        <div class="row small g-3">
          <div class="col-md-6">
            <div><b>Тон:</b> ${g.tone || '—'}</div>
            <div><b>Длина:</b> ${g.avg_length || '—'}</div>
            <div><b>Обращение:</b> ${g.greeting || '—'}</div>
            <div><b>Подпись:</b> ${g.signature || '—'}</div>
            <div><b>Эмодзи:</b> ${g.emoji || '—'}</div>
            <div><b>Структура:</b> ${g.structure || '—'}</div>
          </div>
          <div class="col-md-6">
            ${g.by_rating ? `<div><b>На 5★:</b> ${g.by_rating['5'] || '—'}</div>
            <div><b>На негатив:</b> ${g.by_rating.low || '—'}</div>` : ''}
            ${g.common_phrases ? `<div class="mt-1"><b>Частые фразы:</b><ul class="mb-1">${list(g.common_phrases)}</ul></div>` : ''}
          </div>
          <div class="col-md-6">${g.dos ? `<b class="text-success">Делаем:</b><ul class="mb-0">${list(g.dos)}</ul>` : ''}</div>
          <div class="col-md-6">${g.donts ? `<b class="text-danger">Избегаем:</b><ul class="mb-0">${list(g.donts)}</ul>` : ''}</div>
        </div>
        ${g.system_prompt ? `<details class="mt-2"><summary class="small text-secondary" style="cursor:pointer">Готовый промпт для генерации ответов</summary>
          <pre class="small mt-2 p-2 bg-dark rounded" style="white-space:pre-wrap">${g.system_prompt}</pre></details>` : ''}
      </div>
    </div>`;
}

function renderStats(stats) {
  const el = document.getElementById('reviewsStats');
  if (!el) return;
  const parts = Object.entries(stats.by_platform || {})
    .map(([p, d]) => `${p}: <b>${d.count}</b> (★${d.avg})`).join(' · ');
  el.innerHTML = `Всего отзывов: <b>${stats.total || 0}</b>${parts ? ' · ' + parts : ''} · Последний: ${stats.last_review || '—'}`;
}

function renderRatingsTable(ratings) {
  const el = document.getElementById('ratingsTable');
  if (!el) return;
  const groups   = ratings.groups   || [];
  const articles = ratings.articles || [];
  if (!groups.length && !articles.length) {
    el.innerHTML = '<p class="text-secondary small">Нет данных. Нажмите «Обновить».</p>'; return;
  }
  const color = v => !v ? '' : v >= 4.8 ? 'text-success' : v >= 4.5 ? 'text-warning' : 'text-danger';
  const cell = (v, cnt) => v != null
    ? `<b class="${color(v)}">${v.toFixed(2)}</b> <span class="text-secondary small">(${cnt})</span>`
    : '<span class="text-secondary">—</span>';
  const hdr = `<thead><tr><th>Название</th>
    <th class="text-center">Ozon</th><th class="text-center">WB</th><th class="text-center">YM</th>
  </tr></thead>`;

  let html = '<h6 class="text-secondary small mb-1">По склейкам</h6>';
  html += `<div class="table-responsive mb-3"><table class="table table-dark table-sm table-hover mb-0">${hdr}<tbody>`;
  for (const r of groups) {
    html += `<tr><td class="fw-semibold">${r.group}</td>
      <td class="text-center">${cell(r.ozon, r.ozon_cnt)}</td>
      <td class="text-center">${cell(r.wb,   r.wb_cnt)}</td>
      <td class="text-center">${cell(r.ym,   r.ym_cnt)}</td>
    </tr>`;
  }
  html += '</tbody></table></div>';

  // Группировка по брендам + спец-разбивки, как в Остатках
  const groupMap = {};
  articles.forEach(r => {
    const g = articleGroup(r);
    (groupMap[g] = groupMap[g] || []).push(r);
  });
  Object.values(groupMap).forEach(rows => rows.sort((a, b) => a.sku.localeCompare(b.sku)));
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  html += '<h6 class="text-secondary small mb-1">По артикулам</h6>';
  html += `<div class="table-responsive"><table class="table table-dark table-sm table-hover mb-0">
    <thead><tr><th>Артикул</th><th>Название</th>
      <th class="text-center">Ozon</th><th class="text-center">WB</th><th class="text-center">YM</th>
    </tr></thead><tbody>`;
  for (const [grp, rows] of orderedGroups) {
    html += `<tr class="table-secondary"><td colspan="5"><strong>${grp}</strong> <span class="text-secondary small">(${rows.length} арт.)</span></td></tr>`;
    for (const r of rows) {
      html += `<tr>
        <td class="text-secondary small">${r.sku}</td>
        <td>${r.name || r.sku}</td>
        <td class="text-center">${cell(r.ozon, r.ozon_cnt)}</td>
        <td class="text-center">${cell(r.wb,   r.wb_cnt)}</td>
        <td class="text-center">${cell(r.ym,   r.ym_cnt)}</td>
      </tr>`;
    }
  }
  html += '</tbody></table></div>';
  el.innerHTML = html;
}

function populateDynFilter(ratings) {
  const sel = document.getElementById('dynArtFilter');
  if (!sel) return;
  sel.innerHTML = '<option value="__all__">Все (по платформам)</option>';
  const articles = (ratings.articles || []).slice().sort((a, b) => a.sku.localeCompare(b.sku));
  const grouped = {};
  articles.forEach(r => {
    const g = articleGroup(r);
    (grouped[g] = grouped[g] || []).push(r);
  });
  GROUP_ORDER.filter(g => grouped[g]).forEach(g => {
    const og = document.createElement('optgroup');
    og.label = g;
    grouped[g].forEach(r => {
      const o = document.createElement('option');
      o.value = r.sku;
      o.textContent = `${r.sku} — ${r.name || r.sku}`;
      og.appendChild(o);
    });
    sel.appendChild(og);
  });
}

function renderRatingDynamicsFiltered() {
  const sel = document.getElementById('dynArtFilter');
  const chosen = sel ? sel.value : '__all__';
  if (chosen === '__all__') {
    renderRatingDynamics(_dynData.overview || []);
  } else {
    const artData = (_dynData.by_article || {})[chosen] || [];
    renderRatingDynamics(artData, chosen);
  }
}

function renderRatingDynamics(dyn, artLabel) {
  const cv = document.getElementById('ratingDynChart');
  if (!cv) return;
  if (charts.ratingDyn) charts.ratingDyn.destroy();
  if (!dyn.length) return;
  const dateKey = dyn[0].date !== undefined ? 'date' : 'month';
  const labels = dyn.map(d => d[dateKey] || d.month);
  const mk = (key, label, color) => ({
    label, borderColor: color, backgroundColor: color, tension: 0.3, spanGaps: true,
    pointRadius: 3,
    data: dyn.map(d => d[key] ?? null),
  });

  // Авто-зум шкалы: все рейтинги ~4.2–5.0, фиксированная 1–5 жмёт всё вверх
  const vals = [];
  dyn.forEach(d => ['ozon', 'wb', 'ym'].forEach(k => { if (d[k] != null) vals.push(d[k]); }));
  const dataMin = vals.length ? Math.min(...vals) : 1;
  const yMin = Math.max(1, Math.floor((dataMin - 0.1) * 4) / 4);  // вниз до 0.25
  charts.ratingDyn = new Chart(cv.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [
      mk('ozon', 'Ozon', '#6366f1'),
      mk('wb', 'WB', '#ef4444'),
      mk('ym', 'YM', '#b45309'),
    ] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: CHART_C.legend, usePointStyle: true } },
        title: artLabel ? { display: true, text: artLabel, color: CHART_C.tick, font: { size: 12 } } : { display: false },
      },
      scales: {
        x: { ticks: { color: CHART_C.tick }, grid: { display: false } },
        y: { min: yMin, max: 5, ticks: { color: CHART_C.tick, stepSize: 0.25 },
             grid: { color: CHART_C.gridSoft } },
      },
    },
  });
}

function renderReviewsFeed() {
  const el = document.getElementById('reviewsFeed');
  if (!el) return;
  const platform = document.getElementById('reviewsPlatform')?.value || 'all';
  const onlyText = document.getElementById('reviewsOnlyText')?.checked ?? true;

  let filtered = platform === 'all' ? _allReviews : _allReviews.filter(r => r.platform === platform);
  if (onlyText) filtered = filtered.filter(r => r.text);

  if (!filtered.length) { el.innerHTML = '<p class="text-secondary mt-3">Нет отзывов</p>'; return; }

  const stars = n => '<span class="text-warning">' + '★'.repeat(n) + '</span><span class="text-secondary">' + '☆'.repeat(5 - n) + '</span>';
  const badge = p => {
    const c = { Ozon: 'primary', WB: 'danger', YM: 'warning' };
    return `<span class="badge bg-${c[p] || 'secondary'}">${p}</span>`;
  };

  el.innerHTML = filtered.map(r => `
    <div class="card bg-dark border-secondary mb-2 p-3">
      <div class="d-flex justify-content-between align-items-start mb-1">
        <div class="d-flex align-items-center gap-2 flex-wrap">
          ${badge(r.platform)} ${stars(r.rating)}
          <span class="text-secondary small">${r.date}</span>
        </div>
        <div class="text-end ms-2">
          <div class="small">${r.name || r.sku}</div>
          ${r.group ? `<div class="text-secondary" style="font-size:0.75rem">${r.group}</div>` : ''}
        </div>
      </div>
      ${r.text ? `<div class="mt-1">${r.text}</div>` : '<div class="text-secondary small fst-italic">без текста</div>'}
      ${replyBlock(r)}
    </div>
  `).join('');
}

const ANSWERED_MARK = '✓ Отвечено на платформе';

function replyBlock(r) {
  // Уже ответили на платформе
  if (r.answer === ANSWERED_MARK) {
    return `<div class="mt-2 ps-2 border-start border-success">
      <div class="text-success small">✓ Ответ уже оставлен на платформе</div>
    </div>`;
  }
  if (r.answer) {
    return `<div class="mt-2 ps-2 border-start border-success">
      <div class="text-success small mb-1">✓ Наш ответ</div>
      <div class="small text-secondary">${esc(r.answer)}</div>
    </div>`;
  }
  const d = _drafts[r.id];
  if (!d) {
    return `<div class="mt-2">
      <button class="btn btn-sm btn-outline-info py-0" onclick="genDraft('${r.id}')">✨ Сгенерировать ответ</button>
    </div>`;
  }
  if (d.status === 'approved') {
    return `<div class="mt-2 ps-2 border-start border-success">
      <div class="text-success small mb-1">✓ Одобрено и опубликовано</div>
      <div class="small text-secondary">${esc(d.draft)}</div>
    </div>`;
  }
  if (d.status === 'declined') {
    return `<div class="mt-2">
      <span class="text-danger small">✕ Отклонено.</span>
      <button class="btn btn-sm btn-outline-info py-0 ms-2" onclick="genDraft('${r.id}')">✨ Сгенерировать заново</button>
    </div>`;
  }
  // pending — редактируемый черновик
  const ta = `draft_${cssId(r.id)}`;
  const platIcon = { WB: '🟣', Ozon: '🔵', YM: '🟡' }[r.platform] || '📤';
  return `<div class="mt-2 ps-2 border-start border-info">
    <div class="text-info small mb-1">🤖 Черновик ответа (можно отредактировать)</div>
    <textarea id="${ta}" class="form-control form-control-sm bg-dark text-white border-secondary mb-2"
              rows="3" oninput="autoGrow(this)">${esc(d.draft)}</textarea>
    <div class="d-flex gap-2">
      <button class="btn btn-sm btn-success py-0" onclick="approveDraft('${r.id}')">✓ Одобрить ${platIcon}</button>
      <button class="btn btn-sm btn-outline-danger py-0" onclick="declineDraft('${r.id}')">✕ Отклонить</button>
      <button class="btn btn-sm btn-outline-secondary py-0" onclick="genDraft('${r.id}')">↻ Перегенерировать</button>
    </div>
  </div>`;
}

function cssId(id) { return id.replace(/[^a-zA-Z0-9_-]/g, '_'); }

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = el.scrollHeight + 'px';
}

function esc(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function genDraft(id) {
  _drafts[id] = { draft: 'Генерирую…', status: 'pending' };
  renderReviewsFeed();
  try {
    const res = await fetch(`${API}/api/reviews/draft?id=${encodeURIComponent(id)}`, { method: 'POST' });
    const d = await res.json();
    if (d.error) { delete _drafts[id]; alert(d.error); }
    else _drafts[id] = { draft: d.draft, status: d.status };
  } catch (e) { delete _drafts[id]; alert('Ошибка: ' + e.message); }
  renderReviewsFeed();
}

async function approveDraft(id) {
  const ta = document.getElementById('draft_' + cssId(id));
  const text = ta ? ta.value.trim() : (_drafts[id] && _drafts[id].draft) || '';
  if (!text) { alert('Текст ответа пустой'); return; }
  try {
    const res = await fetch(`${API}/api/reviews/approve?id=${encodeURIComponent(id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const d = await res.json();
    if (d.error) { alert('Не опубликовано: ' + d.error); return; }
    _drafts[id].draft = text;
    _drafts[id].status = 'approved';
  } catch (e) { alert('Ошибка: ' + e.message); }
  renderReviewsFeed();
}

async function declineDraft(id) {
  try {
    await fetch(`${API}/api/reviews/decline?id=${encodeURIComponent(id)}`, { method: 'POST' });
    _drafts[id].status = 'declined';
  } catch (e) { alert('Ошибка: ' + e.message); }
  renderReviewsFeed();
}

async function genBatch() {
  const btn = document.getElementById('genBatchBtn');
  const platform = document.getElementById('batchPlatform')?.value || 'all';
  if (btn) { btn.disabled = true; btn.textContent = '✨ Генерирую…'; }
  try {
    const res = await fetch(`${API}/api/reviews/draft-batch?platform=${platform}&limit=20`, { method: 'POST' });
    const d = await res.json();
    await loadReviews();
    alert(`Сгенерировано черновиков: ${d.generated || 0} из ${d.requested || 0} отзывов`);
  } catch (e) { alert('Ошибка: ' + e.message); }
  if (btn) { btn.disabled = false; btn.textContent = '✨ Сгенерировать ответы'; }
}
