// PrintScan Hub: on touch screens, press and hold anything that has a hover
// tooltip (a title attribute) to see it in a bubble. Desktop hover is unchanged.
// Used by the portal and, copied into the image, by the scan app.
(function () {
  'use strict';
  if (window.__hubLongPressTips) return;
  window.__hubLongPressTips = true;

  var HOLD_MS = 450, MOVE_PX = 10, AUTO_HIDE_MS = 7000;
  var timer = null, startX = 0, startY = 0, target = null, shownAt = 0, bubble = null, hideTimer = null;

  function ensureBubble() {
    if (bubble) return bubble;
    var style = document.createElement('style');
    style.textContent =
      '.hub-tip{position:fixed;z-index:2147483647;max-width:min(320px,calc(100vw - 16px));padding:8px 11px;' +
      'border-radius:8px;background:#1d2330;color:#fff;font:13px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;' +
      'box-shadow:0 4px 18px rgba(0,0,0,.28);pointer-events:none;white-space:pre-line;overflow-wrap:anywhere;' +
      'opacity:0;transform:translateY(4px);transition:opacity .12s,transform .12s}' +
      '.hub-tip.on{opacity:1;transform:none}' +
      '@media (prefers-color-scheme:dark){.hub-tip{background:#e6e9ef;color:#12151b}}' +
      '.hub-tip-target{outline:2px solid rgba(37,99,235,.55);outline-offset:2px;border-radius:4px}';
    document.head.appendChild(style);
    bubble = document.createElement('div');
    bubble.className = 'hub-tip';
    bubble.setAttribute('role', 'tooltip');
    bubble.hidden = true;
    document.body.appendChild(bubble);
    return bubble;
  }

  function titled(el) {
    while (el && el !== document.documentElement) {
      if (el.nodeType === 1 && el.getAttribute('title') && el.getAttribute('title').trim()) return el;
      el = el.parentNode;
    }
    return null;
  }

  function editable(el) {
    return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
  }

  function show(el) {
    var b = ensureBubble();
    b.textContent = el.getAttribute('title').trim();
    b.hidden = false;
    b.classList.remove('on');
    var r = el.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
    var bw = b.offsetWidth, bh = b.offsetHeight, gap = 10;
    var left = Math.max(8, Math.min(r.left + r.width / 2 - bw / 2, vw - bw - 8));
    var top = r.top - bh - gap;                       // above the finger's target...
    if (top < 8) top = Math.min(r.bottom + gap, vh - bh - 8);   // ...or below if no room
    b.style.left = left + 'px';
    b.style.top = Math.max(8, top) + 'px';
    requestAnimationFrame(function () { if (target === el && !b.hidden) b.classList.add('on'); });
    el.classList.add('hub-tip-target');
    target = el;
    shownAt = Date.now();
    if (navigator.vibrate) try { navigator.vibrate(8); } catch (e) {}
    try { window.getSelection().removeAllRanges(); } catch (e) {}
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, AUTO_HIDE_MS);
  }

  function hide() {
    clearTimeout(hideTimer);
    if (bubble) { bubble.classList.remove('on'); bubble.hidden = true; }
    if (target) target.classList.remove('hub-tip-target');
    target = null;
  }

  function cancelPress() { clearTimeout(timer); timer = null; }

  document.addEventListener('touchstart', function (e) {
    hide();
    cancelPress();
    if (e.touches.length !== 1) return;
    var el = titled(e.target);
    if (!el || editable(e.target)) return;
    startX = e.touches[0].clientX; startY = e.touches[0].clientY;
    timer = setTimeout(function () { timer = null; show(el); }, HOLD_MS);
  }, {passive: true, capture: true});

  document.addEventListener('touchmove', function (e) {
    if (!timer) return;
    var t = e.touches[0];
    if (Math.abs(t.clientX - startX) > MOVE_PX || Math.abs(t.clientY - startY) > MOVE_PX) cancelPress();
  }, {passive: true, capture: true});

  document.addEventListener('touchend', function (e) {
    cancelPress();
    // The finger lifted after the tip appeared: don't also press the button/link.
    if (target && Date.now() - shownAt < AUTO_HIDE_MS && e.cancelable) e.preventDefault();
  }, {passive: false, capture: true});
  document.addEventListener('touchcancel', cancelPress, {capture: true});

  // A held press can still turn into a click, the long-press menu or text selection.
  function swallow(e) {
    if ((timer || (target && Date.now() - shownAt < 800)) && !editable(e.target)) {
      e.preventDefault();
      e.stopPropagation();
    }
  }
  document.addEventListener('click', swallow, true);
  document.addEventListener('contextmenu', swallow, true);
  document.addEventListener('selectstart', swallow, true);
  window.addEventListener('scroll', function () { cancelPress(); hide(); }, {passive: true, capture: true});
})();
