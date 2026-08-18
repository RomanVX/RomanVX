// WMS Market Partners — фаза 1. Отдельное приложение (см. docs/wms_spec.md).
'use strict';

let W = { user: null, clients: [], clientId: null };

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
    ? (u.client_name || 'клиент') : ('склад · ' + u.login);
  const tabs = u.role === 'client'
    ? [['stock', 'Остатки'], ['inbounds', 'Поставки'], ['billing', 'Начисления']]
    : [['receive', 'Приёмка'], ['stock', 'Остатки'], ['ship', 'Отгрузка'],
       ['ops', 'Возврат/корр.'], ['billing', 'Начисления'], ['clients', 'Клиенты']];
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
     ship: vShip, ops: vOps, clients: vClients })[tab]();
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

// ── Поставки (список + создание заявки) ─────────────────────────────────────
async function vInbounds() {
  const m = $('wMain');
  m.innerHTML = clientPicker() + '<div class="w-card">Загружаю…</div>';
  let d;
  try { d = await api('/inbounds' + cidQ()); }
  catch (e) { m.innerHTML = `<div class="w-card w-err">${esc(e.message)}</div>`; return; }
  m.innerHTML = clientPicker() + `
    <div class="w-card">
      <div class="w-h">Новая заявка на поставку</div>
      <div class="w-sub">Сообщите складу заранее, что везёте — приёмка пройдёт быстрее и без расхождений.</div>
      <div class="w-form">
        <textarea id="wAsnLines" rows="4" placeholder="Каждая строка: АРТИКУЛ КОЛИЧЕСТВО\nнапример:\nBMN-0028 120\nST-07 50"></textarea>
        <div class="w-row">
          <div><div class="w-label">Дата поставки</div><input id="wAsnDate" type="date" /></div>
          <div><div class="w-label">Комментарий</div><input id="wAsnNote" placeholder="машина, паллеты…" /></div>
        </div>
        <button class="w-btn w-btn-primary" onclick="asnCreate()">Создать заявку</button>
      </div>
    </div>
    <div class="w-card">
      <div class="w-h">Заявки</div>
      ${(d.inbounds || []).map(ib => inboundCard(ib, false)).join('') || '<div class="w-sub">Заявок пока нет</div>'}
    </div>`;
}

function inboundCard(ib, staffMode) {
  const st = { expected: ['ожидается', 'warn'], receiving: ['на приёмке', 'warn'],
               done: ['принята', 'ok'] }[ib.status] || [ib.status, 'grey'];
  return `<div style="border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px">
    <div class="w-row" style="justify-content:space-between">
      <div><b>Заявка №${ib.id}</b> ${ib.act_no ? '· акт ' + esc(ib.act_no) : ''} · ${esc(ib.client)}</div>
      <div style="flex:0"><span class="w-pill ${st[1]}">${st[0]}</span></div>
    </div>
    <div class="w-sub">${ib.expected_date ? 'на ' + esc(ib.expected_date) + ' · ' : ''}создана ${esc((ib.created_at || '').slice(0, 10))}${ib.note ? ' · ' + esc(ib.note) : ''}</div>
    <table class="w-table"><thead><tr><th>Артикул</th><th class="w-num">Заявлено</th><th class="w-num">Принято</th><th>Партия / СГ</th><th>Расхожд.</th></tr></thead><tbody>
    ${(ib.lines || []).map(l => `<tr>
      <td>${esc(l.sku)}</td><td class="w-num">${l.qty_expected}</td>
      <td class="w-num">${ib.status === 'done' ? `<b>${l.qty_received}</b>` : '—'}</td>
      <td>${esc(l.batch_no || '')}${l.expiry_date ? ' · до ' + esc(l.expiry_date) : ''}</td>
      <td>${l.discrepancy_type ? `<span class="w-pill bad">${esc(l.discrepancy_type)} ${l.discrepancy_qty > 0 ? '+' : ''}${l.discrepancy_qty || ''}</span>` : ''}</td>
    </tr>`).join('')}
    </tbody></table>
    ${staffMode && ib.status !== 'done' ? `<button class="w-btn w-btn-primary" style="margin-top:10px" onclick="openReceive(${ib.id})">Принять эту поставку</button>` : ''}
  </div>`;
}

async function asnCreate() {
  const lines = $('wAsnLines').value.split('\n').map(s => s.trim()).filter(Boolean)
    .map(s => { const m = s.match(/^(\S+)\s+(\d+)/); return m ? { sku: m[1], qty: parseInt(m[2], 10) } : null; })
    .filter(Boolean);
  if (!lines.length) { alert('Добавь хотя бы одну строку «АРТИКУЛ КОЛИЧЕСТВО»'); return; }
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
  const open = (d.inbounds || []).filter(i => i.status !== 'done');
  const done = (d.inbounds || []).filter(i => i.status === 'done').slice(0, 5);
  m.innerHTML = clientPicker() + `
    <div class="w-card">
      <div class="w-h">Ожидаются поставки</div>
      ${open.map(ib => inboundCard(ib, true)).join('') || '<div class="w-sub">Нет ожидаемых заявок. Заявку может создать клиент в своём кабинете — или создайте сами на вкладке клиента ниже.</div>'}
      <details style="margin-top:8px"><summary class="w-sub" style="cursor:pointer">Создать заявку за клиента</summary>
        <div class="w-form" style="margin-top:8px">
          <textarea id="wAsnLines" rows="3" placeholder="BMN-0028 120"></textarea>
          <div class="w-row">
            <div><input id="wAsnDate" type="date" /></div>
            <div><input id="wAsnNote" placeholder="комментарий" /></div>
          </div>
          <button class="w-btn" onclick="asnCreate()">Создать</button>
        </div></details>
    </div>
    <div id="wRecvForm"></div>
    <div class="w-card"><div class="w-h">Последние принятые</div>
      ${done.map(ib => inboundCard(ib, false)).join('') || '<div class="w-sub">—</div>'}
    </div>`;
}

async function openReceive(iid) {
  const d = await api('/inbounds' + cidQ());
  const ib = (d.inbounds || []).find(x => x.id === iid);
  if (!ib) return;
  _recv = ib;
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
      <td><input id="rq${l.id}" type="number" inputmode="numeric" value="${l.qty_expected}" style="width:100px;padding:10px;border:1px solid var(--line);border-radius:10px" /></td>
      <td><input id="rb${l.id}" placeholder="—" style="width:120px;padding:10px;border:1px solid var(--line);border-radius:10px" /></td>
      <td><input id="re${l.id}" type="date" style="width:145px;padding:9px;border:1px solid var(--line);border-radius:10px" /></td>
    </tr>`).join('')}
    </tbody></table></div>
    <div class="w-row" style="margin-top:12px">
      <div><div class="w-label">Как принимаем</div>
        <select id="rMode">
          <option value="pallet">Паллетами (моно-короба)</option>
          <option value="unit">Поштучно (маркированный)</option>
          <option value="unit_sorted">Поштучно с сортировкой (россыпь)</option>
        </select></div>
      <div id="rPalletsBox"><div class="w-label">Паллет</div>
        <input id="rPallets" type="number" inputmode="numeric" value="1" /></div>
    </div>
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
      <th class="w-num">Объём, л</th><th class="w-num">Вес, г</th><th class="w-num">Ценность, ₽</th><th>СГ?</th><th>ШК</th></tr></thead><tbody>
    ${(d.skus || []).map(s => `<tr><td><b>${esc(s.code)}</b></td><td>${esc(s.name)}</td>
      <td class="w-num">${s.volume_l || '—'}</td><td class="w-num">${s.weight_g || '—'}</td>
      <td class="w-num">${s.value_rub || '—'}</td>
      <td>${s.requires_expiry ? '<span class="w-pill warn">да</span>' : '—'}</td>
      <td class="w-sub">${(s.barcodes || []).join(', ')}</td></tr>`).join('') || '<tr><td colspan="7">Товаров нет</td></tr>'}
    </tbody></table>
    <div class="w-row" style="margin-top:10px">
      <label class="w-btn w-btn-primary" style="text-align:center">Загрузить из файла (Excel/CSV)
        <input type="file" accept=".xlsx,.csv,.txt" style="display:none" onchange="skusFile(${cid}, this)"></label>
    </div>
    <div class="w-sub" style="margin-top:6px">Колонки файла: Артикул · Название · Объём, л · Вес, г · Ценность, ₽ · СГ (да/1) · Штрихкод — порядок любой, лишние игнорируются.</div>
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
