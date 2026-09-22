const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  // Мини-апп всегда тёмный (фирменный стиль агентства), не подстраивается
  // под системную тему пользователя — поэтому красим и нативную шапку/фон
  // Telegram под тот же цвет, чтобы не было белой рамки вокруг страницы.
  try {
    tg.setHeaderColor('#0b0b0d');
    tg.setBackgroundColor('#0b0b0d');
  } catch (e) { /* старый клиент Telegram — не критично */ }
}

function initData() {
  return tg ? tg.initData : '';
}

let configCache = null;
async function loadConfig() {
  if (configCache) return configCache;
  const res = await fetch('/api/config');
  configCache = await res.json();
  return configCache;
}

async function loadCounts() {
  const res = await fetch('/api/counts?initData=' + encodeURIComponent(initData()));
  if (!res.ok) return { pending: 0, in_progress: 0 };
  return res.json();
}

const CHECKLIST_STORAGE_KEY = 'am_checklist_done';

function getChecklistDone() {
  try {
    return JSON.parse(localStorage.getItem(CHECKLIST_STORAGE_KEY) || '[]');
  } catch (e) {
    return [];
  }
}

function toggleChecklistDone(index) {
  const done = new Set(getChecklistDone());
  if (done.has(index)) done.delete(index); else done.add(index);
  try {
    localStorage.setItem(CHECKLIST_STORAGE_KEY, JSON.stringify([...done]));
  } catch (e) { /* private mode / storage blocked — ignore */ }
  return done;
}

async function renderTraining(root) {
  root.innerHTML = `
    <div class="card">
      <div class="section-title">Обучение</div>
      <div class="section-hint">
        Видео, скрипты и тесты проходятся прямо в чате с ботом — так проще пересдавать
        тесты и не терять прогресс. Нажмите кнопку ниже, чтобы начать (или продолжить).
      </div>
      <button class="btn-primary" id="go-chat-btn">Начать обучение в чате</button>
    </div>
  `;
  const btn = document.getElementById('go-chat-btn');
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await fetch('/api/start-training', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: initData() }),
      });
    } catch (e) { /* best effort — бот всё равно доступен по /start в чате */ }
    if (tg) tg.close();
  };
}

async function renderStart(root) {
  const cfg = await loadConfig();
  const done = new Set(getChecklistDone());
  const items = cfg.checklist || [];
  root.innerHTML = `
    <div class="section-title">Начало работы</div>
    <div class="section-hint">Чек-лист того, что стоит сделать перед первыми звонками.</div>
    <ul class="checklist">
      ${items.map((text, i) => `
        <li class="${done.has(i) ? 'done' : ''}" data-index="${i}">
          <div class="checkbox">${done.has(i) ? '✓' : ''}</div>
          <div class="label">${escapeHtml(text)}</div>
        </li>
      `).join('')}
    </ul>
  `;
  root.querySelectorAll('.checklist li').forEach((li) => {
    li.addEventListener('click', () => {
      const i = Number(li.dataset.index);
      const nowDone = toggleChecklistDone(i);
      li.classList.toggle('done', nowDone.has(i));
      li.querySelector('.checkbox').textContent = nowDone.has(i) ? '✓' : '';
    });
  });
}

const HANDOFF_FIELDS = [
  { key: 'owner_name', label: 'Собственник (ФИО)', required: true },
  { key: 'owner_phone', label: 'Телефон собственника' },
  { key: 'address', label: 'Адрес / район', required: true },
  { key: 'price', label: 'Цена аренды' },
  { key: 'deposit', label: 'Залог' },
  { key: 'showing_time', label: 'Когда удобен показ' },
  { key: 'tenant_criteria', label: 'Кого рассматривает (критерии)' },
  { key: 'notes', label: 'Комментарий', multiline: true },
];

async function renderHandoff(root) {
  root.innerHTML = `
    <div class="section-title">Передача объекта</div>
    <div class="counts-row" id="counts-row">
      <div class="count-pill"><div class="n">—</div><div class="label">на проверке</div></div>
      <div class="count-pill"><div class="n">—</div><div class="label">в работе</div></div>
    </div>
    <form id="handoff-form">
      ${HANDOFF_FIELDS.map((f) => `
        <div class="field">
          <label>${f.label}${f.required ? ' *' : ''}</label>
          ${f.multiline
            ? `<textarea name="${f.key}" rows="3"></textarea>`
            : `<input name="${f.key}" type="text" ${f.key === 'owner_phone' ? 'inputmode="tel"' : ''}>`}
        </div>
      `).join('')}
      <button class="btn-primary" type="submit">Отправить куратору</button>
      <div class="form-status" id="form-status"></div>
    </form>
  `;

  refreshCounts();

  document.getElementById('handoff-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const submitBtn = form.querySelector('button[type=submit]');
    const status = document.getElementById('form-status');

    const payload = { initData: initData() };
    HANDOFF_FIELDS.forEach((f) => { payload[f.key] = form[f.key].value; });

    if (!payload.owner_name.trim() || !payload.address.trim()) {
      status.textContent = 'Заполните обязательные поля: собственник и адрес';
      status.className = 'form-status error';
      return;
    }

    submitBtn.disabled = true;
    status.textContent = '';
    status.className = 'form-status';

    try {
      const res = await fetch('/api/submit-object', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('request failed');
      status.textContent = 'Отправлено куратору на проверку ✅';
      status.className = 'form-status success';
      form.reset();
      refreshCounts();
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    } catch (err) {
      status.textContent = 'Не получилось отправить, попробуйте ещё раз';
      status.className = 'form-status error';
    } finally {
      submitBtn.disabled = false;
    }
  });
}

async function refreshCounts() {
  const row = document.getElementById('counts-row');
  if (!row) return;
  const counts = await loadCounts();
  const pills = row.querySelectorAll('.count-pill .n');
  pills[0].textContent = counts.pending || 0;
  pills[1].textContent = counts.in_progress || 0;
}

async function renderSupport(root) {
  const cfg = await loadConfig();
  root.innerHTML = `
    <div class="card">
      <div class="section-title">Контакт куратора</div>
      <div class="contact-block">${escapeHtml(cfg.contact || '')}</div>
    </div>
    <div class="card">
      <div class="section-title">Общая информация</div>
      <div class="contact-block">${escapeHtml(cfg.support_info || '')}</div>
    </div>
  `;
}

const TABS = {
  training: renderTraining,
  start: renderStart,
  handoff: renderHandoff,
  support: renderSupport,
};

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

async function showTab(tab) {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  const root = document.getElementById('tab-content');
  root.innerHTML = '';
  await TABS[tab](root);
}

document.querySelectorAll('.tab-btn').forEach((btn) => {
  btn.addEventListener('click', () => showTab(btn.dataset.tab));
});

showTab('training');
