// PrintScan Hub: show the selected scanner's document-feeder state under the
// Device field. The state comes from scanservjs' own /api/v1/context, where
// config.local.js adds it per device (features["--source"].hubFeeder).
(function () {
  'use strict';
  var BOX_ID = 'hub-feeder';
  var devices = {};        // device id -> { feeder, mode }
  var atLoad = {};         // device id -> feeder state when the page loaded

  var style = document.createElement('style');
  style.textContent =
    '#' + BOX_ID + '{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:-8px 0 14px;font-size:13px;opacity:.9}' +
    '#' + BOX_ID + ' .dot{width:9px;height:9px;border-radius:50%;background:#9aa3b2;flex:none}' +
    '#' + BOX_ID + ' .txt{flex:1 1 0;min-width:0}' +
    '#' + BOX_ID + '.loaded{color:#2e9e5b;font-weight:600}' +
    '#' + BOX_ID + '.loaded .dot{background:#2e9e5b;box-shadow:0 0 0 3px rgba(46,158,91,.18)}' +
    '#' + BOX_ID + ' button{font:inherit;font-weight:600;padding:2px 10px;border-radius:4px;border:1px solid currentColor;' +
    'background:transparent;color:#5b8def;cursor:pointer}';
  document.head.appendChild(style);

  function deviceField() {
    var labels = document.querySelectorAll('label');
    for (var i = 0; i < labels.length; i++) {
      if (labels[i].textContent.trim().toLowerCase() === 'device') return labels[i].closest('.v-input');
    }
    return null;
  }

  function selectedId(field) {
    var sel = field && field.querySelector('.v-select__selection-text, .v-select__selection');
    var text = sel ? sel.textContent.trim() : '';
    if (devices[text]) return text;
    var ids = Object.keys(devices);
    for (var i = 0; i < ids.length; i++) if (text && ids[i].indexOf(text) === 0) return ids[i];
    return ids.length === 1 ? ids[0] : null;
  }

  function render() {
    var field = deviceField();
    if (!field) return;
    // Put the box after the row holding the Device dropdown and its refresh button.
    var row = field.parentElement && field.parentElement.querySelector('button') ? field.parentElement : field;
    var box = document.getElementById(BOX_ID);
    if (!box) {
      box = document.createElement('div');
      box.id = BOX_ID;
      box.setAttribute('aria-live', 'polite');
    }
    if (box.previousElementSibling !== row) row.insertAdjacentElement('afterend', box);

    var id = selectedId(field), d = id && devices[id];
    if (!d || !d.feeder) { box.hidden = true; return; }
    if (!(id in atLoad)) atLoad[id] = d.feeder;
    var auto = (d.mode || '').indexOf('auto') === 0;
    var loaded = d.feeder === 'loaded';
    box.hidden = false;
    box.className = d.feeder;
    box.title = 'Checked every few seconds. ' + (auto
      ? 'Source is set automatically when the page loads (SCAN_DEFAULT_SOURCE=' + d.mode + ').'
      : 'Choose the feeder (ADF) under Source to use it.');
    var text = loaded ? 'Paper in the feeder' : 'Feeder empty';
    if (auto) text += loaded ? ': starts from the feeder' : ': starts from the glass';
    box.innerHTML = '<span class="dot"></span><span class="txt"></span>';
    box.lastChild.textContent = text;
    if (auto && d.feeder !== atLoad[id]) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = loaded ? 'Switch to feeder' : 'Switch to glass';
      btn.title = 'Reload the scan page so Source matches the feeder';
      btn.onclick = function () { location.reload(); };
      box.appendChild(btn);
    }
  }

  function poll() {
    fetch('api/v1/context', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (ctx) {
      devices = {};
      (ctx.devices || []).forEach(function (dev) {
        var src = dev.features && dev.features['--source'];
        devices[dev.id] = { feeder: src && src.hubFeeder, mode: src && src.hubSourceMode };
      });
      render();
    }).catch(function () {});
  }

  var pending = false;
  new MutationObserver(function () {   // Vue re-renders can drop our box; put it back
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; render(); });
  }).observe(document.documentElement, { childList: true, subtree: true });

  poll();
  setInterval(function () { if (!document.hidden) poll(); }, 4000);
})();
