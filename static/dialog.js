// A confirm dialog in the app's own style (replaces the browser's confirm()).
// Same pattern as sale-hub's ConfirmDialog: title + message, Cancel takes the focus
// (so a stray Enter never confirms), a red button for a destructive action;
// Esc or a click outside cancels. Returns a Promise<boolean>.
window.askConfirm = function ({ title, message, confirmLabel = 'လုပ်မယ်', cancelLabel = 'မလုပ်ဘူး',
                               tone = 'danger', icon = 'warn' }) {
  return new Promise((resolve) => {
    const back = document.createElement('div');
    back.className = 'dlg-back';
    back.innerHTML =
      '<div class="dlg" role="alertdialog" aria-modal="true" aria-labelledby="dlg-t" aria-describedby="dlg-m">' +
      '<button type="button" class="dlg-x" aria-label="Close"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" ' +
      'stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg></button>' +
      '<h3 id="dlg-t"></h3><p id="dlg-m"></p>' +
      '<div class="dlg-btns"><button type="button" class="dlg-cancel"></button>' +
      `<button type="button" class="dlg-ok ${tone}"></button></div></div>`;
    const ICONS = {   // line icons (emoji bring their own white tile)
      restart: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
      trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/><path d="M10 11v6M14 11v6"/>',
      send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
      unplug: '<path d="M18 6 6 18"/><path d="M7 3v4M3 7h4M17 21v-4M21 17h-4"/>',
      warn: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/>' };
    const svg = ICONS[icon] ? '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
      'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[icon] + '</svg>' : '';
    back.querySelector('#dlg-t').textContent = title;
    back.querySelector('#dlg-m').textContent = message;
    const cancel = back.querySelector('.dlg-cancel'), ok = back.querySelector('.dlg-ok');
    cancel.textContent = cancelLabel;
    ok.textContent = confirmLabel;
    const last = document.activeElement;
    const done = (v) => {
      document.removeEventListener('keydown', onKey, true);
      back.classList.add('out');
      setTimeout(() => back.remove(), 150);
      if (last && last.focus) last.focus();
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); done(false); }
      if (e.key === 'Tab') {                       // keep the focus inside the dialog
        e.preventDefault(); (document.activeElement === cancel ? ok : cancel).focus();
      }
    };
    cancel.addEventListener('click', () => done(false));
    back.querySelector('.dlg-x').addEventListener('click', () => done(false));
    ok.addEventListener('click', () => done(true));
    back.addEventListener('mousedown', (e) => { if (e.target === back) done(false); });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(back);
    requestAnimationFrame(() => { back.classList.add('in'); cancel.focus(); });
  });
};
