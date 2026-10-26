/* reading room: shared behaviour. Event delegation, no inline handlers. */
(function () {
  'use strict';
  var d = document, root = d.documentElement, store;
  try { store = window.localStorage; } catch (e) { store = null; }
  function save(k, v) { try { if (store) store.setItem(k, v); } catch (e) {} }

  /* theme + text size (same keys as the article reader, so the choice carries over) */
  window.rrSize = function (n) {
    var c = parseFloat(getComputedStyle(root).getPropertyValue('--fs')) || 17;
    c = Math.max(14, Math.min(28, c + n));
    root.style.setProperty('--fs', c + 'px'); save('rr_fs', String(c));
  };
  window.rrTheme = function () {
    var order = ['light', 'sepia', 'dark'];
    var cur = root.getAttribute('data-theme') ||
      (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    var next = order[(order.indexOf(cur) + 1) % order.length];
    root.setAttribute('data-theme', next); save('rr_theme', next);
  };

  function post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  }

  /* keep-alive: tells the server a person is here, so the cron job does not ingest mid-read */
  function beat() { if (d.visibilityState === 'visible') post('/api/heartbeat').catch(function () {}); }
  beat(); setInterval(beat, 60000);

  /* stars */
  function toggleSave(btn) {
    var module = btn.getAttribute('data-module'), id = btn.getAttribute('data-id');
    if (!module || !id) return;
    post('/save/' + encodeURIComponent(module) + '/' + encodeURIComponent(id)).then(function (r) {
      if (!r.ok) throw new Error('save failed');
      return r.json();
    }).then(function (j) {
      btn.textContent = j.is_saved ? '\u2605' : '\u2606';
      btn.setAttribute('aria-pressed', j.is_saved ? 'true' : 'false');
    }).catch(function () {});
  }

  /* video player modal. The iframe exists only while the modal is open, so closing it stops playback. */
  var modal = null, frame = null, titleEl = null;
  function ensureModal() {
    if (modal) return;
    modal = d.createElement('div'); modal.className = 'modal'; modal.setAttribute('role', 'dialog');
    var box = d.createElement('div'); box.className = 'modal-box';
    var close = d.createElement('button'); close.type = 'button'; close.className = 'modal-close';
    close.setAttribute('data-modal-close', ''); close.textContent = 'Close';
    frame = d.createElement('div'); frame.className = 'modal-frame';
    titleEl = d.createElement('p'); titleEl.className = 'modal-title';
    box.appendChild(close); box.appendChild(frame); box.appendChild(titleEl); modal.appendChild(box);
    modal.addEventListener('click', function (e) { if (e.target === modal) closeModal(); });
    d.body.appendChild(modal);
  }
  function openPlayer(el) {
    var id = el.getAttribute('data-video-id') || '';
    if (!/^[A-Za-z0-9_-]{11}$/.test(id)) return;
    var title = el.getAttribute('data-title') || '';
    ensureModal();
    while (frame.firstChild) frame.removeChild(frame.firstChild);
    var f = d.createElement('iframe');
    f.src = 'https://www.youtube-nocookie.com/embed/' + id + '?autoplay=1&rel=0&modestbranding=1&playsinline=1';
    f.allow = 'autoplay; fullscreen; encrypted-media; picture-in-picture';
    f.setAttribute('allowfullscreen', ''); f.referrerPolicy = 'strict-origin-when-cross-origin';
    frame.appendChild(f); titleEl.textContent = title; modal.className = 'modal open';
    post('/youtube/click', { video_id: id, title: title }).catch(function () {});
  }
  function closeModal() {
    if (!modal) return;
    while (frame.firstChild) frame.removeChild(frame.firstChild);
    modal.className = 'modal';
  }
  d.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });

  /* "mark all read" for the items currently listed */
  function markRead(btn) {
    var target = d.querySelector(btn.getAttribute('data-target') || '');
    if (!target) return;
    var rows = target.querySelectorAll('[data-item-id]'), ids = [];
    for (var i = 0; i < rows.length && ids.length < 100; i++) ids.push(rows[i].getAttribute('data-item-id'));
    if (!ids.length) return;
    post('/api/read', { kind: btn.getAttribute('data-kind'), ids: ids }).then(function (r) {
      if (!r.ok) throw new Error('failed');
      for (var j = 0; j < rows.length; j++) rows[j].className = rows[j].className.replace(/\bunread\b/g, '').trim();
    }).catch(function () {});
  }

  /* history */
  function historyDelete(btn) {
    var id = btn.getAttribute('data-id'), row = btn.closest('li');
    post('/youtube/history/' + encodeURIComponent(id) + '/delete').then(function (r) {
      if (r.ok && row && row.parentNode) row.parentNode.removeChild(row);
    }).catch(function () {});
  }
  function historyClear(btn) {
    if (!window.confirm('Clear all watch history on this device?')) return;
    post('/youtube/history/clear').then(function (r) {
      if (!r.ok) return;
      var list = d.querySelector(btn.getAttribute('data-target') || '');
      if (list) while (list.firstChild) list.removeChild(list.firstChild);
    }).catch(function () {});
  }

  d.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var el;
    if ((el = t.closest('.save-btn'))) { e.preventDefault(); toggleSave(el); return; }
    if ((el = t.closest('[data-play]'))) { e.preventDefault(); openPlayer(el); return; }
    if (t.closest('[data-modal-close]')) { closeModal(); return; }
    if (t.closest('[data-theme-toggle]')) { window.rrTheme(); return; }
    if ((el = t.closest('[data-size]'))) { window.rrSize(parseInt(el.getAttribute('data-size'), 10) || 0); return; }
    if (t.closest('[data-back]')) { e.preventDefault(); history.back(); return; }
    if ((el = t.closest('[data-mark-read]'))) { markRead(el); return; }
    if ((el = t.closest('[data-history-delete]'))) { historyDelete(el); return; }
    if ((el = t.closest('[data-history-clear]'))) { historyClear(el); return; }
  });
})();
