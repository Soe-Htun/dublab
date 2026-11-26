/* DubLab reviews dashboard */
const key = new URLSearchParams(location.search).get('key') || '';
// remember it so the ⚙️ link and the main page's Telegram box skip the password
try { if (key) localStorage.setItem('dublab_admin_key', key); } catch (e) { /* ignore */ }
// keep the password out of the address bar / history (reload goes via /settings)
if (key) history.replaceState(null, '', '/settings');
let allReviews = [];
let filter = 'all';

const $ = (s) => document.querySelector(s);

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function load() {
  try {
    const r = await fetch('/admin/api/reviews?key=' + encodeURIComponent(key));
    if (!r.ok) { $('#list').innerHTML = '<p class="empty">not found</p>'; return; }
    const j = await r.json();
    allReviews = j.reviews || [];
    renderStats(j.stats || {});
    renderList();
  } catch (e) {
    $('#list').innerHTML = '<p class="empty">load failed</p>';
  }
}

function renderStats(s) {
  $('#st-total').textContent = s.total ?? 0;
  $('#st-avg').textContent = s.avg ?? '–';
  $('#st-5').textContent = (s.dist && s.dist['5']) ?? 0;
  const dist = $('#dist');
  dist.innerHTML = '';
  const max = Math.max(1, ...[1, 2, 3, 4, 5].map((i) => (s.dist && s.dist[String(i)]) || 0));
  for (let i = 5; i >= 1; i--) {
    const c = (s.dist && s.dist[String(i)]) || 0;
    const row = document.createElement('div');
    row.className = 'drow';
    row.innerHTML = `<span class="lbl">⭐${i}</span><div class="dbar"><span style="width:${(c / max) * 100}%"></span></div><span class="cnt">${c}</span>`;
    dist.appendChild(row);
  }
}

function renderList() {
  const list = $('#list');
  const rows = allReviews.filter((r) => {
    if (filter === 'all') return true;
    if (filter === 'msg') return (r.message || '').trim().length > 0;
    return r.stars === +filter;
  });
  if (!rows.length) { list.innerHTML = '<p class="empty">မရှိသေးဘူး</p>'; return; }
  list.innerHTML = '';
  rows.forEach((r) => {
    const el = document.createElement('div');
    el.className = 'rev';
    const stars = r.stars ? '⭐'.repeat(r.stars) : '<span style="color:var(--dim)">no rating</span>';
    el.innerHTML =
      `<div class="meta"><span>${esc(fmtTime(r.ts))} · ${esc(r.ip || '?')}${r.job_id ? ' · job ' + esc(r.job_id) : ''}</span>` +
      `<button class="del danger" title="delete"><svg class="ic" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/><path d="M10 11v6M14 11v6"/></svg></button></div>` +
      `<div class="stars">${stars}</div>` +
      `<div class="msg">${esc(r.message || '—')}</div>`;
    el.querySelector('.del').addEventListener('click', async () => {
      if (!(await askConfirm({ title: 'ဒီ review ကို ဖျက်မလား?', icon: 'trash', message: 'ပြန်ယူလို့ မရတော့ဘူး' }))) return;
      try {
        const dr = await fetch('/admin/api/reviews/' + encodeURIComponent(r.id || '') +
          '?key=' + encodeURIComponent(key), { method: 'DELETE' });
        if (dr.ok) load();
      } catch (e) { /* ignore */ }
    });
    list.appendChild(el);
  });
}

$('#filters').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
  $('#filters').querySelectorAll('button').forEach((x) => x.classList.remove('on'));
  b.classList.add('on');
  filter = b.dataset.f;
  renderList();
}));
$('#refresh').addEventListener('click', load);

// ---- settings ----
async function loadSettings() {
  try {
    const r = await fetch('/admin/api/settings?key=' + encodeURIComponent(key));
    if (!r.ok) return;
    const j = await r.json();
    $('#maxmin').value = j.max_minutes;
    $('#maxmin').max = j.max_minutes_limit;
    $('#outroin').value = j.outro || '';
    $('#chanin').value = j.channel_name || '';
    $('#logopos').value = j.logo_position;
    $('#logosize').value = j.logo_size;
    $('#logosizev').textContent = j.logo_size + '%';
    showLogo(j.has_logo);
    $('#capsize').value = j.caption_size; $('#capsizev').textContent = j.caption_size + '%';
    $('#speakspeed').value = j.speak_speed; $('#speakspeedv').textContent = (+j.speak_speed).toFixed(2) + 'x';
    pos.h = { capbottom: j.caption_bottom, blurbottom: j.blur_bottom, blurheight: j.blur_height };
    pos.v = { capbottom: j.caption_bottom_v, blurbottom: j.blur_bottom_v, blurheight: j.blur_height_v };
    showOrient();
    $('#setnote').textContent = `🖴 Disk: ${j.disk_free_gb} GB လွတ်သေး / ${j.disk_total_gb} GB · ` +
      `jobs ဖိုင်တွေ ${j.jobs_gb} GB (၂၄ နာရီနေရင် အလိုလို ဖျက်တယ်) · ဖိုင်အရွယ်အစား အများဆုံး ${j.max_mb} MB (.env ထဲက DUBLAB_MAX_MB)`;
  } catch (e) { /* ignore */ }
}
$('#setsave').addEventListener('click', async () => {
  const btn = $('#setsave'), msg = $('#setmsg');
  btn.disabled = true;
  try {
    const r = await fetch('/admin/api/settings?key=' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_minutes: +$('#maxmin').value, outro: $('#outroin').value, channel_name: $('#chanin').value,
        logo_position: $('#logopos').value, logo_size: +$('#logosize').value,
        caption_size: +$('#capsize').value, speak_speed: +$('#speakspeed').value,
        caption_bottom: +pos.h.capbottom, blur_bottom: +pos.h.blurbottom, blur_height: +pos.h.blurheight,
        caption_bottom_v: +pos.v.capbottom, blur_bottom_v: +pos.v.blurbottom, blur_height_v: +pos.v.blurheight }),
    });
    const j = await r.json().catch(() => ({}));
    msg.textContent = r.ok ? '✅ သိမ်းပြီးပြီ' : (j.error || 'သိမ်းမရဘူး');
    if (r.ok) loadSettings();
  } catch (e) { msg.textContent = 'သိမ်းမရဘူး'; }
  msg.hidden = false;
  btn.disabled = false;
  setTimeout(() => (msg.hidden = true), 5000);
});
loadSettings();

// ---- channel logo ----
function showLogo(has) {
  const img = $('#logoimg');
  img.hidden = !has; $('#logonone').hidden = has; $('#logodel').hidden = !has;
  if (has) img.src = '/admin/api/logo?key=' + encodeURIComponent(key) + '&t=' + Date.now();
}
// caption / blur positions: one set for landscape (YouTube), one for portrait (TikTok)
const pos = { h: {}, v: {} };
let orient = 'h';
function showOrient() {
  document.querySelectorAll('.orient .mini').forEach((b) => b.classList.toggle('on', b.dataset.o === orient));
  ['capbottom', 'blurbottom', 'blurheight'].forEach((id) => {
    $('#' + id).value = pos[orient][id]; $('#' + id + 'v').textContent = pos[orient][id] + '%';
  });
  $('#blurprev').classList.toggle('v', orient === 'v');
  drawBlurPreview();
}
document.querySelectorAll('.orient .mini').forEach((b) => b.addEventListener('click', () => {
  orient = b.dataset.o; showOrient();
}));
function drawBlurPreview() {
  const b = +$('#blurbottom').value, h = +$('#blurheight').value;
  const band = $('#blurband');
  band.style.bottom = b + '%'; band.style.height = h + '%';
}
['capsize', 'capbottom', 'blurbottom', 'blurheight'].forEach((id) => $('#' + id).addEventListener('input', () => {
  $('#' + id + 'v').textContent = $('#' + id).value + '%';
  if (id !== 'capsize') pos[orient][id] = +$('#' + id).value;
  drawBlurPreview();
}));
$('#logosize').addEventListener('input', () => ($('#logosizev').textContent = $('#logosize').value + '%'));
$('#logofile').addEventListener('change', async () => {
  const f = $('#logofile').files[0];
  if (!f) return;
  const fd = new FormData(); fd.append('logo', f);
  try {
    const r = await fetch('/admin/api/logo?key=' + encodeURIComponent(key), { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (r.ok) showLogo(true); else alert(j.error || 'တင်မရဘူး');
  } catch (e) { alert('တင်မရဘူး'); }
  $('#logofile').value = '';
});
$('#logodel').addEventListener('click', async () => {
  if (!(await askConfirm({ title: 'Logo ကို ဖျက်မလား?', icon: 'trash', message: 'နောက် ဗီဒီယိုတွေမှာ logo မပါတော့ဘူး' }))) return;
  const r = await fetch('/admin/api/logo?key=' + encodeURIComponent(key), { method: 'DELETE' });
  if (r.ok) showLogo(false);
});

// ---- Telegram: paste the bot token, Connect; stays connected (settings.json) ----
let tgSt = {};
// connected → bot + chat + [Disconnect]; otherwise → steps + token box + [Connect]
// (token saved but chat not linked yet → Connect without retyping the token)
function tgView() {
  const has = !!tgSt.configured, on = !!tgSt.connected;
  $('#tg-status').hidden = !has;
  $('#tg-steps').hidden = on;
  $('#tg-actions').hidden = !on;
  $('#tg-form').hidden = on;
  $('#tg-token').closest('label').hidden = has;
}
async function loadTelegram() {
  try { tgSt = await (await fetch('/api/telegram?key=' + encodeURIComponent(key))).json(); } catch (e) { return; }
  $('#tg-bot').textContent = tgSt.bot ? '@' + tgSt.bot.username : (tgSt.error || '–');
  $('#tg-tok').textContent = tgSt.token ? '· ' + tgSt.token : '';
  $('#tg-chat').textContent = tgSt.connected ? '✅ ' + tgSt.chat : 'မချိတ်ရသေးဘူး';
  tgView();
}
function tgSay(html) { const m = $('#tg-msg'); m.innerHTML = html; m.hidden = false; }
async function tgConnect(token, btn) {
  btn.disabled = true;
  try {
    const r = await fetch('/api/telegram/connect?key=' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) tgSay('⚠️ ' + esc(j.error || 'မရဘူး'));
    else if (j.need_start) {
      const u = esc(j.bot && j.bot.username);
      tgSay(`✅ Token မှန်တယ် — <b>@${u}</b><br>👉 <a href="https://t.me/${u}" target="_blank" rel="noopener">t.me/${u}</a> ကို ဖွင့်ပြီး <b>Start</b> နှိပ်ပါ → ပြီးရင် <b>🔗 Connect</b> ပြန်နှိပ်ပါ`);
    } else tgSay(`✅ <b>${esc(j.chat)}</b> နဲ့ ချိတ်ပြီးပြီ — Telegram မှာ စမ်းသပ်စာ ပို့ထားတယ်`);
    if (r.ok) $('#tg-token').value = '';
  } catch (e) { tgSay('⚠️ မရဘူး'); }
  btn.disabled = false;
  await loadTelegram();
}
$('#tg-connect').addEventListener('click', () => {
  const t = $('#tg-token').value.trim();
  if (!t && !tgSt.configured) { tgSay('⚠️ Bot token ထည့်ပါ'); $('#tg-token').focus(); return; }
  tgConnect(t, $('#tg-connect'));
});
$('#tg-disconnect').addEventListener('click', async () => {
  if (!(await askConfirm({ title: 'Telegram bot ကို ဖြုတ်မလား?', icon: 'unplug', message: 'ပြန်ချိတ်ရင် token ပြန်ထည့်ရမယ်' }))) return;
  await fetch('/api/telegram/disconnect?key=' + encodeURIComponent(key), { method: 'POST' }).catch(() => {});
  tgSay('✅ Disconnect လုပ်ပြီးပြီ — ပြန်ချိတ်ချင်ရင် token ထည့်ပြီး Connect နှိပ်ပါ'); loadTelegram();
});
loadTelegram();

// ---- 📱 TikTok drafts: Connect once (TikTok login through the ngrok address), then parts go to drafts ----
function tkSay(html) { const m = $('#tk-msg'); m.innerHTML = html; m.hidden = false; }
async function loadTikTok() {
  let s;
  try { s = await (await fetch('/api/tiktok?key=' + encodeURIComponent(key))).json(); } catch (e) { return; }
  if (!s.configured) {
    $('#tk-line').innerHTML = '⚠️ <code>.env</code> ထဲမှာ TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI လိုတယ်';
    $('#tk-connect').hidden = true; return;
  }
  $('#tk-line').innerHTML = s.connected
    ? `✅ <b>${esc(s.user)}</b> · ၂၄ နာရီအတွင်း draft <b>${s.today}</b> ခု ပို့ပြီး`
    : 'မချိတ်ရသေးဘူး — Connect နှိပ်ပြီး TikTok မှာ Allow လုပ်ပါ';
  $('#tk-connect').hidden = s.connected;
  $('#tk-disconnect').hidden = !s.connected;
  $('#tk-autorow').hidden = !s.connected;
  $('#tk-tg').hidden = !s.connected;
  $('#tk-auto').checked = !!s.auto;
  $('#tk-queue').innerHTML = (s.queue || []).map(q => `
    <div class="tgline tkrow" data-id="${esc(q.id)}">
      <b>${esc(q.title)}</b> — part <input type="number" class="tkupto" min="0" max="${q.total}" value="${q.upto}" style="width:4em"> / ${q.total} ထိ TikTok မှာ ရှိပြီး
      ${q.upto >= q.total ? '✅' : `→ နောက် <b>part ${q.upto + 1}</b>`}
      ${q.missing ? ` · ⚠️ ဖိုင် ${q.missing} ခု ဖျက်ပြီးသား` : ''}
      ${q.error ? `<br><span class="dim">⚠️ ${esc(q.error)}</span>` : ''}
    </div>`).join('');
  document.querySelectorAll('.tkupto').forEach(inp => inp.addEventListener('change', async () => {
    const id = inp.closest('.tkrow').dataset.id;
    await fetch(`/api/jobs/${id}/tiktok?key=` + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ upto: +inp.value }),
    }).catch(() => {});
    loadTikTok();
  }));
}
$('#tk-connect').addEventListener('click', () => {
  window.open('/tiktok/login?key=' + encodeURIComponent(key), '_blank', 'noopener');
  tkSay('TikTok login tab မှာ Allow နှိပ်ပြီးရင် ဒီစာမျက်နှာ ပြန်လာပါ');
});
$('#tk-disconnect').addEventListener('click', async () => {
  if (!(await askConfirm({ title: 'TikTok ကို ဖြုတ်မလား?', icon: 'unplug', message: 'ပြန်ချိတ်ရင် login ပြန်ဝင်ရမယ်' }))) return;
  await fetch('/api/tiktok/disconnect?key=' + encodeURIComponent(key), { method: 'POST' }).catch(() => {});
  loadTikTok();
});
$('#tk-auto').addEventListener('change', async () => {
  await fetch('/api/tiktok/settings?key=' + encodeURIComponent(key), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ auto: $('#tk-auto').checked }),
  }).catch(() => {});
});
// 📥 parts the bot already sent to Telegram (deleted from this Mac): Telegram → TikTok drafts
function tkTgSay(t) { const m = $('#tk-tgmsg'); m.textContent = t; m.hidden = !t; }
let tkPoll = null;
async function tkTgStatus() {
  try {
    const s = await (await fetch('/api/tiktok/telegram/status?key=' + encodeURIComponent(key))).json();
    tkTgSay(s.msg || '');
    if (s.state !== 'sending' && tkPoll) { clearInterval(tkPoll); tkPoll = null; loadTikTok(); $('#tk-scan').click(); }
  } catch (e) { /* next tick */ }
}
$('#tk-scan').addEventListener('click', async () => {
  const btn = $('#tk-scan'); btn.disabled = true; tkTgSay('🔎 ရှာနေတယ်…'); $('#tk-films').innerHTML = '';
  try {
    const r = await fetch(`/api/tiktok/telegram?days=${$('#tk-days').value}&key=` + encodeURIComponent(key));
    const j = await r.json();
    if (!r.ok) { tkTgSay('⚠️ ' + (j.error || 'မရဘူး')); btn.disabled = false; return; }
    tkTgSay(j.films.length ? '' : 'ပို့စရာ ဇာတ်ကား မရှိဘူး');
    const extra = [j.finished ? `✅ ${j.finished} ကား အကုန် ပို့ပြီး (စာရင်းက ဖယ်ထား)` : '',
                   j.hidden ? `🙈 ${j.hidden} ကား ဝှက်ထား · <a href="#" id="tk-unhide">ပြန်ပြမယ်</a>` : ''].filter(Boolean).join(' · ');
    $('#tk-films').innerHTML = j.films.map((f, i) => {
      const next = Math.min(f.done + 1, f.parts.length), left = f.parts.length - f.done;
      const opts = f.parts.map(p => `<option value="${p.n}"${p.n === next ? ' selected' : ''}>${esc(p.label)}</option>`).join('');
      return `<div class="tkfilm" data-i="${i}">
        <div class="tkhead"><b>${esc(f.title)}</b><span class="dim">part ${f.parts.length} ခု${f.done ? ` · ✅ ${f.done} ထိ ပို့ပြီး` : ''}
          <button class="tkhide" title="စာရင်းက ဖယ်မယ်">✕</button></span></div>
        ${left <= 0 ? '<p class="dim">✅ အကုန် ပို့ပြီးပြီ</p>' : `
        <div class="tkpick">
          <label>part <select class="tkfrom">${opts}</select> ကနေ</label>
          <label><input type="number" class="tkcount" min="1" max="50" value="${Math.min(5, left)}"> ခု</label>
          <button class="ghost tksend">📤 draft ပို့မယ်</button>
        </div>
        <p class="dim tkrange"></p>`}
      </div>`;
    }).join('') + (extra ? `<p class="dim tkextra">${extra}</p>` : '');
    document.querySelectorAll('.tkhide').forEach(b => b.addEventListener('click', async () => {
      const f = j.films[+b.closest('.tkfilm').dataset.i];
      await fetch('/api/tiktok/telegram/hide?key=' + encodeURIComponent(key), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: f.title, hidden: true }),
      }).catch(() => {});
      b.closest('.tkfilm').remove();
    }));
    const un = $('#tk-unhide');
    if (un) un.addEventListener('click', async e => {
      e.preventDefault();
      await fetch('/api/tiktok/telegram/hide?key=' + encodeURIComponent(key), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: '*' }),
      }).catch(() => {});
      $('#tk-scan').click();
    });
    const range = row => {
      const f = j.films[+row.dataset.i], from = +row.querySelector('.tkfrom').value;
      const pick = f.parts.filter(p => p.n >= from).slice(0, Math.max(1, +row.querySelector('.tkcount').value || 1));
      row.querySelector('.tkrange').textContent = pick.length ? `→ part ${pick[0].label} – ${pick[pick.length - 1].label} (${pick.length} ခု)` : '';
      return pick;
    };
    document.querySelectorAll('.tkfilm').forEach(row => {
      if (!row.querySelector('.tkfrom')) return;
      range(row);
      row.querySelector('.tkfrom').addEventListener('change', () => range(row));
      row.querySelector('.tkcount').addEventListener('input', () => range(row));
    });
    document.querySelectorAll('.tksend').forEach(b => b.addEventListener('click', async () => {
      const row = b.closest('.tkfilm'), f = j.films[+row.dataset.i], pick = range(row);
      if (!pick.length) return;
      if (!(await askConfirm({ title: `${f.title} — part ${pick[0].label}–${pick[pick.length - 1].label} (${pick.length} ခု) draft ပို့မလား?`, icon: 'upload', message: 'Telegram ကနေ ယူပြီး TikTok draft ထဲ ပို့မယ်' }))) return;
      const r2 = await fetch('/api/tiktok/telegram?key=' + encodeURIComponent(key), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: f.title, from: pick[0].n, count: pick.length }),
      });
      const j2 = await r2.json().catch(() => ({}));
      if (!r2.ok) { tkTgSay('⚠️ ' + (j2.error || 'မရဘူး')); return; }
      tkTgSay('📤 စပို့နေပြီ…');
      if (!tkPoll) tkPoll = setInterval(tkTgStatus, 3000);
    }));
  } catch (e) { tkTgSay('⚠️ မရဘူး'); }
  btn.disabled = false;
});
loadTikTok();
tkTgStatus();
setInterval(() => { if (!document.hidden) loadTikTok(); }, 30000);

load();
setInterval(() => { if (!document.hidden) load(); }, 30000);

$('#speakspeed').addEventListener('input', () => { $('#speakspeedv').textContent = (+$('#speakspeed').value).toFixed(2) + 'x'; });

// fill each slider's track up to its thumb (also when a value is set from the server)
function fillRange(el) { const p = (el.value - el.min) * 100 / (el.max - el.min); el.style.setProperty('--fill', p + '%'); }
document.querySelectorAll('.setinput input[type=range]').forEach((el) => {
  fillRange(el); el.addEventListener('input', () => fillRange(el));
  new MutationObserver(() => fillRange(el)).observe(el, { attributes: true });
});
setInterval(() => document.querySelectorAll('.setinput input[type=range]').forEach(fillRange), 500);
