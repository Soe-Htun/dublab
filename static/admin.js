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
      `<button class="del" title="delete">🗑️</button></div>` +
      `<div class="stars">${stars}</div>` +
      `<div class="msg">${esc(r.message || '—')}</div>`;
    el.querySelector('.del').addEventListener('click', async () => {
      if (!confirm('ဒီ review ကို ဖျက်မလား?')) return;
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
        caption_size: +$('#capsize').value,
        caption_bottom: +pos.h.capbottom, blur_bottom: +pos.h.blurbottom, blur_height: +pos.h.blurheight,
        caption_bottom_v: +pos.v.capbottom, blur_bottom_v: +pos.v.blurbottom, blur_height_v: +pos.v.blurheight }),
    });
    const j = await r.json().catch(() => ({}));
    msg.textContent = r.ok ? `✅ သိမ်းပြီးပြီ — ${j.max_minutes} မိနစ်အထိ လက်ခံမယ်` : (j.error || 'သိမ်းမရဘူး');
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
  if (!confirm('Logo ကို ဖျက်မလား?')) return;
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
  if (!confirm('Telegram bot ကို Disconnect လုပ်မလား? (ပြန်ချိတ်ရင် token ပြန်ထည့်ရမယ်)')) return;
  await fetch('/api/telegram/disconnect?key=' + encodeURIComponent(key), { method: 'POST' }).catch(() => {});
  tgSay('✅ Disconnect လုပ်ပြီးပြီ — ပြန်ချိတ်ချင်ရင် token ထည့်ပြီး Connect နှိပ်ပါ'); loadTelegram();
});
loadTelegram();

load();
setInterval(() => { if (!document.hidden) load(); }, 30000);

// ---- 🕘 history: recent jobs — download, pick a cover, send to Telegram again ----
let histTimer = null;
const histFmt = (ts) => fmtTime(ts).slice(5);
async function loadHistory() {
  let j;
  try {
    const r = await fetch('/admin/api/history?key=' + encodeURIComponent(key));
    if (!r.ok) { $('#hist-list').innerHTML = '<p class="empty">not found</p>'; return; }
    j = await r.json();
  } catch (e) { $('#hist-list').innerHTML = '<p class="empty">⚠️ ဖွင့်မရဘူး</p>'; return; }
  $('#hist-note').textContent = `(နောက်ဆုံး ${j.jobs.length} ခု · ${j.ttl_hours} နာရီ သိမ်းထားတယ်)`;
  const list = $('#hist-list');
  if (!j.jobs.length) { list.innerHTML = '<p class="empty">job မရှိသေးဘူး</p>'; return; }
  list.innerHTML = '';
  let busy = false;
  for (const x of j.jobs) {
    const row = document.createElement('div'); row.className = 'rev hjob';
    let state;
    if (x.status === 'done') state = '✅ ပြီးပြီ';
    else if (x.status === 'failed') state = '⚠️ ' + esc(x.error || 'မအောင်မြင်ဘူး');
    else if (x.status === 'review') state = '✏️ ဘာသာပြန်စာ စစ်ဖို့ စောင့်နေတယ်';
    else { state = `⏳ ${esc(x.label)} · ${x.pct}%`; busy = true; }
    const title = x.title || x.name || x.id;
    let html = `<div class="meta"><span>${histFmt(x.created)}${x.recap ? ' · 🎞️ recap' : ''}</span><span>${state}</span></div>
      <div class="htitle">${esc(title)}</div>`;
    if (x.title && x.name && x.name !== x.title) html += `<div class="hsub dim">${esc(x.name)}</div>`;
    if (x.url) html += `<div class="hsub"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a></div>`;
    if (x.outputs.length) {
      html += '<div class="hbtns">' + x.outputs.map((o) =>
        `<a class="mini" href="/r/${x.id}${x.outputs.length > 1 ? '/' + o.n : ''}" download>⬇️ ${x.outputs.length > 1 ? 'အပိုင်း ' + o.n : 'Download'} <span class="dim">${o.mb} MB</span></a>`).join('')
        + `<a class="mini" href="/?job=${x.id}">▶️ ဖွင့်ကြည့်မယ်</a></div>`;
    }
    if (x.covers) {
      html += '<div class="hcovers">';
      for (let k = 1; k <= x.covers; k++)
        html += `<img src="/r/${x.id}/cover/${k}" data-k="${k}" class="${k === x.cover_pick ? 'on' : ''}" loading="lazy" alt="cover ${k}">`;
      html += '</div>';
    }
    if (x.status === 'done') {
      let tg = '';
      if (x.tg === 'sending') { tg = `⏳ Telegram ကို ပို့နေတယ် ${x.tg_pct}%`; busy = true; }
      else if (x.tg === 'done') tg = '✅ Telegram ကို ပို့ပြီးပြီ';
      else if (x.tg === 'failed') tg = '⚠️ ' + esc(x.tg_error || 'ပို့မရဘူး');
      html += `<div class="hbtns"><button class="ghost htg" ${x.tg === 'sending' ? 'disabled' : ''}>📨 Telegram ${x.tg === 'done' ? 'ထပ်ပို့မယ်' : 'ပို့မယ်'}</button></div>`
        + (tg ? `<p class="hsub">${tg}</p>` : '');
    }
    row.innerHTML = html;
    row.querySelectorAll('.hcovers img').forEach((im) => im.addEventListener('click', async () => {
      row.querySelectorAll('.hcovers img').forEach((o) => o.classList.toggle('on', o === im));
      await fetch(`/api/jobs/${x.id}/cover`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ n: +im.dataset.k }) }).catch(() => {});
    }));
    const tgb = row.querySelector('.htg');
    if (tgb) tgb.addEventListener('click', async () => {
      if (x.tg === 'done' && !confirm('Telegram ကို ပို့ပြီးသားပါ — ထပ်ပို့မလား?')) return;
      tgb.disabled = true;
      const r = await fetch(`/api/jobs/${x.id}/telegram${x.tg === 'done' ? '?again=1' : ''}`,
        { method: 'POST', headers: { 'X-Admin-Key': key } }).catch(() => null);
      const res = r ? await r.json().catch(() => ({})) : {};
      if (!r || !r.ok) { alert(res.error || 'ပို့မရဘူး'); tgb.disabled = false; return; }
      loadHistory();
    });
    list.append(row);
  }
  clearTimeout(histTimer);
  if (busy) histTimer = setTimeout(loadHistory, 4000);     // live while something runs
}
$('#hist-refresh').addEventListener('click', loadHistory);
loadHistory();
