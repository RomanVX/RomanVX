// Standalone snapshot of the Biomed Orders view. Changes here stay in Development.
'use strict';

const charts = {};
const CHART_C = document.documentElement.dataset.theme === 'light'
  ? { tick: '#5b6472', tick2: '#8a93a5' }
  : { tick: '#94a3b8', tick2: '#64748b' };
let BRAND_ORDER = ['Джага', 'Satisfucktion', 'Aloe'];
let SUBGROUPS = [
  { name: 'Фисты',             skus: ['BMN-0013', 'BMN-0028', 'BMN-0035', 'BMN-0036', 'ST-07', 'ST-14'] },
  { name: 'Спреи для минета',  skus: ['BMN-0115', 'BMN-0116', 'BMN-0110'] },
];
let GROUP_ORDER = ['Фисты', 'Aloe', 'Спреи для минета', 'Satisfucktion', 'Джага', 'Прочее'];
const filters = new URLSearchParams(sessionStorage.getItem('mp_development_orders') || location.search);


function articleGroup(r) {
  const sku = r.supplierArticle || r.sku || '';
  for (const sg of SUBGROUPS) if (sg.skus.includes(sku)) return sg.name;
  return BRAND_ORDER.includes(r.brand) ? r.brand : 'Прочее';
}

function fmt(n, dec = 0) {
  if (n == null) return '—';
  return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function fmtRub(n) { return fmt(n) + ' ₽'; }

function esc(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function fetchJSON(path, timeoutMs = 60000) {
  const url = new URL(path, location.origin);
  const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || String(response.status));
  return data;
}

const marketplace = document.getElementById('ordersMarketplace');
const brandFilter = document.getElementById('ordersBrand');
const periodFilter = document.getElementById('ordersPeriod');
const _MP_COLORS = { WB: '#c026d3', OZON: '#3b82f6', YM: '#b45309', TOTAL: '#059669' };
const _MP_LABEL = { WB: 'Wildberries', OZON: 'Ozon', YM: 'Яндекс Маркет', TOTAL: 'Все площадки' };
let _ordersData = null;
let requestVersion = 0;

function moscowToday() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts();
  const date = Object.fromEntries(parts.map(({type, value}) => [type, value]));
  return `${date.year}-${date.month}-${date.day}`;
}

const REPORT_COLUMNS = 7;

function isoDate(date) {
  return date.toISOString().slice(0, 10);
}

function reportRange(value) {
  const start = new Date(value + 'T00:00:00Z');
  const mode = periodFilter.value;
  if (mode === 'month') start.setUTCDate(1);
  else start.setUTCDate(start.getUTCDate() - (start.getUTCDay() + 6) % 7);

  if (mode !== 'day') {
    if (mode === 'month') start.setUTCMonth(start.getUTCMonth() - (REPORT_COLUMNS - 1));
    else start.setUTCDate(start.getUTCDate() - (REPORT_COLUMNS - 1) * 7);
  }

  const end = new Date(start);
  if (mode === 'month') end.setUTCMonth(start.getUTCMonth() + REPORT_COLUMNS, 0);
  else end.setUTCDate(start.getUTCDate() + (mode === 'day' ? REPORT_COLUMNS : REPORT_COLUMNS * 7) - 1);
  return { from: isoDate(start), to: isoDate(end) };
}

function saveOrdersFilters() {
  filters.set('mp', marketplace.value);
  filters.set('brand', brandFilter.value);
  filters.set('mode', periodFilter.value);
  filters.delete('from');
  filters.delete('to');
  sessionStorage.setItem('mp_development_orders', filters.toString());
}

async function loadOrders() {
  const version = ++requestVersion;
  const table = document.getElementById('ordersTable');
  const summary = document.getElementById('ordersSummary');
  _ordersData = null;
  renderOrdersTable();
  table.innerHTML = '<tr><td class="text-secondary py-3">Загрузка…</td></tr>';
  saveOrdersFilters();
  const range = reportRange(moscowToday());
  loadOrderMetrics();
  const query = new URLSearchParams({
    period: periodFilter.value, date_from: range.from, date_to: range.to,
  });
  if (brandFilter.value) query.set('brand', brandFilter.value);
  try {
    const data = await fetchJSON('/api/dashboard/development/orders_matrix?' + query);
    if (version !== requestVersion) return;
    _ordersData = data;
    renderOrdersTable();
  } catch (error) {
    if (version !== requestVersion) return;
    table.innerHTML = `<tr><td class="text-danger py-3">Ошибка: ${esc(error.message)}</td></tr>`;
    summary.textContent = '';
  }
}

let _ordPartialIdx = -1;
function _dynArrow(cur, prev, i) {
  if (i !== undefined && i === _ordPartialIdx)
    return `<span class="text-secondary" style="font-size:0.66rem;margin-left:3px" title="Период ещё не закончился — сравнивать с полным прошлым рано">· идёт</span>`;
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
  return `<td class="text-end ${cls}" style="${st}">${fmtRub(v)}${_dynArrow(v, prev, i)}</td>`;
}

function _qtyCell(v, cls, extra = '') {
  return `<td class="text-end ${cls}" style="${extra}">${v ? fmt(v) : '<span class="text-muted">—</span>'}</td>`;
}

function _visWeeks(n) {
  return Array.from({ length: n }, (_, i) => i);
}

function toggleGrpRows(grpId) {
  const rows = document.querySelectorAll(`[data-grp="${grpId}"]`);
  const arr  = document.getElementById('arr-' + grpId);
  const expanded = arr && arr.textContent === '▼';
  rows.forEach(r => { r.style.display = expanded ? 'none' : ''; });
  if (arr) {
    arr.textContent = expanded ? '▶' : '▼';
    arr.closest('tr')?.setAttribute('aria-expanded', String(!expanded));
  }
}

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
  const mp = marketplace.value;
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
  _ordPartialIdx = _ordersData.partial_index ?? -1;
  vis.forEach(i => {
    thead += `<th class="text-end" colspan="2" style="white-space:nowrap;${_WEEK_SEP}">${weeks[i]}${i === _ordPartialIdx ? ' <span class="text-secondary fw-normal" title="Период ещё идёт">· идёт</span>' : ''}</th>`;
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
    // отмены показываем только там, где они реально трекаются
    // (недельный WB); в День/Месяц данных об отменах нет
    const hasCancel = periodFilter.value === 'week' && grpSkus.some(s => s.cancel_rub);
    grpSkus.forEach(s => {
      s.rub.forEach((v, i) => { bRub[i] += v; });
      s.qty.forEach((v, i) => { bQty[i] += v; });
      if (s.cancel_rub) s.cancel_rub.forEach((v, i) => { bCancelRub[i] += v; });
    });

    const grpId = `grp-${mp}-${gi}`;
    const GRP_BG = 'background:var(--chip-bg);color:var(--chip-ink);';

    // Строка-заголовок группы со стрелкой сворачивания
    tbody += `<tr data-row="grp" style="border-top:2px solid var(--sep-strong);cursor:pointer" tabindex="0" role="button" aria-expanded="false"
      onclick="toggleGrpRows('${grpId}')"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleGrpRows('${grpId}')}">`;
    tbody += `<td class="fw-semibold ps-2" style="${GRP_BG}padding:6px 8px">`
           + `<span id="arr-${grpId}" style="display:inline-block;width:14px;font-size:0.8rem;color:var(--muted)">▶</span>`
           + `<span class="me-1" style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${_groupColor(grp)}"></span>`
           + `<strong>${grp}</strong> <span class="small" style="color:var(--muted)">(${grpSkus.length} арт.)</span></td>`;

    // Ячейки по неделям: сумма = все заказы (как «Заказали» воронки WB),
    // под ней — доля отменённых из них (по дате заказа)
    vis.forEach(i => {
      const v    = bRub[i];
      const prev = i > 0 ? bRub[i - 1] : 0;
      const pct  = hasCancel && v > 0 && bCancelRub[i] > 0
        ? Math.round(bCancelRub[i] / v * 100) : null;
      const dynHtml   = v ? _dynArrow(v, prev, i) : '';
      const buyoutHtml = pct !== null
        ? `<div class="buyout-pill" title="«Отменили и вернули» за эту неделю по дате события — как в воронке ЛК WB">↩ ${pct}% отмен</div>`
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
      tbody += `<td class="ps-4 small" style="max-width:260px;overflow:hidden;text-overflow:ellipsis" title="${esc(s.name || s.sku)}">`
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
        `<div style="white-space:nowrap;font-size:0.72rem;color:var(--ink-2);line-height:1.35">`
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
  try { renderOrdersSummary(); } catch (e) { console.warn('orders summary', e); }
}

// ── Сводка над матрицей: что с заказами прямо сейчас (KPI, дни, бренды, лидеры) ──
function _ordBlockFrom(d, mp) {
  if (!d) return null;
  const n = (d.weeks || []).length;
  if (mp !== 'TOTAL') {
    const b = d[mp];
    return b ? { skus: b.skus || [], total_rub: b.total_rub || Array(n).fill(0), total_qty: b.total_qty || Array(n).fill(0), n } : null;
  }
  const merged = {}, tr = Array(n).fill(0), tq = Array(n).fill(0);
  ['WB', 'OZON', 'YM'].forEach(m => (d[m]?.skus || []).forEach(s => {
    const t = merged[s.sku] || (merged[s.sku] = { sku: s.sku, name: s.name, brand: s.brand, group: s.group,
      rub: Array(n).fill(0), qty: Array(n).fill(0), cancel_rub: Array(n).fill(0) });
    (s.rub || []).forEach((v, i) => { t.rub[i] += v; tr[i] += v; });
    (s.qty || []).forEach((v, i) => { t.qty[i] += v; tq[i] += v; });
    (s.cancel_rub || []).forEach((v, i) => { t.cancel_rub[i] += v; });
  }));
  return { skus: Object.values(merged), total_rub: tr, total_qty: tq, n };
}
function renderOrdersSummary() {
  const box = document.getElementById('ordersSummary');
  const mp = marketplace.value;
  const block = _ordBlockFrom(_ordersData, mp);
  if (!block) {
    box.textContent = '';
    _renderOrdDaysChart(null, mp);
    return;
  }

  box.innerHTML = `
    <div class="card border-0 bg-card mb-3"><div class="card-body p-3 p-md-4">
      <div class="fw-semibold mb-3">Динамика заказов, ₽</div>
      <div style="position:relative;height:380px"><canvas id="ordDaysChart"></canvas></div>
    </div></div>`;
  _renderOrdDaysChart(_ordersData, mp);
}

let metricsVersion = 0;

function aggregateMetrics(blocks) {
  const platforms = Object.values(blocks || {});
  const sum = (key, allowPartial = false) => {
    const values = platforms.map(platform => platform[key]).filter(Number.isFinite);
    if (!values.length || (!allowPartial && values.length !== platforms.length)) return null;
    return values.reduce((total, value) => total + value, 0);
  };
  const revenue = sum('revenue');
  const units = sum('units');
  const expenses = sum('expenses', true);
  return {
    revenue, units, expenses, average: units > 0 && revenue != null ? revenue / units : null,
    expensesPartial: expenses != null && platforms.some(p => p.expenses == null || p.expenses_partial),
    impressions: sum('impressions'), cardOpens: sum('card_opens'),
    adRevenue: sum('ad_revenue'), adSpend: sum('ad_spend'), adImpressions: sum('ad_impressions'),
  };
}

function ratio(numerator, denominator) {
  return numerator == null || !denominator ? '—' : fmt(numerator / denominator * 100, 1) + '%';
}

function delta(current, previous, cost = false) {
  if (current == null || previous == null || previous === 0) return '';
  const change = (current - previous) / Math.abs(previous) * 100;
  const direction = change === 0 ? '' : (change > 0) !== cost ? ' up' : ' down';
  const label = (change > 0 ? '+' : '') + fmt(change, 1) + '%';
  return ` <span class="mc-delta${direction} small" title="К предыдущему сопоставимому периоду">${label}</span>`;
}

const periodDateFormat = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
});

function renderMetricsPeriod(period) {
  const label = document.getElementById('ordersMetricsPeriod');
  label.hidden = !period;
  if (!period) return;
  const start = new Date(period.from + 'T00:00:00Z');
  const end = new Date(period.to + 'T00:00:00Z');
  label.textContent = period.from === period.to
    ? periodDateFormat.format(start)
    : periodDateFormat.formatRange(start, end);
}

function renderOrderMetrics(data, status = '') {
  const current = aggregateMetrics(data?.current);
  const previous = aggregateMetrics(data?.previous);
  const amount = value => value == null ? '—' : fmtRub(value);
  const count = value => value == null ? '—' : fmt(value);
  const wb = marketplace.value === 'WB';
  renderMetricsPeriod(data?.period);
  const cards = [
    ['Заказы, руб.', amount(current.revenue), delta(current.revenue, previous.revenue),
      `По рекламе ${amount(current.adRevenue)} · ${ratio(current.adRevenue, current.revenue)}`,
      wb ? 'Заказали на сумму из воронки продаж Wildberries' : 'Сумма заказов по данным отчёта'],
    ['Заказов, шт', count(current.units), delta(current.units, previous.units),
      `Средний чек ${amount(current.average)}${delta(current.average, previous.average)}`,
      'Средний чек: сумма заказов ÷ количество заказанных товаров'],
    ['Расходы', amount(current.expenses), current.expensesPartial || previous.expensesPartial
      ? '' : delta(current.expenses, previous.expenses, true),
      `Реклама ${amount(current.adSpend)} · ДРР ${ratio(current.adSpend, current.revenue)}`
        + (current.expensesPartial ? '<br>По доступным статьям' : ''), 'Расходы по данным площадки и учтённая себестоимость'],
    ['Показы', count(current.impressions), delta(current.impressions, previous.impressions),
      `По рекламе ${count(current.adImpressions)} · ${ratio(current.adImpressions, current.impressions)}`
        + (wb ? `<br>Переходы в карточки ${count(current.cardOpens)}${delta(current.cardOpens, previous.cardOpens)}` : ''),
      wb ? 'Воронка WB отдаёт переходы в карточки. Общие показы в этом отчёте отсутствуют.'
        : 'Общие показы из аналитики площадки'],
  ];
  document.getElementById('ordersMetrics').innerHTML = `
    <div class="row row-cols-1 row-cols-sm-2 row-cols-xl-4 g-2 mb-2">
      ${cards.map(([title, value, trend, note, tooltip]) => `
        <div class="col"><div class="metric-card h-100" title="${tooltip}">
          <div class="mc-head fw-bold">${title}</div>
          <div class="d-flex align-items-baseline flex-wrap gap-1"><span class="mc-val">${value}</span>${trend}</div>
          <div class="mc-sub small">${note}</div>
        </div></div>`).join('')}
    </div>
    ${status ? `<p class="text-secondary small mb-2">${esc(status)}</p>` : ''}`;
}

async function loadOrderMetrics() {
  const version = ++metricsVersion;
  const query = new URLSearchParams({
    period: periodFilter.value, marketplace: marketplace.value, brand: brandFilter.value,
  });
  renderOrderMetrics(null, 'Загружаем показатели…');
  for (let attempt = 0; attempt < 45 && version === metricsVersion; attempt++) {
    try {
      const data = await fetchJSON('/api/dashboard/development/order_metrics?' + query);
      if (version !== metricsVersion) return;
      renderOrderMetrics(data, data.pending
        ? (attempt < 44 ? 'Загружаем расходы и статистику рекламы…' : 'Часть отчётов ещё собирается')
        : '');
      if (!data.pending) return;
    } catch (error) {
      if (version === metricsVersion) renderOrderMetrics(null, 'Показатели недоступны: ' + error.message);
      return;
    }
    if (attempt < 44) await new Promise(resolve => setTimeout(resolve, 5000));
  }
}

function _renderOrdDaysChart(dd, mp) {
  const cv = document.getElementById('ordDaysChart');
  if (charts.ordDays) { try { charts.ordDays.destroy(); } catch (e) {} charts.ordDays = null; }
  if (!cv || !dd) return;
  const labels = (dd.weeks || []).map(String);
  const n = labels.length;
  const mps = mp === 'TOTAL' ? ['WB', 'OZON', 'YM'].filter(m => dd[m] && (dd[m].total_rub || []).some(v => v)) : [mp];
  const datasets = mps.map(m => {
    const c = { WB: '#C026F4', OZON: '#156BFF', YM: '#EA8300' }[m] || '#635BFF';
    return { label: { WB: 'WB', OZON: 'Ozon', YM: 'ЯМ' }[m] || m, data: (dd[m]?.total_rub || Array(n).fill(0)),
             backgroundColor: c, hoverBackgroundColor: c + '88', borderRadius: 7,
             maxBarThickness: 66, categoryPercentage: 0.7, stack: 's' };
  });
  charts.ordDays = new Chart(cv.getContext('2d'), {
    type: 'bar', data: { labels, datasets },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      layout: { padding: { top: 12, right: 12, bottom: 8 } },
      interaction: { mode: 'nearest', intersect: true },
      plugins: { legend: { display: mps.length > 1, position: 'bottom', labels: { boxWidth: 10, font: { size: 11 } } },
                 tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtRub(c.raw)}` } } },
      scales: { x: { stacked: true, grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 0, autoSkip: true } },
                y: { stacked: true, beginAtZero: true, grace: '10%',
                     ticks: { maxTicksLimit: 5, precision: 0, padding: 8, font: { size: 11 }, callback: v => _fmtShort(v) },
                     grid: { color: 'rgba(127,127,127,.10)' } } } },
  });
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


async function initDevelopmentOrders() {
  try {
    const [cabinet, user] = await Promise.all([
      fetchJSON('/api/cabinet'),
      fetchJSON('/api/auth/me'),
    ]);
    if (cabinet.id !== 'biomed' || user.role !== 'owner') {
      throw new Error('Раздел доступен владельцу кабинета Биомеда');
    }
    GROUP_ORDER = cabinet.group_order || GROUP_ORDER;
    BRAND_ORDER = cabinet.brand_order || BRAND_ORDER;
    SUBGROUPS = cabinet.subgroups || SUBGROUPS;
    const marketplaces = cabinet.marketplaces || [];
    const choices = marketplaces.length > 1 ? [...marketplaces, 'TOTAL'] : marketplaces;
    for (const mp of choices) marketplace.add(new Option(_MP_LABEL[mp] || mp, mp));
    if (choices.includes(filters.get('mp'))) marketplace.value = filters.get('mp');

    const brands = new Set(cabinet.brand_order || cabinet.group_order || BRAND_ORDER);
    brands.delete('');
    brands.delete('Прочее');
    const savedBrand = filters.get('brand') || '';
    if (savedBrand) brands.add(savedBrand);
    for (const name of brands) brandFilter.add(new Option(name, name));
    brandFilter.value = savedBrand;

    const savedMode = filters.get('mode');
    periodFilter.value = savedMode === 'days' ? 'day' :
      ['day', 'week', 'month'].includes(savedMode) ? savedMode : 'week';
    marketplace.addEventListener('change', () => {
      saveOrdersFilters();
      renderOrdersTable();
      loadOrderMetrics();
    });
    brandFilter.addEventListener('change', loadOrders);
    periodFilter.addEventListener('change', loadOrders);
    document.getElementById('ordersFilters').hidden = false;
    Chart.defaults.color = CHART_C.tick;
    await loadOrders();
  } catch (error) {
    document.getElementById('pane-salesan').hidden = true;
    const message = document.getElementById('ordersAccessError');
    message.textContent = error.message;
    message.hidden = false;
  }
}

initDevelopmentOrders();
