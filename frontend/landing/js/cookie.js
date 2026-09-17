/* Cookie notice: shown once per browser, dismissed state kept in localStorage.
   Standalone so legal pages can include it without the main page script. */
(() => {
  'use strict';
  const bar = document.getElementById('cookie-bar');
  if (!bar) return;
  let seen = false;
  try { seen = localStorage.getItem('mp-cookie-ok') === '1'; } catch (e) {}
  if (seen) return;
  bar.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => bar.classList.add('is-visible')));
  document.getElementById('cookie-ok').addEventListener('click', () => {
    try { localStorage.setItem('mp-cookie-ok', '1'); } catch (e) {}
    bar.classList.remove('is-visible');
    setTimeout(() => { bar.hidden = true; }, 400);
  });
})();
