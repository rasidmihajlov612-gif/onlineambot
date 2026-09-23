const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  try {
    tg.setHeaderColor('#000000');
    tg.setBackgroundColor('#000000');
  } catch (e) { /* старый клиент Telegram — не критично */ }
}

function initData() {
  return tg ? tg.initData : '';
}

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : str;
  return div.innerHTML;
}

// Даты в базе — UTC-строки SQLite ("2026-09-22 18:33:01"). Куратор живёт по
// Москве, поэтому показываем в МСК, а не в таймзоне устройства.
function fmtDate(raw) {
  if (!raw) return '—';
  const d = new Date(raw.replace(' ', 'T') + 'Z');
  if (isNaN(d)) return raw;
  return d.toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    timeZone: 'Europe/Moscow',
  });
}

function agentLabel(name, username) {
  const base = escapeHtml(name || 'Без имени');
  return username ? `${base} (@${escapeHtml(username)})` : base;
}

function confirmAction(text) {
  if (tg && tg.showConfirm) {
    return new Promise((resolve) => tg.showConfirm(text, resolve));
  }
  return Promise.resolve(window.confirm(text));
}

// PIN живёт только в памяти вкладки — не в localStorage, не в cookie.
// Перезагрузил страницу — вводи заново, это осознанный компромисс для
// потайной панели: удобство сессии против риска, что PIN осядет где-то
// на устройстве.
let sessionPin = null;

const root = document.getElementById('admin-content');
const tabBar = document.getElementById('admin-tabs');
const titleEl = document.getElementById('admin-title');

async function verifyPin(pin) {
  const res = await fetch('/api/admin/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: initData(), pin }),
  });
  return res.ok;
}

async function adminGet(path, params = {}) {
  const query = new URLSearchParams({ initData: initData(), pin: sessionPin, ...params });
  const res = await fetch(`${path}?${query}`);
  if (!res.ok) return null;
  return res.json();
}

async function adminPost(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: initData(), pin: sessionPin, ...payload }),
  });
  return res.ok;
}

function renderGate() {
  titleEl.textContent = 'Вход';
  tabBar.hidden = true;
  root.innerHTML = `
    <div class="card">
      <div class="section-title">Вход</div>
      <div class="section-hint">Панель только для куратора. Введите PIN.</div>
      <div class="field">
        <label>PIN-код</label>
        <input type="password" inputmode="numeric" id="pin-input" autocomplete="off">
      </div>
      <button class="btn-primary" id="pin-btn">Войти</button>
      <div class="form-status" id="pin-status"></div>
    </div>
  `;

  const btn = document.getElementById('pin-btn');
  const status = document.getElementById('pin-status');
  const input = document.getElementById('pin-input');

  const submit = async () => {
    const pin = input.value.trim();
    if (!pin) return;
    btn.disabled = true;
    status.textContent = '';
    status.className = 'form-status';
    const ok = await verifyPin(pin);
    btn.disabled = false;
    if (ok) {
      sessionPin = pin;
      tabBar.hidden = false;
      openTab('money');
    } else {
      status.textContent = 'Неверный PIN или нет доступа с этого аккаунта';
      status.className = 'form-status error';
    }
  };

  btn.onclick = submit;
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
}

function sessionExpired() {
  root.innerHTML = `<div class="section-hint">Сессия истекла или PIN устарел — обновите страницу.</div>`;
}

// Заголовок раздела уже есть в шапке — в пустой карточке его не повторяем
function emptyCard(hint) {
  return `
    <div class="card">
      <div class="section-hint" style="margin-bottom:0;">${escapeHtml(hint)}</div>
    </div>
  `;
}

/* ---------- Деньги ---------- */

async function renderMoney() {
  const data = await adminGet('/api/admin/money');
  if (!data) return sessionExpired();

  if (!data.agents.length) {
    root.innerHTML = emptyCard('Нечего выплачивать — все начисления закрыты.');
    return;
  }

  root.innerHTML = `
    <div class="card">
      <div class="eyebrow-label">К выплате всего</div>
      <div class="finance-amount">${data.grand_total}₽</div>
    </div>
    <div id="agents-list"></div>
  `;

  const list = document.getElementById('agents-list');
  list.innerHTML = data.agents.map((a) => `
    <div class="card" data-agent="${a.user_id}">
      <div class="section-title" style="font-size:16px;">${agentLabel(a.full_name, a.username)}</div>
      <ul class="video-list">
        ${a.items.map((it) => `<li>${escapeHtml(it.address)} — ${escapeHtml(it.kind_label)} — ${it.amount}₽</li>`).join('')}
      </ul>
      <button class="btn-primary payout-btn" data-agent="${a.user_id}">Выплачено (${a.unpaid_total}₽)</button>
    </div>
  `).join('');

  list.querySelectorAll('.payout-btn').forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = 'Отмечаю…';
      const ok = await adminPost('/api/admin/payout', { agent_id: btn.dataset.agent });
      if (!ok) {
        btn.disabled = false;
        btn.textContent = 'Ошибка — повторить';
        return;
      }
      // Перерисовываем целиком, а не убираем карточку: иначе «К выплате
      // всего» сверху останется со старой суммой.
      renderMoney();
    };
  });
}

/* ---------- Баны ---------- */

async function renderBans() {
  const data = await adminGet('/api/admin/banned');
  if (!data) return sessionExpired();

  if (!data.agents.length) {
    root.innerHTML = emptyCard('Удалённых агентов нет — вся команда на месте.');
    return;
  }

  root.innerHTML = `
    <div class="section-hint">
      Удалённые агенты (${data.agents.length}). Разбан вернёт статус, отправит
      человеку сообщение и новые инвайты в чаты.
    </div>
    <div id="bans-list"></div>
  `;

  const list = document.getElementById('bans-list');
  list.innerHTML = data.agents.map((a) => `
    <div class="card">
      <div class="card-head">
        <div class="section-title">${agentLabel(a.full_name, a.username)}</div>
        <div class="badge rejected">удалён</div>
      </div>
      <ul class="fact-list">
        <li class="fact-row"><span class="fact-text">Удалён</span><span class="fact-value">${fmtDate(a.removed_at)}</span></li>
        <li class="fact-row"><span class="fact-text">Последняя активность</span><span class="fact-value">${fmtDate(a.last_active_at)}</span></li>
        <li class="fact-row"><span class="fact-text">Передал объектов</span><span class="fact-value">${a.objects_count}</span></li>
        <li class="fact-row"><span class="fact-text">Telegram id</span><span class="fact-value">${a.user_id}</span></li>
      </ul>
      <button class="btn-ghost unban-btn" data-agent="${a.user_id}"
              data-name="${escapeHtml(a.full_name || 'агента')}">Разбанить</button>
    </div>
  `).join('');

  list.querySelectorAll('.unban-btn').forEach((btn) => {
    btn.onclick = async () => {
      const ok = await confirmAction(`Вернуть ${btn.dataset.name} в команду? Придут новые инвайты в чаты.`);
      if (!ok) return;
      btn.disabled = true;
      btn.textContent = 'Возвращаю…';
      const done = await adminPost('/api/admin/unban', { agent_id: btn.dataset.agent });
      if (!done) {
        btn.disabled = false;
        btn.textContent = 'Ошибка — повторить';
        return;
      }
      renderBans();  // чтобы счётчик в шапке раздела не остался старым
    };
  });
}

/* ---------- Квартиры ---------- */

const FLAT_FILTERS = [
  { key: 'all', label: 'Всего' },
  { key: 'pending', label: 'На проверке' },
  { key: 'in_progress', label: 'В работе' },
  { key: 'closed', label: 'Сдано' },
  { key: 'rejected', label: 'Отклонено' },
  { key: 'failed', label: 'Сорвалось' },
];

let flatFilter = 'all';

function flatFact(label, value, href) {
  if (!value) return '';
  const shown = href
    ? `<a href="${escapeHtml(href)}">${escapeHtml(value)}</a>`
    : escapeHtml(value);
  return `<li class="fact-row"><span class="fact-text">${label}</span><span class="fact-value">${shown}</span></li>`;
}

async function renderFlats() {
  const params = flatFilter === 'all' ? {} : { status: flatFilter };
  const data = await adminGet('/api/admin/objects', params);
  if (!data) return sessionExpired();

  const pills = FLAT_FILTERS.map((f) => `
    <div class="count-pill filter${f.key === flatFilter ? ' active' : ''}" data-filter="${f.key}">
      <div class="n">${data.counts[f.key] || 0}</div>
      <div class="label">${f.label}</div>
    </div>
  `).join('');

  const cards = data.objects.length
    ? data.objects.map((o) => `
        <div class="card">
          <div class="card-head">
            <div class="section-title">${escapeHtml(o.address || 'Без адреса')}</div>
            <div class="badge ${o.status}">${escapeHtml(o.status_label)}</div>
          </div>
          <div class="card-sub">${agentLabel(o.agent_name, o.agent_username)} · ${fmtDate(o.created_at)}</div>
          <ul class="fact-list">
            ${flatFact('Цена', o.price)}
            ${flatFact('Залог', o.deposit)}
            ${flatFact('Собственник', o.owner_name)}
            ${flatFact('Телефон', o.owner_phone, `tel:${(o.owner_phone || '').replace(/[^+\d]/g, '')}`)}
            ${flatFact('Показ', o.showing_time)}
            ${flatFact('Кого рассматривает', o.tenant_criteria)}
            ${flatFact('Комментарий', o.notes)}
          </ul>
        </div>
      `).join('')
    : emptyCard('В этом статусе объектов нет.');

  root.innerHTML = `<div class="counts-row">${pills}</div>${cards}`;

  root.querySelectorAll('.count-pill.filter').forEach((pill) => {
    pill.onclick = () => {
      flatFilter = pill.dataset.filter;
      renderFlats();
    };
  });
}

/* ---------- Тренировки ---------- */

async function renderTrainings() {
  const data = await adminGet('/api/admin/trainings');
  if (!data) return sessionExpired();

  if (!data.sessions.length) {
    root.innerHTML = emptyCard('Тренировок пока нет — никто не дошёл до разбора.');
    return;
  }

  const avg = Math.round(
    data.sessions.reduce((sum, s) => sum + (s.score || 0), 0) / data.sessions.length
  );

  root.innerHTML = `
    <div class="counts-row">
      <div class="count-pill"><div class="n">${data.sessions.length}</div><div class="label">Разговоров</div></div>
      <div class="count-pill"><div class="n">${avg}</div><div class="label">Средний балл</div></div>
      <div class="count-pill"><div class="n">${new Set(data.sessions.map((s) => s.agent_name)).size}</div><div class="label">Агентов</div></div>
    </div>
    ${data.sessions.map((s) => trainingCard(s, data.rubric)).join('')}
  `;
}

function trainingCard(s, rubric) {
  const scoreRows = rubric.map((r) => {
    const got = (s.scores || {})[r.id] || 0;
    return `<div class="score-row ${got >= r.weight ? 'hit' : 'miss'}">
      <span class="label">${escapeHtml(r.label)}</span>
      <span class="pts">${got} / ${r.weight}</span>
    </div>`;
  }).join('');

  const dialogue = (s.messages || []).map((m) => `
    <div class="bubble ${m.role === 'owner' ? 'owner' : 'agent'}">${escapeHtml(m.text)}</div>
  `).join('');

  const advice = (s.advice || []).map((a) => `<li>${escapeHtml(a)}</li>`).join('');

  return `
    <div class="card">
      <div class="card-head">
        <div class="section-title">${agentLabel(s.agent_name, s.agent_username)}</div>
        <div class="badge ${s.score >= 70 ? 'in_progress' : (s.score >= 40 ? 'pending' : 'rejected')}">${s.score} ${plural(s.score, 'балл', 'балла', 'баллов')}</div>
      </div>
      <div class="card-sub">Собеседник: ${escapeHtml(s.persona)} · ${fmtDate(s.finished_at)}</div>
      ${s.verdict ? `<div class="section-hint" style="margin:12px 0 0;">${escapeHtml(s.verdict)}</div>` : ''}
      ${scoreRows}
      ${advice ? `<ul class="video-list">${advice}</ul>` : ''}
      <details class="details-card">
        <summary>Показать разговор целиком</summary>
        <div class="details-body"><div class="chat">${dialogue}</div></div>
      </details>
    </div>
  `;
}

/* ---------- Роутер вкладок ---------- */

const TABS = {
  money: { title: 'Выплаты агентам', render: renderMoney },
  bans: { title: 'Баны', render: renderBans },
  flats: { title: 'Квартиры агентов', render: renderFlats },
  training: { title: 'Тренировки агентов', render: renderTrainings },
};

async function openTab(name) {
  const tab = TABS[name];
  titleEl.textContent = tab.title;
  tabBar.querySelectorAll('.tab-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  root.innerHTML = `<div class="section-hint">Загрузка…</div>`;
  await tab.render();
}

tabBar.querySelectorAll('.tab-btn').forEach((btn) => {
  btn.onclick = () => openTab(btn.dataset.tab);
});

renderGate();
