/* DubLab reviews dashboard */
const key = new URLSearchParams(location.search).get('key') || '';
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
    $('#logopos').value = j.logo_position;
    $('#logosize').value = j.logo_size;
    $('#logosizev').textContent = j.logo_size + '%';
    showLogo(j.has_logo);
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
      body: JSON.stringify({ max_minutes: +$('#maxmin').value, outro: $('#outroin').value,
        logo_position: $('#logopos').value, logo_size: +$('#logosize').value }),
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

load();
setInterval(() => { if (!document.hidden) load(); }, 30000);
