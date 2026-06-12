'use strict';
const API = '';
let charts = {};
let sortState = {};

async function fetchJSON(path) {
  const days = document.getElementById('daysSelect').value;
  const r = await fetch(`${API}${path}?days=${days}`);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}
function fmt(n, decimals = 0) {
  if (n == null) return '—';
  return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
function fmtRub(n) { return fmt(n) + ' ₽'; }

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

async function loadSalesChart() {
  try {
    const data = await fetchJSON('/api/dashboard/sales-dynamics');
    const ctx = document.getElementById('salesChart').getContext('2d');
    if (charts.sales) charts.sales.destroy();
    charts.sales = new Chart(ctx, {
      data: {
        labels: data.map(r => r.date),
        datasets: [
          { type: 'bar', label: 'Выручка ₽', data: data.map(r => r.revenue), backgroundColor: 'rgba(168,85,247,0.5)', borderColor: 'rgba(168,85,247,0.9)', borderWidth: 1, yAxisID: 'y' },
          { type: 'line', label: 'Заказы', data: data.map(r => r.orders_count), borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,0.1)', pointRadius: 3, tension: 0.4, yAxisID: 'y1' },
        ],
      },
      options: {
        responsive: true, interaction: { mode: 'index', intersect: false },
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

async function loadTopSkuChart() {
  try {
    const data = (await fetchJSON('/api/dashboard/top-skus')).slice(0, 10);
    const ctx = document.getElementById('topSkuChart').getContext('2d');
    if (charts.topSku) charts.topSku.destroy();
    charts.topSku = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.map(r => r.subject.length > 22 ? r.subject.slice(0, 22) + '…' : r.subject),
        datasets: [{ label: 'Выручка ₽', data: data.map(r => r.total_revenue), backgroundColor: data.map((_, i) => `hsl(${260 + i * 10},70%,${60 - i * 3}%)`) }],
      },
      options: {
        indexAxis: 'y', responsive: true, plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#64748b', callback: v => fmt(v) }, grid: { color: '#1e2235' } },
          y: { ticks: { color: '#94a3b8', font: { size: 11 } } },
        },
      },
    });
  } catch (e) { console.error('topSkuChart error', e); }
}

async function loadAbcRevenue() {
  try {
    const data = await fetchJSON('/api/dashboard/abc-revenue');
    const groups = { A: 0, B: 0, C: 0 };
    data.forEach(r => { groups[r.abc_category] = (groups[r.abc_category] || 0) + r.total_revenue; });
    const ctx = document.getElementById('abcRevPie').getContext('2d');
    if (charts.abcPie) charts.abcPie.destroy();
    charts.abcPie = new Chart(ctx, {
      type: 'doughnut',
      data: { labels: ['A', 'B', 'C'], datasets: [{ data: [groups.A, groups.B, groups.C], backgroundColor: ['#16a34a', '#2563eb', '#64748b'] }] },
      options: { plugins: { legend: { labels: { color: '#e2e8f0' } }, tooltip: { callbacks: { label: c => ` ${c.label}: ${fmtRub(c.raw)}` } } } },
    });
    renderTable('abcRevBody', data, r => `<td>${r.nmId}</td><td>${r.subject}</td><td>${fmtRub(r.total_revenue)}</td><td>${fmt(r.revenue_share,2)}%</td><td>${fmt(r.cumulative_share,2)}%</td><td><span class="badge badge-${r.abc_category}">${r.abc_category}</span></td>`);
  } catch (e) { console.error('abcRevenue error', e); }
}

async function loadAbcTurnover() {
  try {
    const data = await fetchJSON('/api/dashboard/abc-turnover');
    renderTable('abcTurnBody', data, r => {
      const cls = r.coverage_days < 14 ? 'coverage-low' : r.coverage_days < 30 ? 'coverage-med' : 'coverage-ok';
      const cov = r.coverage_days >= 999 ? '∞' : fmt(r.coverage_days, 1);
      return `<td>${r.nmId}</td><td>${r.subject}</td><td>${fmt(r.stock_qty)}</td><td>${fmt(r.avg_daily_sales,2)}</td><td class="${cls}">${cov}</td><td><span class="badge badge-${r.abc_category}">${r.abc_category}</span></td>`;
    });
  } catch (e) { console.error('abcTurnover error', e); }
}

async function loadReorder() {
  try {
    const data = await fetchJSON('/api/dashboard/reorder');
    renderTable('reorderBody', data, r => {
      const nb = v => v > 0 ? `<span class="need-badge">${fmt(v)}</span>` : `<span class="text-secondary">0</span>`;
      return `<td>${r.nmId}</td><td>${r.subject}</td><td>${fmt(r.stock_qty)}</td><td>${fmt(r.avg_daily_sales,2)}</td><td>${nb(r.need_30d)}</td><td>${nb(r.need_60d)}</td><td>${nb(r.need_90d)}</td>`;
    });
  } catch (e) { console.error('reorder error', e); }
}

function renderTable(tbodyId, data, rowFn) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody._data = data; tbody._rowFn = rowFn;
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
        return asc ? String(va).localeCompare(String(vb),'ru') : String(vb).localeCompare(String(va),'ru');
      });
      tbody._data = sorted;
      tbody.innerHTML = sorted.map(r => `<tr>${tbody._rowFn(r)}</tr>`).join('');
    });
  });
}

async function loadAll() {
  document.getElementById('lastUpdated').textContent = 'Загрузка…';
  await Promise.all([loadKPI(), loadSalesChart(), loadTopSkuChart(), loadAbcRevenue(), loadAbcTurnover(), loadReorder()]);
  document.getElementById('lastUpdated').textContent = 'Обновлено: ' + new Date().toLocaleTimeString('ru-RU');
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.sortable-table').forEach(initSortable);
  document.getElementById('refreshBtn').addEventListener('click', loadAll);
  document.getElementById('daysSelect').addEventListener('change', loadAll);
  loadAll();
  setInterval(loadAll, 5 * 60 * 1000);
});
