const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  // Мини-апп всегда тёмный (фирменный стиль агентства), не подстраивается
  // под системную тему пользователя — поэтому красим и нативную шапку/фон
  // Telegram под тот же цвет, чтобы не было белой рамки вокруг страницы.
  try {
    tg.setHeaderColor('#000000');
    tg.setBackgroundColor('#000000');
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

async function loadProgress() {
  const res = await fetch('/api/progress?initData=' + encodeURIComponent(initData()));
  if (!res.ok) return null;
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
  const [cfg, progress] = await Promise.all([loadConfig(), loadProgress()]);

  const progressBlock = (progress && progress.started) ? `
    <div class="progress-track"><div class="progress-fill" style="width:${progress.percent}%"></div></div>
    <div class="progress-caption">
      <span>${escapeHtml(progress.step_label || '')}</span>
      <span>${progress.percent}%</span>
    </div>
  ` : '';

  const videos = (cfg.extra_videos || []).filter((v) => v.url && !String(v.url).startsWith('TODO'));
  const videosBlock = videos.length ? `
    <div class="card">
      <div class="section-title">Доп. материалы</div>
      <ul class="video-list">
        ${videos.map((v) => `<li><a href="${escapeAttr(v.url)}" target="_blank">🎬 ${escapeHtml(v.title)}</a></li>`).join('')}
      </ul>
    </div>
  ` : '';

  root.innerHTML = `
    <div class="card">
      <div class="section-title">Обучение</div>
      ${progressBlock}
      <div class="section-hint">
        Видео, скрипты и тесты проходятся прямо в чате с ботом — так проще пересдавать
        тесты и не терять прогресс. Нажмите кнопку ниже, чтобы начать (или продолжить).
      </div>
      <button class="btn-primary" id="go-chat-btn">Начать обучение в чате</button>
    </div>
    ${videosBlock}
    ${renderSelfStudy(cfg.self_study || {})}
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

function renderResourceList(items) {
  return `
    <ul class="resource-list">
      ${items.map((r) => `
        <li>
          <a href="${escapeAttr(r.url || '#')}" target="_blank">
            <div class="resource-title">${escapeHtml(r.title || r.name)}</div>
            ${r.desc ? `<div class="resource-desc">${escapeHtml(r.desc)}</div>` : ''}
            ${r.time ? `<div class="resource-time">${escapeHtml(r.time)}</div>` : ''}
          </a>
        </li>
      `).join('')}
    </ul>
  `;
}

function renderSelfStudy(ss) {
  if (!ss || !ss.top5) return '';

  const f30 = ss.first_30_seconds || {};
  const plan = ss.plan_30_days || {};

  return `
    <div class="card">
      <div class="eyebrow-label">Для самостоятельного развития</div>
      <div class="section-title">Топ-5, если времени мало</div>
      ${ss.intro ? `<div class="section-hint">${escapeHtml(ss.intro)}</div>` : ''}
      ${renderResourceList(ss.top5)}
    </div>

    <details class="card details-card">
      <summary>Первые 30 секунд звонка</summary>
      <div class="details-body">
        ${f30.intro ? `<div class="section-hint">${escapeHtml(f30.intro)}</div>` : ''}
        <ul class="fact-list">
          ${(f30.facts || []).map((f) => `
            <li class="fact-row">
              <span class="fact-text">${escapeHtml(f.fact)}</span>
              <span class="fact-value">${escapeHtml(f.value)}</span>
            </li>
          `).join('')}
        </ul>
        ${f30.skeleton_title ? `<div class="section-title" style="font-size:15px;margin-top:16px;">${escapeHtml(f30.skeleton_title)}</div>` : ''}
        <ol class="skeleton-list">
          ${(f30.skeleton || []).map((s) => `<li>${escapeHtml(s)}</li>`).join('')}
        </ol>
      </div>
    </details>

    <details class="card details-card">
      <summary>Каналы</summary>
      <div class="details-body">${renderResourceList(ss.channels || [])}</div>
    </details>

    <details class="card details-card">
      <summary>Книги</summary>
      <div class="details-body">${renderResourceList(ss.books || [])}</div>
    </details>

    <details class="card details-card">
      <summary>Скрипты под аренду</summary>
      <div class="details-body">${renderResourceList(ss.scripts_links || [])}</div>
    </details>

    <details class="card details-card">
      <summary>План на 30 дней</summary>
      <div class="details-body">
        ${plan.intro ? `<div class="section-hint">${escapeHtml(plan.intro)}</div>` : ''}
        ${(plan.weeks || []).map((w) => `
          <div class="plan-week">
            <div class="week-label">Неделя ${w.week}</div>
            <div class="week-what">${escapeHtml(w.what)}</div>
            <div class="week-check">✓ ${escapeHtml(w.check)}</div>
          </div>
        `).join('')}
      </div>
    </details>
  `;
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
      <div class="contact-block">${linkifyContact(cfg.contact || '')}</div>
    </div>
    <div class="card">
      <div class="section-title">Общая информация</div>
      <div class="contact-block">${escapeHtml(cfg.support_info || '')}</div>
    </div>
  `;
}

function linkifyContact(text) {
  // escapeHtml сначала — регексы ниже вставляют уже безопасные <a>-теги поверх
  return escapeHtml(text)
    .replace(/@(\w+)/g, '<a href="https://t.me/$1" target="_blank">@$1</a>')
    .replace(/(\+\d[\d\s\-]{7,}\d)/g, (m) => `<a href="tel:${m.replace(/[\s\-]/g, '')}">${m}</a>`);
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

function escapeAttr(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
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
