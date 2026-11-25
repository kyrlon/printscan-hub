// PrintScan Hub: hover descriptions for scanservjs' controls.
// Matches on visible text (not internal class names or minified code), so it
// keeps working across scanservjs updates and with each scanner's own option names.
(function () {
  'use strict';

  // Field labels -> description (shown when hovering the whole field)
  var FIELDS = {
    'device': 'Which scanner to use.',
    'source': 'Where the paper is scanned from: the glass under the lid, or the document feeder on top (1-sided, or 2-sided if this scanner\'s feeder can scan both sides). The feeder scans every page you load.',
    'resolution': 'Detail in dots per inch (dpi). 200–300 is right for documents, 600 for photos. Higher means bigger files and slower scans.',
    'mode': 'Color, Gray (black-and-white photo quality) or Lineart (pure black and white, smallest files).',
    'batch': 'Manual: check before saving. After each scan you see every page so far: Next adds more pages (next page on the glass, or reload the feeder), Rescan redoes the last scan, Finish saves one file. None / Auto save straight away (the feeder always scans every page).',
    'filters': 'Optional clean-up applied after scanning, e.g. auto level or threshold.',
    'format': 'File type. PDF suits documents (multi-page); JPG/PNG suit images.',
    'top': 'Distance of the scan area from the top edge, in mm. You can also drag the box on the preview.',
    'left': 'Distance of the scan area from the left edge, in mm. You can also drag the box on the preview.',
    'width': 'Width of the scan area in mm (8.5 in = 215.9 mm).',
    'height': 'Height of the scan area in mm (11 in = 279.4 mm).',
    'brightness': 'Make the scan lighter or darker.',
    'contrast': 'Increase or reduce the difference between light and dark areas.'
  };

  // Option / button text -> description
  var OPTIONS = [
    [/flip on long edge/i, 'Feeder, both sides of every page, for pages that turn like a book (bound on the side). Load the stack face up.'],
    [/flip on short edge/i, 'Feeder, both sides of every page, for pages that turn like a notepad (bound at the top). The back pages are turned the right way up.'],
    [/^feeder, 1-sided$/i, 'Document feeder on top: load a stack face up and it pulls every page through. Front sides only.'],
    [/^glass \(flatbed\)$/i, 'The glass under the lid. One page at a time. Best for photos, books, receipts and fragile pages.'],
    [/^flatbed$|^platen$/i,'The glass under the lid. One page at a time. Best for photos, books, receipts and fragile pages.'],
    [/^adf duplex$|duplex/i, 'Document feeder, both sides of every page. Load the stack face up in the top tray.'],
    [/^adf front$/i, 'Document feeder, front side only.'],
    [/^adf back$/i, 'Document feeder, back side only.'],
    [/^adf$|automatic document feeder|^feeder$/i, 'Document feeder on top: load a stack face up and it pulls pages through. Scans one side of each page.'],
    [/transparency|film|tpu/i, 'Film/slide adapter, if fitted.'],
    [/^color$/i, 'Full color.'],
    [/^gray$|^grey$|^grayscale$/i, 'Shades of gray. Good for black-and-white photos.'],
    [/^lineart$|^binary$|^black ?& ?white$/i, 'Pure black and white. Smallest files, good for text.'],
    [/^preview$/i, 'Quick low-resolution scan so you can check the page and drag the box to choose the scan area. Doesn\'t save a file.'],
    [/^scan now$/i, 'Scan with these settings and save the file. Find it under FILES.'],
    [/^clear$/i, 'Reset the settings to their defaults and clear the preview.'],
    [/^paper size$/i, 'Set the scan area to a standard paper size (Letter, A4, …).'],
    [/^scan$/i, 'Scanner settings (this page).'],
    [/^files$/i, 'Your saved scans: download, view or delete them.'],
    [/^settings$/i, 'Preferences for this scan app (theme, thumbnails…).'],
    [/^about$/i, 'Version information.']
  ];

  function ownText(el) {
    var t = '';
    for (var i = 0; i < el.childNodes.length; i++) {
      if (el.childNodes[i].nodeType === 3) t += el.childNodes[i].nodeValue;
    }
    return t.replace(/\s+/g, ' ').trim();
  }

  function describeOption(text) {
    for (var i = 0; i < OPTIONS.length; i++) if (OPTIONS[i][0].test(text)) return OPTIONS[i][1];
    return null;
  }

  function apply() {
    // Field labels: put the description on the whole field.
    document.querySelectorAll('label').forEach(function (label) {
      var key = ownText(label).toLowerCase();
      if (FIELDS[key]) {
        var field = label.closest('.v-input, .v-field, .v-slider, .v-row, div') || label;
        if (!field.title) field.title = FIELDS[key];
        label.title = FIELDS[key];
      }
    });
    // Dropdown items, current selections, buttons and tabs.
    document.querySelectorAll('.v-list-item-title, .v-list-item, .v-select__selection-text, .v-btn, .v-btn__content, .v-tab, button, a').forEach(function (el) {
      var text = ownText(el) || (el.children.length <= 2 ? el.textContent.replace(/\s+/g, ' ').trim() : '');
      if (!text || text.length > 40) return;
      var d = describeOption(text);
      if (!d) return;
      var target = el.closest('.v-list-item, .v-btn, .v-tab, button, a') || el;
      if (target.title !== d) target.title = d;
      if (el.classList.contains('v-select__selection-text')) el.title = d;  // current value
    });
  }

  var pending = false;
  new MutationObserver(function () {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; apply(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', apply);
})();
