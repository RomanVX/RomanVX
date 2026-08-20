// WMS Market Partners — фаза 1. Отдельное приложение (см. docs/wms_spec.md).
'use strict';

let W = { user: null, clients: [], clientId: null, skus: [], inbounds: [] };

const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
const rub = n => (Math.round(Number(n) || 0)).toLocaleString('ru-RU') + ' ₽';

async function api(path, opts) {
  const r = await fetch('/api/wms' + path, Object.assign({
    headers: { 'Content-Type': 'application/json' } }, opts));
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
  return j;
}

// ── Вход ────────────────────────────────────────────────────────────────────
async function wmsDoLogin() {
  $('wLoginErr').textContent = '';
  try {
    W.user = await api('/auth/login', { method: 'POST', body: JSON.stringify({
      login: $('wLogin').value.trim(), password: $('wPass').value }) });
    boot();
  } catch (e) { $('wLoginErr').textContent = e.message; }
}
async function wmsLogout() {
  await api('/auth/logout', { method: 'POST' }).catch(() => {});
  location.reload();
}

async function boot() {
  $('wmsLogin').style.display = 'none';
  $('wmsApp').style.display = 'block';
  const u = W.user;
  $('wWho').textContent = u.role === 'client'
    ? (u.client_name || 'клиент') : u.login;
  const tabs = u.role === 'client'
    ? [['stock', 'Остатки'], ['moves', 'Движения'], ['inbounds', 'Поставки'], ['billing', 'Начисления']]
    : [['receive', 'Приёмка'], ['stock', 'Остатки'], ['ship', 'Отгрузка'],
       ['moves', 'Движения'], ['ops', 'Возврат/корр.'], ['billing', 'Начисления'], ['clients', 'Клиенты']];
  $('wNav').innerHTML = tabs.map(([k, label]) =>
    `<button id="wtab-${k}" onclick="go('${k}')">${label}</button>`).join('');
  if (u.role === 'staff') {
    try { W.clients = (await api('/clients')).clients; } catch (e) { W.clients = []; }
    if (W.clients.length && !W.clientId) W.clientId = W.clients[0].id;
  }
  go(tabs[0][0]);
}

function go(tab) {
  document.querySelectorAll('.w-nav button').forEach(b =>
    b.classList.toggle('active', b.id === 'wtab-' + tab));
  ({ stock: vStock, inbounds: vInbounds, billing: vBilling, receive: vReceive,
     ship: vShip, ops: vOps, clients: vClients, moves: vMoves })[tab]();
}

// селектор клиента для сотрудника
function clientPicker() {
  if (W.user.role !== 'staff') return '';
  return `<div class="w-row" style="margin-bottom:12px"><div>
    <div class="w-label">Клиент</div>
    <select onchange="W.clientId=parseInt(this.value);go(document.querySelector('.w-nav button.active').id.slice(5))">
    ${W.clients.map(c => `<option value="${c.id}" ${c.id === W.clientId ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
    </select></div></div>`;
}
const cidQ = () => W.user.role === 'staff' ? ('?client_id=' + (W.clientId || '')) : '';

// ── Остатки ─────────────────────────────────────────────────────────────────
async function vStock() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + '<div class="w-card">Загружаю…</div>';
  let d;
  try { d = await api('/stock' + cidQ()); }
  catch (e) { m.innerHTML = clientPicker() + `<div class="w-card w-err">${esc(e.message)}</div>`; return; }
  const rows = d.stock || [];
  const totalAv = rows.reduce((s, r) => s + r.available, 0);
  m.innerHTML = clientPicker() + `<div class="w-card">
    <div class="w-h">Остатки <span class="w-sub" style="display:inline">· доступно ${totalAv} шт</span></div>
    <div class="w-sub">Раскрой строку — партии с возрастом. Зелёное «бесплатно N дн» — партия в бесплатном периоде хранения.</div>
    <div class="w-table-wrap"><table class="w-table"><thead><tr>
      <th>Артикул</th><th>Название</th><th class="w-num">Доступно</th>
      <th class="w-num">Брак/карантин</th><th>Партии</th>
    </tr></thead><tbody>
    ${rows.map((r, i) => `<tr>
      <td><b>${esc(r.sku)}</b></td>
      <td>${esc(r.name || '')}</td>
      <td class="w-num"><b>${r.available}</b></td>
      <td class="w-num">${r.quarantine ? `<span class="w-pill bad">${r.quarantine}</span>` : '—'}</td>
      <td>
        <button class="w-btn w-btn-ghost" style="padding:4px 10px" onclick="const b=$('wb${i}');b.style.display=b.style.display==='none'?'block':'none'">партии (${r.batches.length})</button>
        <div id="wb${i}" class="w-batches" style="display:none">
          ${r.batches.map(b => `<div>№${esc(b.batch_no || b.batch_id)} · ${b.qty} шт · принято ${esc(b.received_at || '—')}${b.expiry ? ' · СГ до ' + esc(b.expiry) : ''} ·
            ${b.free_days_left == null ? '' : b.free_days_left > 0
              ? `<span class="w-free">бесплатно ещё ${b.free_days_left} дн</span>`
              : `<span class="w-paid">платное хранение (возраст ${b.age_days} дн)</span>`}</div>`).join('')}
        </div>
      </td></tr>`).join('') || '<tr><td colspan="5">Пока пусто — примите первую поставку</td></tr>'}
    </tbody></table></div></div>`;
}


// компактная форма заявки для склада («создать за клиента»)
function asnForm(skus, compact) {
  if (!skus.length) return '<div class="w-sub">У клиента нет товаров — сначала загрузите справочник (Клиенты → товары)</div>';
  return `<div class="w-table-wrap" style="max-height:340px;overflow-y:auto"><table class="w-table">
    <thead><tr><th>Артикул</th><th>Название</th><th style="width:110px">Кол-во</th></tr></thead><tbody>
    ${skus.map(sk => `<tr>
      <td><b>${esc(sk.code)}</b></td>
      <td class="w-sub" style="font-size:14px">${esc(sk.name || '')}</td>
      <td><input type="number" inputmode="numeric" min="0" placeholder="0" class="asn-qty" data-sku="${esc(sk.code)}" style="width:95px"></td>
    </tr>`).join('')}
    </tbody></table></div>
    <div class="w-row" style="margin-top:10px">
      <div><div class="w-label">Дата поставки</div><input id="wAsnDate" type="date" /></div>
      <div><div class="w-label">Комментарий</div><input id="wAsnNote" placeholder="машина, паллеты…" /></div>
    </div>
    <button class="w-btn w-btn-primary${compact ? '' : ' w-btn-big'}" style="margin-top:10px" onclick="asnCreate()">Создать заявку</button>`;
}

// ── Поставки: таблица-список (как в WB), карточка поставки, мастер ──────────
async function vInbounds() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + '<div class="w-card">Загружаю…</div>';
  let d;
  try { d = await api('/inbounds' + cidQ()); }
  catch (e) { m.innerHTML = `<div class="w-card w-err">${esc(e.message)}</div>`; return; }
  let sk = [];
  try { sk = (await api('/skus' + cidQ())).skus; } catch (e) {}
  W.skus = sk; W.inbounds = d.inbounds || [];
  const opened = W.openInbound && W.inbounds.find(x => x.id === W.openInbound);
  if (opened) {
    m.innerHTML = clientPicker() + supplyCard(opened);
    return;
  }
  W.openInbound = null;
  m.innerHTML = clientPicker() + `
    <div id="wWiz"></div>
    <div class="w-card">
      <div class="w-row" style="justify-content:space-between;align-items:center">
        <div class="w-h" style="margin:0">Поставки</div>
        <div style="flex:0;white-space:nowrap">
          <button class="w-btn w-btn-primary" onclick="wizOpen()">+ Новая поставка</button>
        </div>
      </div>
      <div class="w-row" style="margin:10px 0 4px">
        <input class="w-search" placeholder="Номер поставки, артикул или ШК короба"
          value="${esc(W.supSearch || '')}" oninput="W.supSearch=this.value;supListRender()">
      </div>
      <div id="wSupList"></div>
    </div>`;
  supListRender();
}

function supplyRowsHtml(list) {
  return `<div class="w-table-wrap"><table class="w-table"><thead><tr>
    <th>Номер и тип</th><th>Дата поставки</th><th>Статус</th>
    <th class="w-num">Заявлено, шт</th><th class="w-num">Разложено → Принято</th>
  </tr></thead><tbody>
  ${list.map(ib => {
    const asked  = (ib.lines || []).reduce((s, l) => s + (l.qty_expected || 0), 0);
    const packed = (ib.boxes || []).reduce((s, b) => s + b.items.reduce((q, i) => q + i.qty, 0), 0);
    const recv   = (ib.lines || []).reduce((s, l) => s + (l.qty_received || 0), 0);
    const st = ib.status === 'done'
      ? '<span class="w-pill ok">принята</span>'
      : '<span class="w-pill warn">ожидается</span>';
    return `<tr class="w-sup-row" onclick="openInbound(${ib.id})">
      <td><b>№${ib.id}</b><div class="w-sub" style="margin:0">${ib.pack_type === 'mono' ? 'Монопаллета' : 'Короб'}${W.user.role !== 'client' ? ' · ' + esc(ib.client || '') : ''}</div></td>
      <td>${esc(ib.expected_date || (ib.created_at || '').slice(0, 10))}</td>
      <td>${st}</td>
      <td class="w-num"><b>${asked || packed || '—'}</b></td>
      <td class="w-num">${packed}${ib.status === 'done' ? ` → <b style="color:var(--ok)">${recv}</b>` : ''}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="5" class="w-sub">Пусто</td></tr>'}
  </tbody></table></div>`;
}

function supListRender() {
  const q = (W.supSearch || '').trim().toLowerCase();
  const match = ib => !q || String(ib.id).includes(q) ||
    (ib.lines || []).some(l => (l.sku || '').toLowerCase().includes(q)) ||
    (ib.boxes || []).some(b => (b.code || '').toLowerCase().includes(q) ||
      b.items.some(i => (i.sku || '').toLowerCase().includes(q)));
  const open = W.inbounds.filter(i => i.status !== 'done' && match(i));
  const hist = W.inbounds.filter(i => i.status === 'done' && match(i));
  const el = $('wSupList');
  if (el) el.innerHTML = supplyRowsHtml(open) +
    (hist.length ? `<div class="w-h" style="margin-top:18px;font-size:16px">История</div>${supplyRowsHtml(hist)}` : '');
}

function openInbound(id) { W.openInbound = id; W.packSearch = ''; go(curTab()); }
function closeInbound() { W.openInbound = null; go(curTab()); }

// карточка поставки (клиентский экран «Упаковка и печать ШК»)
function supplyCard(ib) {
  const st = ib.status === 'done'
    ? '<span class="w-pill ok">принята</span>'
    : '<span class="w-pill warn">ожидается</span>';
  return `<div class="w-card">
    <div class="w-row" style="align-items:center;gap:12px">
      <div style="flex:0"><button class="w-btn w-btn-ghost" onclick="closeInbound()">← Поставки</button></div>
      <div class="w-h" style="margin:0;flex:1">Поставка №${ib.id} ${st}</div>
    </div>
    <div class="w-sub">${ib.pack_type === 'mono' ? 'Монопаллета' : 'Короб'}${ib.expected_date ? ' · на ' + esc(ib.expected_date) : ''} · создана ${esc((ib.created_at || '').slice(0, 10))}${W.user.role !== 'client' ? ' · ' + esc(ib.client || '') : ''}${ib.note ? ' · ' + esc(ib.note) : ''}${ib.act_no ? ' · акт ' + esc(ib.act_no) : ''}</div>
    ${(ib.lines || []).length ? `<table class="w-table" style="table-layout:fixed"><thead><tr>
      <th style="width:26%">Артикул</th>
      <th style="width:110px;text-align:center">Заявлено</th>
      <th style="width:110px;text-align:center">Принято</th>
      <th>Расхождение</th></tr></thead><tbody>
    ${ib.lines.map(l => `<tr>
      <td><b>${esc(l.sku)}</b></td>
      <td style="text-align:center">${l.qty_expected}</td>
      <td style="text-align:center">${ib.status === 'done' ? `<b>${l.qty_received}</b>` : '—'}</td>
      <td>${l.discrepancy_type ? `<span class="w-pill bad">${esc(l.discrepancy_type)} ${l.discrepancy_qty > 0 ? '+' : ''}${l.discrepancy_qty || ''}</span>` : ''}</td>
    </tr>`).join('')}
    </tbody></table>` : ''}
    ${boxesSection(ib)}
  </div>`;
}

// ── Мастер «Новая поставка»: 1 товары → 2 упаковка → 3 дата и создание ──────
let WZ = null;
function wizOpen() {
  WZ = { step: 1, qty: {}, search: '', pack: 'box', boxes: 5, pallets: 1, perPallet: 4,
    date: '', note: '' };
  wizRender();
  $('wWiz').scrollIntoView({ behavior: 'smooth' });
}
function wizClose() { WZ = null; $('wWiz').innerHTML = ''; }
const wizUnits = () => Object.values(WZ.qty).reduce((s, q) => s + q, 0);
const wizSkus = () => Object.keys(WZ.qty).length;

function wizRender() {
  if (!WZ) return;
  const steps = [[1, 'Товары'], [2, 'Упаковка'], [3, 'Дата и создание']];
  const stepper = `<div class="w-steps">${steps.map(([n, t]) =>
    `<div class="st ${WZ.step === n ? 'cur' : WZ.step > n ? 'done' : ''}"><span class="n">${WZ.step > n ? '✓' : n}</span>${t}</div>`).join('')}</div>`;
  let body = '';
  if (WZ.step === 1) {
    const q = WZ.search.trim().toLowerCase();
    const list = (W.skus || []).filter(s => !q ||
      s.code.toLowerCase().includes(q) || (s.name || '').toLowerCase().includes(q));
    body = `
      <div class="w-row">
        <input class="w-search" placeholder="Поиск по артикулу или названию" value="${esc(WZ.search)}"
          oninput="WZ.search=this.value;wizRenderKeepFocus(this)">
        <div class="w-count">Добавлено: ${wizSkus()} товаров · ${wizUnits()} шт</div>
      </div>
      <div class="w-table-wrap" style="max-height:400px;overflow-y:auto;margin-top:8px"><table class="w-table">
        <thead><tr><th>Артикул</th><th>Название</th><th style="width:110px">Кол-во</th></tr></thead><tbody>
        ${list.map(sk => `<tr>
          <td><b>${esc(sk.code)}</b>${sk.requires_expiry ? ' <span class="w-pill warn" title="потребуется срок годности">СГ</span>' : ''}</td>
          <td class="w-sub" style="font-size:14px">${esc(sk.name || '')}</td>
          <td><input type="number" inputmode="numeric" min="0" placeholder="0" style="width:95px"
            value="${WZ.qty[sk.code] || ''}"
            oninput="const v=parseInt(this.value,10)||0;if(v>0)WZ.qty['${esc(sk.code)}']=v;else delete WZ.qty['${esc(sk.code)}'];wizCount()"></td>
        </tr>`).join('') || '<tr><td colspan="3">Ничего не найдено</td></tr>'}
        </tbody></table></div>`;
  } else if (WZ.step === 2) {
    body = `
      <div class="w-choices">
        <div class="w-choice ${WZ.pack === 'box' ? 'sel' : ''}" onclick="WZ.pack='box';wizRender()">
          <div class="t">📦 Короба</div>
          <div class="d">Поставка коробами — у каждого свой штрихкод, товар раскладывается по коробам</div>
        </div>
        <div class="w-choice ${WZ.pack === 'mono' ? 'sel' : ''}" onclick="WZ.pack='mono';wizRender()">
          <div class="t">🟫 Монопаллеты</div>
          <div class="d">Крупная поставка на палетах — на каждой палете свои короба, ШК у палет и коробов</div>
        </div>
      </div>
      <div class="w-row" style="margin-top:12px">
        ${WZ.pack === 'box'
          ? `<div><div class="w-label">Сколько коробов создать</div>
              <input type="number" inputmode="numeric" min="0" max="200" value="${WZ.boxes}"
                oninput="WZ.boxes=parseInt(this.value,10)||0"></div>
             <div></div>`
          : `<div><div class="w-label">Палет</div>
              <input type="number" inputmode="numeric" min="1" max="50" value="${WZ.pallets}"
                oninput="WZ.pallets=parseInt(this.value,10)||0"></div>
             <div><div class="w-label">Коробов на палете</div>
              <input type="number" inputmode="numeric" min="1" max="100" value="${WZ.perPallet}"
                oninput="WZ.perPallet=parseInt(this.value,10)||0"></div>`}
      </div>
      <div class="w-sub" style="margin-top:8px">Штрихкоды коробов и палет создадутся сразу — печать и раскладка
        товара будут в карточке поставки. Добавить или удалить короба можно и позже.</div>`;
  } else {
    const packTxt = WZ.pack === 'mono'
      ? `${WZ.pallets} палет × ${WZ.perPallet} коробов`
      : `${WZ.boxes} коробов`;
    body = `
      <div class="w-row">
        <div><div class="w-label">Дата поставки</div>
          <input type="date" value="${esc(WZ.date)}" oninput="WZ.date=this.value"></div>
        <div><div class="w-label">Комментарий</div>
          <input placeholder="машина, время, особенности…" value="${esc(WZ.note)}" oninput="WZ.note=this.value"></div>
      </div>
      <div class="w-batches" style="margin-top:12px">
        Итого: <b>${wizSkus()}</b> товаров · <b>${wizUnits()}</b> шт · упаковка: <b>${packTxt}</b>.
        После создания — разложите товар по коробам (вручную или через Excel) и распечатайте ШК.
      </div>`;
  }
  $('wWiz').innerHTML = `<div class="w-card" style="border-color:var(--brand)">
    <div class="w-row" style="justify-content:space-between;align-items:center;margin-bottom:8px">
      <div class="w-h" style="margin:0">Новая поставка</div>
      <div style="flex:0"><button class="w-btn w-btn-ghost" onclick="wizClose()">✕ Отмена</button></div>
    </div>
    ${stepper}
    ${body}
    <div class="w-wiz-foot">
      <div>${WZ.step > 1 ? `<button class="w-btn" onclick="WZ.step--;wizRender()">← Назад</button>` : ''}</div>
      <div>${WZ.step < 3
        ? `<button class="w-btn w-btn-primary" onclick="wizNext()">Дальше →</button>`
        : `<button class="w-btn w-btn-primary" onclick="wizSubmit()">Создать поставку</button>`}</div>
    </div>
  </div>`;
}

// перерисовка без потери фокуса в поиске
function wizRenderKeepFocus(inp) {
  const pos = inp.selectionStart;
  wizRender();
  const el = document.querySelector('#wWiz .w-search');
  if (el) { el.focus(); el.setSelectionRange(pos, pos); }
}
function wizCount() {
  const el = document.querySelector('#wWiz .w-count');
  if (el) el.textContent = `Добавлено: ${wizSkus()} товаров · ${wizUnits()} шт`;
}
function wizNext() {
  if (WZ.step === 1 && !wizSkus() &&
      !confirm('Товары не выбраны. Продолжить и заявить состав раскладкой по коробам?')) return;
  WZ.step++; wizRender();
}
let _wizBusy = false;
async function wizSubmit() {
  if (_wizBusy) return;
  _wizBusy = true;
  try {
    const lines = Object.entries(WZ.qty).map(([sku, qty]) => ({ sku, qty }));
    const r = await api('/inbounds', { method: 'POST', body: JSON.stringify({
      client_id: W.clientId, lines,
      expected_date: WZ.date, note: WZ.note,
      pack_type: WZ.pack,
      boxes: WZ.pack === 'box' ? WZ.boxes : 0,
      pallets: WZ.pack === 'mono' ? WZ.pallets : 0,
      boxes_per_pallet: WZ.pack === 'mono' ? WZ.perPallet : 0 }) });
    WZ = null;
    // сразу открываем карточку созданной поставки — шаг «Упаковка и печать ШК»
    if (W.user.role === 'client' && r && r.id) W.openInbound = r.id;
    go(W.user.role === 'client' ? 'inbounds' : 'receive');
  } catch (e) { alert(e.message); }
  finally { _wizBusy = false; }
}

function inboundCard(ib, staffMode) {
  const st = { expected: ['ожидается', 'warn'], receiving: ['на приёмке', 'warn'],
               done: ['принята', 'ok'] }[ib.status] || [ib.status, 'grey'];
  return `<div style="border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px">
    <div class="w-row" style="justify-content:space-between">
      <div><b>Заявка №${ib.id}</b> ${ib.act_no ? '· акт ' + esc(ib.act_no) : ''} · ${esc(ib.client)}</div>
      <div style="flex:0"><span class="w-pill ${st[1]}">${st[0]}</span></div>
    </div>
    <div class="w-sub">${ib.expected_date ? 'на ' + esc(ib.expected_date) + ' · ' : ''}создана ${esc((ib.created_at || '').slice(0, 10))}${ib.pack_type === 'mono' ? ' · монопаллеты' : ''}${ib.note ? ' · ' + esc(ib.note) : ''}</div>
    ${(ib.lines || []).length ? `<table class="w-table" style="table-layout:fixed"><thead><tr>
      <th style="width:22%">Артикул</th>
      <th style="width:100px;text-align:center">Заявлено</th>
      <th style="width:100px;text-align:center">Принято</th>
      <th>Партия / СГ</th><th style="width:150px">Расхожд.</th></tr></thead><tbody>
    ${(ib.lines || []).map(l => `<tr>
      <td><b>${esc(l.sku)}</b></td>
      <td style="text-align:center">${l.qty_expected}</td>
      <td style="text-align:center">${ib.status === 'done' ? `<b>${l.qty_received}</b>` : '—'}</td>
      <td>${esc(l.batch_no || '')}${l.expiry_date ? ' · до ' + esc(l.expiry_date) : ''}</td>
      <td>${l.discrepancy_type ? `<span class="w-pill bad">${esc(l.discrepancy_type)} ${l.discrepancy_qty > 0 ? '+' : ''}${l.discrepancy_qty || ''}</span>` : ''}</td>
    </tr>`).join('')}
    </tbody></table>` : ''}
    ${boxesSection(ib)}
    ${staffMode && ib.status !== 'done' ? `<button class="w-btn w-btn-primary" style="margin-top:10px" onclick="openReceive(${ib.id})">${(ib.boxes || []).length ? 'Принять по коробам' : 'Принять эту поставку'}</button>` : ''}
  </div>`;
}

// ── Упаковка поставки (WB ФБО-стиль): аккордеоны коробов, палеты, ШК ────────
W.boxOpen = new Set();

function packItemsHtml(ib, b, editable) {
  const rows = b.items.map(i => `<div class="w-item-row">
      <div class="w-item-name">
        <div class="nm">${esc(i.name || i.sku)}</div>
        <div class="w-item-meta">Артикул: <b>${esc(i.sku)}</b></div>
      </div>
      <div style="white-space:nowrap"><b>${i.qty} шт</b></div>
      <div class="w-item-meta" style="white-space:nowrap">${i.expiry_date ? 'до ' + esc(i.expiry_date) : ''}</div>
      ${editable ? `<button class="w-ibtn danger" title="Убрать из короба" onclick="packRmItem(${ib.id},${b.id},${i.id})">✕</button>` : ''}
    </div>`).join('');
  const opts = (W.skus || []).map(s =>
    `<option value="${esc(s.code)}" data-exp="${s.requires_expiry ? 1 : 0}">${esc(s.code)}${s.requires_expiry ? ' — нужен срок годности' : ''}</option>`).join('');
  const addForm = editable ? `<div class="w-add-row">
      <select id="pkSku${b.id}"><option value="">— выберите товар —</option>${opts}</select>
      <input id="pkQty${b.id}" type="number" inputmode="numeric" min="1" placeholder="шт">
      <input id="pkExp${b.id}" type="date" title="Срок годности">
      <button class="w-btn w-btn-primary" onclick="packAdd(${ib.id},${b.id})">+ В короб</button>
    </div>` : '';
  return `<div class="w-pack-body">${rows || '<div class="w-sub">Короб пуст</div>'}${addForm}</div>`;
}

function packRowHtml(ib, b, editable) {
  const units = b.items.reduce((q, i) => q + i.qty, 0);
  const open = W.boxOpen.has(b.id);
  return `<div class="w-pack-row ${open ? 'open' : ''}" onclick="packToggle(${b.id})">
      <span class="arr">▶</span>
      <div style="min-width:0;flex:1">
        <span class="w-box-code">${esc(b.code)}</span>
        <span class="w-box-sub"> · ${b.items.length} арт · ${units} шт</span>
        ${ib.status === 'done' ? (b.received ? ' <span class="w-pill ok">принят</span>' : ' <span class="w-pill bad">не принят</span>') : ''}
      </div>
      <div class="w-box-actions" onclick="event.stopPropagation()">
        <a class="w-ibtn" title="Печать ШК короба" target="_blank" href="/api/wms/boxes/${b.id}/label">🖨</a>
        ${editable ? `<button class="w-ibtn danger" title="Удалить короб" onclick="boxDel(${b.id})">✕</button>` : ''}
      </div>
    </div>
    ${open ? packItemsHtml(ib, b, editable) : ''}`;
}

function boxesSection(ib) {
  const boxes = ib.boxes || [];
  const pallets = ib.pallets || [];
  const editable = ib.status !== 'done';
  if (!boxes.length && !pallets.length && !editable) return '';
  const packedBySku = {};
  boxes.forEach(b => b.items.forEach(i => {
    packedBySku[i.sku] = (packedBySku[i.sku] || 0) + i.qty; }));
  const packed = Object.values(packedBySku).reduce((s, v) => s + v, 0);
  const asked = (ib.lines || []).reduce((s, l) => s + (l.qty_expected || 0), 0);
  // чипсы «осталось разложить» по заявленным товарам
  const chips = (ib.lines || [])
    .map(l => ({ sku: l.sku, left: (l.qty_expected || 0) - (packedBySku[l.sku] || 0) }))
    .filter(c => c.left > 0)
    .map(c => `<span class="w-chip">${esc(c.sku)} · осталось ${c.left}</span>`).join(' ');
  const hasAny = boxes.length || pallets.length;
  return `<div style="margin-top:12px;padding-top:10px;border-top:1px dashed var(--line)">
    <div class="w-pack-top">
      <input class="w-search" placeholder="Поиск по товару или номеру короба"
        value="${esc(W.packSearch || '')}" oninput="packSearchInput(${ib.id}, this.value)">
      ${editable ? `<button class="w-btn" onclick="boxAdd(${ib.id})">+ Короб</button>
      <div class="w-menu">
        <button class="w-btn" onclick="menuToggle('wPal${ib.id}')">+ Палета ▾</button>
        <div id="wPal${ib.id}" class="w-menu-list" style="display:none;padding:12px;min-width:230px">
          <div class="w-label">Коробов на палете</div>
          <input id="wPalN${ib.id}" type="number" inputmode="numeric" min="0" max="100" value="4" style="width:100%">
          <button class="w-btn w-btn-primary" style="width:100%;margin-top:8px" onclick="palletCreate(${ib.id})">Создать палету</button>
        </div>
      </div>
      <div class="w-menu">
        <button class="w-btn" onclick="menuToggle('wXls${ib.id}')">Через Excel ▾</button>
        <div id="wXls${ib.id}" class="w-menu-list" style="display:none">
          <button onclick="location.href='/api/wms/inbounds/${ib.id}/boxes/template';menuToggle('wXls${ib.id}')">⬇ Скачать шаблон со штрихкодами</button>
          <label>⬆ Загрузить раскладку
            <input type="file" accept=".xlsx,.csv,.txt" style="display:none" onchange="boxesFile(${ib.id}, this)"></label>
        </div>
      </div>` : ''}
      ${hasAny ? `<a class="w-btn" style="text-decoration:none" target="_blank" href="/api/wms/inbounds/${ib.id}/labels">🖨 ШК</a>` : ''}
    </div>
    <div class="w-pack-sum">
      <span><b>${boxes.length}</b> коробов${pallets.length ? ` · <b>${pallets.length}</b> палет` : ''}</span>
      <span>Разложено <b>${packed}</b>${asked ? ` из <b>${asked}</b>` : ''} шт</span>
      ${editable && asked && !chips ? '<span class="w-chip done">✓ всё разложено</span>' : ''}
    </div>
    ${editable && chips ? `<div style="margin:0 0 8px;display:flex;gap:6px;flex-wrap:wrap">${chips}</div>` : ''}
    ${hasAny ? `<div class="w-box-list" id="wPackList${ib.id}">${packListInner(ib, editable)}</div>`
             : '<div class="w-sub">Коробов пока нет — добавьте короб или палету</div>'}
  </div>`;
}

function packListInner(ib, editable) {
  const boxes = ib.boxes || [];
  const pallets = ib.pallets || [];
  const q = (W.packSearch || '').trim().toLowerCase();
  const show = b => !q || b.code.toLowerCase().includes(q) ||
    b.items.some(i => (i.sku || '').toLowerCase().includes(q) || (i.name || '').toLowerCase().includes(q));
  const groups = pallets.map(p => {
    const pb = boxes.filter(b => b.pallet_id === p.id && show(b));
    if (q && !pb.length) return '';
    return `<div class="w-pallet-head">
      <span>🟫 ${esc(p.code)}</span>
      <span class="w-box-sub">· ${pb.length} кор.</span>
      <div class="w-box-actions">
        <a class="w-ibtn" title="Печать ШК палеты" target="_blank" href="/api/wms/pallets/${p.id}/label">🖨</a>
        ${editable ? `<button class="w-ibtn" title="Добавить короб в палету" onclick="boxAdd(${ib.id},${p.id})">＋</button>
        <button class="w-ibtn danger" title="Удалить палету с коробами" onclick="palletDel(${p.id})">✕</button>` : ''}
      </div>
    </div>` + pb.map(b => packRowHtml(ib, b, editable)).join('');
  }).join('');
  const loose = boxes.filter(b => !b.pallet_id && show(b));
  return groups +
    (loose.length && pallets.length ? '<div class="w-pallet-head"><span>📦 Без палеты</span></div>' : '') +
    loose.map(b => packRowHtml(ib, b, editable)).join('') ||
    '<div class="w-sub" style="padding:12px">Ничего не найдено</div>';
}

function packSearchInput(iid, val) {
  W.packSearch = val;
  const ib = (W.inbounds || []).find(x => x.id === iid);
  const el = $('wPackList' + iid);
  if (ib && el) el.innerHTML = packListInner(ib, ib.status !== 'done');
}

function packToggle(bid) {
  if (W.boxOpen.has(bid)) W.boxOpen.delete(bid); else W.boxOpen.add(bid);
  go(curTab());
}

function packAdd(iid, bid) {
  const ib = (W.inbounds || []).find(x => x.id === iid); if (!ib) return;
  const b = (ib.boxes || []).find(x => x.id === bid); if (!b) return;
  const sel = $('pkSku' + bid);
  const sku = sel.value;
  const qty = parseInt($('pkQty' + bid).value, 10) || 0;
  const exp = $('pkExp' + bid).value;
  if (!sku) { alert('Выберите товар'); return; }
  if (qty <= 0) { alert('Укажите количество'); return; }
  const needExp = sel.selectedOptions[0] && sel.selectedOptions[0].dataset.exp === '1';
  if (needExp && !exp) { alert('У этого товара обязателен срок годности'); return; }
  const items = b.items.map(i => ({ sku: i.sku, qty: i.qty, expiry_date: i.expiry_date || '' }));
  // тот же товар с тем же СГ — складываем, не дублируем строку
  const same = items.find(i => i.sku === sku && (i.expiry_date || '') === (exp || ''));
  if (same) same.qty += qty; else items.push({ sku, qty, expiry_date: exp });
  _packOp(() => api(`/boxes/${bid}/items`, { method: 'POST', body: JSON.stringify({ items }) }));
}

function packRmItem(iid, bid, itemId) {
  const ib = (W.inbounds || []).find(x => x.id === iid); if (!ib) return;
  const b = (ib.boxes || []).find(x => x.id === bid); if (!b) return;
  const items = b.items.filter(i => i.id !== itemId)
    .map(i => ({ sku: i.sku, qty: i.qty, expiry_date: i.expiry_date || '' }));
  _packOp(() => api(`/boxes/${bid}/items`, { method: 'POST', body: JSON.stringify({ items }) }));
}

const curTab = () => { const b = document.querySelector('.w-nav button.active'); return b ? b.id.slice(5) : 'stock'; };
function menuToggle(id) {
  const el = $(id);
  const open = el.style.display === 'none';
  document.querySelectorAll('.w-menu-list').forEach(x => { x.style.display = 'none'; });
  el.style.display = open ? 'block' : 'none';
}

// один гард на все мутации упаковки — двойной клик не создаст дублей
let _packBusy = false;
async function _packOp(fn) {
  if (_packBusy) return;
  _packBusy = true;
  try { await fn(); go(curTab()); }
  catch (e) { alert(e.message); }
  finally { _packBusy = false; }
}
const boxAdd = (iid, palletId) => _packOp(() =>
  api(`/inbounds/${iid}/boxes`, { method: 'POST',
    body: JSON.stringify({ count: 1, pallet_id: palletId || null }) }));
const palletCreate = iid => _packOp(() =>
  api(`/inbounds/${iid}/pallets`, { method: 'POST',
    body: JSON.stringify({ boxes: parseInt($('wPalN' + iid).value, 10) || 0 }) }));
function palletDel(pid) {
  if (!confirm('Удалить палету вместе с её коробами?')) return;
  _packOp(() => api(`/pallets/${pid}`, { method: 'DELETE' }));
}
function boxDel(bid) {
  if (!confirm('Удалить короб?')) return;
  _packOp(() => api(`/boxes/${bid}`, { method: 'DELETE' }));
}
function boxesFile(iid, inp) {
  const f = inp.files && inp.files[0];
  if (!f) return;
  inp.value = '';
  _packOp(async () => {
    const fd = new FormData();
    fd.append('file', f);
    const r = await fetch(`/api/wms/inbounds/${iid}/boxes/import`, { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error('Файл не применён:\n' + (j.detail || ('HTTP ' + r.status)));
    alert(`Раскладка загружена: коробов ${j.boxes} · ${j.units} шт`);
  });
}

async function asnCreate() {
  const lines = [...document.querySelectorAll('.asn-qty')]
    .filter(i => parseInt(i.value, 10) > 0)
    .map(i => ({ sku: i.dataset.sku, qty: parseInt(i.value, 10) }));
  if (!lines.length &&
      !confirm('Количества не проставлены. Создать пустую заявку и заявить товар раскладкой по коробам?')) return;
  try {
    await api('/inbounds', { method: 'POST', body: JSON.stringify({
      client_id: W.clientId, lines, expected_date: $('wAsnDate').value,
      note: $('wAsnNote').value }) });
    go(W.user.role === 'client' ? 'inbounds' : 'receive');
  } catch (e) { alert(e.message); }
}

// ── Приёмка (склад) ─────────────────────────────────────────────────────────
let _recv = null;
async function vReceive() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + '<div class="w-card">Загружаю…</div>';
  let d;
  try { d = await api('/inbounds' + cidQ()); }
  catch (e) { m.innerHTML = `<div class="w-card w-err">${esc(e.message)}</div>`; return; }
  W.inbounds = d.inbounds || [];
  const open = (d.inbounds || []).filter(i => i.status !== 'done');
  const done = (d.inbounds || []).filter(i => i.status === 'done').slice(0, 5);
  m.innerHTML = clientPicker() + `
    <div class="w-card">
      <div class="w-h">Ожидаются поставки</div>
      ${open.map(ib => inboundCard(ib, true)).join('') || '<div class="w-sub">Нет ожидаемых заявок. Заявку может создать клиент в своём кабинете — или создайте сами на вкладке клиента ниже.</div>'}
      <details style="margin-top:8px"><summary class="w-sub" style="cursor:pointer">Создать заявку за клиента</summary>
        <div style="margin-top:8px" id="wAsnBox">Загружаю товары…</div>
      </details>
    </div>
    <div id="wRecvForm"></div>
    <div class="w-card"><div class="w-h">Последние принятые</div>
      ${done.map(ib => inboundCard(ib, false)).join('') || '<div class="w-sub">—</div>'}
    </div>`;
  try {
    const sk = (await api('/skus' + cidQ())).skus;
    W.skus = sk;
    const box = $('wAsnBox');
    if (box) box.innerHTML = asnForm(sk, true);
  } catch (e) {}
}

const recvModeRow = (nPallets) => `<div class="w-row" style="margin-top:12px">
      <div><div class="w-label">Как принимаем</div>
        <select id="rMode">
          <option value="pallet">Паллетами (моно-короба)</option>
          <option value="unit">Поштучно (маркированный)</option>
          <option value="unit_sorted">Поштучно с сортировкой (россыпь)</option>
        </select></div>
      <div id="rPalletsBox"><div class="w-label">Паллет</div>
        <input id="rPallets" type="number" inputmode="numeric" value="${nPallets || 1}" /></div>
    </div>`;

async function openReceive(iid) {
  const d = await api('/inbounds' + cidQ());
  const ib = (d.inbounds || []).find(x => x.id === iid);
  if (!ib) return;
  _recv = ib;
  const declaredInBoxes = (ib.boxes || []).reduce((s, b) => s + b.items.reduce((q, i) => q + i.qty, 0), 0);
  if ((ib.boxes || []).length && !declaredInBoxes && (ib.lines || []).some(l => l.qty_expected > 0)) {
    if (confirm('В коробах этой заявки ничего не разложено. Принять построчно по заявке?')) {
      _recv = Object.assign({}, ib, { boxes: [] });
      openReceiveLines(_recv);
      return;
    }
  }
  if ((ib.boxes || []).length) {
    const recvBox = b => `<div style="border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:8px">
        <label style="display:flex;align-items:center;gap:10px;cursor:pointer;font-size:17px">
          <input type="checkbox" id="bx${b.id}" checked style="width:22px;height:22px">
          <b>${esc(b.code)}</b><span class="w-sub" style="margin:0">· ${b.items.reduce((q, i) => q + i.qty, 0)} шт</span>
        </label>
        ${b.items.length ? `<table class="w-table" style="margin-top:6px"><thead><tr>
          <th>Артикул</th><th class="w-num">Заявлено</th><th style="width:110px">Факт</th><th style="width:160px">Срок годности</th>
        </tr></thead><tbody>
        ${b.items.map(i => `<tr>
          <td><b>${esc(i.sku)}</b><div class="w-sub" style="margin:0">${esc(i.name || '')}</div></td>
          <td class="w-num">${i.qty}</td>
          <td><input id="bq${i.id}" type="number" inputmode="numeric" value="${i.qty}" style="width:95px"></td>
          <td><input id="be${i.id}" type="date" value="${i.expiry_date ? esc(i.expiry_date) : ''}" style="width:150px"></td>
        </tr>`).join('')}
        </tbody></table>` : '<div class="w-sub" style="margin:4px 0 0">пустой короб</div>'}
      </div>`;
    const pallets = ib.pallets || [];
    const loose = ib.boxes.filter(b => !b.pallet_id);
    const body = pallets.map(p => {
      const pb = ib.boxes.filter(b => b.pallet_id === p.id);
      return `<div class="w-label" style="margin:10px 0 6px">🟫 ${esc(p.code)} · ${pb.length} кор.</div>` +
        pb.map(recvBox).join('');
    }).join('') +
      (loose.length ? (pallets.length ? '<div class="w-label" style="margin:10px 0 6px">📦 Без палеты</div>' : '') +
        loose.map(recvBox).join('') : '');
    $('wRecvForm').innerHTML = `<div class="w-card" style="border-color:var(--brand)">
      <div class="w-h">Приёмка заявки №${ib.id} по коробам · ${esc(ib.client)}</div>
      <div class="w-sub">Сверяй ШК короба и отмечай галочкой. Кол-во и сроки годности
        уже предзаполнены раскладкой клиента — правь только при расхождении.
        Снятая галочка = короб не приехал.</div>
      ${body}
      ${recvModeRow((ib.pallets || []).length)}
      <button class="w-btn w-btn-primary w-btn-big" style="margin-top:12px" onclick="receiveSubmit()">Завершить приёмку</button>
    </div>`;
    $('wRecvForm').scrollIntoView({ behavior: 'smooth' });
    return;
  }
  openReceiveLines(ib);
}

function openReceiveLines(ib) {
  $('wRecvForm').innerHTML = `<div class="w-card" style="border-color:var(--brand)">
    <div class="w-h">Приёмка заявки №${ib.id} · ${esc(ib.client)}</div>
    <div class="w-sub">Пересчитай факт. Если товар со сроком годности — срок обязателен. Расхождение с заявкой зафиксируется автоматически.</div>
    <div class="w-table-wrap"><table class="w-table"><thead><tr>
      <th>Артикул</th><th class="w-num">Заявлено</th><th style="width:110px">Факт</th>
      <th style="width:130px">№ партии</th><th style="width:150px">Срок годности</th>
    </tr></thead><tbody>
    ${ib.lines.map(l => `<tr>
      <td><b>${esc(l.sku)}</b><div class="w-sub" style="margin:0">${esc(l.name || '')}</div></td>
      <td class="w-num">${l.qty_expected}</td>
      <td><input id="rq${l.id}" type="number" inputmode="numeric" value="${l.qty_expected}" style="width:100px" /></td>
      <td><input id="rb${l.id}" placeholder="—" style="width:120px" /></td>
      <td><input id="re${l.id}" type="date" style="width:150px" /></td>
    </tr>`).join('')}
    </tbody></table></div>
    ${recvModeRow((ib.pallets || []).length)}
    <button class="w-btn w-btn-primary w-btn-big" style="margin-top:12px" onclick="receiveSubmit()">Завершить приёмку</button>
  </div>`;
  $('wRecvForm').scrollIntoView({ behavior: 'smooth' });
}

let _busy = false;
async function receiveSubmit() {
  if (_busy) return;
  _busy = true;
  try { await _receiveSubmit(); } finally { _busy = false; }
}
async function _receiveSubmit() {
  if ((_recv.boxes || []).length) {  // приёмка по коробам (WB-стиль)
    const boxes = _recv.boxes.map(b => ({
      box_id: b.id,
      received: $('bx' + b.id).checked,
      items: b.items.map(i => ({ item_id: i.id,
        qty: parseInt($('bq' + i.id).value, 10) || 0,
        expiry_date: $('be' + i.id).value })) }));
    const body = { boxes, receive_mode: $('rMode').value,
      pallets: parseInt($('rPallets').value, 10) || 0 };
    try {
      const r = await api(`/inbounds/${_recv.id}/receive_boxes`, {
        method: 'POST', body: JSON.stringify(body) });
      alert(`Принято коробов: ${r.boxes_received} из ${r.boxes_total} · ${r.units} шт. Акт ${r.act_no}.`);
      _recv = null; go('receive');
    } catch (e) { alert(e.message); }
    return;
  }
  const lines = _recv.lines.map(l => ({
    line_id: l.id,
    qty_received: parseInt($('rq' + l.id).value, 10) || 0,
    batch_no: $('rb' + l.id).value.trim(),
    expiry_date: $('re' + l.id).value }));
  const body = { lines, receive_mode: $('rMode').value,
    pallets: parseInt($('rPallets').value, 10) || 0 };
  try {
    const r = await api(`/inbounds/${_recv.id}/receive`, {
      method: 'POST', body: JSON.stringify(body) });
    alert(`Принято: ${r.units} шт. Акт ${r.act_no}. Остатки и начисления обновлены.`);
    _recv = null; go('receive');
  } catch (e) { alert(e.message); }
}

// ── Отгрузка (склад) ────────────────────────────────────────────────────────
async function vShip() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + `<div class="w-card">
    <div class="w-h">Отгрузка заказов</div>
    <div class="w-sub">Каждая строка — один заказ: НОМЕР АРТИКУЛ КОЛ-ВО [АРТИКУЛ КОЛ-ВО …].
      Списание идёт с самых старых партий (FIFO), начисление — по тарифу клиента.</div>
    <div class="w-form">
      <textarea id="wShipLines" rows="6" placeholder="12345678 BMN-0028 1\n12345679 ST-07 2\n12345680 BMN-0013 1 BMN-0028 1"></textarea>
      <button class="w-btn w-btn-primary" onclick="shipSubmit()">Отгрузить</button>
      <div id="wShipRes"></div>
    </div></div>`;
}

async function shipSubmit() {
  if (_busy) return;
  _busy = true;
  try { await _shipSubmit(); } finally { _busy = false; }
}
async function _shipSubmit() {
  const orders = $('wShipLines').value.split('\n').map(s => s.trim()).filter(Boolean)
    .map(s => {
      const p = s.split(/\s+/);
      if (p.length < 3) return null;
      const items = [];
      for (let i = 1; i + 1 < p.length; i += 2)
        items.push({ sku: p[i], qty: parseInt(p[i + 1], 10) || 0 });
      return { ref: p[0], items };
    }).filter(Boolean);
  if (!orders.length) { alert('Формат: НОМЕР АРТИКУЛ КОЛ-ВО'); return; }
  try {
    const r = await api('/ops/ship', { method: 'POST', body: JSON.stringify({
      client_id: W.clientId, orders }) });
    $('wShipRes').innerHTML = `<div class="w-pill ok">Отгружено заказов: ${r.shipped}</div>` +
      (r.errors || []).map(e => `<div class="w-err">${esc(e)}</div>`).join('');
    if (r.shipped) $('wShipLines').value = '';
  } catch (e) { alert(e.message); }
}

// ── Возврат / корректировка (склад) ─────────────────────────────────────────
async function vOps() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + `
    <div class="w-card"><div class="w-h">Возврат</div>
      <div class="w-form"><div class="w-row">
        <div><div class="w-label">Артикул</div><input id="wRetSku" /></div>
        <div><div class="w-label">Кол-во</div><input id="wRetQty" type="number" inputmode="numeric" value="1" /></div>
        <div><div class="w-label">Вердикт</div><select id="wRetV">
          <option value="to_stock">Годен — в сток</option>
          <option value="damaged">Брак — в карантин</option>
          <option value="dispose">Утилизация</option></select></div>
      </div>
      <input id="wRetRef" placeholder="Номер возврата/заказа (необязательно)" />
      <button class="w-btn w-btn-primary" onclick="retSubmit()">Провести возврат</button></div>
    </div>
    <div class="w-card"><div class="w-h">Корректировка остатка</div>
      <div class="w-sub">Только по факту пересчёта. Причина обязательна — попадает в журнал и видна клиенту.</div>
      <div class="w-form"><div class="w-row">
        <div><div class="w-label">Артикул</div><input id="wAdjSku" /></div>
        <div><div class="w-label">± штук</div><input id="wAdjQty" type="number" placeholder="-2" /></div>
      </div>
      <input id="wAdjReason" placeholder="Причина (пересчёт ячейки, бой при разгрузке…)" />
      <button class="w-btn" onclick="adjSubmit()">Провести корректировку</button></div>
    </div>`;
}

async function retSubmit() {
  try {
    await api('/ops/return', { method: 'POST', body: JSON.stringify({
      client_id: W.clientId, sku: $('wRetSku').value, qty: parseInt($('wRetQty').value, 10),
      verdict: $('wRetV').value, ref: $('wRetRef').value }) });
    alert('Возврат проведён'); go('ops');
  } catch (e) { alert(e.message); }
}
async function adjSubmit() {
  try {
    const r = await api('/ops/adjust', { method: 'POST', body: JSON.stringify({
      client_id: W.clientId, sku: $('wAdjSku').value,
      qty_delta: parseInt($('wAdjQty').value, 10), reason: $('wAdjReason').value }) });
    alert('Скорректировано: ' + r.adjusted); go('ops');
  } catch (e) { alert(e.message); }
}

// ── Начисления ──────────────────────────────────────────────────────────────
async function vBilling() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + '<div class="w-card">Загружаю…</div>';
  let d;
  try { d = await api('/billing' + cidQ()); }
  catch (e) { m.innerHTML = `<div class="w-card w-err">${esc(e.message)}</div>`; return; }
  m.innerHTML = clientPicker() + `<div class="w-card">
    <div class="w-h">Начисления с ${esc(d.date_from)} <span style="float:right">${rub(d.total)}</span></div>
    <div class="w-sub">Каждая операция попадает сюда в момент выполнения — счёт в конце месяца равен этой сумме.</div>
    <table class="w-table"><thead><tr><th>Услуга</th><th class="w-num">Кол-во</th><th class="w-num">Сумма</th></tr></thead><tbody>
    ${(d.totals || []).map(t => `<tr><td>${esc(t.service_name)}</td>
      <td class="w-num">${t.qty}</td><td class="w-num"><b>${rub(t.amount)}</b></td></tr>`).join('') || '<tr><td colspan="3">Начислений нет</td></tr>'}
    </tbody></table></div>
    <div class="w-card"><div class="w-h">Лента операций</div>
    <div class="w-table-wrap"><table class="w-table"><thead><tr>
      <th>Когда</th><th>Услуга</th><th>Документ</th><th class="w-num">Кол-во × цена</th><th class="w-num">Сумма</th>
    </tr></thead><tbody>
    ${(d.events || []).map(e => `<tr>
      <td style="white-space:nowrap">${esc(e.at)}</td>
      <td>${esc(e.service_name)}${e.note ? `<div class="w-sub" style="margin:0">${esc(e.note)}</div>` : ''}</td>
      <td>${esc(e.source)}</td>
      <td class="w-num">${e.qty} × ${e.price}</td>
      <td class="w-num"><b>${rub(e.amount)}</b></td></tr>`).join('') || '<tr><td colspan="5">—</td></tr>'}
    </tbody></table></div></div>`;
}

// ── Клиенты (склад) ─────────────────────────────────────────────────────────
async function vClients() {
  const m = $('wMain');
  let d;
  try { d = await api('/clients'); W.clients = d.clients; }
  catch (e) { m.innerHTML = `<div class="w-card w-err">${esc(e.message)}</div>`; return; }
  m.innerHTML = `<div class="w-card">
    <div class="w-h">Клиенты</div>
    <table class="w-table"><thead><tr><th>Клиент</th><th class="w-num">Сток, шт</th>
      <th class="w-num">Начислено за месяц</th><th></th></tr></thead><tbody>
    ${d.clients.map(c => `<tr><td><b>${esc(c.name)}</b><div class="w-sub" style="margin:0">${esc(c.code)}${c.inn ? ' · ИНН ' + esc(c.inn) : ''}</div></td>
      <td class="w-num">${c.stock_units}</td>
      <td class="w-num"><b>${rub(c.billed_month)}</b></td>
      <td><button class="w-btn w-btn-ghost" onclick="openSkus(${c.id})">товары</button></td></tr>`).join('') || '<tr><td colspan="4">Клиентов нет — создайте первого</td></tr>'}
    </tbody></table></div>
    <div id="wSkusBox"></div>
    <div class="w-card"><div class="w-h">Новый клиент</div>
      <div class="w-form">
        <div class="w-row">
          <div><div class="w-label">Название</div><input id="wcName" placeholder="Pomatti" /></div>
          <div><div class="w-label">ИНН</div><input id="wcInn" /></div>
        </div>
        <div class="w-row">
          <div><div class="w-label">Контакт</div><input id="wcContact" placeholder="+7…, @tg" /></div>
          <div><div class="w-label">Логин клиента</div><input id="wcLogin" placeholder="pomatti" /></div>
          <div><div class="w-label">Пароль клиента</div><input id="wcPass" /></div>
        </div>
        <button class="w-btn w-btn-primary" onclick="clientCreate()">Создать клиента</button>
        <div class="w-sub">Тариф создаётся из нашего прайса по умолчанию — правится через «товары» → тариф (фаза 2) или по запросу.</div>
      </div></div>`;
}

async function clientCreate() {
  try {
    await api('/clients', { method: 'POST', body: JSON.stringify({
      name: $('wcName').value, inn: $('wcInn').value, contact: $('wcContact').value,
      login: $('wcLogin').value, password: $('wcPass').value }) });
    go('clients');
  } catch (e) { alert(e.message); }
}

async function openSkus(cid) {
  W.clientId = cid;
  let d;
  try { d = await api('/skus?client_id=' + cid); }
  catch (e) { alert(e.message); return; }
  const c = W.clients.find(x => x.id === cid) || {};
  $('wSkusBox').innerHTML = `<div class="w-card" style="border-color:var(--brand)">
    <div class="w-h">Товары · ${esc(c.name || '')}</div>
    <table class="w-table"><thead><tr><th>Артикул</th><th>Название</th>
      <th class="w-num">Д×Ш×В, см</th><th class="w-num">Объём, л</th><th class="w-num">Вес, г</th><th class="w-num">Ценность, ₽</th><th>СГ?</th><th>ШК</th></tr></thead><tbody>
    ${(d.skus || []).map(s => `<tr><td><b>${esc(s.code)}</b></td><td>${esc(s.name)}</td>
      <td class="w-num">${s.length_cm ? `${s.length_cm}×${s.width_cm}×${s.height_cm}` : '—'}</td>
      <td class="w-num">${s.volume_l || '—'}</td><td class="w-num">${s.weight_g || '—'}</td>
      <td class="w-num">${s.value_rub || '—'}</td>
      <td>${s.requires_expiry ? '<span class="w-pill warn">да</span>' : '—'}</td>
      <td class="w-sub">${(s.barcodes || []).join(', ')}</td></tr>`).join('') || '<tr><td colspan="8">Товаров нет</td></tr>'}
    </tbody></table>
    <div class="w-row" style="margin-top:10px">
      <a class="w-btn" style="text-align:center;text-decoration:none;display:block" href="/api/wms/skus/template">⬇ Скачать шаблон Excel</a>
      <label class="w-btn w-btn-primary" style="text-align:center">Загрузить заполненный файл
        <input type="file" accept=".xlsx,.csv,.txt" style="display:none" onchange="skusFile(${cid}, this)"></label>
    </div>
    <div class="w-sub" style="margin-top:6px">Скачайте шаблон, заполните — в нём примеры и лист «Как заполнять» — и загрузите обратно. Подойдёт и своя таблица с теми же колонками.</div>
    <details style="margin-top:10px"><summary class="w-sub" style="cursor:pointer">Или добавить строками вручную</summary>
      <div class="w-form" style="margin-top:8px">
        <textarea id="wSkuLines" rows="4" placeholder="АРТИКУЛ;Название;объём_л;вес_г;ценность_₽;СГ(1/0);штрихкод\nBMN-0028;SEX FIST 500;1.73;620;350;1;2040646073073"></textarea>
        <button class="w-btn" onclick="skusImport(${cid})">Сохранить товары</button>
      </div></details>
  </div>`;
  $('wSkusBox').scrollIntoView({ behavior: 'smooth' });
}

async function skusFile(cid, inp) {
  const f = inp.files && inp.files[0];
  if (!f) return;
  inp.value = '';
  const fd = new FormData();
  fd.append('client_id', cid);
  fd.append('file', f);
  try {
    const r = await fetch('/api/wms/skus/import', { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
    alert(`Загружено товаров: ${j.saved} (в файле распознано ${j.parsed})`);
    openSkus(cid);
  } catch (e) { alert('Не вышло: ' + e.message); }
}

async function skusImport(cid) {
  const items = $('wSkuLines').value.split('\n').map(s => s.trim()).filter(Boolean)
    .map(s => {
      const p = s.split(';').map(x => x.trim());
      if (!p[0]) return null;
      return { code: p[0], name: p[1] || '', volume_l: parseFloat(p[2]) || 0,
        weight_g: parseFloat(p[3]) || 0, value_rub: parseFloat(p[4]) || 0,
        requires_expiry: p[5] === '1', barcodes: p[6] ? [p[6]] : [] };
    }).filter(Boolean);
  if (!items.length) { alert('Нет строк'); return; }
  try {
    const r = await api('/skus', { method: 'POST', body: JSON.stringify({
      client_id: cid, items }) });
    alert('Сохранено: ' + r.saved); openSkus(cid);
  } catch (e) { alert(e.message); }
}

// ── Старт ───────────────────────────────────────────────────────────────────
(async () => {
  try { W.user = await api('/auth/me'); boot(); }
  catch (e) { /* остаёмся на логине */ }
  ['wLogin', 'wPass'].forEach(id =>
    $(id).addEventListener('keydown', e => { if (e.key === 'Enter') wmsDoLogin(); }));
})();


// ── Движения (журнал списаний/приходов) ─────────────────────────────────────
async function vMoves() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + '<div class="w-card">Загружаю…</div>';
  let d;
  try { d = await api('/moves' + cidQ()); }
  catch (e) { m.innerHTML = clientPicker() + `<div class="w-card w-err">${esc(e.message)}</div>`; return; }
  m.innerHTML = clientPicker() + `<div class="w-card">
    <div class="w-h">Движения товара</div>
    <div class="w-sub">Каждый приход и списание — построчно, с документом и партией. Остаток на вкладке «Остатки» — это сумма этих строк.</div>
    <div class="w-table-wrap"><table class="w-table"><thead><tr>
      <th>Когда</th><th>Операция</th><th>Артикул</th><th class="w-num">± шт</th><th>Партия</th><th>Документ</th>
    </tr></thead><tbody>
    ${(d.moves || []).map(r => `<tr>
      <td style="white-space:nowrap">${esc(r.at)}</td>
      <td>${esc(r.doc)}${r.status === 'quarantine' ? ' <span class="w-pill bad">брак</span>' : ''}${r.note ? `<div class="w-sub" style="margin:0">${esc(r.note)}</div>` : ''}</td>
      <td><b>${esc(r.sku)}</b></td>
      <td class="w-num" style="color:${r.qty < 0 ? 'var(--bad)' : 'var(--ok)'}"><b>${r.qty > 0 ? '+' : ''}${r.qty}</b></td>
      <td>${esc(r.batch_no || '')}</td>
      <td class="w-sub">${esc(r.ref || '')}</td>
    </tr>`).join('') || '<tr><td colspan="6">Движений пока нет</td></tr>'}
    </tbody></table></div></div>`;
}
