// Standalone snapshot of the Biomed Orders view. Changes here stay in Development.
'use strict';

const charts = {};
const API = '';
const _G = color => color;
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
const brand = filters.get('brand') || '';

function articleGroup(r) {
  const sku = r.supplierArticle || r.sku || '';
  for (const sg of SUBGROUPS) if (sg.skus.includes(sku)) return sg.name;
  return BRAND_ORDER.includes(r.brand) ? r.brand : 'Прочее';
}

function saveOrdersFilters() {
  filters.set('mp', _ordersMp);
  filters.set('mode', _ordersMode);
  sessionStorage.setItem('mp_development_orders', filters.toString());
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
  url.searchParams.set('days', '30');
  if (brand) url.searchParams.set('brand', brand);
  const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || String(response.status));
  return data;
}

let _ordersData = null;
let _ordersMp = 'WB';
let _ordersCompact = false;   // true → показываем только текущую и прошлую неделю

const _MP_COLORS = { WB: '#c026d3', OZON: '#3b82f6', YM: '#b45309', TOTAL: _G('#059669') };
const _MP_LABEL  = { WB: 'WB (Wildberries)', OZON: 'Ozon', YM: 'Яндекс Маркет', TOTAL: 'Все площадки' };

async function loadSalesAnalytics() {
  await loadOrders();
}

function setOrdersMp(mp) {
  _ordersMp = mp;
  saveOrdersFilters();
  ['WB','OZON','YM','TOTAL'].forEach(m => {
    const el = document.getElementById('ordMp' + m);
    if (el) el.classList.toggle('active', m === mp);
  });
  renderOrdersTable();
}

// ── Режим Заказов: День / Неделя / Месяц — одна и та же таблица ─────────────
let _ordersMode = 'week';
let _ordersDataBy = { days: null, month: null };   // данные orders_matrix

function setOrdersMode(mode) {
  _ordersMode = mode;
  saveOrdersFilters();
  [['ordModeDays', 'days'], ['ordModeWeek', 'week'], ['ordModeMonth', 'month']]
    .forEach(([id, m]) => document.getElementById(id)?.classList.toggle('active', m === mode));
  const week = document.getElementById('ordersWeekCard');
  const month = document.getElementById('ordersMonthlyWrap');
  const days = document.getElementById('ordersDailyWrap');
  if (week) week.style.display = '';
  if (month) month.style.display = 'none';
  if (days) days.style.display = 'none';
  if (mode === 'week' || _ordersDataBy[mode]) renderOrdersTable();
  else loadOrdersMatrix(mode);
}

async function loadOrdersMatrix(mode, refresh) {
  const tbl = document.getElementById('ordersTable');
  if ((!_ordersDataBy[mode] || refresh) && tbl)
    tbl.innerHTML = '<tr><td class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span> Загрузка…</td></tr>';
  try {
    const period = mode === 'days' ? 'day' : 'month';
    _ordersDataBy[mode] = await fetchJSON('/api/dashboard/orders_matrix?period=' + period, 60000);
  } catch (e) {
    if (tbl) tbl.innerHTML = `<tr><td class="text-danger py-3">Ошибка: ${esc(e.message)}</td></tr>`;
    return;
  }
  if (_ordersMode === mode) renderOrdersTable();
}

let _dailyData = null;

async function loadOrdersDaily(refresh) {
  const wrap = document.getElementById('ordersDailyWrap');
  if (!wrap) return;
  if (!_dailyData || refresh) {
    wrap.innerHTML = '<div class="text-center text-secondary py-4"><span class="spinner-border"></span></div>';
    const from = new Date(Date.now() - 30 * 86400e3).toISOString().slice(0, 10);
    try {
      _dailyData = await fetchJSON(`/api/dashboard/sales_history?date_from=${from}`, 60000);
    } catch (e) {
      wrap.innerHTML = `<div class="alert alert-danger">Ошибка: ${esc(e.message)}</div>`;
      return;
    }
  }
  renderOrdersDaily();
}

function renderOrdersDaily() {
  const wrap = document.getElementById('ordersDailyWrap');
  if (!wrap || !_dailyData) return;
  const daily = (_dailyData.daily || []).slice(-30).reverse();   // свежие сверху
  const MPs = [['wb', 'WB', '#c026d3'], ['ozon', 'Ozon', '#3b82f6'], ['ym', 'ЯМ', '#b45309']];
  let html = `<div class="card border-0 bg-card"><div class="card-body p-0"><div class="table-responsive" style="max-height:70vh">
    <table class="table table-sm align-middle mb-0 text-nowrap" style="font-size:.85rem"><thead><tr>
    <th>Дата</th>` +
    MPs.map(([, l, c]) => `<th class="text-end" colspan="2" style="color:${c}">${l}</th>`).join('') +
    `<th class="text-end" colspan="2">Итого</th></tr>
    <tr><th></th>` + Array(4).fill('<th class="text-end text-secondary" style="font-size:.72rem">₽</th><th class="text-end text-secondary" style="font-size:.72rem">шт</th>').join('') +
    `</tr></thead><tbody>`;
  const DOW = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  // итог за период — строкой в стиле недельной таблицы
  const tot = { rub: 0, qty: 0 };
  const mpTot = {};
  daily.forEach(d => MPs.forEach(([k]) => {
    const r = d[k] || 0, q = d[k + '_qty'] || 0;
    (mpTot[k] = mpTot[k] || { r: 0, q: 0 }).r += r; mpTot[k].q += q;
    tot.rub += r; tot.qty += q;
  }));
  html += `<tr style="background:var(--t-mp-row);font-weight:700">
    <td>Итого ${daily.length} дн.</td>` +
    MPs.map(([k]) => `<td class="text-end">${fmtRub((mpTot[k] || {}).r || 0)}</td>` +
                     `<td class="text-end text-secondary">${(mpTot[k] || {}).q || 0}</td>`).join('') +
    `<td class="text-end">${fmtRub(tot.rub)}</td><td class="text-end">${tot.qty}</td></tr>`;
  daily.forEach(d => {
    const dt = new Date(d.date + 'T00:00:00');
    let trub = 0, tqty = 0;
    let cells = '';
    MPs.forEach(([k]) => {
      const r = d[k] || 0, q = d[k + '_qty'] || 0;
      trub += r; tqty += q;
      cells += `<td class="text-end">${r ? fmtRub(r) : '<span class="text-muted">—</span>'}</td>` +
               `<td class="text-end text-secondary">${q || ''}</td>`;
    });
    const wknd = dt.getDay() === 0 || dt.getDay() === 6;
    html += `<tr${wknd ? ' style="background:var(--brand-soft)"' : ''}>
      <td>${d.date.slice(8, 10)}.${d.date.slice(5, 7)} <span class="text-secondary small">${DOW[dt.getDay()]}</span></td>
      ${cells}<td class="text-end fw-semibold">${fmtRub(trub)}</td><td class="text-end fw-semibold">${tqty}</td></tr>`;
  });
  html += '</tbody></table></div></div></div>';
  wrap.innerHTML = html;
}

async function reloadOrders() {
  // Read again without clearing the production dashboard's server cache.
  _ordersData = null;
  _ordersDataBy = { days: null, month: null };
  if (_ordersMode === 'week') await loadOrders();
  else { loadOrders(); await loadOrdersMatrix(_ordersMode, true); }
}

async function loadOrders() {
  const tbl = document.getElementById('ordersTable');
  if (tbl) tbl.innerHTML =
    '<tr><td class="text-center text-secondary py-4"><span class="spinner-border spinner-border-sm"></span> Загрузка…</td></tr>';
  try {
    _ordersData = await fetchJSON('/api/dashboard/weekly_orders');
    renderOrdersTable();
  } catch (e) {
    if (tbl) tbl.innerHTML = `<tr><td class="text-danger py-3">Ошибка: ${esc(e.message)}</td></tr>`;
  }
}

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

  const allMPs = [...MPs, { key: 'total', label: 'Итого', color: _G('#059669') }];

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

// стрелка динамики week-over-week (как в остатках): ▲ рост / ▼ падение
// индекс колонки текущего (незаконченного) периода — по ней динамику не красим:
// 3 дня недели против 7 дней прошлой дают ложное «▼50%»
let _ordPartialIdx = -1;
function _ordCalcPartial(weeks) {
  _ordPartialIdx = -1;
  const n = (weeks || []).length;
  if (!n) return;
  const last = String(weeks[n - 1] || '');
  if (_ordersMode !== 'week') { _ordPartialIdx = n - 1; return; }
  const m = last.match(/(\d{1,2})\.(\d{1,2})\s*$/);
  if (!m) return;
  const now = new Date();
  const end = new Date(now.getFullYear(), +m[2] - 1, +m[1], 23, 59, 59);
  if (end - now > 30 * 864e5) end.setFullYear(end.getFullYear() - 1);
  if (end >= now) _ordPartialIdx = n - 1;
}
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
  if (arr) {
    arr.textContent = expanded ? '▶' : '▼';
    arr.closest('tr')?.setAttribute('aria-expanded', String(!expanded));
  }
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
  'Aloe':             _G('#22c55e'),
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
const _GROUP_PALETTE = ['#0ea5e9', '#a855f7', '#f59e0b', _G('#22c55e'), '#ec4899',
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
  const d = _ordersMode === 'week' ? _ordersData : _ordersDataBy[_ordersMode];
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
  _ordCalcPartial(weeks);
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
    const hasCancel = _ordersMode === 'week' && grpSkus.some(s => s.cancel_rub);
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
function _pctDelta(cur, prev) {
  if (!prev || cur == null) return '';
  const p = Math.round((cur - prev) / Math.abs(prev) * 100);
  if (!p) return '<span class="mc-delta">0%</span>';
  return `<span class="mc-delta ${p > 0 ? 'up' : 'down'}">${p > 0 ? '▲' : '▼'}${Math.abs(p)}%</span>`;
}
function _weekPartial(weeks) {
  const n = (weeks || []).length; if (!n) return false;
  const m = String(weeks[n - 1]).match(/(\d{1,2})\.(\d{1,2})\s*$/);
  if (!m) return true;
  const now = new Date();
  const end = new Date(now.getFullYear(), +m[2] - 1, +m[1], 23, 59, 59);
  if (end - now > 30 * 864e5) end.setFullYear(end.getFullYear() - 1);
  return end >= now;
}
let _ordDaysLoading = false;
function renderOrdersSummary() {
  const box = document.getElementById('ordersSummary');
  if (!box) return;
  const mp = _ordersMp;
  const w = _ordBlockFrom(_ordersData, mp);
  if (!w || !w.n) { box.innerHTML = ''; return; }
  const dd = _ordersDataBy.days;
  if (!dd && !_ordDaysLoading) {
    _ordDaysLoading = true;
    fetchJSON('/api/dashboard/orders_matrix?period=day', 60000)
      .then(d => { _ordersDataBy.days = d; })
      .catch(() => {})
      .finally(() => { _ordDaysLoading = false; if (_ordersDataBy.days) renderOrdersSummary(); });
  }
  const dblk = _ordBlockFrom(dd, mp);
  const card = (head, val, sub) => `<div class="col-6 col-lg-3"><div class="metric-card"><div class="mc-head">${head}</div>
      <div class="mc-val">${val}</div><div class="mc-sub" style="font-size:11px">${sub || ''}</div></div></div>`;
  const cards = [];

  // по дням: вчера и текущая неделя к тем же дням прошлой
  if (dblk && dblk.n >= 9) {
    const n = dblk.n, R = dblk.total_rub, Q = dblk.total_qty, L = dd.weeks;
    const y = n - 2;
    cards.push(card(`Вчера, ${esc(String(L[y]))}`, fmtRub(R[y]),
      `${fmt(Q[y])} шт · к ${esc(String(L[y - 7] || ''))}: ${_pctDelta(R[y], R[y - 7])}`));
    let mon = -1;
    for (let i = n - 1; i >= 0; i--) { if (/пн/i.test(String(L[i]))) { mon = i; break; } }
    if (mon >= 0 && mon <= n - 2 && mon - 7 >= 0) {
      let cr = 0, cq = 0, pr = 0;
      for (let i = mon; i <= n - 2; i++) { cr += R[i]; cq += Q[i]; pr += R[i - 7] || 0; }
      const days = n - 1 - mon;
      cards.push(card(`Эта неделя, ${days} дн.`, fmtRub(cr),
        `${fmt(cq)} шт · к тем же дням прошлой: ${_pctDelta(cr, pr)}`));
    } else if (mon === n - 1) {
      cards.push(card('Эта неделя', fmtRub(R[n - 1]), 'неделя началась сегодня, день ещё идёт'));
    }
  } else if (_ordDaysLoading) {
    cards.push(card('Вчера', '<span class="spinner-border spinner-border-sm text-secondary"></span>', ''));
  }

  // по неделям: прошлая полная неделя к позапрошлой + отмены
  const wk = _ordersData.weeks || [];
  const Lw = _weekPartial(wk) ? w.n - 2 : w.n - 1;
  const Pw = Lw - 1;
  if (Lw >= 0) {
    const r = w.total_rub[Lw], q = w.total_qty[Lw];
    const avg = q ? r / q : 0, avgP = w.total_qty[Pw] ? w.total_rub[Pw] / w.total_qty[Pw] : 0;
    cards.push(card(`Прошлая неделя, ${esc(String(wk[Lw]))}`, fmtRub(r),
      `${fmt(q)} шт · ${_pctDelta(r, w.total_rub[Pw])} · ср. чек ${fmtRub(Math.round(avg))} ${_pctDelta(avg, avgP)}`));
    const cancel = i => w.skus.reduce((a, s) => a + ((s.cancel_rub || [])[i] || 0), 0);
    const cL = cancel(Lw), cP = Pw >= 0 ? cancel(Pw) : 0;
    if (cL > 0 && r) {
      const pc = cL / r * 100, pp = (Pw >= 0 && w.total_rub[Pw]) ? cP / w.total_rub[Pw] * 100 : null;
      const dpp = pp != null ? Math.round(pc - pp) : null;
      cards.push(card('Отменили и вернули, прошлая неделя', `${Math.round(pc)}%`,
        `${fmtRub(Math.round(cL))}` + (dpp != null && dpp !== 0
          ? ` · <span class="mc-delta ${dpp > 0 ? 'down' : 'up'}">${dpp > 0 ? '▲' : '▼'}${Math.abs(dpp)} п.п.</span>` : '')
        + ' <span title="Сумма отмен и возвратов за неделю по дате события к сумме заказов недели">ⓘ</span>'));
    }
  }

  // бренды и лидеры: прошлая полная неделя против позапрошлой
  const groups = {};
  w.skus.forEach(s => {
    const g = articleGroup(s);
    const t = groups[g] || (groups[g] = { r: 0, p: 0, c: 0 });
    t.r += s.rub[Lw] || 0; t.p += (Pw >= 0 ? s.rub[Pw] : 0) || 0; t.c += ((s.cancel_rub || [])[Lw] || 0);
  });
  const totL = w.total_rub[Lw] || 0;
  const gRows = Object.entries(groups).filter(([, t]) => t.r || t.p).sort((a, b) => b[1].r - a[1].r);
  const brandTbl = gRows.length > 1 ? `<div class="card border-0 bg-card h-100"><div class="card-body p-2">
      <div class="fw-semibold small mb-1">Бренды за прошлую неделю</div>
      <div class="table-responsive"><table class="table table-sm mb-0 ord-sum-tbl"><thead><tr>
        <th>Бренд</th><th class="text-end">₽</th><th class="text-end">к пред.</th><th class="text-end">доля</th>${gRows.some(([, t]) => t.c) ? '<th class="text-end">отмены</th>' : ''}
      </tr></thead><tbody>${gRows.slice(0, 12).map(([g, t]) => `<tr>
        <td><span class="me-1" style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${_groupColor(g)}"></span>${esc(g)}</td>
        <td class="text-end">${_fmtShort(t.r)}</td><td class="text-end">${_pctDelta(t.r, t.p)}</td>
        <td class="text-end">${totL ? Math.round(t.r / totL * 100) : 0}%</td>${gRows.some(([, x]) => x.c) ? `<td class="text-end">${t.r && t.c ? Math.round(t.c / t.r * 100) + '%' : '—'}</td>` : ''}
      </tr>`).join('')}</tbody></table></div></div></div>` : '';
  const movers = w.skus.map(s => ({ s, cur: s.rub[Lw] || 0, prev: (Pw >= 0 ? s.rub[Pw] : 0) || 0 }))
    .map(x => ({ ...x, d: x.cur - x.prev })).filter(x => x.d);
  const up = movers.filter(x => x.d > 0).sort((a, b) => b.d - a.d).slice(0, 5);
  const down = movers.filter(x => x.d < 0).sort((a, b) => a.d - b.d).slice(0, 5);
  const mvList = (arr, title, cls) => `<div class="fw-semibold small mb-1 ${cls}">${title}</div>` + (arr.length ? arr.map(x => `
      <div class="d-flex align-items-baseline gap-2 py-1 border-bottom small" style="border-color:var(--border)!important">
        <span class="text-truncate" style="min-width:0;flex:1" title="${esc(x.s.name || '')}"><b>${esc(x.s.sku)}</b> <span class="text-secondary">${esc(x.s.name || '')}</span></span>
        <span class="text-nowrap">${_fmtShort(x.cur)}</span>
        <span class="text-nowrap mc-delta ${x.d > 0 ? 'up' : 'down'}" style="min-width:64px;text-align:right">${x.d > 0 ? '+' : '−'}${_fmtShort(Math.abs(x.d))}</span>
      </div>`).join('') : '<div class="small text-secondary">—</div>');
  const moversCard = movers.length ? `<div class="card border-0 bg-card h-100"><div class="card-body p-2">
      <div class="small text-secondary mb-1">Артикулы: прошлая неделя против позапрошлой, ₽</div>
      <div class="row g-3"><div class="col-12 col-md-6">${mvList(up, '▲ Рост', 'text-success')}</div>
      <div class="col-12 col-md-6">${mvList(down, '▼ Падение', 'text-danger')}</div></div></div></div>` : '';

  box.innerHTML = `
    <div class="row g-2 mb-2">${cards.join('')}</div>
    <div class="card border-0 bg-card mb-2"><div class="card-body p-2">
      <div class="d-flex align-items-baseline gap-2 mb-1"><span class="fw-semibold small">Заказы по дням, ₽</span>
        <span class="small text-secondary">последние 14 дней · сегодняшний день ещё идёт (светлый)</span></div>
      <div style="position:relative;height:190px"><canvas id="ordDaysChart"></canvas></div>
    </div></div>
    <div class="row g-2 mb-3">
      ${brandTbl ? `<div class="col-12 col-lg-5">${brandTbl}</div>` : ''}
      ${moversCard ? `<div class="col-12 ${brandTbl ? 'col-lg-7' : ''}">${moversCard}</div>` : ''}
    </div>`;
  _renderOrdDaysChart(dd, mp);
}
function _renderOrdDaysChart(dd, mp) {
  const cv = document.getElementById('ordDaysChart');
  if (charts.ordDays) { try { charts.ordDays.destroy(); } catch (e) {} charts.ordDays = null; }
  if (!cv || !dd) return;
  const labels = (dd.weeks || []).map(String);
  const n = labels.length;
  const mps = mp === 'TOTAL' ? ['WB', 'OZON', 'YM'].filter(m => dd[m] && (dd[m].total_rub || []).some(v => v)) : [mp];
  const datasets = mps.map(m => {
    const c = _MP_COLORS[m] || '#6366f1';
    return { label: { WB: 'WB', OZON: 'Ozon', YM: 'ЯМ' }[m] || m, data: (dd[m]?.total_rub || Array(n).fill(0)),
             backgroundColor: labels.map((_, i) => i === n - 1 ? c + '55' : c), borderRadius: 4, stack: 's' };
  });
  charts.ordDays = new Chart(cv.getContext('2d'), {
    type: 'bar', data: { labels, datasets },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: mps.length > 1, position: 'bottom', labels: { boxWidth: 10, font: { size: 11 } } },
                 tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtRub(c.raw)}` } } },
      scales: { x: { stacked: true, grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 0, autoSkip: true } },
                y: { stacked: true, ticks: { font: { size: 10 }, callback: v => _fmtShort(v) }, grid: { color: 'rgba(127,127,127,.12)' } } } },
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
    for (const mp of ['WB', 'OZON', 'YM', 'TOTAL']) {
      const available = mp === 'TOTAL' ? marketplaces.length > 1 : marketplaces.includes(mp);
      document.getElementById('ordMp' + mp).hidden = !available;
    }
    const select = document.getElementById('ordersBrand');
    const brands = new Set(cabinet.brand_order || cabinet.group_order || BRAND_ORDER);
    brands.delete('');
    brands.delete('Прочее');
    if (brand) brands.add(brand);
    for (const name of brands) select.add(new Option(name, name));
    select.value = brand;
    select.hidden = false;
    select.addEventListener('change', () => {
      filters.set('brand', select.value);
      saveOrdersFilters();
      location.reload();
    });

    const requestedMp = filters.get('mp');
    const mp = marketplaces.includes(requestedMp) || (requestedMp === 'TOTAL' && marketplaces.length > 1)
      ? requestedMp : marketplaces[0] || 'WB';
    const requestedMode = filters.get('mode');
    setOrdersMp(mp);
    if (['days', 'month'].includes(requestedMode)) setOrdersMode(requestedMode);
    Chart.defaults.color = CHART_C.tick;
    await loadSalesAnalytics();
  } catch (error) {
    document.getElementById('pane-salesan').hidden = true;
    const message = document.getElementById('ordersAccessError');
    message.textContent = error.message;
    message.hidden = false;
  }
}

initDevelopmentOrders();
