# scanservjs + the same driver manager as the print hub, so scanner drivers
# (brscan, epsonscan2, sane backends...) can be added from drivers/scanner/.
# Pinned to the version the patches below were written and tested against
# (v3.0.4 = sha256:e42263444a8d...). Newer releases change the client bundle;
# bump deliberately and re-test the patches.
ARG SCANSERVJS_VERSION=v3.0.4
FROM sbs20/scanservjs:${SCANSERVJS_VERSION}

ENV DRIVERS_DIR=/drivers DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d

# Rename the main action button "Scan" -> "Scan now" so it isn't confused with
# the SCAN tab in scanservjs' top bar. The pattern ignores minified variable
# names; the grep makes the build fail loudly if a new scanservjs version
# changes the bundle so the patch no longer applies.
RUN sed -i -E 's/("btn-scan":[^,]*\(\[)(["`])Scan\2\]/\1\2Scan now\2]/g' /usr/lib/scanservjs/client/assets/index-*.js \
    && grep -q '"btn-scan":[^,]*Scan now' /usr/lib/scanservjs/client/assets/index-*.js

# Let a hub-chosen default Source (SCAN_DEFAULT_SOURCE, e.g. "auto" = feeder when
# it has paper) win over the source the browser remembered from last time.
# Also ignore a remembered source the scanner no longer lists (e.g. "ADF Duplex"
# from before the hub renamed the choices), which the server would reject.
# Original: this.params.source = saved.params.source || device.features["--source"].default
RUN sed -i 's/this\.params\.source=\([A-Za-z_$]*\)\.params\.source||\([A-Za-z_$]*\)\.features\["--source"\]\.default/this.params.source=(\2.features["--source"].hubForceDefault||!\2.features["--source"].options.includes(\1.params.source)?null:\1.params.source)||\2.features["--source"].default/' /usr/lib/scanservjs/client/assets/index-*.js \
    && grep -q 'hubForceDefault' /usr/lib/scanservjs/client/assets/index-*.js

# Feeder on a scanner that scans both sides: show Batch as "Auto", greyed out,
# and send batch=auto with the scan (helpers in scanner/hub-feeder.js). Two patches:
#   Batch field: h(X,{modelValue:o.request.batch,...  -> value/disabled/hint from hubBatchLocked
#   (and add disabled/hint to the props Vue re-checks on updates, its "8,[...]" list)
#   scan():      const t=Zt.clone(this.request)       -> passed through hubFixRequest
RUN f=$(ls /usr/lib/scanservjs/client/assets/index-*.js) \
    && sed -i -E 's/([A-Za-z_$]+)\(([A-Za-z_$]+),\{modelValue:([A-Za-z_$]+)\.request\.batch,/\1(\2,{modelValue:window.hubBatchLocked\&\&hubBatchLocked(\3.device,\3.request.params.source)?"auto":\3.request.batch,disabled:!!(window.hubBatchLocked\&\&hubBatchLocked(\3.device,\3.request.params.source)),hint:window.hubBatchLocked\&\&hubBatchLocked(\3.device,\3.request.params.source)?"Feeder: every page. Glass for other options":void 0,"persistent-hint":!0,/' "$f" \
    && sed -i -E 's/(items:[A-Za-z_$]+\.batchModes,[^}]*\},null,8,\["modelValue")/\1,"disabled","hint"/' "$f" \
    && sed -i -E 's/(scan\(e\)\{[^}]*const t=)([A-Za-z_$]+\.clone\(this\.request\))/\1window.hubFixRequest?hubFixRequest(\2,this.device):\2/' "$f" \
    && grep -q 'disabled:!!(window.hubBatchLocked' "$f" && grep -q 'hubFixRequest(' "$f" \
    && grep -q 'batchModes,[^}]*},null,8,\["modelValue","disabled","hint"' "$f"

# Use sane-airscan only for driverless scanners: SANE's older built-in "escl"
# backend finds the same devices again, so every scanner was listed 2-3 times.
RUN sed -i 's/^escl$/#escl  (disabled: sane-airscan handles eSCL)/' /etc/sane.d/dll.conf \
    && ! grep -qx escl /etc/sane.d/dll.conf

# Hover descriptions for the scan settings (press and hold on touch screens),
# and the feeder status shown under the Device field.
# hub-viewer.js links finished scans to the hub's View & rotate page.
COPY scanner/hub-tooltips.js scanner/hub-feeder.js scanner/hub-viewer.js hub/portal/static/longpress-tips.js /usr/lib/scanservjs/client/
RUN sed -i 's#</body>#<script src="hub-tooltips.js"></script><script src="hub-feeder.js"></script><script src="hub-viewer.js"></script><script src="longpress-tips.js"></script></body>#' /usr/lib/scanservjs/client/index.html \
    && grep -q 'hub-viewer.js' /usr/lib/scanservjs/client/index.html

# FILES list: an eye icon before Download that opens View & rotate (window.hubView).
# Original: "item.actions":j(({item:i})=>[h(Ve,{class:"mr-2",icon:r.mdiDownload,...
RUN f=$(ls /usr/lib/scanservjs/client/assets/index-*.js) \
    && sed -i -E 's#("item\.actions":[A-Za-z_$]+\(\(\{item:([A-Za-z_$]+)\}\)=>\[)([A-Za-z_$]+)\(([A-Za-z_$]+),\{class:"mr-2",icon:([A-Za-z_$]+)\.mdiDownload#\1\3(\4,{class:"mr-2",title:"View and rotate",icon:"M12,9A3,3 0 0,0 9,12A3,3 0 0,0 12,15A3,3 0 0,0 15,12A3,3 0 0,0 12,9M12,17A5,5 0 0,1 7,12A5,5 0 0,1 12,7A5,5 0 0,1 17,12A5,5 0 0,1 12,17M12,4.5C7,4.5 2.73,7.61 1,12C2.73,16.39 7,19.5 12,19.5C17,19.5 21.27,16.39 23,12C21.27,7.61 17,4.5 12,4.5Z",onClick:s=>window.hubView\&\&hubView(\2)},null,8,["onClick"]),\3(\4,{class:"mr-2",icon:\5.mdiDownload#' "$f" \
    && grep -q 'hubView(' "$f"

# Same browser-tab icon as the hub portal (replaces scanservjs' own icon).
COPY hub/portal/static/icon.svg /usr/lib/scanservjs/client/hub-icon.svg
COPY hub/portal/static/apple-touch-icon.png /usr/lib/scanservjs/client/hub-apple-touch-icon.png
RUN sed -i -E 's#<link rel="(icon|shortcut icon|apple-touch-icon)"[^>]*>##g; s#</head>#<link rel="icon" type="image/svg+xml" href="hub-icon.svg"><link rel="apple-touch-icon" href="hub-apple-touch-icon.png"></head>#' /usr/lib/scanservjs/client/index.html \
    && grep -q 'hub-icon.svg' /usr/lib/scanservjs/client/index.html

COPY shared/hub-driver /usr/local/bin/hub-driver
# Runs around each scan (whole feeder stack, short-edge flip, review thumbnails);
# config.local.js calls it.
COPY scanner/hub-scan-pass /usr/local/bin/hub-scan-pass
COPY scanner/entrypoint-wrapper.sh /entrypoint-wrapper.sh
RUN sed -i 's/\r$//' /usr/local/bin/hub-driver /usr/local/bin/hub-scan-pass /entrypoint-wrapper.sh \
    && chmod +x /usr/local/bin/hub-driver /usr/local/bin/hub-scan-pass /entrypoint-wrapper.sh

ENTRYPOINT ["/entrypoint-wrapper.sh"]
