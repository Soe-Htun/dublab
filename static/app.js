const $ = (s) => document.querySelector(s);
const drop = $('#drop'), fileInput = $('#file'), preview = $('#preview');
const direction = 'auto2mm';  // language is detected on the server
let voice = 'auto', pollTimer = null, lastJid = null, ytUrl = '', split = '0', tgMsg = null, recap = '0';

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
  ytUrl = ''; tgMsg = null; $('#urlin').value = ''; syncUrlBtn();
  if (!/^video\//.test(f.type) && !/\.(mp4|mov|mkv|webm|m4v)$/i.test(f.name)) {
    alert('ဗီဒီယိုဖိုင်ပဲ တင်လို့ရတယ်'); return;
  }
  preview.src = URL.createObjectURL(f);
  preview.hidden = false;
  srcDur = 0;
  preview.onloadedmetadata = () => { srcDur = preview.duration || 0; setRecapLen(recapLen); };
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
  ytUrl = u; tgMsg = null; srcDur = 0; setRecapLen(recapLen);
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
  if (!f && !ytUrl && !tgMsg) return;
  const btn = $('#go');
  btn.disabled = true; btn.textContent = ytUrl || tgMsg ? '⏳ စစ်နေတယ်…' : '⏳ တင်နေတယ်…';
  const fd = new FormData();
  if (tgMsg) fd.append('tg_msg', tgMsg); else if (ytUrl) fd.append('url', ytUrl); else fd.append('video', f);
  fd.append('direction', direction); fd.append('voice', voice);
  fd.append('review', $('#reviewchk').checked ? '1' : '0');
  fd.append('keep_bg', '0');          // background music option removed
  fd.append('model', model);
  fd.append('outro', $('#outrochk') && $('#outrochk').checked ? '1' : '0');
  fd.append('intro', $('#introchk').checked ? '1' : '0');
  fd.append('hook', $('#hookchk') && $('#hookchk').checked ? '1' : '0');
  fd.append('commentary', '1');       // 🎙️ silent scenes explained: part of the character-voice style
  fd.append('fullstyle', '');          // 🗣️ the character-voice style is the 🎬 recap now
  fd.append('youtube', $('#ytchk').checked ? '1' : '0');
  fd.append('ytmode', 'recap');       // 📺 YouTube = the recap as one video (the 83% long version was dropped)
  fd.append('show_title', $('#titlechk').checked ? '1' : '0');
  fd.append('title', ($('#covertitle_in').value || '').trim());
  fd.append('episode', ($('#episode_in').value || '').replace(/\D/g, ''));
  fd.append('cover', '1');            // every video gets a cover
  fd.append('split', split);
  const orig = $('#fullchk').checked;   // 🎞️ no recap = original sound + Burmese captions
  fd.append('orig_audio', orig ? '1' : '0');
  fd.append('no_music', orig && $('#nomusicchk') && $('#nomusicchk').checked ? '1' : '0');   // 🎵 only when ticked (~25 min on a 2-hour film)
  fd.append('recap', $('#recapchk').checked && !orig ? recapLen : '0');
  fd.append('game', $('#gamechk') && $('#gamechk').checked ? '1' : '0');
  fd.append('game_cut', $('#gamecutchk') && $('#gamecutchk').checked ? '1' : '0');
  fd.append('game_slow', $('#gameslowchk') && $('#gameslowchk').checked ? '1' : '0');
  fd.append('game_music', $('#gamemusicchk') && $('#gamemusicchk').checked ? '1' : '0');
  fd.append('game_meme', $('#gamememechk') && $('#gamememechk').checked ? '1' : '0');
  fd.append('game_hero', ($('#gamehero_in') && $('#gamehero_in').value || '').trim());
  const music = $('#musicchk') && $('#musicchk').checked;        // 🎵 a Burmese song + typewriter lyrics
  fd.append('music', music ? '1' : '0');
  if (music && $('#musicphoto').files[0]) fd.append('music_photo', $('#musicphoto').files[0]);
  if (music) fd.append('music_lyrics', ($('#musiclyrics').value || '').trim());
  if (music) fd.append('music_title', ($('#musictitle').value || '').trim());
  try { localStorage.setItem('dublab_split', split); } catch (e) {}
  fd.append('logo', $('#logochk') && $('#logochk').checked ? '1' : '0');
  fd.append('telegram', !$('#tgrow').hidden && $('#tgchk').checked ? '1' : '0');
  fd.append('posts', '0');            // 📝 captions now go with each video on Telegram (no separate message)
  fd.append('captions', $('#capchk').checked ? '1' : '0');
  fd.append('blur', $('#blurchk').checked ? '1' : '0');
  fd.append('censor', $('#censorchk').checked ? '1' : '0');
  // NOTE: iOS Safari's fetch() throws "The string did not match the expected
  // pattern" on FormData POST uploads (WebKit bug) — XHR works reliably.
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/jobs');
  xhr.setRequestHeader('X-Admin-Key', admKey());
  xhr.onload = () => {
    btn.disabled = false; btn.textContent = '✨ Dub စလုပ်မယ်';
    try {
      const j = JSON.parse(xhr.responseText);
      if (xhr.status >= 200 && xhr.status < 300 && j.job_id) {
        showProgress(j.job_id);
      }
      else showError(j.error || 'upload failed');
    } catch (e) { showError('upload failed'); }
  };
  xhr.onerror = () => { btn.disabled = false; btn.textContent = '✨ Dub စလုပ်မယ်'; showError('upload failed'); };
  xhr.send(fd);
});

const JOBKEY = 'dublab_job';     // per tab (sessionStorage): a new tab starts a new job, a refresh keeps this one
try { localStorage.removeItem(JOBKEY); } catch (e) {}   // the old browser-wide key
function showProgress(jid) {
  try { sessionStorage.setItem(JOBKEY, jid); } catch (e) {}    // a refresh brings this job back
  try { history.replaceState(null, '', '/?job=' + jid); } catch (e) {}   // the address shows this job, not an old one
  ['card-upload', 'card-opts', 'card-review', 'card-result', 'card-error'].forEach((id) => ($('#' + id).hidden = true));
  $('#card-progress').hidden = false;
  $('#card-progress').scrollIntoView({ behavior: 'smooth' });
  const isUrl = !!ytUrl || !!tgMsg;
  const dl = document.querySelector('#stages li[data-s="download"]');
  if (dl) dl.hidden = !isUrl;
  const sp = document.querySelector('#stages li[data-s="split"]');
  if (sp) sp.hidden = !(+split);       // only when a split length is chosen
  const rc = document.querySelector('#stages li[data-s="recap"]');
  const isRecap = $('#recapchk').checked;
  if (rc) rc.hidden = !isRecap;
  const vo = document.querySelector('#stages li[data-s="voice"]');
  if (vo) vo.hidden = $('#fullchk').checked;   // 🔊 original sound: no voicing
  const mu = document.querySelector('#stages li[data-s="music"]');
  if (mu) mu.hidden = true;           // no background-music step any more
  const order = ['fixcheck', 'fixing', 'download', 'audio', 'transcribe', 'translate', 'review', 'music', 'voice', 'recap', 'mux', 'split'];
  addRings();
  const stopLog = startLiveLog(jid);
  const stopAll = () => { clearInterval(pollTimer); stopLog(); };
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/jobs/' + jid);
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'status failed');
      $('#barfill').style.width = j.pct + '%';
      const vli = document.querySelector('#stages li[data-s="voice"]');
      if (vli && j.orig) vli.hidden = true;            // 🔊 original sound (also after a refresh)
      // the step running now is always shown (music removal, captions…): with it hidden, no ring
      // turned and the page looked stuck
      const now = document.querySelector(`#stages li[data-s="${j.stage}"]`);
      if (now && now.hidden && j.kind !== 'fix') now.hidden = false;
      if (j.kind === 'fix') document.querySelectorAll('#stages li').forEach((li) => {
        li.hidden = !['fixcheck', 'fixing'].includes(li.dataset.s);
      });
      $('#stagelabel').textContent = j.status === 'queued' && j.ahead
        ? `⏳ တန်းစီနေတယ် — ရှေ့က job ${j.ahead} ခု ပြီးရင် အလိုလို စမယ် (ဒီ tab ကို ဖွင့်ထားရုံပဲ)`
        : j.label + (j.pct ? ` · ${j.pct}%` : '');
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
      if (j.status === 'done') {
        stopAll();
        showResult(jid, j.parts); $('#coverbox').hidden = true;   // covers go to Telegram; not shown here
        showFixNote(j);
        showRisk(j);
        showPosts(j);
        showYtChild(j.yt_child);
      }
      if (j.status === 'failed') { stopAll(); showError(j.error || 'အမှားရှိတယ်', j.resumable ? jid : null); }
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
    $('#introbox').hidden = j.intro == null;
    $('#titlebox').hidden = j.title == null;
    $('#titletext').value = j.title || '';
    $('#introtext').value = j.intro || '';
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
    const body = { texts: texts };
    if (!$('#introbox').hidden) body.intro = $('#introtext').value;
    if (!$('#titlebox').hidden) body.title = $('#titletext').value;
    xhr.send(JSON.stringify(body));
  };
}

const mmss = (s) => Math.floor(s / 60) + ':' + String(Math.round(s % 60)).padStart(2, '0');
// 📺 the YouTube long version is its own job: a button on the TikTok result opens it
function showYtChild(c) {
  const b = $('#ytchild');
  b.hidden = !c;
  if (!c) return;
  const st = c.status === 'done' ? '✅ ပြီးပြီ' : c.status === 'failed' ? '❌ မအောင်မြင် — နှိပ်ပြီး ▶️ ဆက်လုပ်ပါ'
    : c.status === 'queued' ? '⏳ TikTok ပြီးရင် စမယ်' : `⚙️ လုပ်နေဆဲ · ${c.pct}%`;
  b.textContent = `📺 YouTube အရှည် version (${st})`;
  b.onclick = () => (c.status === 'done' ? openDone(c.id) : showProgress(c.id));
}
async function openDone(jid) {
  const j = await (await fetch('/api/jobs/' + jid)).json();
  showResult(jid, j.parts); $('#coverbox').hidden = true; showYtChild(null);
  showFixNote(j); showRisk(j); showPosts(j);
}

// 🔧 a fix job: its explanation / appeal on the result card; when the video could not be
// fixed by editing there is no new video — only the explanation is shown
function showFixNote(j) {
  const box = $('#fixnote');
  const fix = j && j.kind === 'fix';
  box.hidden = !(fix && j.fix_note);
  if (box.hidden) { $('#resulthead').textContent = '🎉 ရပြီ!'; $('#fixappeal').hidden = true; return; }
  // the note without the appeal (that has its own box with a copy button)
  box.textContent = (j.fix_note || '').split('\n📝 Appeal')[0].trim();
  const ap = $('#fixappeal');
  ap.hidden = !j.fix_appeal;
  if (j.fix_appeal) {
    $('#fixappealtext').textContent = j.fix_appeal;
    $('#fixcopy').onclick = async () => {
      try { await navigator.clipboard.writeText(j.fix_appeal); }
      catch (e) {                                   // older browsers: select and copy
        const r = document.createRange(); r.selectNodeContents($('#fixappealtext'));
        const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand('copy');
      }
      const b = $('#fixcopy');                      // ✓ for 2 seconds, then the copy icon again
      b.classList.add('done'); b.title = 'Copied!';
      clearTimeout(b._t); b._t = setTimeout(() => { b.classList.remove('done'); b.title = 'Copy'; }, 2000);
    };
  }
  box.classList.toggle('warn', !j.fix_changed);
  const noVideo = !j.fix_changed;
  $('#resulthead').textContent = noVideo ? '⚠️ ဗီဒီယို ပြင်လို့ မရပါ' : '🔧 ပြင်ပြီးပါပြီ';
  ['#resultvideo', '#speedrow', '#download', '#parts'].forEach((s) => { const e = $(s); if (e && noVideo) e.hidden = true; });
  if (noVideo) { $('#resultvideo').removeAttribute('src'); }
}

// 📝 upload texts with a copy button each (no more file names as captions)
const COPY_SVG = '<svg class="ic-copy" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
  + '<svg class="ic-ok" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
function showPosts(j) {
  const box = $('#postsbox'); if (!box) return;
  const p = j && j.posts;
  box.hidden = !(p && p.items && p.items.length);
  if (box.hidden) return;
  const wrap = $('#postsitems'); wrap.innerHTML = '';
  const items = p.ai ? p.items.concat([{ label: '🤖 AI ရုပ်ရှင်', text: '', note: 'TikTok မှာ “AI-generated content”၊ YouTube မှာ “Altered or synthetic content → Yes” ကို ဖွင့်ပါ' }]) : p.items;
  items.forEach((it) => {
    const d = document.createElement('div'); d.className = 'fixappeal postitem';
    const h = document.createElement('div'); h.className = 'fixappeal-head'; h.innerHTML = '<b></b>'; h.firstChild.textContent = it.label;
    d.appendChild(h);
    const t = document.createElement('p'); t.textContent = it.text || it.note || ''; d.appendChild(t);
    if (it.text) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'copybtn'; b.title = 'Copy'; b.innerHTML = COPY_SVG;
      b.onclick = async () => {
        try { await navigator.clipboard.writeText(it.text); }
        catch (e) { const r = document.createRange(); r.selectNodeContents(t); const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand('copy'); }
        b.classList.add('done'); b.title = 'Copied!';
        clearTimeout(b._t); b._t = setTimeout(() => { b.classList.remove('done'); b.title = 'Copy'; }, 2000);
      };
      d.appendChild(b);
    }
    wrap.appendChild(d);
  });
}

// ⚠️ before uploading: what could bring a claim / warning / strike
function showRisk(j) {
  const box = $('#riskbox');
  const r = (j && j.kind !== 'fix' && j.risk) || [];
  box.hidden = !r.length;
  if (!r.length) return;
  const worst = r[0].level;
  $('#riskhead').textContent = { high: '🔴 Upload မလုပ်ခင် သတိ — အန္တရာယ် ရှိတယ်',
    mid: '🟡 Upload မလုပ်ခင် စစ်ကြည့်ပါ', low: '🟢 အန္တရာယ် နည်းတယ်' }[worst];
  const ul = $('#risklist'); ul.innerHTML = '';
  r.forEach((x) => { const li = document.createElement('li'); li.className = x.level; li.textContent = x.text; ul.appendChild(li); });
}

function showResult(jid, parts) {
  $('#card-progress').hidden = true;
  $('#card-result').hidden = false;
  $('#homeback').hidden = false;          // the header's back arrow: to a fresh upload page
  ['#resultvideo', '#speedrow'].forEach((s) => { const e = $(s); if (e) e.hidden = false; });
  $('#fixnote').hidden = true; $('#fixappeal').hidden = true; $('#riskbox').hidden = true;
  lastJid = jid;
  $('#resultvideo').src = '/r/' + jid;
  setSpeed(1);
  $('#download').href = '/r/' + jid;
  // split video: one row per part — play it here, or download it
  const box = $('#parts');
  box.innerHTML = '';
  const many = parts && parts.length > 1;
  box.hidden = !many; $('#download').hidden = many;
  if (many) {
    parts.forEach((p) => {
      const row = document.createElement('div'); row.className = 'part';
      const play = document.createElement('button'); play.className = 'ghost' + (p.n === 1 ? ' on' : '');
      play.textContent = p.full ? `📺 YouTube (အပြည့်)  ·  ${mmss(p.end)}`
        : (p.tt ? '📱 TikTok ' : '▶️ ') + `အပိုင်း ${p.k || p.n}  ·  ${mmss(p.start)}–${mmss(p.end)}`;
      play.addEventListener('click', () => {
        $('#resultvideo').src = '/r/' + jid + '/' + p.n; setSpeed(1);
        box.querySelectorAll('.ghost').forEach((b) => b.classList.toggle('on', b === play));
      });
      const dl = document.createElement('a'); dl.className = 'cta dl'; dl.href = '/r/' + jid + '/' + p.n;
      dl.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/></svg>'; dl.title = 'Download · ' + p.name; dl.setAttribute('aria-label', 'Download');
      row.append(play, dl); box.append(row);
    });
  }
  initTelegram(jid);
  $('#card-result').scrollIntoView({ behavior: 'smooth' });
}

let resumeJid = null;
function showError(msg, canResume) {
  if (pollTimer) clearInterval(pollTimer);
  ['card-upload', 'card-opts', 'card-progress', 'card-review', 'card-result'].forEach((id) => ($('#' + id).hidden = true));
  $('#card-error').hidden = false;
  $('#errmsg').textContent = msg;
  resumeJid = canResume || null;          // ▶️ only when the job's files are still kept
  $('#resume').hidden = !resumeJid;
}

$('#resume').addEventListener('click', async () => {
  if (!resumeJid) return;
  const btn = $('#resume'); btn.disabled = true;
  try {
    const r = await fetch('/api/jobs/' + resumeJid + '/resume', { method: 'POST' });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'resume failed');
    showProgress(resumeJid);
  } catch (e) { $('#errmsg').textContent = e.message; $('#resume').hidden = true; }
  btn.disabled = false;
});

// back to a fresh upload page (not location.reload(): the address may hold ?job=… and reopen it)
const goHome = () => { try { sessionStorage.removeItem(JOBKEY); } catch (e) {} location.href = '/'; };
$('#again').addEventListener('click', goHome);
$('#homeback').addEventListener('click', goHome);
$('#retry').addEventListener('click', () => { try { sessionStorage.removeItem(JOBKEY); } catch (e) {} location.reload(); });

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
const fstars = makeStars('fstars');

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

// ---- 📨 videos forwarded to the bot (owner only) ----
const fmtDur = (s) => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s % 60).padStart(2, '0'); };
async function loadTgVideos() {
  const list = $('#tglist');
  list.hidden = false; list.textContent = '⏳ …';
  let j = {};
  try {
    const r = await fetch('/api/telegram/videos', { headers: { 'X-Admin-Key': admKey() } });
    j = await r.json();
    if (!r.ok) { list.textContent = '⚠️ ' + (j.error || 'မရဘူး'); return; }
  } catch (e) { list.textContent = '⚠️ မရဘူး'; return; }
  list.innerHTML = '';
  const hint = document.createElement('p'); hint.className = 'note left';
  hint.textContent = `Telegram မှာ ဗီဒီယိုကို @${j.bot} ဆီ forward လုပ်ပြီး 🔄 နှိပ်ပါ (၂၄ နာရီအတွင်း ပို့ထားတာတွေ ပေါ်မယ်)`;
  const refresh = document.createElement('button'); refresh.className = 'ghost mini-r'; refresh.textContent = '🔄';
  refresh.addEventListener('click', loadTgVideos);
  const head = document.createElement('div'); head.className = 'tghead'; head.append(hint, refresh);
  list.append(head);
  if (!j.videos.length) { const p = document.createElement('p'); p.className = 'note'; p.textContent = 'ဗီဒီယို မရှိသေးဘူး'; list.append(p); return; }
  j.videos.forEach((v) => {
    const b = document.createElement('button'); b.className = 'ghost tgitem';
    const long = j.max_minutes > 0 && v.duration > j.max_minutes * 60;
    b.innerHTML = '';
    const t = document.createElement('b'); t.textContent = '🎬 ' + v.name;
    const m = document.createElement('span'); m.className = 'dim';
    m.textContent = ` · ${fmtDur(v.duration)} · ${(v.size / 1048576).toFixed(0)} MB` + (v.from ? ' · ' + v.from : '') +
      (long ? ` · ⚠️ ${j.max_minutes} မိနစ်ထက် ရှည်တယ်` : '');
    b.append(t, m);
    b.addEventListener('click', () => {
      tgMsg = v.id; ytUrl = ''; fileInput.value = ''; $('#urlin').value = ''; syncUrlBtn();
      srcDur = v.duration; setRecapLen(recapLen);
      preview.hidden = true; preview.removeAttribute('src');
      list.querySelectorAll('.tgitem').forEach((x) => x.classList.toggle('on', x === b));
      const meta = $('#filemeta'); meta.hidden = false; meta.textContent = '📨 ' + v.name;
      $('#card-opts').hidden = false;
      $('#card-opts').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
    list.append(b);
  });
}

// ---- cover: one for the whole film, used for every part ----
function showCovers(jid, n, title, nMain) {
  nMain = nMain || n;
  const box = $('#coverbox'), grid = $('#covergrid');
  box.hidden = !n; grid.innerHTML = '';
  if (!n) return;
  $('#covertitle').textContent = title ? '— ' + title : '';
  grid.classList.toggle('many', n > 1);
  for (let k = 1; k <= n; k++) {      // split film: one cover per part ("title - 1", "title - 2"…)
    const a = document.createElement('a');
    a.href = `/r/${jid}/cover/${k}?dl=1`; a.className = 'coverlink'; a.title = 'download';
    const im = document.createElement('img');
    im.src = `/r/${jid}/cover/${k}`; im.alt = 'cover ' + k; im.loading = 'lazy';
    a.append(im);
    if (n > 1) {
      const tt = k > nMain, kk = tt ? k - nMain : k;
      const c = document.createElement('span'); c.textContent = `⬇️ ${tt ? '📱 TikTok ' : ''}အပိုင်း ${kk}`; a.append(c);
    }
    grid.append(a);
  }
  $('#coverdl').hidden = n > 1;
  $('#coverdl').href = `/r/${jid}/cover/1?dl=1`;
}

// ---- Telegram (owner only: needs the admin password) ----
const ADMKEY = 'dublab_admin_key';
const admKey = () => { try { return localStorage.getItem(ADMKEY) || ''; } catch (e) { return ''; } };
let tgJid = null, tgTimer = null;

async function tgState() {
  try { return await (await fetch('/api/telegram', { headers: { 'X-Admin-Key': admKey() } })).json(); }
  catch (e) { return { configured: false }; }
}
// options card: "send to Telegram when done" checkbox for the owner
(async function () {
  const st = await tgState();
  if (st.configured && st.authorized && st.connected) {
    $('#tgpick').hidden = false;
    $('#tgrow').hidden = false;
    $('#tgrow-to').textContent = '→ ' + (st.chat || '') + (st.bot && st.bot.username ? ' (@' + st.bot.username + ')' : '');
  }
})();

async function initTelegram(jid) {
  tgJid = jid;
  const st = await tgState();
  if (!st.configured) return;
  $('#tgbox').hidden = false;
  ['tg-auth', 'tg-noconn', 'tg-send'].forEach((id) => ($('#' + id).hidden = true));
  if (!st.authorized) { $('#tg-auth').hidden = false; return; }
  if (!st.connected) { $('#tg-noconn').hidden = false; return; }
  $('#tg-send').hidden = false;
  $('#tg-to').textContent = '→ ' + (st.chat || '');
  pollTelegram();
}
$('#tg-keybtn').addEventListener('click', () => {
  try { localStorage.setItem(ADMKEY, $('#tg-key').value.trim()); } catch (e) {}
  initTelegram(tgJid);
});
let tgSent = false;
$('#tg-btn').addEventListener('click', async () => {
  if (tgSent && !(await askConfirm({ title: 'Telegram ကို ထပ်ပို့မလား?', icon: 'send', tone: 'primary',
    message: 'ပို့ပြီးသားပါ' }))) return;
  $('#tg-btn').disabled = true;
  try {
    const r = await fetch('/api/jobs/' + tgJid + '/telegram' + (tgSent ? '?again=1' : ''),
      { method: 'POST', headers: { 'X-Admin-Key': admKey() } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { alert(j.error || 'ပို့မရဘူး'); $('#tg-btn').disabled = false; return; }
  } catch (e) { alert('ပို့မရဘူး'); $('#tg-btn').disabled = false; return; }
  pollTelegram();
});
function pollTelegram() {
  clearInterval(tgTimer);
  const show = async () => {
    let t;
    try { t = await (await fetch('/api/jobs/' + tgJid + '/telegram')).json(); } catch (e) { return; }
    const s = $('#tg-status');
    if (t.state === 'sending') {
      s.textContent = '⏳ Telegram ကို ပို့နေတယ်…' + (t.pct ? ' ' + t.pct + '%' : '') + (t.sent ? ` (${t.sent} ပိုင်း ပို့ပြီး)` : '');
      $('#tg-btn').disabled = true;
    } else if (t.state === 'done') {
      tgSent = true;
      s.textContent = t.how === 'link' ? '✅ ပို့ပြီးပြီ — ဖိုင်ကြီးလို့ download link ပို့ထားတယ်'
        : '✅ Telegram ကို ဗီဒီယို ပို့ပြီးပြီ' + (t.gone ? ' · 🗑️ disk နေရာ မစားအောင် ဒီစက်ကနေ ဖျက်လိုက်ပြီ' : '');
      if (t.gone) { $('#tg-btn').hidden = true; clearInterval(tgTimer); return; }
      $('#tg-btn').firstChild.textContent = '📨 ထပ်ပို့မယ် ';
      $('#tg-btn').disabled = false; clearInterval(tgTimer);
    } else if (t.state === 'failed') { s.textContent = '⚠️ ' + (t.error || 'မအောင်မြင်ဘူး'); $('#tg-btn').disabled = false; clearInterval(tgTimer); }
    else if (t.pre === 'uploading') { s.textContent = '⬆️ Telegram အတွက် ကြိုတင် တင်ထားနေတယ် ' + (t.pre_pct || 0) + '% — နှိပ်ထားရင် ပြီးတာနဲ့ ပို့မယ်'; }
    else if (t.pre === 'ready') { s.textContent = '⚡ အဆင်သင့် — နှိပ်တာနဲ့ ချက်ချင်း ပို့မယ်'; clearInterval(tgTimer); }
    else { s.textContent = ''; clearInterval(tgTimer); }
  };
  show();
  tgTimer = setInterval(show, 3000);
}

// ⚙️: with a remembered password, open the admin page directly (no login hop)
(() => {
  const g = document.querySelector('.gear');
  const k = admKey();
  if (g && k) g.href = '/admin/reviews?key=' + encodeURIComponent(k);
})();

// remember the last split choice
// ✂️ split length: chips; remembers the last choice
function setSplit(v) {
  const b = document.querySelector(`#splitgrid .opt[data-split="${v}"]`);
  if (!b) return;
  split = v;
  document.querySelectorAll('#splitgrid .opt').forEach((x) => x.classList.toggle('selected', x === b));
  if (typeof partsNote === 'function' && $('#recapchk')) try { partsNote(); } catch (e) { /* not ready yet */ }
}
document.querySelectorAll('#splitgrid .opt').forEach((b) => b.addEventListener('click', () => setSplit(b.dataset.split)));
setSplit('5');                         // ✂️ default: 5-minute TikTok parts (the ~80% recap made 3-min parts too many)

$('#tgpickbtn').addEventListener('click', loadTgVideos);

// 🎬 recap version (checkbox → length) and how many parts that makes
let srcDur = 0;          // seconds of the chosen video, when known
function partsNote() {
  const sp = +split, rc = $('#recapchk').checked ? srcDur * 0.8 / 60 : 0;   // 🎬 recap ≈ 80% of the film
  const len = rc ? rc * 60 : srcDur;              // finished video length (s)
  const yt = $('#ytchk') && $('#ytchk').checked;
  let txt = +sp ? `${sp} မိနစ်ထက် ရှည်ရင် ${sp} မိနစ်စီ ခွဲမယ် — နာမည်-1, နာမည်-2 … နာမည်-End` : 'ဗီဒီယို တစ်ခုတည်း ထွက်မယ်';
  if (+sp && len) {
    // same rule as the server: no part under 4 min (merged, or all parts made equal)
    let n = Math.ceil(len / (sp * 60));
    if (n > 1 && len - (n - 1) * sp * 60 < Math.min(240, sp * 30) && len / (n - 1) <= sp * 60 * 1.3) n -= 1;
    n = Math.max(1, n);
    txt = n > 1 ? `≈ ${n} ပိုင်း ထွက်မယ် (${sp} မိနစ်စီ) — နာမည်-1 … နာမည်-End` : 'ဗီဒီယို တစ်ခုတည်း ထွက်မယ်';
  }
  if (+sp && yt) txt = '📱 TikTok: ' + txt + ' · 📺 YouTube: တစ်ကားလုံး ၁ ခု';
  $('#splitnote').textContent = txt + (+sp ? ' · like & follow က အပိုင်းတိုင်းရဲ့ အဆုံးမှာ' : '');
}
let recapLen = 'auto';
function setRecapLen(v) {
  if (v === '50') v = '40';            // old saved choice (50+ became 40+)
  let b = document.querySelector(`#recapgrid .opt[data-recap="${v}"]`);
  if (!b) { v = 'auto'; b = document.querySelector('#recapgrid .opt[data-recap="auto"]'); }   // old saved choice
  recapLen = v;
  document.querySelectorAll('#recapgrid .opt').forEach((x) => x.classList.toggle('selected', x === b));
  if (v === 'auto') {                  // 🤖 ~30% of the film's story, 2–25 min
    const m = srcDur ? Math.max(2, Math.min(25, Math.round(srcDur * 0.28 / 60))) : 0;
    $('#recapnote').textContent = '🤖 Recap အရှည်ကို AI က ဆုံးဖြတ်မယ် — ဇာတ်ကားရဲ့ ~30% (အများဆုံး 25 မိနစ်)' +
      (m ? ` · မူရင်း ${Math.round(srcDur / 60)} မိနစ်ဆို ~${m}–${Math.round(m * 1.15)} မိနစ်` : '');
    partsNote();
    return;
  }
  const aim = Math.round(v * 1.25);
  const src = srcDur ? ` (မူရင်း ${Math.round(srcDur / 60)} မိနစ်)` : '';
  $('#recapnote').textContent = srcDur && aim * 60 >= srcDur * 0.85
    ? `⚠️ မူရင်းက ${Math.round(srcDur / 60)} မိနစ်ပဲ ရှိလို့ ${v}+ မိနစ် recap က မတိုဘူး — ပိုတိုတာ ရွေးပါ (မဟုတ်ရင် အပြည့် dub မယ်)`
    : `ဇာတ်လမ်းအဓိကအခန်းတွေကို ရွေးပြီး အနည်းဆုံး ${v} မိနစ် (intro/outro မပါ · ~${v}–${+v >= 40 ? 60 : aim + 5} မိနစ်) recap ထုတ်မယ်${src}`;
  partsNote();
}
// 🎞️ no recap → may keep the film's own sound (🔊): then there is no voice to pick
function syncOrig() {
  const full = $('#fullchk').checked;
  $('#fullopts').hidden = !full;
  const on = full;
  if ($('#origwarn')) $('#origwarn').hidden = !on;   // ⚠️ the "unoriginal" warning: subtitles-only style
  $('#voicegrid').hidden = on;
  $('#voicehead').hidden = on;
}

function syncRecap() {
  const on = $('#recapchk').checked;
  $('#recapopts').hidden = !on;
  recap = on ? recapLen : '0';
  setRecapLen(recapLen);
}
// recap / no recap are radios: either one changing re-syncs everything
['#recapchk', '#fullchk', '#gamechk', '#musicchk'].forEach((id) => $(id).addEventListener('change', () => {
  syncRecap(); syncOrig(); try { syncYtMode(); partsNote(); } catch (e) { /* not ready */ }
  if ($('#gameopts')) $('#gameopts').hidden = !$('#gamechk').checked;
  if ($('#musicopts')) $('#musicopts').hidden = !$('#musicchk').checked;
  if ($('#musicchk').checked) setSplit('0');          // 🎵 one song = one video
  // 🖼️ title: on for a recap, off for the plain video (can still be changed by hand)
  $('#titlechk').checked = $('#recapchk').checked;
  $('#covertitle_in').hidden = !$('#titlechk').checked; $('#eprow').hidden = !$('#titlechk').checked;
  // 📺 YouTube 16:9: on by default in both modes (can still be unticked by hand)
  $('#ytchk').checked = true;
  // ✂️ a recap goes to TikTok in 5-minute parts (can still be changed by hand)
  if ($('#recapchk').checked) setSplit('5');
  try { syncYtMode(); partsNote(); } catch (e) { /* not ready */ }
}));
document.querySelectorAll('#recapgrid .opt').forEach((b) => b.addEventListener('click', () => {
  setRecapLen(b.dataset.recap); recap = recapLen;
  try { localStorage.setItem('dublab_recap_len', recapLen); } catch (e) {}
}));
try {
  $('#recapchk').checked = true;     // recap is the default every time; untick for a full dub
  recapLen = 'auto';                   // the length is always decided automatically now
} catch (e) { /* ignore */ }
syncRecap(); syncOrig();

// 🖼️ title: the text box only matters while "add a title" is ticked
$('#titlechk').addEventListener('change', () => { $('#covertitle_in').hidden = $('#eprow').hidden = !$('#titlechk').checked; });

// after a refresh: bring back the job this browser was following while it is still
// running, waiting for review, or failed (▶️ resume); a finished one is forgotten
(async () => {
  let jid = null;
  try { jid = sessionStorage.getItem(JOBKEY); } catch (e) {}
  const q = new URLSearchParams(location.search).get('job');    // /?job=<id> opens that job
  if (q) jid = q;
  if (!jid) return;
  try {
    let r = await fetch('/api/jobs/' + jid);
    if (!r.ok) { sessionStorage.removeItem(JOBKEY); return; }
    let j = await r.json();
    // TikTok done but its 📺 YouTube long version still going (or failed): follow that one
    const c = j.status === 'done' && j.yt_child;
    if (c && c.status !== 'done') {
      r = await fetch('/api/jobs/' + c.id);
      if (r.ok) { jid = c.id; j = await r.json(); }
    }
    if (j.status === 'processing' || j.status === 'queued') showProgress(jid);
    else if (j.status === 'review') showReview(jid);
    else if (j.status === 'done') {
      sessionStorage.removeItem(JOBKEY);
      if (q) openDone(jid);            // opened by its link (/?job=…): show that result, not an empty page
    }
    else if (j.status === 'failed') showError(j.error || 'အမှားရှိတယ်', j.resumable ? jid : null);
  } catch (e) { /* server down: stay on the upload page */ }
})();

$('#ytchk').addEventListener('change', () => { try { partsNote(); } catch (e) { /* not ready */ } });

// ⏹ stop the running job and start over (it would otherwise come back after a refresh)
$('#stopjob').addEventListener('click', async () => {
  const yes = await askConfirm({ title: 'ရပ်ပြီး အသစ်ပြန်လုပ်မလား?', icon: 'restart',
    message: 'လုပ်ပြီးသမျှ ပျက်သွားမယ်၊ ဆက်လုပ်လို့ မရတော့ဘူး' });
  if (!yes) return;
  let jid = null;
  try { jid = sessionStorage.getItem(JOBKEY); sessionStorage.removeItem(JOBKEY); } catch (e) {}
  if (jid) { try { await fetch('/api/jobs/' + jid + '/cancel', { method: 'POST' }); } catch (e) {} }
  location.reload();
});

// 📺 YouTube length choice: only when YouTube is ticked and it is a recap
function syncYtMode() { /* the YouTube length choice was removed */ }
$('#ytchk').addEventListener('change', syncYtMode);
syncYtMode();

// 📝 upload texts only make sense when the videos go to Telegram

// ---- 🔧 fix one flagged part (owner only: it needs the admin key) ----
(function () {
  const open = $('#fixopen');
  if (!open) return;
  if (admKey()) open.hidden = false;
  let fixCensor = 'auto';
  const back = $('#fixback'), fdrop = $('#fixdrop'), finput = $('#fixfile');
  let fixFile = null;
  // Same as the settings page: the back arrow sits in the header while the fix card is open.
  const showFix = (on) => {
    $('#card-fix').hidden = !on; $('#card-upload').hidden = on; back.hidden = !on;
    window.scrollTo(0, 0);
  };
  open.addEventListener('click', () => showFix(true));
  back.addEventListener('click', () => showFix(false));
  // Same drop box as step 1: click to pick or drag a file in.
  const setFixFile = (f) => {
    fixFile = f || null;
    $('#fixfilename').innerHTML = fixFile
      ? '✅ ' + fixFile.name.replace(/[&<>"']/g, (c) => '&#' + c.charCodeAt(0) + ';') + ' <span class="dim">— ပြောင်းချင်ရင် နှိပ်ပါ</span>'
      : 'နှိပ်၍ ရွေးပါ <span class="dim">သို့မဟုတ် ဖိုင်ဆွဲချပါ</span>';
  };
  fdrop.addEventListener('click', () => finput.click());
  ['dragover', 'dragenter'].forEach((e) => fdrop.addEventListener(e, (ev) => { ev.preventDefault(); fdrop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((e) => fdrop.addEventListener(e, (ev) => { ev.preventDefault(); fdrop.classList.remove('over'); }));
  fdrop.addEventListener('drop', (ev) => {
    const f = ev.dataTransfer.files[0];
    if (f && f.type.startsWith('video/')) setFixFile(f);
  });
  finput.addEventListener('change', () => setFixFile(finput.files[0]));
  // 📸 screenshots of the warning (the model reads the text in them)
  // a drop box like the video one, with thumbnails (each removable); ⌘V pastes a screenshot
  let shots = [];
  const sdrop = $('#shotdrop'), sinput = $('#fixshots'), grid = $('#shotgrid');
  const drawShots = () => {
    if (!grid) return;
    grid.innerHTML = ''; grid.hidden = !shots.length;
    shots.forEach((f, i) => {
      const t = document.createElement('div'); t.className = 'shot';
      const img = document.createElement('img'); img.src = URL.createObjectURL(f); img.alt = '';
      const x = document.createElement('button'); x.type = 'button'; x.className = 'shotx'; x.setAttribute('aria-label', 'remove');
      x.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';
      x.addEventListener('click', (e) => { e.stopPropagation(); shots.splice(i, 1); drawShots(); });
      t.append(img, x); grid.append(t);
    });
  };
  const addShots = (files) => {
    [...files].filter((f) => f.type.startsWith('image/')).forEach((f) => { if (shots.length < 5) shots.push(f); });
    drawShots();
  };
  // (an older page without the drop box still works: the plain file input is used)
  if (sdrop) {
    sdrop.addEventListener('click', () => sinput.click());
    ['dragover', 'dragenter'].forEach((e) => sdrop.addEventListener(e, (ev) => { ev.preventDefault(); sdrop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((e) => sdrop.addEventListener(e, (ev) => { ev.preventDefault(); sdrop.classList.remove('over'); }));
    sdrop.addEventListener('drop', (ev) => addShots(ev.dataTransfer.files));
  }
  if (sinput) sinput.addEventListener('change', () => { addShots(sinput.files); if (sdrop) sinput.value = ''; });
  document.addEventListener('paste', (ev) => {
    if ($('#card-fix').hidden) return;
    const imgs = [...(ev.clipboardData?.files || [])].filter((f) => f.type.startsWith('image/'));
    if (imgs.length) { ev.preventDefault(); addShots(imgs); }
  });
  document.querySelectorAll('#fixcensor .opt').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('#fixcensor .opt').forEach((x) => x.classList.remove('selected'));
    b.classList.add('selected'); fixCensor = b.dataset.v;
  }));
  $('#fixgo').addEventListener('click', () => {
    const f = fixFile;
    if (!f) { $('#fixfilename').innerHTML = '⚠️ ဗီဒီယို ဖိုင် အရင် ရွေးပါ'; return; }
    const fd = new FormData();
    fd.append('video', f);
    fd.append('message', $('#fixmsg').value);
    shots.slice(0, 5).forEach((s, i) => fd.append('shots', s, s.name || `shot${i + 1}.png`));
    fd.append('cuts', $('#fixcuts').value);
    fd.append('censor', fixCensor);
    fd.append('telegram', '1');
    const btn = $('#fixgo'); btn.disabled = true; btn.textContent = '⬆️ တင်နေတယ်…';
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/fix');
    xhr.setRequestHeader('X-Admin-Key', admKey());
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) btn.textContent = `⬆️ တင်နေတယ် ${Math.round(e.loaded * 100 / e.total)}%`; };
    xhr.onload = () => {
      btn.disabled = false; btn.textContent = '🔧 ပြင်မယ်';
      let j = {}; try { j = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status !== 200 || !j.job_id) { showError(j.error || 'upload failed'); return; }
      $('#card-fix').hidden = true; back.hidden = true;
      showProgress(j.job_id);
    };
    xhr.onerror = () => { btn.disabled = false; btn.textContent = '🔧 ပြင်မယ်'; showError('upload failed'); };
    xhr.send(fd);
  });
})();

// 🎵 music photo: show which one was picked
function musicPhotoView() {
  const f = $('#musicphoto').files[0], img = $('#musicthumb');
  if (img.src) { URL.revokeObjectURL(img.src); img.removeAttribute('src'); }
  img.hidden = !f; $('#musicclear').hidden = !f;
  if (f) img.src = URL.createObjectURL(f);
  $('#musicpick').textContent = f ? '🔄 ပုံ ပြောင်းမယ်' : '🖼️ ပုံ ရွေးမယ်';
  $('#musicphotonote').textContent = f ? 'music player ပုံစံ (ပုံ အဝိုင်း လည်နေ၊ အသံ bar တွေ ကနေ)' : 'ပုံ မထည့်ရင် မူရင်း ဗီဒီယို + သီချင်းစာသား';
}
if ($('#musicphoto')) {
  $('#musicphoto').addEventListener('change', musicPhotoView);
  $('#musicclear').addEventListener('click', () => { $('#musicphoto').value = ''; musicPhotoView(); });
}
