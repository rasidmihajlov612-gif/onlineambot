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

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// PIN живёт только в памяти вкладки — не в localStorage, не в cookie.
// Перезагрузил страницу — вводи заново, это осознанный компромисс для
// потайной панели: удобство сессии против риска, что PIN осядет где-то
// на устройстве.
let sessionPin = null;

async function verifyPin(pin) {
  const res = await fetch('/api/admin/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: initData(), pin }),
  });
  return res.ok;
}

function renderGate(root) {
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
      renderDashboard(root);
    } else {
      status.textContent = 'Неверный PIN или нет доступа с этого аккаунта';
      status.className = 'form-status error';
    }
  };

  btn.onclick = submit;
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
}

async function loadMoney() {
  const url = `/api/admin/money?initData=${encodeURIComponent(initData())}&pin=${encodeURIComponent(sessionPin)}`;
  const res = await fetch(url);
  if (!res.ok) return null;
  return res.json();
}

async function renderDashboard(root) {
  root.innerHTML = `<div class="section-hint">Загрузка…</div>`;
  const data = await loadMoney();

  if (!data) {
    root.innerHTML = `<div class="section-hint">Сессия истекла или PIN устарел — обновите страницу.</div>`;
    return;
  }

  if (!data.agents.length) {
    root.innerHTML = `
      <div class="card">
        <div class="section-title">Выплаты</div>
        <div class="section-hint" style="margin-bottom:0;">Нечего выплачивать — все начисления закрыты.</div>
      </div>
    `;
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
      <div class="section-title" style="font-size:16px;">
        ${escapeHtml(a.full_name)}${a.username ? ` (@${escapeHtml(a.username)})` : ''}
      </div>
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
      try {
        const res = await fetch('/api/admin/payout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ initData: initData(), pin: sessionPin, agent_id: btn.dataset.agent }),
        });
        if (!res.ok) throw new Error('failed');
        btn.closest('.card').remove();
        if (!list.children.length) renderDashboard(root);
      } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Ошибка — повторить';
      }
    };
  });
}

renderGate(document.getElementById('admin-content'));
