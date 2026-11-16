const $ = (s) => document.querySelector(s);
const drop = $('#drop'), fileInput = $('#file'), preview = $('#preview');
const direction = 'auto2mm';  // language is detected on the server
let voice = 'auto', pollTimer = null, lastJid = null, ytUrl = '';

function selectGrid(gridSel, attr, cb) {
  document.querySelectorAll(gridSel + ' .opt').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll(gridSel + ' .opt').forEach((x) => x.classList.remove('selected'));
      b.classList.add('selected');
      cb(b.dataset[attr]);
    });
  });
}
selectGrid('#voicegrid', 'voice', (v) => (voice = v));
let model = (document.querySelector('#modelgrid .opt.selected') || {}).dataset?.model || 'small';
selectGrid('#modelgrid', 'model', (v) => (model = v));

drop.addEventListener('click', () => fileInput.click());
['dragover', 'dragenter'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (ev) => {
  if (ev.dataTransfer.files.length) handleFile(ev.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) handleFile(fileInput.files[0]); });

function handleFile(f) {
  ytUrl = ''; $('#urlin').value = ''; syncUrlBtn();
  if (!/^video\//.test(f.type) && !/\.(mp4|mov|mkv|webm|m4v)$/i.test(f.name)) {
    alert('ဗီဒီယိုဖိုင်ပဲ တင်လို့ရတယ်'); return;
  }
  preview.src = URL.createObjectURL(f);
  preview.hidden = false;
  const meta = $('#filemeta');
  meta.hidden = false;
  meta.textContent = `${f.name} · ${(f.size / 1048576).toFixed(1)} MB`;
  $('#card-opts').hidden = false;
  drop.querySelector('.drop-title').innerHTML = '✅ ရွေးပြီးပြီ <span class="dim">— ပြောင်းချင်ရင် နှိပ်ပါ</span>';
  $('#card-opts').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ---- YouTube / TikTok link instead of a file ----
const YT_RE = /^https?:\/\/((www|m|music|vm|vt)\.)?(youtube\.com|youtu\.be|tiktok\.com)\//i;
function useUrl() {
  const u = $('#urlin').value.trim();
  if (!YT_RE.test(u)) { alert('YouTube / TikTok link ပဲ ထည့်လို့ရတယ်'); return; }
  ytUrl = u;
  fileInput.value = '';
  preview.hidden = true; preview.removeAttribute('src');
  const meta = $('#filemeta');
  meta.hidden = false;
  meta.textContent = '🔗 ' + u;
  drop.querySelector('.drop-title').innerHTML = 'နှိပ်၍ ရွေးပါ <span class="dim">သို့မဟုတ် ဖိုင်ဆွဲချပါ</span>';
  $('#card-opts').hidden = false;
  $('#card-opts').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
const syncUrlBtn = () => { $('#urlbtn').disabled = !$('#urlin').value.trim(); };
$('#urlin').addEventListener('input', syncUrlBtn);
syncUrlBtn();  // browser may restore a value after reload
$('#urlbtn').addEventListener('click', useUrl);
$('#urlin').addEventListener('keydown', (e) => { if (e.key === 'Enter' && $('#urlin').value.trim()) useUrl(); });

$('#go').addEventListener('click', () => {
  const f = fileInput.files[0];
  if (!f && !ytUrl) return;
  const btn = $('#go');
  btn.disabled = true; btn.textContent = ytUrl ? '⏳ link စစ်နေတယ်…' : '⏳ တင်နေတယ်…';
  const fd = new FormData();
  if (ytUrl) fd.append('url', ytUrl); else fd.append('video', f); fd.append('direction', direction); fd.append('voice', voice);
  fd.append('review', $('#reviewchk').checked ? '1' : '0');
  fd.append('keep_bg', $('#bgchk').checked ? '1' : '0');
  fd.append('model', model);
  fd.append('outro', $('#outrochk') && $('#outrochk').checked ? '1' : '0');
  fd.append('logo', $('#logochk') && $('#logochk').checked ? '1' : '0');
  fd.append('captions', $('#capchk').checked ? '1' : '0');
  fd.append('blur', $('#blurchk').checked ? '1' : '0');
  // NOTE: iOS Safari's fetch() throws "The string did not match the expected
  // pattern" on FormData POST uploads (WebKit bug) — XHR works reliably.
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/jobs');
  xhr.onload = () => {
    btn.disabled = false; btn.textContent = '✨ Dub စလုပ်မယ်';
    try {
      const j = JSON.parse(xhr.responseText);
      if (xhr.status >= 200 && xhr.status < 300 && j.job_id) showProgress(j.job_id);
      else showError(j.error || 'upload failed');
    } catch (e) { showError('upload failed'); }
  };
  xhr.onerror = () => { btn.disabled = false; btn.textContent = '✨ Dub စလုပ်မယ်'; showError('upload failed'); };
  xhr.send(fd);
});

function showProgress(jid) {
  ['card-upload', 'card-opts', 'card-review', 'card-result', 'card-error'].forEach((id) => ($('#' + id).hidden = true));
  $('#card-progress').hidden = false;
  $('#card-progress').scrollIntoView({ behavior: 'smooth' });
  const isUrl = !!ytUrl;
  const dl = document.querySelector('#stages li[data-s="download"]');
  if (dl) dl.hidden = !isUrl;
  const order = ['download', 'audio', 'transcribe', 'translate', 'review', 'music', 'voice', 'mux'];
  addRings();
  const stopLog = startLiveLog(jid);
  const stopAll = () => { clearInterval(pollTimer); stopLog(); };
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/jobs/' + jid);
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'status failed');
      $('#barfill').style.width = j.pct + '%';
      $('#stagelabel').textContent = j.label + (j.pct ? ` · ${j.pct}%` : '');
      document.querySelectorAll('#stages li').forEach((li) => {
        const s = li.dataset.s;
        li.classList.remove('active', 'done', 'spin');
        let p = 0;
        if (j.stage === 'done' || order.indexOf(s) < order.indexOf(j.stage)) { li.classList.add('done'); p = 100; }
        else if (s === j.stage) {
          li.classList.add('active');
          if (j.stage_pct == null) li.classList.add('spin'); else p = j.stage_pct;
        }
        setRing(li, p, s === j.stage && j.stage_pct != null);
      });
      if (j.status === 'review') { stopAll(); showReview(jid); }
      if (j.status === 'done') { stopAll(); showResult(jid); }
      if (j.status === 'failed') { stopAll(); showError(j.error || 'အမှားရှိတယ်'); }
    } catch (e) { stopAll(); showError(e.message); }
  }, 2000);
}

// ---- circular progress ring on each stage row ----
const RING_C = 2 * Math.PI * 15;  // circumference for r=15
function addRings() {
  document.querySelectorAll('#stages li').forEach((li) => {
    if (li.querySelector('.ring')) return;
    li.insertAdjacentHTML('beforeend',
      '<span class="ring" aria-hidden="true"><svg viewBox="0 0 36 36">' +
      '<circle class="trk" cx="18" cy="18" r="15"/>' +
      `<circle class="val" cx="18" cy="18" r="15" stroke-dasharray="${RING_C}" stroke-dashoffset="${RING_C}"/>` +
      '</svg><b></b></span>');
  });
}
function setRing(li, pct, showNum) {
  const ring = li.querySelector('.ring');
  if (!ring) return;
  const val = ring.querySelector('.val');
  if (!li.classList.contains('spin')) val.setAttribute('stroke-dashoffset', RING_C * (1 - pct / 100));
  ring.querySelector('b').textContent = li.classList.contains('done') ? '✓' : (showNum ? pct + '%' : '');
}

// Live job logs — polls /api/jobs/<jid>/log, auto-scrolls.
function startLiveLog(jid) {
  const pre = $('#livelog');
  pre.textContent = '';
  const t = setInterval(async () => {
    try {
      const r = await fetch('/api/jobs/' + jid + '/log');
      const j = await r.json();
      if (r.ok && typeof j.log === 'string' && j.log !== pre.textContent) {
        pre.textContent = j.log;
        pre.scrollTop = pre.scrollHeight;
      }
    } catch (e) { /* keep old content */ }
  }, 2000);
  return () => clearInterval(t);
}

// ---- check / edit translation before voicing ----
function fmtTime(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ':' + String(sec).padStart(2, '0');
}
async function showReview(jid) {
  $('#card-progress').hidden = true;
  const box = $('#lines');
  box.textContent = '';
  try {
    const r = await fetch('/api/jobs/' + jid + '/lines');
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'load failed');
    j.lines.forEach((ln) => {
      const row = document.createElement('div');
      row.className = 'line';
      const meta = document.createElement('div');
      meta.className = 'line-meta';
      meta.textContent = `${fmtTime(ln.start)}–${fmtTime(ln.end)} · ${ln.orig}`;
      const ta = document.createElement('textarea');
      ta.rows = 2; ta.maxLength = 500; ta.value = ln.text;
      row.append(meta, ta);
      box.append(row);
    });
  } catch (e) { showError(e.message); return; }
  $('#card-review').hidden = false;
  $('#card-review').scrollIntoView({ behavior: 'smooth' });
  const btn = $('#voicego');
  btn.disabled = false;
  btn.onclick = () => {
    const texts = [...box.querySelectorAll('textarea')].map((t) => t.value);
    btn.disabled = true; btn.textContent = '⏳ ပို့နေတယ်…';
    const xhr = new XMLHttpRequest();  // XHR, not fetch (iOS Safari POST bug)
    xhr.open('POST', '/api/jobs/' + jid + '/lines');
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = () => {
      btn.textContent = '🎙️ အသံဖော်မယ်';
      let j = {};
      try { j = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
      if (xhr.status < 300 && j.ok) showProgress(jid);
      else { btn.disabled = false; alert(j.error || 'ပို့မရဘူး'); }
    };
    xhr.onerror = () => { btn.disabled = false; btn.textContent = '🎙️ အသံဖော်မယ်'; alert('ပို့မရဘူး'); };
    xhr.send(JSON.stringify({ texts: texts }));
  };
}

function showResult(jid) {
  $('#card-progress').hidden = true;
  $('#card-result').hidden = false;
  lastJid = jid;
  $('#resultvideo').src = '/r/' + jid;
  setSpeed(1);
  $('#download').href = '/r/' + jid;
  $('#dlsrt').href = '/r/' + jid + '.srt';
  initTikTok(jid);
  $('#card-result').scrollIntoView({ behavior: 'smooth' });
}

function showError(msg) {
  if (pollTimer) clearInterval(pollTimer);
  ['card-upload', 'card-opts', 'card-progress', 'card-review', 'card-result'].forEach((id) => ($('#' + id).hidden = true));
  $('#card-error').hidden = false;
  $('#errmsg').textContent = msg;
}

$('#again').addEventListener('click', () => location.reload());
$('#retry').addEventListener('click', () => location.reload());

// ---- reviews ----
function makeStars(id) {
  const el = document.getElementById(id);
  let val = 0;
  const btns = el.querySelectorAll('button');
  btns.forEach((b) => b.addEventListener('click', () => {
    val = +b.dataset.s;
    btns.forEach((x) => (x.textContent = +x.dataset.s <= val ? '⭐' : '☆'));
  }));
  return {
    get: () => val,
    reset: () => { val = 0; btns.forEach((x) => (x.textContent = '☆')); },
  };
}
const rstars = makeStars('rstars'), fstars = makeStars('fstars');

function postReview(stars, msgEl, jid, doneEl, starsObj) {
  const msg = msgEl.value.trim();
  if (!stars && !msg) { alert('star ဒါမှမဟုတ် စာတစ်ခုခု ပေးပါ'); return; }
  const xhr = new XMLHttpRequest();  // XHR, not fetch (iOS Safari FormData/POST bug)
  xhr.open('POST', '/api/reviews');
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = () => {
    let ok = false, err = '';
    try { const j = JSON.parse(xhr.responseText); ok = xhr.status < 300 && !!j.ok; err = j.error || ''; }
    catch (e) { /* ignore */ }
    if (ok) {
      doneEl.hidden = false;
      msgEl.value = ''; starsObj.reset();
      setTimeout(() => (doneEl.hidden = true), 4000);
    } else if (err) alert(err);
    else alert('ပို့မရဘူး');
  };
  xhr.onerror = () => alert('ပို့မရဘူး');
  xhr.send(JSON.stringify({ stars: stars, message: msg, job_id: jid || '' }));
}
$('#rsend').addEventListener('click', () =>
  postReview(rstars.get(), $('#rmsg'), lastJid, $('#rdone'), rstars));
$('#fsend').addEventListener('click', () =>
  postReview(fstars.get(), $('#fmsg'), null, $('#fdone'), fstars));

// ---- playback speed (review request: အနှေး/အမြန်) ----
function setSpeed(r) {
  const v = $('#resultvideo');
  if (v) v.playbackRate = r;
  const slider = $('#speedslider'), val = $('#speedval');
  if (slider) slider.value = r;
  if (val) val.textContent = r.toFixed(2) + 'x';
  document.querySelectorAll('#speedrow button').forEach((x) =>
    x.classList.toggle('on', parseFloat(x.dataset.r) === r));
}
document.querySelectorAll('#speedrow button').forEach((b) => b.addEventListener('click', () => {
  setSpeed(parseFloat(b.dataset.r));
}));
(function () {
  const slider = $('#speedslider');
  if (slider) slider.addEventListener('input', () => setSpeed(parseFloat(slider.value)));
})();

// ---- TikTok posting (owner only: needs the admin password) ----
const TTKEY = 'dublab_admin_key';
const ttKey = () => { try { return localStorage.getItem(TTKEY) || ''; } catch (e) { return ''; } };
const ttHeaders = (extra) => Object.assign({ 'X-Admin-Key': ttKey() }, extra || {});
let ttJid = null, ttTimer = null;

async function initTikTok(jid) {
  ttJid = jid;
  let st;
  try { st = await (await fetch('/api/tiktok', { headers: ttHeaders() })).json(); } catch (e) { return; }
  if (!st.configured) return;                 // TikTok not set up on this server
  $('#ttbox').hidden = false;
  ['tt-auth', 'tt-connect', 'tt-form'].forEach((id) => ($('#' + id).hidden = true));
  if (!st.authorized) { $('#tt-auth').hidden = false; return; }
  if (!st.connected) { $('#tt-connect').hidden = false; return; }
  $('#tt-form').hidden = false;
  $('#tt-name').textContent = st.user.display_name || 'TikTok';
  if (st.user.avatar_url) $('#tt-avatar').src = st.user.avatar_url;
  try {
    const c = await (await fetch('/api/tiktok/creator', { headers: ttHeaders() })).json();
    const sel = $('#tt-privacy');
    sel.length = 1;
    const label = { PUBLIC_TO_EVERYONE: 'Everyone', MUTUAL_FOLLOW_FRIENDS: 'Friends', FOLLOWER_OF_CREATOR: 'Followers', SELF_ONLY: 'Only me' };
    (c.privacy_level_options || []).forEach((v) => sel.add(new Option(label[v] || v, v)));
    [['tt-comment', c.comment_disabled], ['tt-duet', c.duet_disabled], ['tt-stitch', c.stitch_disabled]]
      .forEach(([id, off]) => { $('#' + id).disabled = !!off; $('#' + id).checked = false; });
    if (c.creator_nickname) $('#tt-name').textContent = c.creator_nickname;
  } catch (e) { /* posting will report the error */ }
  pollTikTok();
}

$('#tt-keybtn').addEventListener('click', () => {
  try { localStorage.setItem(TTKEY, $('#tt-key').value.trim()); } catch (e) {}
  initTikTok(ttJid);
});
$('#tt-login').addEventListener('click', () => {
  window.open('/tiktok/login?key=' + encodeURIComponent(ttKey()), 'tiktok_login', 'width=520,height=760');
});
window.addEventListener('message', (e) => {
  if (e.origin === location.origin && e.data && 'tiktok' in e.data) initTikTok(ttJid);
});
$('#tt-logout').addEventListener('click', async () => {
  if (!confirm('TikTok account ကို ဖြုတ်မလား?')) return;
  await fetch('/api/tiktok/disconnect', { method: 'POST', headers: ttHeaders() });
  initTikTok(ttJid);
});
document.querySelectorAll('input[name="ttmode"]').forEach((r) => r.addEventListener('change', () => {
  const direct = document.querySelector('input[name="ttmode"]:checked').value === 'direct';
  $('#tt-direct').hidden = !direct; $('#tt-draftnote').hidden = direct;
}));
$('#tt-post').addEventListener('click', async () => {
  const mode = document.querySelector('input[name="ttmode"]:checked').value;
  const body = { mode, title: $('#tt-title').value, privacy: $('#tt-privacy').value,
    allow_comment: $('#tt-comment').checked, allow_duet: $('#tt-duet').checked, allow_stitch: $('#tt-stitch').checked };
  if (mode === 'direct' && !body.privacy) { alert('Who can view this video — ရွေးပါ'); return; }
  $('#tt-post').disabled = true;
  try {
    const r = await fetch('/api/jobs/' + ttJid + '/tiktok', { method: 'POST',
      headers: ttHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { alert(j.error || 'တင်မရဘူး'); $('#tt-post').disabled = false; return; }
  } catch (e) { alert('တင်မရဘူး'); $('#tt-post').disabled = false; return; }
  pollTikTok();
});
function pollTikTok() {
  clearInterval(ttTimer);
  const show = async () => {
    let t;
    try { t = await (await fetch('/api/jobs/' + ttJid + '/tiktok')).json(); } catch (e) { return; }
    const s = $('#tt-status');
    if (t.state === 'uploading') { s.textContent = `⏳ TikTok ကို တင်နေတယ်… ${t.progress || 0}%`; $('#tt-post').disabled = true; }
    else if (t.state === 'processing') { s.textContent = '⏳ TikTok က ပြင်ဆင်နေတယ်…'; $('#tt-post').disabled = true; }
    else if (t.state === 'done') {
      s.textContent = t.mode === 'draft' ? '✅ TikTok app ရဲ့ Draft/inbox ထဲ ပို့ပြီးပြီ' : '✅ TikTok မှာ တင်ပြီးပြီ (ပေါ်လာဖို့ မိနစ်အနည်းငယ် ကြာနိုင်တယ်)';
      $('#tt-post').disabled = false; clearInterval(ttTimer);
    } else if (t.state === 'failed') { s.textContent = '⚠️ ' + (t.error || 'မအောင်မြင်ဘူး'); $('#tt-post').disabled = false; clearInterval(ttTimer); }
    else { s.textContent = ''; clearInterval(ttTimer); }
  };
  show();
  ttTimer = setInterval(show, 3000);
}
