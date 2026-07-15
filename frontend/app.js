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
const dirty = { salesan: true, stocks: true, reviews: true, finance: true, unit: true, tools: true, toolsoz: true, docs: true };
let _advertData = [];
let currentTab = 'finance';
let prodAllData = [];

// ── Группировка по брендам + спец-разбивки (фисты / спреи для минета) ──────────
// Дефолты — кабинет Biomed; для других кабинетов переопределяется из /api/cabinet
let BRAND_ORDER = ['Джага', 'Satisfucktion', 'Aloe'];
let SUBGROUPS = [
  { name: 'Фисты',             skus: ['BMN-0013', 'BMN-0028', 'BMN-0035', 'BMN-0036', 'ST-07'] },
  { name: 'Спреи для минета',  skus: ['BMN-0115', 'BMN-0116', 'BMN-0110'] },
];
let GROUP_ORDER = ['Фисты', 'Aloe', 'Спреи для минета', 'Satisfucktion', 'Джага', 'Прочее'];

// ── Кабинет (мультикабинетность: один деплой = один кабинет) ──────────────────
let _cab = null;   // {id, name, marketplaces, group_order?, other?}

async function loadCabinetInfo() {
  try {
    _cab = await (await fetch('/api/cabinet')).json();
  } catch (e) { return; }
  if (_cab.group_order) GROUP_ORDER = _cab.group_order;
  if (_cab.brand_order) BRAND_ORDER = _cab.brand_order;
  if (_cab.subgroups) SUBGROUPS = _cab.subgroups;
  // имя кабинета в шапке и на карточке выбора
  const brand = document.getElementById('cabBrandName');
  if (brand) brand.textContent = `${_cab.name} — аналитика`;
  const cabName = document.getElementById('cabCurrentName');
  if (cabName) cabName.textContent = _cab.name;
  if (_cab.id === 'fk') {
    // иконка текущего кабинета — монограмма ФК в стиле квадрата BN
    const img = document.querySelector('#cabBiomed img');
    if (img) img.outerHTML = '<div class="cab-mono">ФК</div>';
    const navImg = document.querySelector('.navbar-brand img');
    if (navImg) navImg.outerHTML = '<span class="cab-mono cab-mono-sm">ФК</span>';
  }
  // карточка второго кабинета
  const grid = document.querySelector('.mp-cab-grid');
  if (grid && _cab.other && _cab.other.url && !document.getElementById('cabOther')) {
    const div = document.createElement('div');
    div.id = 'cabOther';
    div.className = 'mp-cab-card';
    div.onclick = () => { window.location.href = _cab.other.url.replace(/\/+$/, '') + '/?enter=1'; };
    // иконка второй карточки — по тому, КУДА она ведёт: у ФК ссылка на Biomed (лев BN), у Biomed — на ФК (помада)
    const otherIcon = _cab.id === 'fk'
      ? '<img src="/static/lion_logo.svg" alt="BN" style="width:64px;height:64px" />'
      : '<div class="cab-mono">ФК</div>';
    div.innerHTML = `${otherIcon}<span>${_cab.other.name}</span>`;
    grid.appendChild(div);
  }
  // скрываем площадки, которых нет в кабинете (ЯМ у Фабрики красоты)
  const mps = _cab.marketplaces || [];
  if (!mps.includes('YM')) {
    ['ordMpYM', 'finMpYM', 'unitMpYM'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
  }
}
document.addEventListener('DOMContentLoaded', loadCabinetInfo);

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

let _me = null;   // {login, role} — текущий пользователь (серверная сессия)

function applyRole() {
  if (!_me) return;
  // менеджеру скрываем вкладку Финансы (юнитка остаётся)
  if (_me.role === 'manager') {
    document.querySelectorAll('#mainTabs .nav-link').forEach(a => {
      if (a.textContent.trim() === 'Финансы') a.closest('li').style.display = 'none';
    });
  }
  // владельцу — кнопка управления доступами
  const btn = document.getElementById('usersBtn');
  if (btn) btn.style.display = _me.role === 'owner' ? '' : 'none';
}

function showOverlay(name) {
  document.getElementById('loginOverlay').style.display   = name === 'login'   ? 'flex' : 'none';
  document.getElementById('cabinetOverlay').style.display = name === 'cabinet' ? 'flex' : 'none';
  const showApp = name === 'app';
  document.getElementById('mainNav').style.display     = showApp ? 'flex'  : 'none';
  document.getElementById('mainContent').style.display = showApp ? 'block' : 'none';
}

async function doLogin() {
  const u = document.getElementById('loginUser').value.trim();
  const p = document.getElementById('loginPass').value;
  const err = document.getElementById('loginError');
  err.textContent = '';
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login: u, password: p }),
    });
    if (!r.ok) {
      err.textContent = (await r.json().catch(() => ({}))).detail || 'Неверный логин или пароль';
      return;
    }
    _me = await r.json();
    applyRole();
    showOverlay('cabinet');
  } catch (e) {
    err.textContent = 'Сервер недоступен: ' + e.message;
  }
}

async function doLogout() {
  await fetch('/api/auth/logout', { method: 'POST' }).catch(() => {});
  _me = null;
  localStorage.removeItem('mp_cabinet');
  location.reload();
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
    const sep = path.includes('?') ? '&' : '?';
    r = await fetch(`${API}${path}${sep}${getParams()}`, { signal: ctrl.signal });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('Таймаут запроса (' + Math.round(timeoutMs/1000) + 'с)');
    throw e;
  }
  clearTimeout(timer);
  if (r.status === 401) { showOverlay('login'); throw new Error('Не авторизован'); }
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
  ['salesan', 'stocks', 'reviews', 'finance', 'unit', 'tools', 'toolsoz', 'docs'].forEach(t => {
    const el = document.getElementById('pane-' + t);
    if (el) el.style.display = t === name ? 'block' : 'none';
  });
  currentTab = name;
  if (dirty[name]) {
    dirty[name] = false;
    ({ salesan: loadSalesAnalytics, stocks: loadStocks,
       reviews: loadReviews, finance: loadFinance, tools: loadTools, toolsoz: loadOzTool, docs: loadDocs,
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
  // Biomed
  'Фисты':            '#ef4444',
  'Aloe':             '#22c55e',
  'Спреи для минета': '#f59e0b',
  'Satisfucktion':    '#ec4899',
  'Джага':            '#8b5cf6',
  // Фабрика красоты
  'Крема':            '#0ea5e9',
  'Сыворотки':        '#a855f7',
  'Прочее':           CHART_C.tick2,
};
// запасная палитра для любых незнакомых групп (в любом кабинете) —
// стабильный цвет по имени, чтобы не сваливались все в серый
const _GROUP_PALETTE = ['#0ea5e9', '#a855f7', '#f59e0b', '#22c55e', '#ec4899',
                        '#8b5cf6', '#ef4444', '#14b8a6', '#eab308', '#6366f1'];
function _groupColor(g) {
  if (_GROUP_COLORS[g]) return _GROUP_COLORS[g];
  if (g === 'Прочее' || !g) return CHART_C.tick2;
  let h = 0;
  for (let i = 0; i < g.length; i++) h = (h * 31 + g.charCodeAt(i)) >>> 0;
  return _GROUP_PALETTE[h % _GROUP_PALETTE.length];
}

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
      const mps = (_cab && _cab.marketplaces) || ['WB', 'OZON', 'YM'];
      const jobs = mps.map(m => loadFinanceMp(m));
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
  const cabMps = (_cab && _cab.marketplaces) || ['WB', 'OZON', 'YM'];
  const PLATS = [['WB', 'WB'], ['OZON', 'Ozon'], ['YM', 'ЯМ']].filter(([k]) => cabMps.includes(k));
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

const _manCostMks = new Set();   // выбранные месяцы для новой статьи

function toggleManCostMk(mk, btn) {
  if (_manCostMks.has(mk)) { _manCostMks.delete(mk); btn.classList.remove('active'); }
  else { _manCostMks.add(mk); btn.classList.add('active'); }
}

async function addManualCost() {
  const label = (document.getElementById('manCostLabel')?.value || '').trim();
  const amount = parseFloat(document.getElementById('manCostAmount')?.value || '0');
  if (!_manCostMks.size || !label || !amount) return;
  await fetch('/api/finance/manual_costs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mks: [..._manCostMks], label, amount }),
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

// ── График выплат (Тотал) ────────────────────────────────────────────────────

let _payoutsData = null;

async function loadPayouts(force) {
  const box = document.getElementById('payoutsBody');
  if (!box) return;
  if (_payoutsData && !force) { renderPayouts(); return; }
  box.innerHTML = '<div class="text-center text-secondary py-3"><span class="spinner-border spinner-border-sm me-2"></span>Загружаем балансы…</div>';
  try {
    _payoutsData = await fetchJSON('/api/finance/payouts' + (force ? '?refresh=true' : ''), 60000);
    renderPayouts();
  } catch (e) {
    box.innerHTML = `<div class="text-danger small">Ошибка: ${e.message}</div>`;
  }
}

function renderPayouts() {
  const box = document.getElementById('payoutsBody');
  if (!box || !_payoutsData) return;
  const d = _payoutsData;
  let html = `<div class="d-flex flex-column gap-2">`;
  (d.items || []).forEach(it => {
    html += `<div class="d-flex align-items-start gap-2 flex-wrap" style="border-bottom:1px solid var(--border);padding-bottom:8px">
      <span class="mp-selector-dot mt-1" style="background:${it.color}"></span>
      <span class="fw-semibold" style="min-width:52px;color:var(--ink)">${it.mp}</span>
      <div class="d-flex gap-4 flex-wrap">
        ${it.balance != null ? `<span><span class="text-secondary small">Баланс кабинета:</span> <b style="color:var(--val)">${fmtRub(it.balance)}</b></span>` : ''}
        ${it.for_withdraw != null && it.for_withdraw !== it.balance ? `<span><span class="text-secondary small">К выводу:</span> <b style="color:var(--pos)">${fmtRub(it.for_withdraw)}</b></span>` : ''}
        <span><span class="text-secondary small">Ожидается к поступлению:</span> <b style="color:var(--pos)">~${fmtRub(it.upcoming || 0)}</b></span>
      </div>
      <div class="text-secondary small w-100" style="margin-left:24px">${it.note || ''}</div>
    </div>`;
  });
  html += `<div class="d-flex gap-4 flex-wrap pt-1">
    <span class="fw-semibold" style="color:var(--ink)">Итого ожидается: <span style="color:var(--pos)">~${fmtRub(d.total_upcoming || 0)}</span></span>
    ${d.total_balance ? `<span class="text-secondary">балансы (где доступны): ${fmtRub(d.total_balance)}</span>` : ''}
    <button class="btn btn-sm btn-outline-secondary py-0 ms-auto" onclick="loadPayouts(true)">↻ Обновить</button>
  </div>
  <div class="text-secondary small text-end">${d.fetched_at || ''}</div></div>`;
  box.innerHTML = html;
}

function payoutsPanel() {
  return `
  <details class="rev-fold mt-3" ontoggle="if(this.open)loadPayouts()">
    <summary>💸 График выплат — балансы кабинетов и предстоящие поступления</summary>
    <div class="card bg-card mt-2 p-3" id="payoutsBody"></div>
  </details>`;
}

// ── Сравнение площадок (Тотал): затраты в % от выручки ───────────────────────

const _CMP_CATS = [
  ['Комиссия и эквайринг',   [/комисси/i, /вознаграждени/i, /эквайринг/i, /приём и перевод/i]],
  ['Логистика и доставка',   [/логистик/i, /доставк/i]],
  ['Хранение и FBO',         [/хранени/i, /fbo/i, /приёмк/i]],
  ['Реклама и продвижение',  [/продвижени/i, /реклам/i, /буст/i, /лояльн/i]],
  ['Удержания и прочее',     [/./]],   // всё остальное
];

function mpComparePanel() {
  const cabMps = (_cab && _cab.marketplaces) || ['WB', 'OZON', 'YM'];
  const PLATS = [['WB', 'WB', '#c026d3'], ['OZON', 'Ozon', '#3b82f6'], ['YM', 'ЯМ', '#b45309']]
    .filter(([k]) => cabMps.includes(k));
  const cols = [];
  for (const [k, label, color] of PLATS) {
    const d = _financeData[k];
    if (!d || !(d.months || []).length || d.source === 'weekly') continue;
    const sum = key => (d.rows || []).filter(r => r.key === key)
      .reduce((a, r) => a + Object.values(r.values || {}).reduce((x, y) => x + (y || 0), 0), 0);
    const revenue = sum('retailAmount');
    if (revenue <= 0) continue;
    const payout = sum('bankPayment');
    const cogs = Math.abs(sum('cogs'));
    const gross = sum('gross');
    // затратные строки → категории
    const SKIP = new Set(['retailAmount', 'bankPayment', 'cogs', 'gross', 'gross_pct', 'subsidies']);
    const cats = {};
    (d.rows || []).forEach(r => {
      if (SKIP.has(r.key) || r.style === 'note' || r.style === 'pct') return;
      const total = Object.values(r.values || {}).reduce((a, v) => a + (v || 0), 0);
      if (total >= 0) return;   // берём только затраты
      const cat = _CMP_CATS.find(([, pats]) => pats.some(p => p.test(r.label || '')));
      const name = cat ? cat[0] : 'Удержания и прочее';
      cats[name] = (cats[name] || 0) + Math.abs(total);
    });
    cols.push({ k, label, color, revenue, payout, cogs, gross, cats });
  }
  if (cols.length < 2) return '';

  const pct = (v, rev) => rev > 0 ? v / rev * 100 : 0;
  const fmtPct = v => (Math.round(v * 10) / 10).toLocaleString('ru-RU') + '%';
  const cell = (v, best, worst, invert) => {
    // для затрат меньше = лучше; для маржи (invert) больше = лучше
    const isBest = invert ? v === best : v === best;
    const isWorst = invert ? v === worst : v === worst;
    const clr = isBest ? 'var(--pos)' : isWorst ? 'var(--neg)' : 'var(--ink)';
    return `<td class="text-end" style="padding:5px 14px"><span style="color:${clr};font-weight:${isBest || isWorst ? 700 : 500}">${fmtPct(v)}</span></td>`;
  };

  let html = `<div class="table-responsive"><table class="table table-sm mb-0" style="font-size:0.85rem">
    <thead><tr><th style="min-width:230px">Статья (% от выручки площадки)</th>`;
  cols.forEach(c => { html += `<th class="text-end" style="color:${c.color};min-width:100px">${c.label}</th>`; });
  html += `</tr></thead><tbody>`;

  _CMP_CATS.forEach(([name]) => {
    const vals = cols.map(c => pct(c.cats[name] || 0, c.revenue));
    if (!vals.some(v => v > 0.05)) return;
    const best = Math.min(...vals), worst = Math.max(...vals);
    html += `<tr><td style="color:var(--ink-2);padding:5px 12px">− ${name}</td>` +
      vals.map(v => cell(v, best, worst)).join('') + `</tr>`;
  });

  // итоги
  const totCost = cols.map(c => pct(c.revenue - c.payout, c.revenue));
  html += `<tr style="background:var(--fin-subtotal);font-weight:600"><td style="padding:6px 12px;color:var(--val)">Все затраты площадки</td>` +
    totCost.map(v => cell(v, Math.min(...totCost), Math.max(...totCost))).join('') + `</tr>`;
  const cogsP = cols.map(c => pct(c.cogs, c.revenue));
  html += `<tr><td style="color:var(--ink-2);padding:5px 12px">− Себестоимость</td>` +
    cogsP.map(v => `<td class="text-end" style="padding:5px 14px;color:var(--ink)">${fmtPct(v)}</td>`).join('') + `</tr>`;
  const grossP = cols.map(c => pct(c.gross, c.revenue));
  html += `<tr style="background:var(--fin-total);font-weight:700"><td style="padding:6px 12px;color:var(--val)">Остаётся (валовая маржа)</td>` +
    grossP.map(v => cell(v, Math.max(...grossP), Math.min(...grossP), true)).join('') + `</tr>`;
  // абсолюты для контекста
  html += `<tr><td style="color:var(--muted);padding:5px 12px;font-size:0.78rem">Выручка за период, ₽</td>` +
    cols.map(c => `<td class="text-end" style="padding:5px 14px;color:var(--dim);font-size:0.78rem">${fmtRub(Math.round(c.revenue))}</td>`).join('') + `</tr>`;
  html += `<tr><td style="color:var(--muted);padding:5px 12px;font-size:0.78rem">Валовая прибыль, ₽</td>` +
    cols.map(c => `<td class="text-end" style="padding:5px 14px;color:var(--dim);font-size:0.78rem">${fmtRub(Math.round(c.gross))}</td>`).join('') + `</tr>`;
  html += `</tbody></table></div>
  <div class="text-secondary small mt-2">За загруженный период (последние месяцы). Зелёное — лучшая площадка по статье, красное — худшая. Маржа считается от выручки площадки, поэтому напрямую показывает, где рубль выручки приносит больше.</div>`;

  return `
  <details class="rev-fold mt-3">
    <summary>⚖️ Сравнение площадок — где зарабатываем, а где теряем</summary>
    <div class="card bg-card mt-2 p-3">${html}</div>
  </details>`;
}

function manualCostsPanel(months) {
  const items = ((_manualCosts && _manualCosts.items) || [])
    .slice().sort((a, b) => b.mk.localeCompare(a.mk) || a.label.localeCompare(b.label));
  // месяцы — кликабельные чипы, можно выбрать сразу несколько
  [..._manCostMks].forEach(mk => { if (!months.find(m => m.key === mk)) _manCostMks.delete(mk); });
  const monthChips = [...months].sort((a, b) => a.key.localeCompare(b.key))
    .map(m => `<button class="btn btn-sm btn-outline-info ${_manCostMks.has(m.key) ? 'active' : ''}"
                       onclick="toggleManCostMk('${m.key}', this)">${m.label}</button>`).join('');
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
  <details class="rev-fold mt-3">
    <summary>➕ Ручные статьи затрат <span class="text-secondary small fw-normal">(аренда, зарплаты и т.д. — вычитаются из валовой в «Финансовый итог месяца»)</span></summary>
    <div class="card bg-card mt-2 p-3">
    <div class="d-flex gap-1 flex-wrap align-items-center mb-2">
      <span class="text-secondary small me-1">Месяцы (можно несколько):</span>${monthChips}
    </div>
    <div class="d-flex gap-2 flex-wrap align-items-center">
      <input id="manCostLabel" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:260px" placeholder="Название (напр. Аренда склада)">
      <input id="manCostAmount" type="number" min="0" step="100" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:130px" placeholder="Сумма ₽/мес">
      <button class="btn btn-sm btn-outline-success" onclick="addManualCost()">Добавить</button>
    </div>
    ${list}
    </div>
  </details>`;
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
  // предупреждаем только если себестоимости реально нет в цифрах:
  // флаг cogs_loaded может отсутствовать в сводных/восстановленных данных
  const cogsRow = rows.find(r => r.key === 'cogs');
  const cogsEmpty = !cogsRow || !Object.values(cogsRow.values || {}).some(v => Math.abs(v) > 0);
  if (!d.cogs_loaded && cogsEmpty) {
    html += `<div class="text-warning small mt-2">⚠ Себестоимость не загружена — строка COGS показывает 0. Загрузите справочник через раздел загрузки.</div>`;
  }
  if (mp === 'WB' && d.detail_upto) {
    const tail = d.tail_days
      ? ` Продажи после этой даты (${d.tail_days} дн.) добавлены из оперативных данных — комиссия и логистика по ним уточнятся после формирования недельного отчёта WB.`
      : '';
    html += `<div class="text-secondary small mt-2">Отчёт реализации WB сформирован по ${d.detail_upto}.${tail}</div>`;
  }
  if (d.fetched_at) html += `<div class="text-secondary text-end small mt-1">Обновлено: ${d.fetched_at}</div>`;
  if (mp === 'TOTAL') html += manualCostsPanel(d.months || []) + payoutsPanel() + mpComparePanel();
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
    if (!_unitMonth) {
      // по умолчанию — прошлый (полный) месяц, а не текущий незавершённый
      const now = new Date();
      const prevKey = `${now.getFullYear() - (now.getMonth() === 0 ? 1 : 0)}-${String(now.getMonth() === 0 ? 12 : now.getMonth()).padStart(2, '0')}`;
      _unitMonth = months.find(m => m.key === prevKey) ? prevKey
                 : (months.length ? months[months.length - 1].key : 'ALL');
    }
    if (_unitMode === 'month' || _unitMode === 'perunit') {
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

// ── Режим «На единицу»: цена покупателя (редактируемая) → прибыль на штуку ─────
let _perUnitBase = {};          // sku → фикс. затраты на единицу
let _perUnitPrice = {};         // sku → изменённая цена (ручная правка)

function renderUnitPerUnit() {
  const wrap = document.getElementById('unitTableWrap');
  const months = _unitData.months || [];
  const selMonths = _unitMonth === 'ALL' ? months : months.filter(m => m.key === _unitMonth);
  const skus = _unitData.skus || [];
  _perUnitBase = {};

  const rows = [];
  skus.forEach(r => {
    const c = _unitCellSum(r.months, selMonths);
    const qty = c.qty || 0;
    if (qty <= 0 || !c.revenue) return;
    const per = k => (c[k] || 0) / qty;               // средняя величина на штуку
    const price0 = Math.round(per('revenue'));         // цена до СПП = выручка/шт
    const commPct = c.revenue ? (c.commission || 0) / c.revenue : 0;  // доля комиссии
    const base = {
      sku: r.sku, nmId: r.nmId, name: r.name, brand: r.brand,
      price0,
      commPct,                                         // комиссия масштабируется с ценой
      acqPct: c.revenue ? (c.acquiring || 0) / c.revenue : 0,
      logist: per('delivery'), storage: per('storage'),
      advert: Math.max(0, per('advert') - per('advert_bonus')),   // за вычетом компенсации баллами
      other: per('deductions') + per('penalty') + per('acceptance'),
      cogs: r.unitCost || per('cogs'),
    };
    _perUnitBase[r.sku] = base;
    rows.push(base);
  });

  // группировка как везде
  const groupMap = {};
  rows.forEach(b => {
    const g = articleGroup({ supplierArticle: b.sku, brand: b.brand });
    (groupMap[g] = groupMap[g] || []).push(b);
  });
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  const COLS = ['Артикул', 'Название', 'Цена, ₽ (правьте)', 'Комиссия', 'Логистика',
                'Хранение', 'Продвиж.', 'Эквайринг', 'Проч.', 'Себес.',
                _unitTax > 0 ? `Налог ${_unitTax}%` : 'Налог', 'Прибыль/ед', 'Маржа %'];
  let html = `<div class="text-secondary small mb-2">Цена — средняя за период (до СПП). Правьте её, чтобы проверить: продаём в плюс или минус. Комиссия и эквайринг пересчитываются от цены (%), остальные затраты фиксированы на штуку.</div>`;
  html += `<div class="table-responsive"><table class="table table-sm align-middle mb-0 text-nowrap" style="font-size:0.83rem"><thead><tr>`
    + COLS.map((l, i) => `<th class="${i > 1 ? 'text-end' : ''}" style="${i > 2 ? 'border-left:1px solid var(--sep);' : ''}">${l}</th>`).join('')
    + `</tr></thead><tbody>`;

  orderedGroups.forEach(([gname, list]) => {
    list.sort((a, b) => b.price0 - a.price0);
    html += `<tr style="background:var(--surface-3);font-weight:600"><td colspan="${COLS.length}" style="padding:6px 10px;color:var(--val)">${gname} <span class="text-secondary small">(${list.length} арт.)</span></td></tr>`;
    list.forEach(b => {
      const price = _perUnitPrice[b.sku] != null ? _perUnitPrice[b.sku] : b.price0;
      html += `<tr style="background:var(--t-row)" data-sku="${esc(b.sku)}">
        <td style="padding:5px 10px"><code style="color:var(--val-soft)">${esc(b.sku)}</code></td>
        <td style="padding:5px 10px;max-width:300px;overflow:hidden;text-overflow:ellipsis"><span class="text-secondary small">${esc(b.name || '')}</span></td>
        <td class="text-end" style="padding:3px 6px;border-left:1px solid var(--sep)">
          <input type="number" value="${price}" oninput="recalcPerUnit('${esc(b.sku)}', this.value)"
                 style="width:82px;text-align:right;background:rgba(201,168,76,.08);border:1px solid var(--border);border-radius:6px;color:var(--ink);padding:2px 6px"></td>
        <td class="text-end pu-comm" style="padding:5px 10px;border-left:1px solid var(--sep)"></td>
        <td class="text-end" style="padding:5px 10px"><span style="color:var(--neg)">−</span>${fmtRub(Math.round(b.logist))}</td>
        <td class="text-end" style="padding:5px 10px"><span style="color:var(--neg)">−</span>${fmtRub(Math.round(b.storage))}</td>
        <td class="text-end" style="padding:5px 10px"><span style="color:var(--neg)">−</span>${fmtRub(Math.round(b.advert))}</td>
        <td class="text-end pu-acq" style="padding:5px 10px"></td>
        <td class="text-end" style="padding:5px 10px"><span style="color:var(--neg)">−</span>${fmtRub(Math.round(b.other))}</td>
        <td class="text-end" style="padding:5px 10px;background:rgba(201,168,76,.05)">${b.cogs ? '<span style="color:var(--neg)">−</span>' + fmtRub(Math.round(b.cogs)) : '—'}</td>
        <td class="text-end pu-tax" style="padding:5px 10px"></td>
        <td class="text-end pu-profit" style="padding:5px 10px;border-left:1px solid var(--sep)"></td>
        <td class="text-end pu-margin" style="padding:5px 10px"></td>
      </tr>`;
    });
  });
  html += `</tbody></table></div>`;
  wrap.innerHTML = html;
  // первичный расчёт всех строк
  Object.keys(_perUnitBase).forEach(sku => recalcPerUnit(sku,
    _perUnitPrice[sku] != null ? _perUnitPrice[sku] : _perUnitBase[sku].price0, true));
}

function recalcPerUnit(sku, priceVal, silent) {
  const b = _perUnitBase[sku];
  if (!b) return;
  const price = Math.max(0, parseFloat(priceVal) || 0);
  if (!silent) _perUnitPrice[sku] = price;
  const comm = price * b.commPct;
  const acq = price * b.acqPct;
  const grossBeforeTax = price - comm - acq - b.logist - b.storage - b.advert - b.other - b.cogs;
  const tax = _unitTax > 0 ? Math.max(grossBeforeTax, 0) * _unitTax / 100 : 0;
  const profit = grossBeforeTax - tax;
  const margin = price > 0 ? Math.round(profit / price * 100) : 0;
  const row = document.querySelector(`tr[data-sku="${CSS.escape(sku)}"]`);
  if (!row) return;
  row.querySelector('.pu-comm').innerHTML = `<span style="color:var(--neg)">−</span>${fmtRub(Math.round(comm))}`;
  row.querySelector('.pu-acq').innerHTML = acq ? `<span style="color:var(--neg)">−</span>${fmtRub(Math.round(acq))}` : '<span class="text-muted small">—</span>';
  row.querySelector('.pu-tax').innerHTML = tax ? `<span style="color:var(--neg)">−</span>${fmtRub(Math.round(tax))}` : '<span class="text-muted small">—</span>';
  const pclr = profit >= 0 ? 'var(--pos)' : 'var(--neg)';
  row.querySelector('.pu-profit').innerHTML = `<span style="color:${pclr};font-weight:700">${profit < 0 ? '−' : ''}${fmtRub(Math.abs(Math.round(profit)))}</span>`;
  const mclr = margin >= 20 ? 'var(--pos)' : margin >= 0 ? 'var(--warn-c)' : 'var(--neg)';
  row.querySelector('.pu-margin').innerHTML = `<span style="color:${mclr};font-weight:700">${margin}%</span>`;
  row.style.background = profit < 0 ? 'rgba(248,113,113,.07)' : 'var(--t-row)';
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
    // компенсация баллами — плюс (WB вернул на продвижение), зелёным
    if (key === 'advert_bonus') {
      if (!v) return `<td class="text-end" style="padding:5px 10px"><span class="text-muted small">—</span></td>`;
      return `<td class="text-end" style="padding:5px 10px"><span style="color:var(--pos)">+${fmtRub(Math.abs(Math.round(v)))}</span></td>`;
    }
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

// ── Документы (сертификаты/декларации) ───────────────────────────────────────

let _docsData = null;

async function loadDocs(refresh) {
  const wrap = document.getElementById('docsWrap');
  if (!wrap) return;
  if (_docsData && !refresh) { renderDocs(); return; }
  wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Загружаем документы…</div>';
  try {
    _docsData = await fetchJSON('/api/docs/summary' + (refresh === true ? '?refresh=true' : ''), 120000);
    renderDocs();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${e.message}</div>`;
  }
}

const _DOC_EXP = {
  expired: ['🔴 истёк',     'var(--neg)'],
  soon:    ['🟡 истекает',  'var(--warn-c)'],
  ok:      ['🟢 действует', 'var(--pos)'],
  unknown: ['— без срока',  'var(--muted)'],
};

function renderDocs() {
  const wrap = document.getElementById('docsWrap');
  if (!wrap || !_docsData) return;
  const d = _docsData;
  let html = '';

  // ── Ozon из API ──
  const oz = d.ozon || {};
  html += `<div class="card bg-card p-3 mb-3">
    <div class="fw-semibold mb-2" style="color:var(--ink)">🔵 Ozon — сертификаты из API</div>`;
  if (oz.error) html += `<div class="alert alert-warning py-2 small">⚠ ${esc(oz.error)}</div>`;
  if ((oz.certs || []).length) {
    html += `<div class="table-responsive"><table class="table table-sm mb-2" style="font-size:.82rem"><thead><tr>
      <th>Документ</th><th>Тип</th><th>Статус Ozon</th><th>Действует до</th><th>Срок</th><th>Товары</th>
    </tr></thead><tbody>`;
    oz.certs.forEach(c => {
      const [lbl, clr] = _DOC_EXP[c.expiry] || _DOC_EXP.unknown;
      html += `<tr>
        <td><code style="color:var(--val-soft)">${esc(c.number)}</code> <span class="text-secondary small">${esc(c.name !== c.number ? c.name : '')}</span></td>
        <td class="small">${esc(String(c.type || ''))}</td>
        <td class="small">${esc(String(c.status || ''))}</td>
        <td>${esc(c.valid_to || '—')}</td>
        <td><span style="color:${clr}">${lbl}</span></td>
        <td class="small" style="max-width:340px">${(c.products || []).map(p => `<code style="color:var(--dim)">${esc(p)}</code>`).join(' ') || '<span class="text-secondary">нет привязок</span>'}</td>
      </tr>`;
    });
    html += `</tbody></table></div>`;
    if ((oz.uncovered || []).length) {
      html += `<div class="small" style="color:var(--warn-c)">⚠ Без действующего документа (${oz.uncovered.length} из ${oz.total_products}): ${oz.uncovered.map(a => `<code>${esc(a)}</code>`).join(' ')}</div>`;
    } else {
      html += `<div class="small" style="color:var(--pos)">✓ Все ${oz.total_products} товаров Ozon покрыты действующими документами</div>`;
    }
  } else if (!oz.error) {
    html += `<div class="text-secondary small">Ozon не вернул сертификаты — либо они не загружены в кабинет, либо у токена нет прав на раздел «Сертификаты».</div>`;
  }
  html += `</div>`;

  // ── Ручной реестр ──
  html += `<div class="card bg-card p-3">
    <div class="fw-semibold mb-2" style="color:var(--ink)">📄 Реестр документов (WB и общие) <span class="text-secondary small fw-normal">— заносится вручную, система следит за сроками и покрытием</span></div>
    <div class="d-flex gap-2 flex-wrap align-items-center mb-2">
      <select id="docType" class="form-select form-select-sm w-auto bg-dark text-white border-secondary">
        <option>Декларация</option><option>Сертификат</option><option>Отказное письмо</option><option>СГР</option><option>Другое</option>
      </select>
      <input id="docNumber" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:220px" placeholder="Номер (ЕАЭС N RU Д-RU…)">
      <input id="docTitle" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:200px" placeholder="Название (необязательно)">
      <input id="docValidTo" type="date" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:150px">
      <input id="docSkus" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:260px" placeholder="Артикулы через запятую">
      <button class="btn btn-sm btn-outline-success" onclick="addDoc()">Добавить</button>
    </div>
    <div id="docError" class="text-danger small mb-2"></div>`;

  if ((d.manual || []).length) {
    html += `<div class="table-responsive"><table class="table table-sm mb-2" style="font-size:.82rem"><thead><tr>
      <th>Тип</th><th>Номер / название</th><th>Действует до</th><th>Срок</th><th>Артикулы</th><th></th>
    </tr></thead><tbody>`;
    d.manual.forEach(m => {
      const [lbl, clr] = _DOC_EXP[m.expiry] || _DOC_EXP.unknown;
      html += `<tr>
        <td class="small">${esc(m.doc_type)}</td>
        <td><code style="color:var(--val-soft)">${esc(m.number || '')}</code> <span class="text-secondary small">${esc(m.title || '')}</span></td>
        <td>${esc(m.valid_to || '—')}</td>
        <td><span style="color:${clr}">${lbl}</span></td>
        <td class="small" style="max-width:380px">${(m.skus || []).map(s => `<code style="color:var(--dim)">${esc(s)}</code>`).join(' ') || '—'}</td>
        <td><button class="btn btn-sm btn-outline-danger py-0 px-1" style="font-size:.7rem" onclick="delDoc(${m.id})">✕</button></td>
      </tr>`;
    });
    html += `</tbody></table></div>`;
  } else {
    html += `<div class="text-secondary small mb-2">Документов пока нет — добавь первую декларацию выше.</div>`;
  }
  if ((d.manual_uncovered || []).length && (d.manual || []).length) {
    html += `<div class="small" style="color:var(--warn-c)">⚠ Артикулы без покрытия в реестре (${d.manual_uncovered.length} из ${d.catalog_total}): ${d.manual_uncovered.slice(0, 60).map(a => `<code>${esc(a)}</code>`).join(' ')}${d.manual_uncovered.length > 60 ? ' …' : ''}</div>`;
  }
  html += `</div>`;
  wrap.innerHTML = html;
}

async function addDoc() {
  const payload = {
    doc_type: document.getElementById('docType').value,
    number: document.getElementById('docNumber').value.trim(),
    title: document.getElementById('docTitle').value.trim(),
    valid_to: document.getElementById('docValidTo').value,
    skus: document.getElementById('docSkus').value.trim(),
  };
  const r = await fetch('/api/docs/manual', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  if (!r.ok) {
    document.getElementById('docError').textContent = (await r.json().catch(() => ({}))).detail || 'Ошибка';
    return;
  }
  _docsData = null;
  loadDocs();
}

async function delDoc(id) {
  if (!confirm('Удалить документ из реестра?')) return;
  await fetch(`/api/docs/manual/${id}`, { method: 'DELETE' });
  _docsData = null;
  loadDocs();
}

// ── Инструменты WB ────────────────────────────────────────────────────────────

let _toolActive = 'prod';   // prod | clusters

const _TOOL_HINTS = {
  prod: 'Анализ отзывов WB по каждому артикулу',
  clusters: 'Сток и продажи по федеральным округам складов WB, локализация и дозаказ',
  adv: 'Кампании, куда уходят деньги, ДРР, ключевые фразы и советы по оптимизации',
  niche: 'Спрос по вашим товарам из Джем: поисковые запросы, частотность, ваша позиция, заказы и точки роста',
  nichecalc: 'Выходить ли с товаром: конкуренты из выдачи WB, цены, спрос и вердикт (сбор через агент)',
  visuals: 'Заглавные фото топ-20 карточек конкурентов + разбор визуала через Claude',
  margin: 'Затраты на единицу из фактической юнитки: вводите цену/себестоимость — видите прибыль, маржу и точку безубыточности',
};

function setTool(t) {
  if (_toolActive === t) return;
  _toolActive = t;
  ['Prod', 'Clusters', 'Adv', 'Niche', 'Nichecalc', 'Visuals', 'Margin'].forEach(k => {
    document.getElementById('tool' + k)?.classList.toggle('active', k.toLowerCase() === t);
  });
  const hint = document.getElementById('toolHint');
  if (hint) hint.textContent = _TOOL_HINTS[t] || '';
  loadTools();
}

function reloadTool() {
  ({ prod: () => loadProductolog(true), clusters: () => loadClusters(true),
     adv: () => loadAdv(true), niche: () => loadDemand(true),
     nichecalc: () => renderNicheForm(), visuals: () => renderVisualsForm(),
     margin: () => loadMargin(true) })[_toolActive]();
}
function loadTools() {
  ({ prod: loadProductolog, clusters: loadClusters, adv: loadAdv,
     niche: loadDemand, nichecalc: renderNicheForm,
     visuals: renderVisualsForm, margin: loadMargin })[_toolActive]();
}

// ── Воронка Ozon (Premium) ────────────────────────────────────────────────────

// ── Инструменты Ozon: переключатель ──────────────────────────────────────────
let _ozTool = 'funnel';
const _OZ_HINTS = {
  funnel: 'Premium-аналитика Ozon: показы → карточка → корзина → заказ по каждому SKU, где теряем продажи',
  clusters: 'Остатки по кластерам Ozon: покрытие, скорость, что везти / остальное (Ozon сам считает продажи/день и покрытие)',
  ads: 'Реклама Ozon (Performance): расход, ДРР, ROAS, заказы и куда утекают деньги по каждой кампании',
  phrases: 'По каким поисковым запросам показываются и кликают ваши товары в рекламе Ozon',
};
function setOzTool(t) {
  if (_ozTool === t) return;
  _ozTool = t;
  [['ozFunnel', 'funnel'], ['ozClusters', 'clusters'], ['ozAds', 'ads'], ['ozPhrases', 'phrases']].forEach(([id, k]) =>
    document.getElementById(id)?.classList.toggle('active', k === t));
  const hint = document.getElementById('ozToolHint');
  if (hint) hint.textContent = _OZ_HINTS[t] || '';
  loadOzTool();
}
function loadOzTool() {
  ({ funnel: loadFunnel, clusters: loadOzClusters, ads: loadOzAds, phrases: loadOzPhrases })[_ozTool]();
}
function reloadOzTool() {
  ({ funnel: () => loadFunnel(true), clusters: () => loadOzClusters(true),
     ads: () => loadOzAds(true), phrases: () => loadOzPhrases(true) })[_ozTool]();
}

let _ozClustersData = null;
async function loadOzClusters(refresh) {
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || _ozTool !== 'clusters') return;
  if (_ozClustersData && !refresh) { renderOzClusters(); return; }
  wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Загружаем остатки Ozon по кластерам…</div>';
  try {
    _ozClustersData = await fetchJSON('/api/tools/ozon/clusters' + (refresh ? '?refresh=true' : ''), 120000);
    renderOzClusters();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${esc(e.message)}</div>`;
  }
}

function renderOzClusters() {
  if (_ozTool !== 'clusters') return;
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || !_ozClustersData) return;
  const d = _ozClustersData;
  if (d.message) { wrap.innerHTML = `<div class="alert alert-info">${esc(d.message)}</div>`; return; }
  let html = `<div class="d-flex justify-content-end mb-2">
    <a href="/api/tools/ozon/clusters/export" class="btn btn-sm btn-outline-success" download>⬇ Экспорт в Excel</a></div>
    <div class="d-flex gap-3 flex-wrap mb-3">
    <div class="metric-card" style="min-width:160px"><div class="mc-head">⚠ Слабых кластеров</div>
      <div class="mc-val" style="color:${d.weak ? 'var(--warn-c)' : 'var(--pos)'}">${d.weak}</div>
      <div class="mc-sub">покрытие меньше 15 дней</div></div>
    <div class="metric-card" style="min-width:160px"><div class="mc-head">📦 Излишки (всего)</div>
      <div class="mc-val">${fmt(d.excess_total || 0)}</div><div class="mc-sub">шт сверх нужного (по оценке Ozon)</div></div>
  </div><div class="row g-3">`;
  (d.items || []).forEach(it => {
    const [label, clr] = _CL_STATUS[it.status] || _CL_STATUS.ok;
    html += `<div class="col-12 col-md-6 col-xl-4"><div class="metric-card h-100" style="border-left:3px solid ${clr}">
      <div class="d-flex justify-content-between align-items-start">
        <b style="color:var(--ink);font-size:15px">${esc(it.cluster)}</b>
        <span class="small" style="color:${clr}">${label}</span></div>
      <div class="small mb-2" style="color:${it.need ? 'var(--neg)' : 'var(--muted)'}">К заказу: ${it.need ? fmt(it.need) + ' шт' : 'не требуется'}</div>
      <div class="d-flex gap-4">
        <div><div style="font-size:20px;font-weight:700;color:var(--val)">${it.spd.toLocaleString('ru-RU')}</div><div class="text-secondary" style="font-size:11px">продаж/день</div></div>
        <div><div style="font-size:20px;font-weight:700;color:var(--val)">${fmt(it.stock)}</div><div class="text-secondary" style="font-size:11px">остаток, шт</div></div>
      </div>
      <div class="small mt-2" style="color:var(--ink-2)">Покрытие: <b style="color:${clr}">${it.coverage != null ? it.coverage + ' дн' : '—'}</b>${it.excess ? ` · <span class="text-secondary">излишки ${fmt(it.excess)} шт</span>` : ''}</div>
      ${(it.skus || []).length ? `
      <details class="mt-2">
        <summary class="small" style="cursor:pointer;color:var(--gold)">🚚 Что везти: ${it.skus.length} арт.</summary>
        <table class="table table-sm mb-0 mt-1" style="font-size:.74rem">
          <thead><tr><th>Артикул</th><th class="text-end">Прод/д</th><th class="text-end">Здесь</th><th class="text-end">Покр.</th><th class="text-end">Везти</th></tr></thead>
          <tbody>${it.skus.map(s => `<tr>
            <td><code style="color:var(--val-soft)">${esc(s.sku)}</code> <span class="text-secondary">${esc((s.name || '').slice(0, 20))}</span></td>
            <td class="text-end">${s.ads}</td><td class="text-end">${fmt(s.stock)}</td>
            <td class="text-end" style="color:${s.idc < 7 ? 'var(--neg)' : s.idc < 15 ? 'var(--warn-c)' : 'var(--ink)'}">${s.idc}</td>
            <td class="text-end"><b style="color:var(--pos)">${fmt(s.need)}</b></td></tr>`).join('')}</tbody>
        </table></details>` : ''}
      ${(it.other_skus || []).length ? `
      <details class="mt-1">
        <summary class="small" style="cursor:pointer;color:var(--dim)">📦 Остальное: ${it.other_skus.length} арт.</summary>
        <table class="table table-sm mb-0 mt-1" style="font-size:.74rem">
          <thead><tr><th>Артикул</th><th class="text-end">Прод/д</th><th class="text-end">Здесь</th><th class="text-end">Покр.</th><th class="text-end">Оборач.</th></tr></thead>
          <tbody>${it.other_skus.map(s => `<tr>
            <td><code style="color:var(--val-soft)">${esc(s.sku)}</code> <span class="text-secondary">${esc((s.name || '').slice(0, 20))}</span></td>
            <td class="text-end">${s.ads}</td><td class="text-end">${fmt(s.stock)}</td>
            <td class="text-end" style="color:var(--ink-2)">${s.idc}</td>
            <td class="text-end text-secondary">${esc(s.grade)}</td></tr>`).join('')}</tbody>
        </table></details>` : ''}
    </div></div>`;
  });
  html += `</div><div class="text-secondary small mt-3">Данные — из аналитики остатков Ozon (/v1/analytics/stocks): продажи/день (ads), покрытие (idc), оборачиваемость и излишки Ozon считает сам, по кластерам. Дозаказ — до ${d.target_days} дней покрытия. Обновлено: ${d.fetched_at}</div>`;
  wrap.innerHTML = html;
}

let _funnelData = null;

async function loadFunnel(refresh) {
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || _ozTool !== 'funnel') return;
  if (_funnelData && !refresh) { renderFunnel(); return; }
  wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Загружаем аналитику Ozon…</div>';
  try {
    _funnelData = await fetchJSON('/api/tools/funnel' + (refresh ? '?refresh=true' : ''), 120000);
    if (_ozTool !== 'funnel') return;
    renderFunnel();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${e.message}</div>`;
  }
}

const _FUNNEL_BN = {
  visibility: ['🔴 Нет показов',    'var(--neg)'],
  ctr:        ['🟠 Не кликают',     'var(--warn-c)'],
  cart:       ['🟡 Не кладут в корзину', 'var(--warn-c)'],
  checkout:   ['🟣 Не выкупают',    '#c084fc'],
  ok:         ['🟢 Здоровая',       'var(--pos)'],
};

function renderFunnel() {
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || !_funnelData) return;
  const d = _funnelData;
  if (d.error) { wrap.innerHTML = `<div class="alert alert-warning">⚠ ${esc(d.error)}</div>`; return; }
  if (!(d.items || []).length) { wrap.innerHTML = '<div class="alert alert-info">Ozon не вернул данных аналитики</div>'; return; }

  // группировка как везде
  const groupMap = {};
  d.items.forEach(it => {
    const g = articleGroup({ supplierArticle: it.sku, brand: it.group });
    (groupMap[g] = groupMap[g] || []).push(it);
  });
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  let html = `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:75vh">
    <table class="table table-sm align-middle mb-0 text-nowrap"><thead><tr>
      <th style="min-width:200px;position:sticky;left:0;background:var(--t-sticky);z-index:2">Товар</th>
      <th class="text-end" title="Средняя позиция в категории (ниже — выше в выдаче)">Ср. поз.</th>
      <th class="text-end" title="Показы в поиске и каталоге">Показы</th>
      <th class="text-end" title="Открытия карточки">Карточка</th>
      <th class="text-end" title="CTR: карточка/показы">CTR</th>
      <th class="text-end" title="Добавления в корзину">Корзина</th>
      <th class="text-end" title="Корзина/карточка">В корз.%</th>
      <th class="text-end">Заказы</th>
      <th class="text-end" title="Заказы/корзина">Выкуп%</th>
      <th class="text-end">Выручка</th>
      <th style="min-width:260px">Узкое место</th>
    </tr></thead><tbody>`;
  orderedGroups.forEach(([gname, list]) => {
    html += `<tr class="table-secondary"><td colspan="11" style="padding:6px 12px"><strong>${gname}</strong> <span class="text-secondary small">(${list.length} арт.)</span></td></tr>`;
    list.forEach(it => {
      const [lbl, clr] = _FUNNEL_BN[it.bottleneck] || _FUNNEL_BN.ok;
      // мини-воронка: 4 сегмента с шириной по log
      const seg = (v, max) => Math.max(4, Math.round(Math.min(1, (v || 0) / (max || 1)) * 60));
      const mx = it.search || 1;
      html += `<tr style="background:var(--t-row)">
        <td style="position:sticky;left:0;background:var(--t-sticky);padding:6px 12px">
          <code style="color:var(--val-soft)">${esc(it.sku)}</code>
          <div class="small text-secondary" style="max-width:190px;overflow:hidden;text-overflow:ellipsis">${esc(it.name)}</div></td>
        <td class="text-end" style="color:${it.position ? (it.position <= 50 ? 'var(--pos)' : it.position <= 120 ? 'var(--warn-c)' : 'var(--neg)') : 'var(--muted)'}">${it.position || '—'}</td>
        <td class="text-end">${fmt(it.search)}</td>
        <td class="text-end">${fmt(it.pdp)}</td>
        <td class="text-end" style="color:${it.ctr != null && it.ctr < 2 ? 'var(--neg)' : 'var(--ink)'}">${it.ctr != null ? it.ctr + '%' : '—'}</td>
        <td class="text-end">${fmt(it.tocart)}</td>
        <td class="text-end" style="color:${it.cart_pct != null && it.cart_pct < 5 ? 'var(--neg)' : it.cart_pct > 100 ? 'var(--muted)' : 'var(--ink)'}">${it.cart_pct != null ? it.cart_pct + '%' : '—'}</td>
        <td class="text-end" style="color:var(--val);font-weight:600">${fmt(it.orders)}</td>
        <td class="text-end" style="color:${it.buy_pct > 100 ? 'var(--muted)' : 'var(--ink)'}">${it.buy_pct != null ? it.buy_pct + '%' : '—'}</td>
        <td class="text-end" style="color:var(--pos)">${fmtRub(it.revenue)}</td>
        <td class="small"><span style="color:${clr};font-weight:600">${lbl}</span> <span class="text-secondary">${esc(it.bottleneck_why)}</span></td>
      </tr>`;
    });
  });
  html += `</tbody></table></div></div></div>
  <div class="text-secondary small mt-2">Premium-аналитика Ozon за период <b>${d.period || d.days + ' дней'}</b> (полные дни, сегодня/вчера исключены — Ozon отдаёт с задержкой). «Показы» — только из поиска/каталога; карточку могут открыть и напрямую (реклама, ссылка, избранное), а заказ оформить без корзины («Купить сразу») — поэтому проценты в корзину/выкуп иногда выше 100%, это не ошибка, а не-строгая воронка. Товары с проблемной воронкой — сверху. Обновлено: ${d.fetched_at}</div>`;
  wrap.innerHTML = html;
}

// ── Реклама Ozon (Performance) ────────────────────────────────────────────────
let _ozAdsData = null;
async function loadOzAds(refresh) {
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || _ozTool !== 'ads') return;
  if (_ozAdsData && !refresh) { renderOzAds(); return; }
  if (!_ozAdsData) wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Загружаем рекламу Ozon…</div>';
  try {
    _ozAdsData = await fetchJSON('/api/tools/ozads' + (refresh ? '?refresh=true' : ''), 120000);
    renderOzAds();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-2">Ошибка: ${esc(e.message)}</div>`;
  }
}

function _drrColor(drr) {
  if (drr == null) return 'var(--muted)';
  return drr <= 10 ? 'var(--pos)' : drr <= 20 ? 'var(--warn-c)' : 'var(--neg)';
}

function renderOzAds() {
  if (_ozTool !== 'ads') return;
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || !_ozAdsData) return;
  const d = _ozAdsData;
  if (d.error) { wrap.innerHTML = `<div class="alert alert-warning">⚠ ${esc(d.error)}</div>`; return; }
  const T = d.total || {};
  const items = d.items || [];
  if (!items.length) { wrap.innerHTML = '<div class="alert alert-info">Нет активных рекламных кампаний за период</div>'; return; }

  // карточки-итоги
  let html = `<div class="d-flex justify-content-end mb-2">
    <a href="/api/tools/ozads/export" class="btn btn-sm btn-outline-success" download>⬇ Экспорт в Excel</a></div>
    <div class="d-flex gap-3 flex-wrap mb-3">
    <div class="metric-card"><div class="mc-head">Расход</div><div class="mc-val">${fmtRub(Math.round(T.spent))}</div></div>
    <div class="metric-card"><div class="mc-head">Выручка с рекламы</div><div class="mc-val" style="color:var(--pos)">${fmtRub(Math.round(T.orders_money))}</div><div class="mc-sub">${fmt(T.orders)} заказов</div></div>
    <div class="metric-card"><div class="mc-head">ДРР</div><div class="mc-val" style="color:${_drrColor(T.drr)}">${T.drr != null ? T.drr + '%' : '—'}</div><div class="mc-sub">доля рекламы в выручке</div></div>
    <div class="metric-card"><div class="mc-head">ROAS</div><div class="mc-val">${T.roas != null ? '×' + T.roas : '—'}</div><div class="mc-sub">выручка / расход</div></div>
    <div class="metric-card"><div class="mc-head">Цена заказа</div><div class="mc-val">${fmtRub(T.cpo)}</div><div class="mc-sub">CTR ${T.ctr}%</div></div>
  </div>`;

  if (d.advice) {
    html += `<details class="rev-fold mb-3" open><summary>🧠 Куда утекают деньги (Claude)</summary>
      <div class="card bg-card mt-2 p-3 small" style="white-space:pre-wrap;line-height:1.6;color:var(--ink)">${esc(d.advice)}</div></details>`;
  }

  html += `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:70vh">
    <table class="table table-sm align-middle mb-0 text-nowrap"><thead><tr>
      <th style="min-width:220px;position:sticky;left:0;background:var(--t-sticky);z-index:2">Кампания</th>
      <th class="text-end">Расход</th><th class="text-end">Заказы</th><th class="text-end">Выручка</th>
      <th class="text-end" title="Доля рекламных расходов">ДРР</th>
      <th class="text-end" title="Окупаемость: выручка/расход">ROAS</th>
      <th class="text-end" title="Цена заказа">Цена зак.</th>
      <th class="text-end">Показы</th><th class="text-end">Клики</th>
      <th class="text-end" title="Клики/показы">CTR</th>
      <th class="text-end" title="Заказы/клики">CR</th>
    </tr></thead><tbody>`;
  // ИТОГО
  html += `<tr style="background:var(--fin-total);font-weight:700">
    <td style="position:sticky;left:0;background:var(--fin-total);padding:6px 12px;color:var(--val)">ИТОГО (${items.length})</td>
    <td class="text-end">${fmtRub(Math.round(T.spent))}</td><td class="text-end">${fmt(T.orders)}</td>
    <td class="text-end" style="color:var(--pos)">${fmtRub(Math.round(T.orders_money))}</td>
    <td class="text-end" style="color:${_drrColor(T.drr)}">${T.drr != null ? T.drr + '%' : '—'}</td>
    <td class="text-end">${T.roas != null ? '×' + T.roas : '—'}</td>
    <td class="text-end">${fmtRub(T.cpo)}</td>
    <td class="text-end">${fmt(T.views)}</td><td class="text-end">${fmt(T.clicks)}</td>
    <td class="text-end">${T.ctr}%</td><td class="text-end">—</td></tr>`;
  items.forEach(i => {
    const noOrders = !i.orders && i.spent > 0;
    html += `<tr style="background:${noOrders ? 'rgba(248,113,113,.08)' : 'var(--t-row)'}">
      <td style="position:sticky;left:0;background:var(--t-sticky);padding:6px 12px">
        <span style="color:var(--ink)">${esc(i.title || i.id)}</span>
        <div class="text-secondary small">${esc(i.type)}${i.state ? ' · ' + esc(i.state.toLowerCase()) : ''}</div></td>
      <td class="text-end" style="color:var(--ink)">${fmtRub(Math.round(i.spent))}</td>
      <td class="text-end" style="color:var(--val);font-weight:600">${fmt(i.orders)}</td>
      <td class="text-end" style="color:var(--pos)">${fmtRub(Math.round(i.orders_money))}</td>
      <td class="text-end" style="color:${_drrColor(i.drr)};font-weight:600">${i.drr != null ? i.drr + '%' : (noOrders ? '∞' : '—')}</td>
      <td class="text-end">${i.roas != null ? '×' + i.roas : '—'}</td>
      <td class="text-end">${i.cpo ? fmtRub(i.cpo) : '—'}</td>
      <td class="text-end">${fmt(i.views)}</td><td class="text-end">${fmt(i.clicks)}</td>
      <td class="text-end" style="color:${i.ctr < 1 ? 'var(--neg)' : 'var(--ink)'}">${i.ctr}%</td>
      <td class="text-end">${i.cr}%</td></tr>`;
  });
  html += `</tbody></table></div></div></div>
  <div class="text-secondary small mt-2">Реклама Ozon (Performance API) за период <b>${d.period || d.days + ' дней'}</b> (полные дни). ДРР = расход / выручка с рекламы; красным — кампании без заказов (деньги в никуда) и ДРР >20%. Обновлено: ${d.fetched_at}</div>`;
  wrap.innerHTML = html;
}

// ── Запросы Ozon (Performance): по чему находят ───────────────────────────────
let _ozPhrData = null;
async function loadOzPhrases(refresh) {
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || _ozTool !== 'phrases') return;
  if (_ozPhrData && !refresh) { renderOzPhrases(); return; }
  if (!_ozPhrData) wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Собираем поисковые запросы Ozon (отчёт генерируется ~минуту)…</div>';
  try {
    _ozPhrData = await fetchJSON('/api/tools/ozphrases' + (refresh ? '?refresh=true' : ''), 180000);
    renderOzPhrases();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-2">Ошибка: ${esc(e.message)}</div>`;
  }
}

function _phrCtrClr(ctr) { return ctr >= 5 ? 'var(--pos)' : ctr < 1 ? 'var(--neg)' : 'var(--ink)'; }
function toggleOzPhrRow(sku) {
  const rows = document.querySelectorAll(`tr[data-ozphr="${CSS.escape(sku)}"]`);
  if (!rows.length) return;
  const open = rows[0].style.display !== 'none';
  rows.forEach(r => { r.style.display = open ? 'none' : ''; });
  const car = document.getElementById('ozphrcar-' + sku);
  if (car) car.textContent = open ? '▸' : '▾';
}

function renderOzPhrases() {
  if (_ozTool !== 'phrases') return;
  const wrap = document.getElementById('toolsOzWrap');
  if (!wrap || !_ozPhrData) return;
  const d = _ozPhrData;
  if (d.error) { wrap.innerHTML = `<div class="alert alert-warning">⚠ ${esc(d.error)}</div>`; return; }
  const items = d.items || [];
  if (!items.length) { wrap.innerHTML = '<div class="alert alert-info">Нет данных по поисковым запросам (нужны активные SKU-кампании)</div>'; return; }

  // группировка товаров по категориям (Спреи, Aloe, Фисты...)
  const groupMap = {};
  items.filter(it => it && it.sku).forEach(it => {
    const g = articleGroup({ supplierArticle: it.art || it.sku, brand: it.group });
    (groupMap[g] = groupMap[g] || []).push(it);
  });
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  let html = `<div class="d-flex gap-3 flex-wrap mb-3 align-items-center">
    <div class="metric-card"><div class="mc-head">Товаров</div><div class="mc-val">${fmt(items.length)}</div></div>
    <div class="metric-card"><div class="mc-head">Показы</div><div class="mc-val">${fmt(d.total_views)}</div></div>
    <div class="metric-card"><div class="mc-head">Клики</div><div class="mc-val">${fmt(d.total_clicks)}</div></div>
    <div class="metric-card"><div class="mc-head">CTR</div><div class="mc-val">${d.total_views ? (d.total_clicks / d.total_views * 100).toFixed(1) : 0}%</div></div>
    <a href="/api/tools/ozphrases/export" class="btn btn-sm btn-outline-success ms-auto" download>⬇ Экспорт в Excel</a>
  </div>`;
  html += `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:74vh">
    <table class="table table-sm align-middle mb-0"><thead><tr>
      <th style="min-width:280px;position:sticky;left:0;background:var(--t-sticky);z-index:2">Товар / запрос</th>
      <th class="text-end">Показы</th><th class="text-end">Клики</th>
      <th class="text-end" title="Клики/показы">CTR</th>
    </tr></thead><tbody>`;
  orderedGroups.forEach(([gname, list]) => {
    html += `<tr class="table-secondary"><td colspan="4" style="padding:6px 12px"><strong>${gname}</strong> <span class="text-secondary small">(${list.length} тов.)</span></td></tr>`;
    list.forEach(it => {
      // строка товара — кликабельная, раскрывает фразы
      html += `<tr style="background:var(--surface-3);cursor:pointer" onclick="toggleOzPhrRow('${esc(it.sku)}')">
        <td style="position:sticky;left:0;background:var(--surface-3);padding:6px 12px">
          <span id="ozphrcar-${esc(it.sku)}" style="color:var(--gold)">▸</span>
          ${it.art ? `<code style="color:var(--val-soft)">${esc(it.art)}</code> ` : ''}
          <span style="color:var(--ink)">${esc(String(it.name || it.sku || '').slice(0, 50))}</span>
          <span class="text-secondary small">· ${it.phrase_count} фраз</span></td>
        <td class="text-end" style="color:var(--ink)">${fmt(it.views)}</td>
        <td class="text-end" style="color:var(--val);font-weight:600">${fmt(it.clicks)}</td>
        <td class="text-end" style="color:${_phrCtrClr(it.ctr)}">${it.ctr}%</td></tr>`;
      // вложенные фразы (скрыты по умолчанию)
      (it.phrases || []).forEach(f => {
        html += `<tr data-ozphr="${esc(it.sku)}" class="ozphr-child" style="display:none;background:var(--t-row)">
          <td style="position:sticky;left:0;background:var(--t-sticky);padding:4px 12px 4px 34px;color:var(--ink-2)">${esc(f.phrase)}</td>
          <td class="text-end small">${fmt(f.views)}</td>
          <td class="text-end small" style="color:var(--val)">${fmt(f.clicks)}</td>
          <td class="text-end small" style="color:${_phrCtrClr(f.ctr)}">${f.ctr}%</td></tr>`;
      });
    });
  });
  html += `</tbody></table></div></div></div>
  <div class="text-secondary small mt-2">Поисковые запросы рекламы Ozon за <b>${d.period || d.days + ' дней'}</b>, сгруппированы по товарам и категориям. Клик по товару — раскрыть его фразы. Высокий CTR (зелёный) — целевой запрос, усилить ставку; низкий CTR при больших показах (красный) — нецелевой, в минус-слова. Обновлено: ${d.fetched_at}</div>`;
  wrap.innerHTML = html;
}

// ── Спрос WB (Джем): поисковые запросы по своим товарам ───────────────────────

let _demandData = null;
let _demandTimer = null;

async function loadDemand(refresh) {
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || _toolActive !== 'niche') return;
  if (_demandData && !refresh) { renderDemand(); return; }
  if (!_demandData) wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Загружаем спрос из Джем…</div>';
  try {
    _demandData = await fetchJSON('/api/tools/demand' + (refresh ? '?refresh=true' : ''), 60000);
    renderDemand();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${esc(e.message)}</div>`;
  }
}

function _dChip(cur, dyn, unit) {
  const d = dyn || 0;
  const arrow = d > 0 ? `<span style="color:var(--pos)">▲${fmt(Math.abs(d))}</span>`
              : d < 0 ? `<span style="color:var(--neg)">▼${fmt(Math.abs(d))}</span>` : '';
  return `<b>${fmt(cur)}${unit||''}</b> ${arrow}`;
}

function renderDemand() {
  if (_toolActive !== 'niche') return;
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || !_demandData) return;
  const d = _demandData;
  const items = d.items || [];
  if (!items.length) {
    wrap.innerHTML = `<div class="alert alert-info">${d.message || 'Данных по спросу пока нет.'}</div>`;
    if (d.building && !_demandTimer)
      _demandTimer = setTimeout(() => { _demandTimer = null; if (_toolActive === 'niche') loadDemand(true); }, 20000);
    return;
  }
  let html = '';
  if (d.building) html += `<div class="alert alert-info py-2 small">⏳ Обновляем спрос из Джем — страница обновится сама.</div>`;
  const opp = items.filter(i => i.opportunity).length;
  html += `<div class="d-flex flex-wrap gap-3 mb-2 align-items-center">
    <div class="text-secondary small">Период: ${esc(d.period||'')} · запросов: ${items.length}${opp?` · <span style="color:var(--warn-c)">точек роста: ${opp}</span>`:''}</div></div>`;
  html += `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:78vh">
    <table class="table table-sm align-middle mb-0"><thead><tr>
      <th style="min-width:200px">Поисковый запрос</th>
      <th style="min-width:180px">Товар</th>
      <th class="text-end" style="min-width:110px">Частотность</th>
      <th class="text-end" style="min-width:100px">Ваша позиция</th>
      <th class="text-end" style="min-width:100px">Заказы</th>
      <th class="text-end" style="min-width:90px">→ корзина %</th>
    </tr></thead><tbody>`;
  items.forEach(it => {
    const posClr = it.position <= 10 ? 'var(--pos)' : it.position <= 30 ? 'var(--warn-c)' : 'var(--neg)';
    html += `<tr style="background:var(--t-row)${it.opportunity ? ';box-shadow:inset 3px 0 0 var(--warn-c)':''}">
      <td style="padding:8px 12px"><b>${esc(it.query)}</b>${it.opportunity?' <span title="высокий спрос, низкая позиция — точка роста">🌱</span>':''}</td>
      <td class="small"><code style="color:var(--val-soft)">${esc(it.sku||'')}</code> ${esc(it.name||'')}</td>
      <td class="text-end">${_dChip(it.freq, it.freq_dyn)}<div class="text-secondary" style="font-size:.7rem">нед: ${fmt(it.week_freq)}</div></td>
      <td class="text-end"><span style="color:${posClr};font-weight:700">${it.position}</span> ${it.pos_dyn?`<span class="text-secondary" style="font-size:.7rem">${it.pos_dyn>0?'▼':'▲'}${Math.abs(it.pos_dyn)}</span>`:''}</td>
      <td class="text-end">${_dChip(it.orders, it.orders_dyn)}</td>
      <td class="text-end">${it.open_to_cart||0}%</td>
    </tr>`;
  });
  html += `</tbody></table></div></div></div>
    <div class="text-secondary small mt-2">Данные из отчёта Джем «Поисковые запросы: ваши товары». 🌱 — точка роста: высокая частотность при низкой позиции (есть куда подниматься рекламой/SEO). Частотность — сколько раз искали запрос; позиция — ваше среднее место в выдаче (меньше = лучше).</div>`;
  wrap.innerHTML = html;
}

// ── Калькулятор ниши (устар., публичная выдача WB закрыта анти-ботом) ─────────

let _nicheResult = null;

async function renderNicheForm() {
  if (_toolActive !== 'nichecalc') return;
  const wrap = document.getElementById('toolsWrap');
  if (!wrap) return;
  let hist = [];
  try { hist = (await fetchJSON('/api/tools/niche/history')).items || []; } catch (e) {}
  wrap.innerHTML = `
  <div class="card bg-card p-3 mb-3">
    <div class="fw-semibold mb-2" style="color:var(--ink)">Оценка: выходить ли с товаром на WB</div>
    <div class="d-flex gap-2 flex-wrap align-items-center">
      <input id="nicheQuery" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:340px" placeholder="Поисковый запрос (напр. крем для лица с цинком)"
             onkeydown="if(event.key==='Enter')runNiche()">
      <button id="nicheGo" class="btn btn-sm btn-outline-success" onclick="runNiche()">Проанализировать</button>
    </div>
    <details class="mt-2">
      <summary class="small" style="cursor:pointer;color:var(--muted)">Доп. параметры для юнит-прикидки (необязательно)</summary>
      <div class="d-flex gap-2 flex-wrap mt-2">
        <input id="nichePrice" type="number" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:130px" placeholder="Ваша цена ₽">
        <input id="nicheCost" type="number" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:130px" placeholder="Себес ₽">
        <input id="nicheLog" type="number" class="form-control form-control-sm bg-dark text-white border-secondary" style="width:130px" placeholder="Логистика ₽ (70)">
      </div>
    </details>
    <div class="text-secondary small mt-2">Данные — из публичной выдачи WB. Оценка продаж конкурентов появляется со второго замера (прирост отзывов).</div>
    ${hist.length ? `<div class="small mt-2"><span class="text-secondary">Раньше считали:</span> ${hist.map(h => `<a href="#" class="me-2" style="color:var(--gold)" onclick="openNiche('${esc(h.query)}');return false">${esc(h.query)}</a>`).join('')}</div>` : ''}
  </div>
  <div id="nicheOut">${_nicheResult ? '' : ''}</div>`;
  if (_nicheResult) renderNicheResult();
}

async function openNiche(q) {
  try {
    _nicheResult = await fetchJSON('/api/tools/niche/get?query=' + encodeURIComponent(q));
    renderNicheResult();
  } catch (e) {}
}

function _nicheSpin(text) {
  const out = document.getElementById('nicheOut');
  if (out) out.innerHTML = `<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>${esc(text)}</div>`;
}

async function runNiche(_retry) {
  const btn = document.getElementById('nicheGo');
  const q = document.getElementById('nicheQuery').value.trim();
  if (!q) return;
  btn.disabled = true;
  _nicheSpin('Запускаем анализ — WB отдаёт данные в несколько шагов с паузами, обычно 1-4 мин…');
  try {
    const r = await fetch('/api/tools/niche', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q,
        price: parseFloat(document.getElementById('nichePrice').value) || 0,
        cost: parseFloat(document.getElementById('nicheCost').value) || 0,
        logistics: parseFloat(document.getElementById('nicheLog').value) || 0 }) });
    // 502/503 = сервер перезапускается (деплой/пробуждение) — подождём и повторим
    if ((r.status === 502 || r.status === 503) && (_retry || 0) < 6) {
      _nicheSpin(`сервер перезапускается — повторим через 20с (${(_retry || 0) + 1}/6)…`);
      setTimeout(() => runNiche((_retry || 0) + 1), 20000);
      return;
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.status);
    if (j.building) { setTimeout(() => pollNiche(j.query || q), 6000); return; }
    _nicheResult = j;
    renderNicheResult();
    btn.disabled = false;
  } catch (e) {
    if (/Failed to fetch|NetworkError/i.test(e.message) && (_retry || 0) < 6) {
      _nicheSpin(`сервер недоступен — повторим через 20с (${(_retry || 0) + 1}/6)…`);
      setTimeout(() => runNiche((_retry || 0) + 1), 20000);
      return;
    }
    const out = document.getElementById('nicheOut');
    if (out) out.innerHTML = `<div class="alert alert-danger">Ошибка: ${esc(e.message)}</div>`;
    btn.disabled = false;
  }
}

let _nicheFails = 0;
let _nicheRestarts = 0;
async function pollNiche(q) {
  if (_toolActive !== 'nichecalc') return;   // ушли с вкладки — не дёргаем DOM
  const btn = document.getElementById('nicheGo');
  try {
    const st = await fetchJSON('/api/tools/niche/status');
    _nicheFails = 0;
    if ((!st.status || st.status === 'idle') && _nicheRestarts < 2) {
      // сервер перезапустился и потерял фоновую задачу — стартуем заново
      _nicheRestarts++;
      _nicheSpin(`сервер перезапускался — перезапускаем анализ (${_nicheRestarts}/2)…`);
      setTimeout(() => runNiche(), 3000);
      return;
    }
    if (st.status === 'running') {
      _nicheSpin((st.stage || 'анализируем…') + ' — страница обновится сама');
      setTimeout(() => pollNiche(q), 8000);
      return;
    }
    if (st.status === 'error') throw new Error(st.error || 'анализ не удался');
    _nicheResult = await fetchJSON('/api/tools/niche/get?query=' + encodeURIComponent(q));
    _nicheRestarts = 0;
    renderNicheResult();
  } catch (e) {
    // сервер на free-тарифе может на минуту зависнуть на тяжёлой фоновой
    // задаче — разовый таймаут не повод бросать опрос
    if (/Таймаут|Failed to fetch|NetworkError/i.test(e.message) && _nicheFails < 5) {
      _nicheFails++;
      _nicheSpin(`сервер занят (попытка опроса ${_nicheFails}/5) — ждём…`);
      setTimeout(() => pollNiche(q), 15000);
      return;
    }
    _nicheFails = 0;
    const out = document.getElementById('nicheOut');
    if (out) out.innerHTML = `<div class="alert alert-danger">Ошибка: ${esc(e.message)}</div>`;
  }
  if (btn) btn.disabled = false;
}

function renderNicheResult() {
  const out = document.getElementById('nicheOut');
  if (!out || !_nicheResult) return;
  const d = _nicheResult;
  let html = `<div class="d-flex justify-content-end mb-2">
    <a href="/api/tools/niche/export?query=${encodeURIComponent(d.query || '')}" class="btn btn-sm btn-outline-success" download>⬇ Экспорт в Excel</a></div>
    <div class="d-flex gap-3 flex-wrap mb-3">
    <div class="metric-card"><div class="mc-head">Товаров по запросу</div><div class="mc-val">${fmt(d.total)}</div></div>
    <div class="metric-card"><div class="mc-head">Цены (медиана)</div><div class="mc-val">${fmtRub(d.median_price)}</div><div class="mc-sub">${fmtRub(d.price_min)} – ${fmtRub(d.price_max)}</div></div>
    <div class="metric-card"><div class="mc-head">Отзывы топ-30</div><div class="mc-val">${fmt(d.feedbacks_top30)}</div><div class="mc-sub">ср. рейтинг ${d.avg_rating}★</div></div>
    <div class="metric-card"><div class="mc-head">Монополизация</div><div class="mc-val" style="color:${d.top3_brand_share > 60 ? 'var(--neg)' : d.top3_brand_share > 40 ? 'var(--warn-c)' : 'var(--pos)'}">${d.top3_brand_share}%</div><div class="mc-sub">доля топ-3 брендов</div></div>
    <div class="metric-card"><div class="mc-head">Шанс новичку</div><div class="mc-val" style="color:${d.newcomers_top30 >= 5 ? 'var(--pos)' : d.newcomers_top30 >= 2 ? 'var(--warn-c)' : 'var(--neg)'}">${d.newcomers_top30}</div><div class="mc-sub">карточек &lt;50 отзывов в топ-30</div></div>
  </div>`;
  if (d.unit) {
    const u = d.unit;
    html += `<div class="card bg-card p-3 mb-3"><div class="fw-semibold mb-1" style="color:var(--ink)">Юнит-прикидка</div>
      <div class="small" style="color:var(--ink-2)">Цена ${fmtRub(u.price)} − комиссия WB ${u.commission_pct}% (${fmtRub(u.commission)}) − логистика ${fmtRub(u.logistics)} − себес ${fmtRub(u.cost)} =
      <b style="color:${u.profit > 0 ? 'var(--pos)' : 'var(--neg)'}">${fmtRub(u.profit)} (${u.margin}%)</b> с единицы <span class="text-secondary">— без учёта рекламы, хранения и налога</span></div></div>`;
  }
  if (d.verdict) {
    html += `<details class="rev-fold mb-3" open><summary>🧠 Вердикт (Claude)</summary>
      <div class="card bg-card mt-2 p-3 small" style="white-space:pre-wrap;line-height:1.6;color:var(--ink)">${esc(d.verdict)}</div></details>`;
  }
  html += `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:60vh">
    <table class="table table-sm align-middle mb-0"><thead><tr>
      <th></th><th>Товар</th><th class="text-end">Цена</th><th class="text-end">Рейтинг</th>
      <th class="text-end">Отзывы</th><th class="text-end">~Прод/мес</th>
    </tr></thead><tbody>`;
  (d.products || []).forEach((p, i) => {
    html += `<tr style="background:var(--t-row)">
      <td style="width:52px"><img src="${wbPhotoUrl(p.nm)}" loading="lazy" onerror="this.remove()" style="width:36px;height:48px;object-fit:cover;border-radius:6px"></td>
      <td><span class="text-secondary small">${i + 1}.</span> <span style="color:var(--ink)">${esc(p.name)}</span>
        <div class="text-secondary" style="font-size:.72rem">${esc(p.brand)} · <a href="https://www.wildberries.ru/catalog/${p.nm}/detail.aspx" target="_blank" style="color:var(--dim)">${p.nm}</a></div></td>
      <td class="text-end" style="color:var(--val);font-weight:600">${p.price ? fmtRub(p.price) : '—'}</td>
      <td class="text-end">${p.rating}★</td>
      <td class="text-end">${fmt(p.feedbacks)}</td>
      <td class="text-end">${p.sales_month_est != null ? '<b style=\'color:var(--pos)\'>' + fmt(p.sales_month_est) + '</b>' : '<span class="text-secondary small">со 2-го замера</span>'}</td>
    </tr>`;
  });
  html += `</tbody></table></div></div></div>
  <div class="text-secondary small mt-2">Топ-30 выдачи WB по популярности. ~Прод/мес — оценка по приросту отзывов между замерами (÷4% оставляющих отзыв); чтобы она появилась, повтори анализ этого запроса через 3+ дня. ${d.analyzed_at}</div>`;
  out.innerHTML = html;
}

// ── Визуалы топ-20 ────────────────────────────────────────────────────────────
let _visualsResult = null;
async function renderVisualsForm() {
  if (_toolActive !== 'visuals') return;
  const wrap = document.getElementById('toolsWrap');
  if (!wrap) return;
  let hist = [];
  try { hist = (await fetchJSON('/api/tools/niche/history')).items || []; } catch (e) {}
  wrap.innerHTML = `<div class="card bg-card p-3 mb-3">
    <div class="fw-semibold mb-2" style="color:var(--ink)">Анализ визуалов заглавных карточек топ-20</div>
    <div class="d-flex gap-2 flex-wrap align-items-center">
      <input id="visualsQuery" class="form-control form-control-sm" style="max-width:340px" placeholder="Поисковый запрос (как в калькуляторе ниши)"
             onkeydown="if(event.key==='Enter')runVisuals()">
      <button id="visualsGo" class="btn btn-sm btn-outline-danger" onclick="runVisuals()">Показать и разобрать</button>
    </div>
    <div class="text-secondary small mt-2">Берёт топ-20 из собранной ниши по этому запросу: показывает заглавные фото и разбирает визуал через Claude. ${hist.length ? 'Уже собраны: ' + hist.slice(0,6).map(h => `<a href="#" onclick="document.getElementById('visualsQuery').value='${esc(h.query)}';runVisuals();return false" style="color:var(--dim)">${esc(h.query)}</a>`).join(' · ') : 'Сначала соберите нишу в «Калькуляторе ниши».'}</div>
  </div><div id="visualsOut"></div>`;
  if (_visualsResult) renderVisualsResult();
}

async function runVisuals() {
  const q = document.getElementById('visualsQuery').value.trim();
  const out = document.getElementById('visualsOut');
  const btn = document.getElementById('visualsGo');
  if (!q || !out) return;
  btn.disabled = true;
  out.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Собираем фото и разбираем визуал через Claude (до минуты)…</div>';
  try {
    _visualsResult = await fetchJSON('/api/tools/visuals?query=' + encodeURIComponent(q) + '&refresh=true', 120000);
    renderVisualsResult();
  } catch (e) {
    out.innerHTML = `<div class="alert alert-danger">Ошибка: ${esc(e.message)}</div>`;
  }
  btn.disabled = false;
}

function renderVisualsResult() {
  const out = document.getElementById('visualsOut');
  if (!out || !_visualsResult) return;
  const d = _visualsResult;
  if (d.message) { out.innerHTML = `<div class="alert alert-info">${esc(d.message)}</div>`; return; }
  let html = '';
  if (d.analysis) {
    html += `<details class="rev-fold mb-3" open><summary>🎨 Разбор визуала (Claude)</summary>
      <div class="card bg-card mt-2 p-3 small" style="white-space:pre-wrap;line-height:1.6;color:var(--ink)">${esc(d.analysis)}</div></details>`;
    html += `<div class="card bg-card p-3 mb-3">
      <div class="fw-semibold mb-2" style="color:var(--ink)">✍ Написать промт для nano banana</div>
      <textarea id="visualsNotes" class="form-control form-control-sm mb-2" rows="2"
        placeholder="Ваши комментарии (необязательно): что на флаконе, объём, вкус, фишка, цвет бренда, что подчеркнуть…"></textarea>
      <button id="visualsPromptGo" class="btn btn-sm btn-outline-warning" onclick="genVisualsPrompt('${esc(d.query || '')}')">Сгенерировать промт</button>
      <div id="visualsPromptOut" class="mt-2"></div></div>`;
  }
  html += `<div class="d-flex flex-wrap gap-3">`;
  (d.items || []).forEach(it => {
    html += `<div style="width:150px">
      <a href="${it.wb_url}" target="_blank" rel="noopener">
        <img src="${it.photo}" loading="lazy" data-fallback="0"
             onerror="const f=+this.dataset.fallback; if(f<2){this.dataset.fallback=f+1; this.src=this.src.replace(/\\/[0-9]+\\.webp/, '/'+(f+2)+'.webp');} else {this.style.opacity=.2;}"
             style="width:150px;height:200px;object-fit:cover;border-radius:8px;border:1px solid var(--border)"></a>
      <div class="small mt-1" style="color:var(--ink)"><b>${it.position}.</b> ${esc(it.brand || '')}</div>
      <div class="text-secondary" style="font-size:.72rem;line-height:1.2">${esc((it.name || '').slice(0,44))}</div>
      <div class="small"><span style="color:var(--val);font-weight:600">${it.price ? fmtRub(it.price) : '—'}</span>
        <span class="text-secondary">· ${it.rating}★ · ${fmt(it.feedbacks)}</span></div>
    </div>`;
  });
  html += `</div><div class="text-secondary small mt-2">Заглавные фото топ-20 выдачи WB. ${d.analyzed_at || ''}</div>`;
  out.innerHTML = html;
}

async function genVisualsPrompt(query) {
  const notes = document.getElementById('visualsNotes')?.value.trim() || '';
  const out = document.getElementById('visualsPromptOut');
  const btn = document.getElementById('visualsPromptGo');
  if (!out) return;
  btn.disabled = true;
  out.innerHTML = '<div class="text-secondary small"><span class="spinner-border spinner-border-sm me-2"></span>Пишу промт по разбору топ-20…</div>';
  try {
    const r = await fetch('/api/tools/visuals/prompt', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, notes }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    const txt = j.prompt || '';
    out.innerHTML = `<div class="card bg-card p-3 small" style="white-space:pre-wrap;line-height:1.6;color:var(--ink);position:relative">
      <button class="btn btn-sm btn-outline-secondary" style="position:absolute;top:8px;right:8px" onclick="navigator.clipboard.writeText(this.parentNode.dataset.txt);this.textContent='✓ Скопировано'">Копировать</button>
      ${esc(txt)}</div>`;
    out.querySelector('.card').dataset.txt = txt;
  } catch (e) {
    out.innerHTML = `<div class="alert alert-danger py-2 small">Ошибка: ${esc(e.message)}</div>`;
  }
  btn.disabled = false;
}

// ── Калькулятор маржи ─────────────────────────────────────────────────────────
let _marginData = null;
let _marginMp = 'WB';
let _marginPrice = {};      // sku → изменённая цена
let _marginCost = {};       // sku → изменённая себестоимость
let _marginDrr = {};        // sku → изменённый ДРР, %
let _marginTax = parseFloat(localStorage.getItem('unit_tax_profit_pct') || '0');
let _marginTarget = 25;     // целевая маржа %
let _marginAdvOn = true;    // учитывать продвижение

async function loadMargin(refresh) {
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || _toolActive !== 'margin') return;
  if (_marginData && !refresh) { renderMargin(); return; }
  wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Считаем затраты на единицу из юнитки…</div>';
  try {
    _marginData = await fetchJSON('/api/tools/margin?mp=' + _marginMp, 60000);
    renderMargin();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-2">Ошибка: ${esc(e.message)}</div>`;
  }
}
function setMarginMp(mp) { if (mp === _marginMp) return; _marginMp = mp; _marginData = null; loadMargin(true); }
function setMarginTax(v) { _marginTax = Math.max(0, parseFloat(v) || 0); localStorage.setItem('unit_tax_profit_pct', String(_marginTax)); recalcAllMargin(); }
function setMarginTarget(v) { _marginTarget = Math.max(0, parseFloat(v) || 0); recalcAllMargin(); }
function toggleMarginAdv(on) { _marginAdvOn = on; renderMargin(); }
function resetMargin() { _marginPrice = {}; _marginCost = {}; _marginDrr = {}; renderMargin(); }
function _marginDrr0(b) { return b.price0 > 0 ? Math.round(b.advert / b.price0 * 1000) / 10 : 0; }

// ядро расчёта одной строки
function _marginCalc(b) {
  const price = _marginPrice[b.sku] != null ? _marginPrice[b.sku] : b.price0;
  const cogs = _marginCost[b.sku] != null ? _marginCost[b.sku] : b.cogs;
  const drr = _marginAdvOn ? (_marginDrr[b.sku] != null ? _marginDrr[b.sku] : _marginDrr0(b)) : 0;
  const advert = price * drr / 100;
  const pctPart = (b.comm_pct + b.acq_pct + drr) / 100;     // масштабируется с ценой
  const fixed = b.logist + b.storage + b.other + cogs;      // не зависит от цены
  const comm = price * b.comm_pct / 100;
  const acq = price * b.acq_pct / 100;
  const grossBeforeTax = price - comm - acq - advert - fixed;
  // цена покупателя: фактическое соотношение после СПП, масштабируем с ценой
  const buyer = (b.buyer0 && b.price0 > 0) ? price * b.buyer0 / b.price0 : null;
  const tax = _marginTax > 0 ? Math.max(grossBeforeTax, 0) * _marginTax / 100 : 0;
  const profit = grossBeforeTax - tax;
  const margin = price > 0 ? profit / price * 100 : 0;
  const roi = cogs > 0 ? profit / cogs * 100 : null;
  // точка безубыточности: price*(1-pctPart) - fixed = 0  (при profit=0 налог=0)
  const denomBE = 1 - pctPart;
  const breakEven = denomBE > 0 ? fixed / denomBE : null;
  // цена для целевой маржи: price*(1-pctPart) - fixed = profit; profit=target%*price; с налогом
  const t = _marginTarget / 100, taxF = 1 - _marginTax / 100;
  const denomTgt = denomBE - (taxF > 0 ? t / taxF : Infinity);
  const targetPrice = denomTgt > 0.001 ? fixed / denomTgt : null;
  return { price, cogs, comm, acq, advert, drr, buyer, fixed, profit, margin, roi, breakEven, targetPrice };
}

function recalcMarginRow(sku) {
  const b = (_marginData.items || []).find(x => x.sku === sku);
  if (!b) return;
  const row = document.querySelector(`tr[data-msku="${CSS.escape(sku)}"]`);
  if (!row) return;
  const c = _marginCalc(b);
  const set = (cls, html) => { const el = row.querySelector('.' + cls); if (el) el.innerHTML = html; };
  set('mg-comm', `<span style="color:var(--neg)">−</span>${fmtRub(Math.round(c.comm + c.acq))}
    <div class="small" style="color:var(--ink-3)">${b.comm_pct}%${b.comm_exact ? '' : '≈'}</div>`);
  set('mg-adv', c.advert > 0 ? `<span style="color:var(--neg)">−</span>${fmtRub(Math.round(c.advert))}` : '<span class="text-secondary">0 ₽</span>');
  set('mg-buyer', c.buyer != null ? fmtRub(Math.round(c.buyer)) : '<span class="text-secondary">—</span>');
  set('mg-costs', fmtRub(Math.round(c.comm + c.acq + c.advert + c.fixed)));
  const pclr = c.profit >= 0 ? 'var(--pos)' : 'var(--neg)';
  set('mg-profit', `<span style="color:${pclr};font-weight:700">${c.profit < 0 ? '−' : ''}${fmtRub(Math.abs(Math.round(c.profit)))}</span>`);
  const mclr = c.margin >= 20 ? 'var(--pos)' : c.margin >= 0 ? 'var(--warn-c)' : 'var(--neg)';
  set('mg-margin', `<span style="color:${mclr};font-weight:700">${Math.round(c.margin)}%</span>`);
  set('mg-roi', c.roi != null ? Math.round(c.roi) + '%' : '—');
  set('mg-be', c.breakEven != null ? fmtRub(Math.round(c.breakEven)) : '—');
  set('mg-tgt', c.targetPrice != null ? fmtRub(Math.round(c.targetPrice)) : '<span class="text-secondary small">—</span>');
  row.style.background = c.profit < 0 ? 'rgba(248,113,113,.07)' : 'var(--t-row)';
}
function recalcAllMargin() {
  if (_toolActive !== 'margin' || !_marginData) return;
  (_marginData.items || []).forEach(b => recalcMarginRow(b.sku));
  // подпись целевой маржи в шапке
  const th = document.getElementById('mgTgtHead');
  if (th) th.textContent = `Цена для ${_marginTarget}% маржи`;
}

function renderMargin() {
  if (_toolActive !== 'margin') return;
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || !_marginData) return;
  const d = _marginData;
  if (d.error) { wrap.innerHTML = `<div class="alert alert-warning">⚠ ${esc(d.error)}</div>`; return; }
  const items = d.items || [];
  if (!items.length) {
    const msg = d.message || 'Нет данных юнитки для расчёта';
    const building = /собира|минут|готов/i.test(msg);
    wrap.innerHTML = `<div class="alert alert-info">${esc(msg)}${building ? '<div class="text-secondary small mt-1">Обновится автоматически…</div>' : ''}</div>`;
    if (building && !window._marginTimer) {
      window._marginTimer = setTimeout(() => {
        window._marginTimer = null;
        if (_toolActive === 'margin') loadMargin(true);
      }, 20000);
    }
    return;
  }

  // группировка по категориям
  const groupMap = {};
  items.forEach(b => {
    const g = articleGroup({ supplierArticle: b.sku, brand: b.group });
    (groupMap[g] = groupMap[g] || []).push(b);
  });
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  const mpBtn = (mp, lbl) => `<button class="btn btn-sm ${_marginMp === mp ? 'btn-info' : 'btn-outline-info'}" onclick="setMarginMp('${mp}')">${lbl}</button>`;
  let html = `<div class="card bg-card p-3 mb-3">
    <div class="d-flex gap-3 flex-wrap align-items-end">
      <div><div class="text-secondary small mb-1">Площадка</div><div class="btn-group">${mpBtn('WB', 'WB')}${mpBtn('OZON', 'Ozon')}${mpBtn('YM', 'ЯМ')}</div></div>
      <div><div class="text-secondary small mb-1">Налог с прибыли, %</div>
        <input type="number" value="${_marginTax || ''}" onchange="setMarginTax(this.value)" placeholder="0"
          style="width:90px" class="form-control form-control-sm"></div>
      <div><div class="text-secondary small mb-1">Целевая маржа, %</div>
        <input type="number" value="${_marginTarget}" onchange="setMarginTarget(this.value)"
          style="width:90px" class="form-control form-control-sm"></div>
      <div><div class="text-secondary small mb-1">Продвижение</div>
        <div class="form-check form-switch"><input class="form-check-input" type="checkbox" ${_marginAdvOn ? 'checked' : ''} onchange="toggleMarginAdv(this.checked)"></div></div>
      <button class="btn btn-sm btn-outline-secondary" onclick="resetMargin()">↺ Сбросить правки</button>
    </div>
    <div class="text-secondary small mt-2">Актуальные статьи (цена, комиссия, логистика, ДРР) — за <b>${esc(d.window_recent || 'посл. 2 мес')}</b>; редкие (хранение, штрафы) сглажены за <b>${esc(d.window_smooth || '4 мес')}</b>; если продаж мало (&lt;10 шт) — окно у артикула расширяется автоматически (подписано рядом с артикулом). <b>Правьте цену, себестоимость и ДРР</b> (голубые поля) — прибыль, маржа и безубыточность пересчитываются вживую. <b>Цена покупателя</b> — с учётом скидки WB (СПП).</div>
  </div>`;

  html += `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:70vh">
    <table class="table table-sm align-middle mb-0 text-nowrap" style="font-size:.83rem"><thead><tr>
      <th style="min-width:230px;position:sticky;left:0;background:var(--t-sticky);z-index:2">Товар</th>
      <th class="text-end" title="Себестоимость — редактируется">Себес ₽</th>
      <th class="text-end" title="Комиссия WB + эквайринг (% от цены)">Комиссия</th>
      <th class="text-end">Логист.</th><th class="text-end">Хранен.</th>
      <th class="text-end" title="Доля рекламных расходов от цены — редактируется">ДРР %</th>
      <th class="text-end" title="Продвижение на штуку = цена × ДРР">Продв.</th>
      <th class="text-end" title="Цена — редактируется" style="background:rgba(16,185,129,.10)">Цена ₽</th>
      <th class="text-end" title="Сколько платит покупатель после скидки WB (СПП) — по фактическому соотношению за 6 мес">Цена покупателя</th>
      <th class="text-end">Затраты</th>
      <th class="text-end">Прибыль/ед</th><th class="text-end">Маржа</th><th class="text-end">ROI</th>
      <th class="text-end" title="Цена, при которой прибыль = 0">Безубыт.</th>
      <th class="text-end" id="mgTgtHead">Цена для ${_marginTarget}% маржи</th>
    </tr></thead><tbody>`;
  orderedGroups.forEach(([gname, list]) => {
    list.sort((a, b) => b.qty - a.qty);
    html += `<tr class="table-secondary"><td colspan="15" style="padding:6px 12px"><strong>${gname}</strong> <span class="text-secondary small">(${list.length} арт.)</span></td></tr>`;
    list.forEach(b => {
      const price = _marginPrice[b.sku] != null ? _marginPrice[b.sku] : b.price0;
      const cogs = _marginCost[b.sku] != null ? _marginCost[b.sku] : b.cogs;
      const drr = _marginDrr[b.sku] != null ? _marginDrr[b.sku] : _marginDrr0(b);
      const advDim = _marginAdvOn ? '' : 'opacity:.35';
      html += `<tr data-msku="${esc(b.sku)}" style="background:var(--t-row)">
        <td style="position:sticky;left:0;background:var(--t-sticky);padding:5px 12px">
          <code style="color:var(--val-soft)">${esc(b.sku)}</code>
          ${b.window ? `<span class="text-secondary small" title="Окно данных для цены/комиссии/логистики/ДРР (расширено, если продаж мало)"> · ${esc(b.window)}</span>` : ''}
          <div class="small text-secondary" style="max-width:210px;overflow:hidden;text-overflow:ellipsis">${esc(b.name || '')}</div></td>
        <td class="text-end" style="padding:3px 6px"><input type="number" value="${cogs}" oninput="_marginCost['${esc(b.sku)}']=parseFloat(this.value)||0;recalcMarginRow('${esc(b.sku)}')"
          style="width:76px;text-align:right;background:rgba(56,189,248,.10);border:1px solid var(--border);border-radius:6px;color:var(--ink);padding:2px 6px"></td>
        <td class="text-end mg-comm" title="${b.subject ? `Тариф WB для «${esc(b.subject)}»: ${b.comm_pct}% + эквайринг ${b.acq_pct}%` : b.comm_exact ? `Комиссия WB ${b.comm_pct}% (из последней продажи) + эквайринг ${b.acq_pct}%` : `Средняя комиссия ${b.comm_pct}% + эквайринг ${b.acq_pct}%`}"></td>
        <td class="text-end"><span style="color:var(--neg)">−</span>${fmtRub(b.logist)}</td>
        <td class="text-end"><span style="color:var(--neg)">−</span>${fmtRub(b.storage)}</td>
        <td class="text-end" style="padding:3px 6px;${advDim}"><input type="number" step="0.1" value="${drr}" oninput="_marginDrr['${esc(b.sku)}']=parseFloat(this.value)||0;recalcMarginRow('${esc(b.sku)}')"
          style="width:62px;text-align:right;background:rgba(56,189,248,.10);border:1px solid var(--border);border-radius:6px;color:var(--ink);padding:2px 6px"></td>
        <td class="text-end mg-adv" style="${advDim}"></td>
        <td class="text-end" style="padding:3px 6px;background:rgba(16,185,129,.06)"><input type="number" value="${price}" oninput="_marginPrice['${esc(b.sku)}']=parseFloat(this.value)||0;recalcMarginRow('${esc(b.sku)}')"
          style="width:84px;text-align:right;background:rgba(16,185,129,.12);border:1px solid var(--pos);border-radius:6px;color:var(--ink);font-weight:600;padding:2px 6px"></td>
        <td class="text-end mg-buyer" style="color:var(--gold);font-weight:600"></td>
        <td class="text-end mg-costs" style="color:var(--ink-2)"></td>
        <td class="text-end mg-profit" style="border-left:1px solid var(--sep)"></td>
        <td class="text-end mg-margin"></td>
        <td class="text-end mg-roi text-secondary"></td>
        <td class="text-end mg-be text-secondary"></td>
        <td class="text-end mg-tgt"></td>
      </tr>`;
    });
  });
  html += `</tbody></table></div></div></div>`;
  // сноска: действующая комиссия WB по категориям (официальный тариф)
  const bySubject = {};
  items.filter(b => b.comm_exact).forEach(b => {
    const key = b.subject || articleGroup({ supplierArticle: b.sku, brand: b.group });
    (bySubject[key] = bySubject[key] || new Set()).add(b.comm_pct);
  });
  const commBySubj = Object.entries(bySubject).map(([s, set]) => {
    const pcts = [...set].sort((a, b) => a - b);
    const val = pcts.length === 1 ? `${pcts[0]}%` : `${pcts[0]}–${pcts[pcts.length - 1]}%`;
    return `${esc(s)} <b style="color:var(--ink)">${val}</b>`;
  });
  if (commBySubj.length) {
    const hasTariff = items.some(b => b.subject);
    html += `<div class="text-secondary small mt-2">📌 Комиссия WB сейчас${hasTariff ? ' (официальный тариф по категориям, FBO)' : ' (из последних продаж)'}: ${commBySubj.join(' · ')} — сверх неё эквайринг ~${(items.reduce((s, b) => s + b.acq_pct, 0) / items.length).toFixed(1)}%.</div>`;
  }
  html += `<div class="text-secondary small mt-2">💡 <b>Безубыточность</b> — минимальная цена, ниже которой уходите в минус (при текущей себестоимости). <b>Цена для X% маржи</b> — по какой цене продавать, чтобы получить нужную маржу. Убыточные при текущей цене строки подсвечены красным. ${d.fetched_at}</div>`;
  wrap.innerHTML = html;
  recalcAllMargin();
}

// ── Контроль рекламы ──────────────────────────────────────────────────────────

let _advToolData = null;
let _advTimer = null;

async function loadAdv(refresh) {
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || _toolActive !== 'adv') return;
  if (_advToolData && !refresh) { renderAdvTool(); return; }
  if (!_advToolData) wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Загружаем рекламу…</div>';
  try {
    _advToolData = await fetchJSON('/api/tools/adv' + (refresh ? '?refresh=true' : ''), 60000);
    renderAdvTool();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${e.message}</div>`;
  }
}

const _ADV_VERDICT = {
  waste: ['🔴 Сливает бюджет', 'var(--neg)'],
  bad:   ['🔴 Убыточна',       'var(--neg)'],
  warn:  ['🟡 На грани',       'var(--warn-c)'],
  good:  ['🟢 Эффективна',     'var(--pos)'],
  idle:  ['◽ Без расхода',    'var(--muted)'],
};

function renderAdvTool() {
  if (_toolActive !== 'adv') return;
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || !_advToolData) return;
  const d = _advToolData;
  let html = '';
  if (d.building || (!d.campaigns?.length && !d.error)) {
    html += `<div class="alert alert-info py-2 small">⏳ Собираем статистику рекламы (${d.progress || 'старт'}) — страница обновится сама.</div>`;
    if (!_advTimer) _advTimer = setTimeout(() => { _advTimer = null; _advToolData = null; if (currentTab === 'tools' && _toolActive === 'adv') loadAdv(); }, 25000);
  }
  if (d.error) html += `<div class="alert alert-warning py-2 small">⚠ ${d.error}</div>`;
  if (!d.campaigns?.length) { wrap.innerHTML = html || '<div class="alert alert-info">Кампаний нет</div>'; return; }

  html += `<div class="d-flex gap-3 flex-wrap mb-3">
    <div class="metric-card" style="min-width:150px"><div class="mc-head">Расход за ${d.days} дн.</div>
      <div class="mc-val">${fmtRub(d.total_spend)}</div></div>
    <div class="metric-card" style="min-width:150px"><div class="mc-head">Заказы с рекламы</div>
      <div class="mc-val" style="color:var(--pos)">${fmtRub(d.total_revenue)}</div></div>
    <div class="metric-card" style="min-width:130px"><div class="mc-head">Средняя ДРР</div>
      <div class="mc-val" style="color:${d.total_drr > 20 ? 'var(--neg)' : d.total_drr > 12 ? 'var(--warn-c)' : 'var(--pos)'}">${d.total_drr != null ? d.total_drr + '%' : '—'}</div></div>
    <div class="metric-card" style="min-width:150px"><div class="mc-head">Впустую (без заказов)</div>
      <div class="mc-val" style="color:${d.waste ? 'var(--neg)' : 'var(--pos)'}">${fmtRub(d.waste)}</div></div>
  </div>`;

  if (d.advice && d.advice.text) {
    html += `<details class="rev-fold mb-3" open>
      <summary>🧠 Советы по оптимизации (Claude)</summary>
      <div class="card bg-card mt-2 p-3 small" style="white-space:pre-wrap;line-height:1.6;color:var(--ink)">${esc(d.advice.text)}</div>
    </details>`;
  }

  html += `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:70vh">
    <table class="table table-sm align-middle mb-0 text-nowrap"><thead><tr>
      <th style="min-width:220px">Кампания</th><th>Статус</th>
      <th class="text-end">Расход</th><th class="text-end">Показы</th><th class="text-end">Клики</th>
      <th class="text-end">CTR</th><th class="text-end">CPC</th>
      <th class="text-end">Заказы</th><th class="text-end">Выручка</th>
      <th class="text-end">ДРР</th><th style="min-width:190px">Вердикт</th>
    </tr></thead><tbody>`;
  d.campaigns.forEach(c => {
    const [vLabel, vClr] = _ADV_VERDICT[c.verdict] || ['', 'var(--muted)'];
    const words = c.words || [];
    const hasWords = words.length > 0;
    const bids = c.bids || {};
    const bidStr = [bids.cpm != null ? `CPM ${bids.cpm}₽` : '',
                    bids.search != null ? `поиск ${bids.search}₽` : '',
                    bids.catalog != null ? `каталог ${bids.catalog}₽` : ''].filter(Boolean).join(' · ');
    const skuChips = (c.skus || []).slice(0, 6).map(s =>
      `<code style="color:var(--dim)">${esc(s.sku)}</code>${s.spend ? `<span class="text-secondary" style="font-size:.68rem">·${fmtRub(s.spend)}</span>` : ''}`).join(' ')
      + ((c.skus || []).length > 6 ? ` <span class="text-secondary small">+${c.skus.length - 6}</span>` : '');
    const modeClr = c.mode === 'Автоматическая' ? 'var(--warn-c)' : '#38bdf8';
    html += `<tr style="background:var(--t-row);${hasWords ? 'cursor:pointer' : ''}" ${hasWords ? `onclick="toggleAdvWords(${c.id})"` : ''}>
      <td style="padding:6px 10px">${hasWords ? `<span id="advArr${c.id}" style="color:var(--dim)">▶</span> ` : ''}<span style="color:var(--ink)">${esc(c.name)}</span>
        <span class="small" style="color:${modeClr}">· ${c.mode || ''}</span>
        <span class="text-secondary small">· ${c.type}${bidStr ? ' · ставка: ' + bidStr : ''}</span>
        ${skuChips ? `<div style="font-size:.72rem;margin-top:2px">${skuChips}</div>` : ''}</td>
      <td class="small">${c.status}</td>
      <td class="text-end" style="color:var(--val);font-weight:600">${fmtRub(Math.round(c.spend))}</td>
      <td class="text-end" style="color:var(--dim)">${fmt(c.views)}</td>
      <td class="text-end">${fmt(c.clicks)}</td>
      <td class="text-end">${c.ctr}%</td>
      <td class="text-end">${c.cpc != null ? c.cpc + ' ₽' : '—'}</td>
      <td class="text-end">${fmt(c.orders)}</td>
      <td class="text-end" style="color:var(--pos)">${fmtRub(c.revenue)}</td>
      <td class="text-end" style="font-weight:700;color:${c.drr == null ? 'var(--neg)' : c.drr > 20 ? 'var(--neg)' : c.drr > 12 ? 'var(--warn-c)' : 'var(--pos)'}">${c.drr != null ? c.drr + '%' : '∞'}</td>
      <td class="small"><span style="color:${vClr}">${vLabel}</span> <span class="text-secondary">${esc(c.verdict_why || '')}</span></td>
    </tr>`;
    if (hasWords) {
      html += `<tr id="advWords${c.id}" style="display:none;background:var(--fin-cost)"><td colspan="11" style="padding:8px 14px 12px 30px">
        <div class="small mb-1" style="color:var(--ink-2)">Ключевые фразы (топ по расходу; 🔥 — работает, 🗑 — кандидат в минус):</div>
        <div class="d-flex flex-wrap gap-1">${words.map(w => {
          const clr = w.flag === 'minus' ? 'rgba(248,113,113,.12);color:var(--neg);border:1px solid rgba(248,113,113,.3)'
                    : w.flag === 'hot' ? 'rgba(34,197,94,.13);color:var(--pos);border:1px solid rgba(34,197,94,.3)'
                    : 'var(--surface-3);color:var(--ink-2);border:1px solid var(--border)';
          const icon = w.flag === 'minus' ? '🗑 ' : w.flag === 'hot' ? '🔥 ' : '';
          const metr = w.cluster ? `${fmt(w.views)} показов` : `${fmt(w.views)}п · ${fmt(w.clicks)}к · CTR ${w.ctr}%${w.sum ? ' · ' + fmtRub(Math.round(w.sum)) : ''}`;
          return `<span style="background:${clr};border-radius:7px;padding:3px 9px;font-size:.74rem">${icon}${esc(w.phrase)} <span style="opacity:.7">(${metr})</span></span>`;
        }).join('')}</div></td></tr>`;
    }
  });
  html += `</tbody></table></div></div></div>
  <div class="text-secondary small mt-2">Статистика fullstats WB за ${d.days} дней. ДРР = расход / выручка с рекламы. Фразы показаны для кампаний с расходом. ${d.built_at ? 'Обновлено: ' + d.built_at + ' UTC' : ''}</div>`;
  wrap.innerHTML = html;
}

function toggleAdvWords(id) {
  const row = document.getElementById('advWords' + id);
  const arr = document.getElementById('advArr' + id);
  if (!row) return;
  const open = row.style.display !== 'none';
  row.style.display = open ? 'none' : '';
  if (arr) arr.textContent = open ? '▶' : '▼';
}

// ── Остатки по кластерам ──────────────────────────────────────────────────────

let _clustersData = null;

async function loadClusters(refresh) {
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || _toolActive !== 'clusters') return;
  if (_clustersData && !refresh) { renderClusters(); return; }
  wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Считаем кластеры…</div>';
  try {
    _clustersData = await fetchJSON('/api/tools/clusters' + (refresh ? '?refresh=true' : ''), 120000);
    renderClusters();
  } catch (e) {
    wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${e.message}</div>`;
  }
}

const _CL_STATUS = {
  urgent:   ['🔴 Срочно — требует пополнения', 'var(--neg)'],
  warn:     ['🟡 Скоро закончится',            'var(--warn-c)'],
  ok:       ['🟢 Всё хорошо',                  'var(--pos)'],
  over:     ['⚫ Перегруз',                    'var(--dim)'],
  no_sales: ['◽ Нет продаж',                  'var(--muted)'],
};

function renderClusters() {
  if (_toolActive !== 'clusters') return;
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || !_clustersData) return;
  const d = _clustersData;
  let html = `<div class="d-flex justify-content-end mb-2">
    <a href="/api/tools/clusters/export" class="btn btn-sm btn-outline-success" download>⬇ Экспорт в Excel</a></div>
    <div class="d-flex gap-3 flex-wrap mb-3">
    <div class="metric-card" style="min-width:160px"><div class="mc-head">📍 Локализация (всего)</div>
      <div class="mc-val">${d.localization_total != null ? d.localization_total + '%' : '—'}</div>
      <div class="mc-sub">доля продаж, отгруженных из округа покупателя</div></div>
    <div class="metric-card" style="min-width:160px"><div class="mc-head">⚠ Слабых кластеров</div>
      <div class="mc-val" style="color:${d.weak ? 'var(--warn-c)' : 'var(--pos)'}">${d.weak}</div>
      <div class="mc-sub">покрытие меньше 15 дней</div></div>
  </div><div class="row g-3">`;
  (d.items || []).forEach(it => {
    const [label, clr] = _CL_STATUS[it.status] || _CL_STATUS.ok;
    html += `<div class="col-12 col-md-6 col-xl-4"><div class="metric-card h-100" style="border-left:3px solid ${clr}">
      <div class="d-flex justify-content-between align-items-start">
        <b style="color:var(--ink);font-size:15px">${it.cluster}</b>
        <span class="small" style="color:${clr}">${label}</span>
      </div>
      <div class="small mb-2" style="color:${it.need ? 'var(--neg)' : 'var(--muted)'}">К заказу у поставщика: ${it.need ? fmt(it.need) + ' шт' : 'не требуется'}</div>
      <div class="d-flex gap-4">
        <div><div style="font-size:20px;font-weight:700;color:var(--val)">${it.spd.toLocaleString('ru-RU')}</div><div class="text-secondary" style="font-size:11px">продаж/день</div></div>
        <div><div style="font-size:20px;font-weight:700;color:var(--val)">${fmt(it.stock)}</div><div class="text-secondary" style="font-size:11px">остаток, шт</div></div>
      </div>
      <div class="small mt-2" style="color:var(--ink-2)">
        Покрытие: <b style="color:${clr}">${it.coverage != null ? it.coverage + ' дн' : '—'}</b>
        ${it.localization != null ? ` · Локализация: <b>${it.localization}%</b> <span class="text-secondary">(спрос ${fmt(it.demand)})</span>` : ''}
      </div>
      ${(it.skus || []).length ? `
      <details class="mt-2">
        <summary class="small" style="cursor:pointer;color:var(--gold)">🚚 Что везти: ${(it.skus || []).length} арт. (${fmt(it.need_by_demand || 0)} шт от спроса округа)</summary>
        <table class="table table-sm mb-0 mt-1" style="font-size:.74rem">
          <thead><tr><th>Артикул</th><th class="text-end">Спрос/д</th><th class="text-end">Здесь, шт</th><th class="text-end">Покр., дн</th><th class="text-end">Везти</th></tr></thead>
          <tbody>${it.skus.map(s => `<tr>
            <td><code style="color:var(--val-soft)">${esc(s.sku)}</code> <span class="text-secondary">${esc((s.name || '').slice(0, 22))}</span></td>
            <td class="text-end">${s.demand_spd}</td>
            <td class="text-end">${fmt(s.stock)}</td>
            <td class="text-end" style="color:${s.coverage < 7 ? 'var(--neg)' : s.coverage < 15 ? 'var(--warn-c)' : 'var(--ink)'}">${s.coverage}</td>
            <td class="text-end"><b style="color:var(--pos)">${fmt(s.need)}</b></td>
          </tr>`).join('')}</tbody>
        </table>
      </details>` : ''}
      ${(it.other_skus || []).length ? `
      <details class="mt-1">
        <summary class="small" style="cursor:pointer;color:var(--dim)">📦 Остальное: ${(it.other_skus || []).length} арт. (лежат / покрытие в норме)</summary>
        <table class="table table-sm mb-0 mt-1" style="font-size:.74rem">
          <thead><tr><th>Артикул</th><th class="text-end">Спрос/д</th><th class="text-end">Здесь, шт</th><th class="text-end">Покр., дн</th></tr></thead>
          <tbody>${it.other_skus.map(s => `<tr>
            <td><code style="color:var(--val-soft)">${esc(s.sku)}</code> <span class="text-secondary">${esc((s.name || '').slice(0, 22))}</span></td>
            <td class="text-end">${s.demand_spd}</td>
            <td class="text-end">${fmt(s.stock)}</td>
            <td class="text-end" style="color:var(--ink-2)">${s.coverage}</td>
          </tr>`).join('')}</tbody>
        </table>
      </details>` : ''}
    </div></div>`;
  });
  html += `</div><div class="text-secondary small mt-3">Продажи/день — среднее за ${d.days} дн. по складам округа (только выкупы). Покрытие = остаток / скорость. Дозаказ — до ${d.target_days} дней покрытия. Локализация — доля заказов покупателей округа, отгруженных со складов этого же округа (выше — дешевле логистика WB). «Что везти» считается от СПРОСА покупателей округа — показывает и те артикулы, что сейчас продаются с чужих складов. Обновлено: ${d.fetched_at}</div>`;
  wrap.innerHTML = html;
}

// ── Инструменты WB: Продуктолог ───────────────────────────────────────────────

let _prodData = null;
let _prodTimer = null;

function _prodSig(d) {
  return (d && d.items || []).map(i => i.sku + ':' + (i.analyzed ? 1 : 0) + ':' + i.count).join('|')
    + '|p' + (d && d.pending || 0) + '|b' + (d && d.building ? 1 : 0);
}

async function loadProductolog(refresh) {
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || _toolActive !== 'prod') return;
  const poll = refresh === 'poll';
  if (_prodData && !refresh) { renderProductolog(); return; }
  if (!_prodData) wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border me-2"></span>Загружаем анализ отзывов…</div>';
  try {
    const data = await fetchJSON('/api/tools/productolog' + (refresh && !poll ? '?refresh=true' : ''), 60000);
    if (poll && _prodData && _prodSig(data) === _prodSig(_prodData)) {
      // ничего не изменилось — не трогаем DOM, просто ждём дальше
      _prodData = data;
      if (!_prodTimer && (data.pending > 0 || data.building) && _toolActive === 'prod')
        _prodTimer = setTimeout(() => { _prodTimer = null; if (currentTab === 'tools' && _toolActive === 'prod') loadProductolog('poll'); }, 25000);
      return;
    }
    _prodData = data;
    renderProductolog();
  } catch (e) {
    if (!poll) wrap.innerHTML = `<div class="alert alert-danger mt-3">Ошибка: ${e.message}</div>`;
  }
}

function _prodChips(list, kind) {
  if (!list || !list.length) return '<span class="text-secondary small">—</span>';
  const style = kind === 'plus'
    ? 'background:rgba(34,197,94,.13);color:var(--pos);border:1px solid rgba(34,197,94,.3)'
    : 'background:rgba(248,113,113,.12);color:var(--neg);border:1px solid rgba(248,113,113,.3)';
  return list.map(p => `<span style="${style};border-radius:7px;padding:2px 8px;font-size:.76rem;display:inline-block;margin:2px 3px 2px 0;white-space:nowrap">${esc(p.tag)} · ${p.pct}%</span>`).join('');
}

function renderProductolog() {
  if (_toolActive !== 'prod') return;
  const wrap = document.getElementById('toolsWrap');
  if (!wrap || !_prodData) return;
  const d = _prodData;
  const items = d.items || [];
  if (!items.length) {
    wrap.innerHTML = `<div class="alert alert-info">${d.error ? '⚠ ' + d.error : 'Отзывов WB пока мало для анализа — сводка появится, когда накопятся.'}</div>`;
    return;
  }
  let html = '';
  html += `<div class="d-flex justify-content-end mb-2">
    <a href="/api/tools/productolog/export" class="btn btn-sm btn-outline-success" download>⬇ Экспорт в Excel</a></div>`;
  if (d.pending > 0 || d.building) {
    html += `<div class="alert alert-info py-2 small">⏳ Анализируем отзывы (${d.progress || `осталось ${d.pending} арт.`}) — по 4 товара сразу, страница обновится сама.</div>`;
    if (!_prodTimer) _prodTimer = setTimeout(() => { _prodTimer = null; if (currentTab === 'tools' && _toolActive === 'prod') loadProductolog('poll'); }, 15000);
  }
  if (d.error) {
    html += `<div class="alert alert-warning py-2 small">⚠ ${d.error}</div>`;
  }
  html += `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:78vh">
    <table class="table table-sm align-middle mb-0"><thead><tr>
      <th style="min-width:190px;position:sticky;left:0;background:var(--t-sticky);z-index:2">Товар</th>
      <th class="text-center" style="min-width:120px">Отзывы</th>
      <th style="min-width:230px">Плюсы (%)</th>
      <th style="min-width:230px">Минусы (%)</th>
      <th style="min-width:280px">Рекомендации к изменению</th>
    </tr></thead><tbody>`;
  // классическая группировка (Фисты, Спреи и т.д.) — как в заказах/остатках
  const groupMap = {};
  items.forEach(it => {
    const g = articleGroup({ supplierArticle: it.sku, brand: it.group });
    (groupMap[g] = groupMap[g] || []).push(it);
  });
  const orderedGroups = GROUP_ORDER.filter(g => groupMap[g]).map(g => [g, groupMap[g]]);

  const rowHtml = it => {
    const bar = `<div class="d-flex" style="height:6px;border-radius:3px;overflow:hidden;width:100px;margin:4px auto 2px">
      <div style="width:${it.pos}%;background:var(--pos)"></div>
      <div style="width:${it.neu}%;background:var(--warn-c)"></div>
      <div style="width:${it.neg}%;background:var(--neg)"></div></div>`;
    const avgClr = it.avg >= 4.8 ? 'var(--pos)' : it.avg >= 4.5 ? 'var(--warn-c)' : 'var(--neg)';
    const row = `<tr style="background:var(--t-row)">
      <td style="position:sticky;left:0;background:var(--t-sticky);padding:8px 12px;vertical-align:top">
        ${it.wb_link
          ? `<a href="${it.wb_link}" target="_blank" rel="noopener" style="text-decoration:none"><code style="color:var(--val-soft)">${esc(it.sku)}</code> <span style="font-size:.7rem">🔗</span></a>`
          : `<code style="color:var(--val-soft)">${esc(it.sku)}</code>`}
        <div class="small" style="color:var(--ink)">${esc(it.name)}</div>
        <div class="text-secondary" style="font-size:.72rem">${esc(it.group || '')}</div></td>
      <td class="text-center" style="vertical-align:top;padding-top:10px">
        <b style="color:var(--val)">${fmt(it.count)}</b>${bar}
        <span style="color:${avgClr};font-weight:700">${it.avg.toFixed(2)}★</span>
        <div class="text-secondary" style="font-size:.7rem">🟢${it.pos}% · ⚪${it.neu}% · 🔴${it.neg}%</div></td>
      <td style="vertical-align:top;padding:8px">${it.analyzed ? _prodChips(it.pluses, 'plus')
        : it.analyzable ? '<span class="text-secondary small">⏳ анализируется…</span>'
        : `<span class="text-secondary small">мало текстовых отзывов (${it.text_reviews||0}) — только оценки</span>`}</td>
      <td style="vertical-align:top;padding:8px">${it.analyzed ? _prodChips(it.minuses, 'minus') : ''}</td>
      <td style="vertical-align:top;padding:8px" class="small">${it.analyzed
        ? `<span style="color:var(--ink-2)">🛠 ${esc(it.recommendation)}</span>`
        : ''}</td>
    </tr>`;
    return row;
  };
  orderedGroups.forEach(([gname, list]) => {
    html += `<tr class="table-secondary"><td colspan="5" style="padding:6px 12px"><strong>${gname}</strong> <span class="text-secondary small">(${list.length} арт.)</span></td></tr>`;
    list.forEach(it => { html += rowHtml(it); });
  });
  html += `</tbody></table></div></div></div>
  <div class="text-secondary small mt-2">Проблемные товары сверху (по доле негативных отзывов). Проценты в плюсах/минусах — доля отзывов, где тема упомянута. Анализ пересобирается автоматически, когда накапливаются новые отзывы.</div>`;
  // сохранить прокрутку — автообновление не должно сбрасывать чтение
  const prevTable = wrap.querySelector('.table-responsive');
  const tScroll = prevTable ? prevTable.scrollTop : 0;
  const wScroll = window.scrollY;
  wrap.innerHTML = html;
  const newTable = wrap.querySelector('.table-responsive');
  if (newTable && tScroll) newTable.scrollTop = tScroll;
  if (wScroll) window.scrollTo(0, wScroll);
}

// ── Init ──────────────────────────────────────────────────────────────────────

function initDashboard() {
  switchTab('salesan', document.querySelector('#mainTabs .nav-link'));
  setInterval(() => { markAllDirty(); switchTab(currentTab); }, 30 * 60 * 1000);

  // Preload all other tabs in background so switching feels instant
  const bgTabs = [
    { name: 'stocks',  fn: loadStocks },
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

  // серверная сессия: спрашиваем /api/auth/me
  fetch('/api/auth/me').then(async r => {
    if (!r.ok) { showOverlay('login'); return; }
    _me = await r.json();
    applyRole();
    // переход по карточке с другого кабинета (?enter=1) — сразу внутрь
    if (new URLSearchParams(location.search).has('enter')) {
      localStorage.setItem('mp_cabinet', 'auto');
      history.replaceState(null, '', location.pathname);
    }
    if (localStorage.getItem('mp_cabinet')) {
      showOverlay('app');
      initDashboard();
    } else {
      showOverlay('cabinet');
    }
  }).catch(() => showOverlay('login'));
});

// ── Управление доступами (владелец) ──────────────────────────────────────────

async function openUsers() {
  const modal = new bootstrap.Modal(document.getElementById('usersModal'));
  modal.show();
  await renderUsers();
}

async function renderUsers() {
  const box = document.getElementById('usersList');
  if (!box) return;
  try {
    const d = await (await fetch('/api/users')).json();
    const ROLE_RU = { owner: 'Владелец', director: 'Директор', manager: 'Менеджер' };
    box.innerHTML = (d.users || []).map(u => `
      <div class="d-flex align-items-center gap-3 py-1" style="border-bottom:1px solid var(--border)">
        <b style="min-width:140px;color:var(--ink)">${esc(u.login)}</b>
        <span class="text-secondary small" style="min-width:90px">${ROLE_RU[u.role] || u.role}</span>
        ${u.login !== (_me && _me.login) ? `<button class="btn btn-sm btn-outline-danger py-0 ms-auto" onclick="deleteUser('${esc(u.login)}')">✕ Удалить</button>` : '<span class="text-secondary small ms-auto">это вы</span>'}
      </div>`).join('') || '<div class="text-secondary small">Пока никого</div>';
  } catch (e) {
    box.innerHTML = `<div class="text-danger small">${e.message}</div>`;
  }
}

async function addUser() {
  const login = document.getElementById('nuLogin').value.trim();
  const password = document.getElementById('nuPass').value;
  const role = document.getElementById('nuRole').value;
  const err = document.getElementById('nuError');
  err.textContent = '';
  const r = await fetch('/api/users', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ login, password, role }),
  });
  if (!r.ok) { err.textContent = (await r.json().catch(() => ({}))).detail || 'Ошибка'; return; }
  document.getElementById('nuLogin').value = '';
  document.getElementById('nuPass').value = '';
  renderUsers();
}

async function deleteUser(login) {
  if (!confirm(`Удалить пользователя ${login}?`)) return;
  const r = await fetch(`/api/users/${encodeURIComponent(login)}`, { method: 'DELETE' });
  if (!r.ok) alert((await r.json().catch(() => ({}))).detail || 'Ошибка');
  renderUsers();
}


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
  buildArtDD();   // держим список артикулов в актуальном состоянии

  let filtered = platform === 'all' ? _allReviews : _allReviews.filter(r => r.platform === platform);
  if (onlyText) filtered = filtered.filter(r => r.text);
  if (_reviewsArtSel.size) filtered = filtered.filter(r => _reviewsArtSel.has(r.sku || ''));
  window._reviewsFiltered = filtered;

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
        <div class="d-flex align-items-start gap-2 ms-2">
          <div class="text-end">
            <div class="small">${r.name || r.sku}</div>
            ${r.group ? `<div class="text-secondary" style="font-size:0.75rem">${r.group}</div>` : ''}
          </div>
          ${r.nm ? `<img src="${wbPhotoUrl(r.nm)}" loading="lazy" onerror="this.remove()"
               style="width:40px;height:52px;object-fit:cover;border-radius:8px;border:1px solid var(--border-2);flex-shrink:0" />` : ''}
        </div>
      </div>
      ${r.text ? `<div class="mt-1">${r.text}</div>` : '<div class="text-secondary small fst-italic">без текста</div>'}
      ${replyBlock(r)}
    </div>
  `).join('');
}


// ── Мультиселект артикулов в отзывах (по группам) ─────────────────────────────
let _reviewsArtSel = new Set();   // выбранные sku (пусто = все)
let _artDDBuilt = '';             // подпись списка, чтобы не перерисовывать зря

function buildArtDD() {
  const menu = document.getElementById('reviewsArtMenu');
  if (!menu) return;
  // уникальные артикулы из отзывов, сгруппированные
  const byGroup = {};
  const seen = new Set();
  (_allReviews || []).forEach(r => {
    const sku = r.sku || '';
    if (!sku || seen.has(sku)) return;
    seen.add(sku);
    const g = articleGroup({ supplierArticle: sku, brand: r.brand });
    (byGroup[g] = byGroup[g] || []).push({ sku, name: r.name || '' });
  });
  const sig = Object.keys(byGroup).sort().join('|') + ':' + seen.size;
  if (sig === _artDDBuilt) { updateArtBtn(); return; }   // список не изменился
  _artDDBuilt = sig;

  const ordered = GROUP_ORDER.filter(g => byGroup[g]).map(g => [g, byGroup[g]]);
  let html = `<div class="d-flex gap-2 mb-2">
      <button class="btn btn-sm btn-outline-light py-0 flex-fill" onclick="artDDAll(true)">Все</button>
      <button class="btn btn-sm btn-outline-secondary py-0 flex-fill" onclick="artDDAll(false)">Снять</button>
    </div>`;
  ordered.forEach(([g, list]) => {
    list.sort((a, b) => a.sku.localeCompare(b.sku));
    html += `<div class="fw-semibold small mt-2 mb-1" style="color:var(--val)">
      <a href="#" onclick="artDDGroup('${esc(g)}');return false" style="color:var(--val);text-decoration:none">${esc(g)}</a>
      <span class="text-secondary">(${list.length})</span></div>`;
    list.forEach(it => {
      const ck = _reviewsArtSel.has(it.sku) ? 'checked' : '';
      html += `<label class="d-block small" style="cursor:pointer;color:var(--ink)">
        <input type="checkbox" class="form-check-input me-1 art-ck" data-group="${esc(g)}" value="${esc(it.sku)}" ${ck}
               onchange="artDDToggle(this)">
        <code style="color:var(--val-soft)">${esc(it.sku)}</code>
        <span class="text-secondary">${esc((it.name || '').slice(0, 34))}</span></label>`;
    });
  });
  menu.innerHTML = html;
  updateArtBtn();
}

function updateArtBtn() {
  const btn = document.getElementById('reviewsArtBtn');
  if (btn) btn.textContent = _reviewsArtSel.size ? `Артикулы: ${_reviewsArtSel.size}` : 'Все артикулы';
}

function toggleArtDD(e) {
  e.stopPropagation();
  document.getElementById('reviewsArtMenu')?.classList.toggle('show');
}
document.addEventListener('click', e => {
  const dd = document.getElementById('reviewsArtDD');
  if (dd && !dd.contains(e.target)) document.getElementById('reviewsArtMenu')?.classList.remove('show');
});

function artDDToggle(cb) {
  if (cb.checked) _reviewsArtSel.add(cb.value); else _reviewsArtSel.delete(cb.value);
  updateArtBtn();
  renderReviewsFeed();
}
function artDDGroup(g) {
  // выбрать/снять всю группу
  const cks = [...document.querySelectorAll(`.art-ck[data-group="${CSS.escape(g)}"]`)];
  const allOn = cks.every(c => c.checked);
  cks.forEach(c => { c.checked = !allOn; if (!allOn) _reviewsArtSel.add(c.value); else _reviewsArtSel.delete(c.value); });
  updateArtBtn();
  renderReviewsFeed();
}
function artDDAll(on) {
  _reviewsArtSel.clear();
  if (on) document.querySelectorAll('.art-ck').forEach(c => { c.checked = true; _reviewsArtSel.add(c.value); });
  else document.querySelectorAll('.art-ck').forEach(c => { c.checked = false; });
  updateArtBtn();
  renderReviewsFeed();
}

function exportReviews() {
  const platform = document.getElementById('reviewsPlatform')?.value || 'all';
  const onlyText = document.getElementById('reviewsOnlyText')?.checked ? '1' : '0';
  const skus = encodeURIComponent([..._reviewsArtSel].join(','));
  window.open(`${API}/api/reviews/export?platform=${platform}&only_text=${onlyText}&skus=${skus}&${getParams()}`, '_blank');
}

// главное фото товара WB по nmId (раскладка по basket-хостам)
function wbPhotoUrl(nm) {
  nm = parseInt(nm, 10);
  if (!nm) return '';
  const vol = Math.floor(nm / 1e5), part = Math.floor(nm / 1e3);
  const R = [143,287,431,719,1007,1061,1115,1169,1313,1601,1655,1919,2045,2189,2405,2621,2837,3053,3269,3485,3701,3917,4133,4349,4565,4877,5189,5501,5813,6125,6437];
  let b = R.length + 1;
  for (let i = 0; i < R.length; i++) if (vol <= R[i]) { b = i + 1; break; }
  return `https://basket-${String(b).padStart(2,'0')}.wbbasket.ru/vol${vol}/part${part}/${nm}/images/c246x328/1.webp`;
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
