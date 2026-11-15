// scanservjs configuration overrides
// See https://github.com/sbs20/scanservjs/blob/master/packages/server/src/config.js
// for all available options.

// Default scan area per paper size, in millimetres (SCAN_DEFAULT_PAPER in .env).
const PAPER = {
  letter: [215.9, 279.4],   // 8.5 x 11 in
  legal:  [215.9, 355.6],   // 8.5 x 14 in
  a4:     [210, 297],
  a5:     [148, 210],
  full:   null,             // the scanner's whole glass
};

module.exports = {
  afterConfig(config) {
    // Port comes from SCAN_PORT in .env (the portal's "Scan" link uses it too).
    config.port = parseInt(process.env.SCAN_PORT || '8080', 10);

    // Uncomment to set default file name:
    // config.defaultOutputFilename = 'scan-{date}-{time}';
  },

  afterDevices(devices) {
    const key = (process.env.SCAN_DEFAULT_PAPER || 'letter').toLowerCase();
    const size = PAPER[key];
    if (size === undefined) {
      console.warn(`SCAN_DEFAULT_PAPER=${key} is unknown; use one of ${Object.keys(PAPER).join(', ')}`);
      return;
    }
    if (!size) return;
    devices.forEach(device => {
      ['-x', '-y'].forEach((name, i) => {
        const feature = device.features[name];
        if (!feature) return;
        const max = Array.isArray(feature.limits) ? feature.limits[1] : size[i];
        feature.default = Math.min(size[i], max);   // never larger than the glass
      });
      ['-l', '-t'].forEach(name => {
        if (device.features[name]) device.features[name].default = 0;
      });
    });
  }
};
