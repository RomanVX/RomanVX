'use strict';

const API = '';
let charts = {};
let sortState = {};
const dirty = { finance: true, products: true, salesan: true, stocks: true, supplies: true, unitec: true, advert: true };
let _advertData = [];
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
function skuName(r) {
  const art = r.supplierArticle || r.nmId;
  return r.subject && r.subject !== art ? `${art} <span class="text-secondary small">${r.subject}</span>` : String(art);
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function switchTab(name, linkEl) {
  document.querySelectorAll('#mainTabs .nav-link').forEach(a => a.classList.remove('active'));
  if (linkEl) linkEl.classList.add('active');
  ['finance', 'products', 'salesan', 'stocks', 'supplies', 'unitec', 'advert'].forEach(t => {
    document.getElementById('pane-' + t).style.display = t === name ? 'block' : 'none';
  });
  currentTab = name;
  if (dirty[name]) {
    dirty[name] = false;
    ({ finance: loadFinance, products: loadProducts, salesan: loadSalesAnalytics, stocks: loadStocks,
       supplies: loadSupplies, unitec: loadUnitEc, advert: loadAdvert })[name]();
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
  const BRAND_ORDER = ['Джага', 'Satisfucktion', 'Aloe'];

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
    const g = BRAND_ORDER.includes(r.brand) ? r.brand : 'Прочее';
    (groupMap[g] = groupMap[g] || []).push(r);
  });
  Object.values(groupMap).forEach(rows => {
    rows.sort((a, b) => {
      const av = a.wb_per_day || a.oz_per_day || 0;
      const bv = b.wb_per_day || b.oz_per_day || 0;
      return bv - av;
    });
  });
  const orderedGroups = [...BRAND_ORDER, 'Прочее']
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
      ? `<span style="color:#fff;font-weight:600">${fmt(qty)}</span>`
      : '<span class="text-secondary">—</span>';
    const v = perDay > 0
      ? `<span style="color:#fff;font-weight:600">${fmt(perDay, 1)}</span>`
      : '<span class="text-secondary">—</span>';
    return `<td class="text-end" style="border-left:2px solid #2a2a2a">${q}</td><td class="text-end">${v}</td><td class="text-end">${oos}</td>`;
  }

  const header = `<thead class="sticky-top">
    <tr style="background:#111">
      ${thSort('supplierArticle','Артикул')}
      ${thSort('name','Название')}
      ${thSort('brand','Бренд')}
      <th class="text-center" colspan="3" style="background:#6b21a8;color:#fff;border-left:2px solid #2a2a2a">WB</th>
      <th class="text-center" colspan="3" style="background:#1d4ed8;color:#fff;border-left:2px solid #2a2a2a">OZON</th>
      <th class="text-center" colspan="3" style="background:#854d0e;color:#fff;border-left:2px solid #2a2a2a">YM</th>
      ${thSort('status','Статус')}
    </tr>
    <tr class="small" style="background:#1a1a1a;color:#9ca3af">
      <th></th><th></th><th></th>
      <th class="text-end" style="border-left:2px solid #2a2a2a" title="Остаток WB (quantity)">Ост</th>
      <th class="text-end" title="Продаж/день WB">Пр/д</th>
      <th class="text-end" title="Дней до OOS">Дней</th>
      <th class="text-end" style="border-left:2px solid #2a2a2a" title="Остаток Ozon">Ост</th>
      <th class="text-end" title="Продаж/день Ozon">Пр/д</th>
      <th class="text-end" title="Дней до OOS">Дней</th>
      <th class="text-end" style="border-left:2px solid #2a2a2a" title="Остаток YM">Ост</th>
      <th class="text-end" title="Продаж/день YM">Пр/д</th>
      <th class="text-end" title="Дней до OOS">Дней</th>
      <th></th>
    </tr>
  </thead>`;

  const bodyRows = orderedGroups.map(([grp, rows]) => {
    const grpRow = `<tr class="table-secondary">
      <td colspan="13"><strong>${grp}</strong> <span class="text-secondary small">(${rows.length} арт.)</span></td>
    </tr>`;
    const itemRows = rows.map(r => `<tr style="font-size:14px">
      <td><code style="color:#e2e8f0">${r.supplierArticle}</code></td>
      <td style="color:#fff">${r.name}</td>
      <td class="text-secondary small">${r.brand}</td>
      ${cell(r.wb_qty, r.wb_per_day, r.wb_days)}
      ${cell(r.oz_qty, r.oz_per_day, r.oz_days)}
      ${cell(r.ym_qty, r.ym_per_day, r.ym_days)}
      <td>${STATUS_ICON[r.status]}</td>
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

let _saData = null;
let _saMode = 'rev';
let _saMP   = 'all';
let _saSort = { col: 'total_rev', asc: false };
let _saSearch = '';
let _saChartDyn = null;
let _saChartTop = null;

function setSaMP(mp, el) {
  _saMP = mp;
  document.querySelectorAll('#saMPGroup button').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  loadSalesAnalytics();
}

function setSaMode(mode) {
  _saMode = mode;
  document.querySelectorAll('#saModeBtn button').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === mode));
  if (_saData) {
    renderSaDyn(_saData.dynamics);
    renderSaTop(_saData.top10);
  }
}

async function loadSalesAnalytics() {
  const brand = (document.getElementById('saBrand') || {}).value || 'all';
  const p = getDateParams() + `&marketplace=${_saMP}&brand=${encodeURIComponent(brand)}`;
  document.getElementById('saKPI').innerHTML =
    '<div class="col-12 text-center text-secondary py-3"><span class="spinner-border spinner-border-sm"></span> Загрузка данных…</div>';
  document.getElementById('saBody').innerHTML =
    '<tr><td colspan="11" class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span></td></tr>';
  try {
    const r = await fetch(`${API}/api/dashboard/sales_analytics?${p}`);
    if (!r.ok) { const b = await r.json().catch(() => ({})); throw new Error(b.detail || r.status); }
    _saData = await r.json();
    renderSaKPI(_saData.kpi);
    renderSaDyn(_saData.dynamics);
    renderSaTop(_saData.top10);
    renderSaTable(_saData.table);
    initSaTableSort();
  } catch(e) {
    document.getElementById('saKPI').innerHTML =
      `<div class="col-12 text-danger text-center py-3">Ошибка: ${e.message}</div>`;
  }
}

function renderSaKPI(kpi) {
  const best = kpi.best;
  document.getElementById('saKPI').innerHTML = `
    <div class="col-6 col-md-3">
      <div class="card border-0 bg-card text-center p-3">
        <div class="text-secondary small mb-1">Продажи, шт</div>
        <div class="fs-3 fw-bold">${fmt(kpi.total_qty)}</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card border-0 bg-card text-center p-3">
        <div class="text-secondary small mb-1">Выручка, ₽</div>
        <div class="fs-3 fw-bold">${fmtRub(kpi.total_rev)}</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card border-0 bg-card text-center p-3">
        <div class="text-secondary small mb-1">Продаж / день</div>
        <div class="fs-3 fw-bold">${fmt(kpi.avg_per_day, 1)}</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card border-0 bg-card text-center p-3">
        <div class="text-secondary small mb-1">Лучший артикул</div>
        <div class="fw-bold" style="font-size:15px">${best ? best.article : '—'}</div>
        <div class="text-secondary" style="font-size:11px">${best ? fmtRub(best.rev) : ''}</div>
      </div>
    </div>`;
}

function renderSaDyn(dynamics) {
  const m = _saMode;
  const labels  = dynamics.map(d => d.date.slice(5));
  const wbData  = dynamics.map(d => m === 'rev' ? d.wb_rev  : d.wb_qty);
  const ozData  = dynamics.map(d => m === 'rev' ? d.oz_rev  : d.oz_qty);
  const ymData  = dynamics.map(d => m === 'rev' ? d.ym_rev  : d.ym_qty);
  if (_saChartDyn) _saChartDyn.destroy();
  const ctx = document.getElementById('saDynChart').getContext('2d');
  _saChartDyn = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'WB',   data: wbData, borderColor: '#9333ea', backgroundColor: 'rgba(147,51,234,.15)', tension: 0.3, fill: true, pointRadius: 2 },
        { label: 'Ozon', data: ozData, borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,.15)',  tension: 0.3, fill: true, pointRadius: 2 },
        { label: 'YM',   data: ymData, borderColor: '#eab308', backgroundColor: 'rgba(234,179,8,.15)',   tension: 0.3, fill: true, pointRadius: 2 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#aaa' } } },
      scales: {
        x: { ticks: { color: '#777', maxTicksLimit: 12 }, grid: { color: '#1e1e1e' } },
        y: { ticks: { color: '#777' }, grid: { color: '#1e1e1e' } },
      }
    }
  });
}

function renderSaTop(top10) {
  const m = _saMode;
  const labels = top10.map(r => r.article);
  const wbData = top10.map(r => m === 'rev' ? r.wb_rev : r.wb_qty);
  const ozData = top10.map(r => m === 'rev' ? r.oz_rev : r.oz_qty);
  const ymData = top10.map(r => m === 'rev' ? r.ym_rev : r.ym_qty);
  if (_saChartTop) _saChartTop.destroy();
  const ctx = document.getElementById('saTopChart').getContext('2d');
  _saChartTop = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'WB',   data: wbData, backgroundColor: '#9333ea' },
        { label: 'Ozon', data: ozData, backgroundColor: '#3b82f6' },
        { label: 'YM',   data: ymData, backgroundColor: '#eab308' },
      ]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#aaa' } } },
      scales: {
        x: { stacked: true, ticks: { color: '#777' }, grid: { color: '#1e1e1e' } },
        y: { stacked: true, ticks: { color: '#ccc', font: { size: 11 } } },
      }
    }
  });
}

function renderSaTable(rows) {
  const q = _saSearch.toLowerCase();
  let data = rows.filter(r =>
    !q || (r.article || '').toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q));
  const { col, asc } = _saSort;
  data.sort((a, b) => {
    const av = a[col] ?? '', bv = b[col] ?? '';
    if (typeof av === 'number') return asc ? av - bv : bv - av;
    return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  });
  const tbody = document.getElementById('saBody');
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="text-center text-secondary py-4">Нет данных</td></tr>';
    return;
  }
  tbody.innerHTML = data.map(r => `
    <tr>
      <td class="fw-semibold">${r.article}</td>
      <td class="text-secondary small">${r.name || '—'}</td>
      <td><span class="badge bg-secondary">${r.brand || '—'}</span></td>
      <td>${fmt(r.wb_qty)}</td>
      <td>${r.wb_rev ? fmtRub(r.wb_rev) : '—'}</td>
      <td>${fmt(r.oz_qty)}</td>
      <td>${r.oz_rev ? fmtRub(r.oz_rev) : '—'}</td>
      <td>${fmt(r.ym_qty)}</td>
      <td>${r.ym_rev ? fmtRub(r.ym_rev) : '—'}</td>
      <td class="fw-semibold">${fmt(r.total_qty)}</td>
      <td class="fw-semibold text-warning">${fmtRub(r.total_rev)}</td>
    </tr>`).join('');
}

function initSaTableSort() {
  document.querySelectorAll('#saTableHead th[data-sa]').forEach(th => {
    th.style.cursor = 'pointer';
    th.onclick = () => {
      const col = th.dataset.sa;
      if (_saSort.col === col) _saSort.asc = !_saSort.asc;
      else { _saSort.col = col; _saSort.asc = false; }
      if (_saData) renderSaTable(_saData.table);
    };
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
