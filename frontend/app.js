'use strict';

const API = '';
let charts = {};
let sortState = {};

// ── Auth ──────────────────────────────────────────────────────────────────────

const CREDS = { user: 'admin', pass: 'admin' };

function showOverlay(name) {
  document.getElementById('loginOverlay').style.display   = name === 'login'   ? 'flex' : 'none';
  document.getElementById('cabinetOverlay').style.display = name === 'cabinet' ? 'flex' : 'none';
  const showApp = name === 'app';
  document.getElementById('mainNav').style.display     = showApp ? 'flex' : 'none';
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
  document.getElementById('dateTo').value = toISO(to);
  document.getElementById('dateFrom').value = toISO(from);
}

function getDateParams() {
  const from = document.getElementById('dateFrom').value;
  const to   = document.getElementById('dateTo').value;
  return from && to ? `date_from=${from}&date_to=${to}` : `days=30`;
}

// ── Fetch ─────────────────────────────────────────────────────────────────────

async function fetchJSON(path) {
  const params = getDateParams();
  const r = await fetch(`${API}${path}?${params}`);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function fmt(n, decimals = 0) {
  if (n == null) return '—';
  return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
function fmtRub(n) { return fmt(n) + ' ₽'; }

// ── KPI ───────────────────────────────────────────────────────────────────────

async function loadKPI() {
  try {
    const d = await fetchJSON('/api/dashboard/kpi');
    document.getElementById('kpi-revenue').textContent = fmtRub(d.total_revenue);
    document.getElementById('kpi-orders').textContent = fmt(d.total_orders);
    document.getElementById('kpi-buyout').textContent = `выкуп: ${d.buyout_rate}%`;
    document.getElementById('kpi-sales').textContent = fmt(d.total_sales);
    document.getElementById('kpi-stock').textContent = fmtRub(d.stock_value);
  } catch (e) { console.error('KPI error', e); }
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
            type: 'bar',
            label: 'Выручка ₽',
            data: data.map(r => r.revenue),
            backgroundColor: 'rgba(201,168,76,0.45)',
            borderColor: 'rgba(201,168,76,0.9)',
            borderWidth: 1,
            yAxisID: 'y',
          },
          {
            type: 'line',
            label: 'Заказы',
            data: data.map(r => r.orders_count),
            borderColor: '#38bdf8',
            backgroundColor: 'rgba(56,189,248,0.1)',
            pointRadius: 2,
            tension: 0.4,
            yAxisID: 'y1',
          },
          {
            type: 'line',
            label: 'Продажи, шт',
            data: data.map(r => r.sales_count),
            borderColor: '#4ade80',
            backgroundColor: 'rgba(74,222,128,0.1)',
            pointRadius: 2,
            tension: 0.4,
            borderDash: [4, 3],
            yAxisID: 'y1',
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { color: '#94a3b8' } } },
        scales: {
          x: { ticks: { color: '#64748b', maxRotation: 45 }, grid: { color: '#1e2235' } },
          y: { position: 'left', ticks: { color: '#94a3b8', callback: v => fmt(v) + ' ₽' }, grid: { color: '#1e2235' } },
          y1: { position: 'right', ticks: { color: '#38bdf8' }, grid: { drawOnChartArea: false } },
        },
      },
    });
  } catch (e) { console.error('salesChart error', e); }
}

// ── Top SKU chart ─────────────────────────────────────────────────────────────

async function loadTopSkuChart() {
  try {
    const data = (await fetchJSON('/api/dashboard/top-skus')).slice(0, 10);
    const ctx = document.getElementById('topSkuChart').getContext('2d');
    if (charts.topSku) charts.topSku.destroy();
    const labels = data.map(r => {
      const art = r.supplierArticle || r.nmId;
      const name = r.subject.length > 16 ? r.subject.slice(0, 16) + '…' : r.subject;
      return `${art} · ${name}`;
    });
    charts.topSku = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Выручка ₽',
          data: data.map(r => r.total_revenue),
          backgroundColor: data.map((_, i) => `hsl(${42 + i * 8},70%,${58 - i * 2}%)`),
        }],
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#64748b', callback: v => fmt(v) }, grid: { color: '#1e2235' } },
          y: { ticks: { color: '#94a3b8', font: { size: 11 } } },
        },
      },
    });
  } catch (e) { console.error('topSkuChart error', e); }
}

// ── ABC revenue ───────────────────────────────────────────────────────────────

async function loadAbcRevenue() {
  try {
    const data = await fetchJSON('/api/dashboard/abc-revenue');
    const groups = { A: 0, B: 0, C: 0 };
    data.forEach(r => { groups[r.abc_category] = (groups[r.abc_category] || 0) + r.total_revenue; });
    const ctx = document.getElementById('abcRevPie').getContext('2d');
    if (charts.abcPie) charts.abcPie.destroy();
    charts.abcPie = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['A', 'B', 'C'],
        datasets: [{ data: [groups.A, groups.B, groups.C], backgroundColor: ['#16a34a', '#2563eb', '#64748b'] }],
      },
      options: { plugins: {
        legend: { labels: { color: '#e2e8f0' } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmtRub(ctx.raw)}` } },
      }},
    });
    renderTable('abcRevBody', data, row => `
      <td>${row.nmId}</td>
      <td>${row.supplierArticle || row.nmId}</td>
      <td>${row.subject}</td>
      <td>${fmtRub(row.total_revenue)}</td>
      <td>${fmt(row.revenue_share, 2)}%</td>
      <td>${fmt(row.cumulative_share, 2)}%</td>
      <td><span class="badge badge-${row.abc_category}">${row.abc_category}</span></td>`);
  } catch (e) { console.error('abcRevenue error', e); }
}

// ── ABC turnover ──────────────────────────────────────────────────────────────

async function loadAbcTurnover() {
  try {
    const data = await fetchJSON('/api/dashboard/abc-turnover');
    renderTable('abcTurnBody', data, row => {
      const cls = row.coverage_days < 14 ? 'coverage-low' : row.coverage_days < 30 ? 'coverage-med' : 'coverage-ok';
      const cov = row.coverage_days >= 999 ? '∞' : fmt(row.coverage_days, 1);
      return `
        <td>${row.nmId}</td>
        <td>${row.supplierArticle || row.nmId}</td>
        <td>${row.subject}</td>
        <td>${fmt(row.stock_qty)}</td>
        <td>${fmt(row.avg_daily_sales, 2)}</td>
        <td class="${cls}">${cov}</td>
        <td><span class="badge badge-${row.abc_category}">${row.abc_category}</span></td>`;
    });
  } catch (e) { console.error('abcTurnover error', e); }
}

// ── Reorder ───────────────────────────────────────────────────────────────────

async function loadReorder() {
  try {
    const data = await fetchJSON('/api/dashboard/reorder');
    renderTable('reorderBody', data, row => {
      const b = v => v > 0 ? `<span class="need-badge">${fmt(v)}</span>` : `<span class="text-secondary">0</span>`;
      return `
        <td>${row.nmId}</td>
        <td>${row.supplierArticle || row.nmId}</td>
        <td>${row.subject}</td>
        <td>${fmt(row.stock_qty)}</td>
        <td>${fmt(row.avg_daily_sales, 2)}</td>
        <td>${b(row.need_30d)}</td>
        <td>${b(row.need_60d)}</td>
        <td>${b(row.need_90d)}</td>`;
    });
  } catch (e) { console.error('reorder error', e); }
}

// ── Table helpers ─────────────────────────────────────────────────────────────

function renderTable(tbodyId, data, rowFn) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody._data = data;
  tbody._rowFn = rowFn;
  tbody.innerHTML = data.map(r => `<tr>${rowFn(r)}</tr>`).join('');
}

function initSortable(tableEl) {
  tableEl.querySelectorAll('thead th[data-col]').forEach(th => {
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

// ── Load dashboard ────────────────────────────────────────────────────────────

async function loadAll() {
  document.getElementById('lastUpdated').textContent = 'Загрузка…';
  await Promise.all([loadKPI(), loadSalesChart(), loadTopSkuChart(), loadAbcRevenue(), loadAbcTurnover(), loadReorder()]);
  document.getElementById('lastUpdated').textContent = 'Обновлено: ' + new Date().toLocaleTimeString('ru-RU');
}

function initDashboard() {
  initDates(30);
  document.querySelectorAll('.sortable-table').forEach(initSortable);

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

  document.getElementById('refreshBtn').addEventListener('click', loadAll);

  loadAll();
  setInterval(loadAll, 5 * 60 * 1000);
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Auth events
  document.getElementById('loginBtn').addEventListener('click', doLogin);
  ['loginUser', 'loginPass'].forEach(id => {
    document.getElementById(id).addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  });
  document.getElementById('cabBiomed').addEventListener('click', enterCabinet);
  document.getElementById('changeCabBtn').addEventListener('click', changeCabinet);

  // Restore session
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