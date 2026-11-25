// scanservjs configuration overrides
// See https://github.com/sbs20/scanservjs/blob/master/packages/server/src/config.js
// for all available options.
const { execFile, execFileSync } = require('child_process');
const fs = require('fs');

// Folder shared with the hub (./data/shared): we write what was detected, the
// hub's Admin page writes which scanners to hide.
const SHARED = process.env.HUB_SHARED_DIR || '/hub-shared';
// Device ids carry a position ("airscan:e1:Name") that can change between
// searches; hide by the id without it ("airscan:Name").
const stableKey = id => id.replace(/^([a-z0-9]+):[ew]\d+:/i, '$1:');
let lastDetected = '';

function readHidden() {
  try {
    return new Set(JSON.parse(fs.readFileSync(`${SHARED}/scanners-hidden.json`, 'utf8')).hidden || []);
  } catch (e) {
    return new Set();
  }
}

function writeDetected(devices, hidden) {
  const list = devices.map(d => {
    const name = d.id.split(':').slice(2).join(':') || d.id;
    const src = d.features['--source'], res = d.features['--resolution'];
    return {
      id: d.id, key: stableKey(d.id), name,
      url: esclUrls[name] || esclUrls[name.replace(/_/g, ' ')] || null,
      sources: (src && src.options) || [],
      resolutions: (res && res.options) || [],
      feeder: (src && src.hubFeeder) || null,
      hidden: hidden.has(stableKey(d.id)),
    };
  });
  const s = JSON.stringify(list);
  if (s === lastDetected) return;              // only write when something changed
  try {
    fs.mkdirSync(SHARED, { recursive: true });
    fs.writeFileSync(`${SHARED}/scanners-detected.json`,
                     JSON.stringify({ updated: new Date().toISOString(), devices: list }, null, 1));
    lastDetected = s;
  } catch (e) {
    console.warn(`Couldn't write ${SHARED}/scanners-detected.json: ${e.message}`);
  }
}

// Default scan area per paper size, in millimetres (SCAN_DEFAULT_PAPER in .env).
const PAPER = {
  letter: [215.9, 279.4],   // 8.5 x 11 in
  legal:  [215.9, 355.6],   // 8.5 x 14 in
  a4:     [210, 297],
  a5:     [148, 210],
  full:   null,             // the scanner's whole glass
};

// ---- Source (glass vs. document feeder) ------------------------------------
// SCAN_DEFAULT_SOURCE: auto | auto-duplex | flatbed | adf | duplex | remember
//   auto        feeder if it has paper, otherwise the glass (checked on page load)
//   remember    scanservjs' own behaviour: whatever was used last time
// Option names differ per scanner/backend, so match them loosely.
const SOURCE_MATCH = {
  flatbed: o => /flatbed|platen|glass/i.test(o),
  adf:     o => /adf|feeder|automatic/i.test(o) && !/duplex|back/i.test(o),
  duplex:  o => /duplex/i.test(o),
};

// eSCL base URLs per scanner name: from AIRSCAN_DEVICES, plus auto-discovered ones.
const esclUrls = {};
(process.env.AIRSCAN_DEVICES || '').split(new RegExp(`[${process.env.DELIMITER || ';'}\\n]`)).forEach(entry => {
  const m = entry.match(/^\s*"?([^"=]+?)"?\s*=\s*([^,\s]+)\s*,\s*escl/i);
  if (m) esclUrls[m[1].trim()] = m[2];
});
// Every scanner address ever found on the network (eSCL and WSD), so the hub can
// tell whether a scanner is still there even when it only speaks WSD and has no
// status page. Written to scanners-seen.json after each search.
const seenUrls = {};
// IPv4 http first: IPv6 link-local addresses can't be used from another host.
const urlRank = u => (/^http:\/\/\d/.test(u) ? 0 : /^http:/.test(u) ? 1 : 2);
function discover() {
  // Lists 'Name = http://host/eSCL/, eSCL' or '..., WSD' (name may or may not be quoted).
  execFile('airscan-discover', { timeout: 20000 }, (err, stdout) => {
    if (err && !stdout) return;                // search didn't run: keep the last answer
    const now = {};
    (stdout || '').split('\n').forEach(line => {
      const m = line.match(/^\s*"?(.+?)"?\s*=\s*(\S+?)\s*,\s*(escl|wsd)\s*$/i);
      if (!m) return;
      const [, name, url, proto] = m, kind = proto.toLowerCase();
      if (kind === 'escl' && !esclUrls[name]) esclUrls[name] = url;
      const e = (now[name] = now[name] || {});
      if (!e[kind] || urlRank(url) < urlRank(e[kind])) e[kind] = url;
    });
    Object.keys(seenUrls).forEach(n => { seenUrls[n].seen = false; });
    Object.entries(now).forEach(([n, e]) => { seenUrls[n] = { ...seenUrls[n], ...e, seen: true }; });
    try {
      fs.mkdirSync(SHARED, { recursive: true });
      fs.writeFileSync(`${SHARED}/scanners-seen.json.tmp`,
                       JSON.stringify({ updated: new Date().toISOString(), devices: seenUrls }, null, 1));
      fs.renameSync(`${SHARED}/scanners-seen.json.tmp`, `${SHARED}/scanners-seen.json`);
    } catch (e) {
      console.warn(`Couldn't write ${SHARED}/scanners-seen.json: ${e.message}`);
    }
  });
}
discover();
// Often enough that a scanner switched off shows as such within a few minutes;
// a search takes about 5 s even on a Raspberry Pi.
setInterval(discover, 2 * 60 * 1000).unref();

// Ask the scanner whether paper is in the feeder. Synchronous (afterDevices is),
// with a short timeout so an offline scanner can't stall the page.
// The Scan page refreshes this every few seconds, so answers are cached briefly,
// and the scanner is left alone while a scan is running: some scanners (e.g.
// Brother over Wi-Fi) refuse the scan ("Invalid argument") if a status request
// arrives while it is starting.
const ADF_CACHE_MS = 15 * 1000;
const adfCache = {};
function scanRunning() {
  try {
    return fs.readdirSync('/proc').some(pid => {
      if (!/^\d+$/.test(pid)) return false;
      try { return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0')[0].endsWith('scanimage'); }
      catch (e) { return false; }
    });
  } catch (e) {
    return false;
  }
}

function adfLoaded(baseUrl) {
  const hit = adfCache[baseUrl];
  if (hit && (Date.now() - hit.at < ADF_CACHE_MS || scanRunning())) return hit.loaded;
  if (!hit && scanRunning()) return null;
  const loaded = fetchAdfLoaded(baseUrl);
  adfCache[baseUrl] = { loaded, at: Date.now() };
  return loaded;
}

function fetchAdfLoaded(baseUrl) {
  const url = baseUrl.replace(/\/?$/, '/') + 'ScannerStatus';
  const script = `fetch(${JSON.stringify(url)},{signal:AbortSignal.timeout(2000)})` +
                 `.then(r=>r.text()).then(t=>process.stdout.write(t)).catch(()=>{})`;
  try {
    const xml = execFileSync(process.execPath, ['-e', script], { timeout: 3000, encoding: 'utf8' });
    const m = xml.match(/<scan:AdfState>([^<]+)</);
    return m ? m[1] === 'ScannerAdfLoaded' : null;
  } catch (e) {
    return null;
  }
}

function applySource(device) {
  const mode = (process.env.SCAN_DEFAULT_SOURCE || 'auto').toLowerCase();
  const feature = device.features['--source'];
  if (!feature || !Array.isArray(feature.options)) return;
  const pick = kind => feature.options.find(SOURCE_MATCH[kind]);

  // Feeder state for this device, shown under the Device field by hub-feeder.js.
  let loaded = null;
  if (pick('adf') || pick('duplex')) {
    const name = device.id.split(':').slice(2).join(':');
    const url = esclUrls[name] || esclUrls[name.replace(/_/g, ' ')];
    loaded = url ? adfLoaded(url) : null;
  }
  feature.hubFeeder = loaded === true ? 'loaded' : loaded === false ? 'empty' : null;
  feature.hubSourceMode = mode;
  if (mode === 'remember') return;

  let choice;
  if (mode.startsWith('auto')) {
    if (loaded === true) choice = pick(mode === 'auto-duplex' ? 'duplex' : 'adf') || pick('adf');
    else if (loaded === false) choice = pick('flatbed');
    // unknown (no eSCL URL / offline): leave scanservjs' default alone
  } else if (SOURCE_MATCH[mode]) {
    choice = pick(mode);
  }
  if (choice) {
    feature.default = choice;
    // Tells the patched client to start from this default instead of the
    // source remembered in the browser (see scanner/Dockerfile).
    feature.hubForceDefault = true;
  }
}

// The Scan page's device list shows device.name, which scanservjs sets to the
// raw SANE id ("airscan:e1:Brother ..."). Scanning uses device.id, so name is
// free to be a readable label: drop the backend/index prefix and HP's [MAC] tag,
// and say "(network)" only when the same model is also listed another way.
function friendlyNames(devices) {
  const label = d => {
    const m = d.id.match(/^airscan:[ew]\d+:(.+)$/i);
    if (!m) return d.id;                              // other backends: keep as is
    return m[1].replace(/\s*\[[0-9A-F]{6}\]/i, '').trim();
  };
  const base = n => n.replace(/\s*\(usb\)$/i, '').toLowerCase();
  const names = devices.map(label);
  devices.forEach((d, i) => {
    const dup = names.some((n, j) => j !== i && base(n) === base(names[i]));
    d.name = dup && !/\(usb\)$/i.test(names[i]) && names[i] !== d.id
      ? `${names[i]} (network)` : names[i];
  });
}

module.exports = {
  afterConfig(config) {
    // Internal port; people reach the scan page through the portal (/scan/app/),
    // which decides who can see which files. SCAN_BIND keeps it off the network.
    config.port = parseInt(process.env.SCAN_PORT || '8080', 10);
    config.host = process.env.SCAN_BIND || '127.0.0.1';

    // Uncomment to set default file name:
    // config.defaultOutputFilename = 'scan-{date}-{time}';
  },

  // Runs every time the scan page loads (on a fresh copy of the cached device list).
  afterDevices(devices) {
    const key = (process.env.SCAN_DEFAULT_PAPER || 'letter').toLowerCase();
    const size = PAPER[key];
    if (size === undefined) {
      console.warn(`SCAN_DEFAULT_PAPER=${key} is unknown; use one of ${Object.keys(PAPER).join(', ')}`);
    }
    const hidden = readHidden();
    devices.forEach(device => {
      if (hidden.has(stableKey(device.id))) return;   // hidden: skip the feeder check etc.
      if (size) {
        ['-x', '-y'].forEach((name, i) => {
          const feature = device.features[name];
          if (!feature) return;
          const max = Array.isArray(feature.limits) ? feature.limits[1] : size[i];
          feature.default = Math.min(size[i], max);   // never larger than the glass
        });
        ['-l', '-t'].forEach(name => {
          if (device.features[name]) device.features[name].default = 0;
        });
      }
      applySource(device);
    });

    // Tell the hub what we found, then drop hidden scanners from the Scan page.
    // Never hide them all: the scan page needs at least one device.
    writeDetected(devices, hidden);
    const visible = devices.filter(d => !hidden.has(stableKey(d.id)));
    if (visible.length && visible.length < devices.length) {
      devices.splice(0, devices.length, ...visible);
    }
    friendlyNames(devices);
  }
};
