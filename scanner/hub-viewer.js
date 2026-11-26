// PrintScan Hub: links from the scan app to the hub's "View & rotate" page
// (hub/portal/scanview.py): a bar after each finished scan, and an eye icon in the
// FILES list (patched into scanservjs, see scanner/Dockerfile).
(function () {
  'use strict';

  // The scan app is served by the portal at /scan/app/; the viewer is /scan/view.
  function viewUrl(name) {
    return new URL('../view?f=' + encodeURIComponent(name), location.href.split('#')[0]).href;
  }
  function open(name) {
    var url = viewUrl(name);
    try { (window.top || window).location.href = url; } catch (e) { location.href = url; }
  }
  window.hubView = function (item) { if (item && item.name) open(item.name); };

  var style = document.createElement('style');
  style.textContent =
    '#hub-saved{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:3000;display:flex;' +
    'align-items:center;gap:12px;flex-wrap:wrap;max-width:calc(100% - 24px);padding:10px 14px;border-radius:8px;' +
    'background:#1f2937;color:#f3f4f6;box-shadow:0 6px 24px rgba(0,0,0,.35);font:14px/1.4 system-ui,sans-serif}' +
    '#hub-saved .name{font-weight:600;word-break:break-all}' +
    '#hub-saved a{background:#2563eb;color:#fff;text-decoration:none;font-weight:600;padding:6px 12px;border-radius:6px}' +
    '#hub-saved button{background:transparent;border:0;color:#9ca3af;font-size:18px;cursor:pointer;padding:0 4px}';
  document.head.appendChild(style);

  function showSaved(name) {
    var bar = document.getElementById('hub-saved');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'hub-saved';
      bar.setAttribute('role', 'status');
      document.body.appendChild(bar);
    }
    bar.innerHTML = '<span>Saved <span class="name"></span></span><a>View &amp; rotate</a>' +
                    '<button type="button" title="Close" aria-label="Close">×</button>';
    bar.querySelector('.name').textContent = name;
    var a = bar.querySelector('a');
    a.href = viewUrl(name);
    a.title = 'See every page, zoom in and turn pages the right way up';
    a.onclick = function (e) { e.preventDefault(); open(name); };
    bar.querySelector('button').onclick = function () { bar.remove(); };
    clearTimeout(showSaved.timer);
    showSaved.timer = setTimeout(function () { if (bar.isConnected) bar.remove(); }, 60000);
  }

  // Notice finished scans: scanservjs POSTs api/v1/scan and gets { file: {...} } back.
  var realFetch = window.fetch;
  window.fetch = function (input, init) {
    var p = realFetch.apply(this, arguments);
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (method === 'POST' && /api\/v1\/scan(\?|$)/.test(url)) {
      p.then(function (r) {
        if (!r.ok) return;
        r.clone().json().then(function (d) {
          if (d && d.file && d.file.name) showSaved(d.file.name);
        }).catch(function () {});
      }).catch(function () {});
    }
    return p;
  };
})();
