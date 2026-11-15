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
# Original: this.params.source = saved.params.source || device.features["--source"].default
RUN sed -i 's/this\.params\.source=\([A-Za-z_$]*\)\.params\.source||\([A-Za-z_$]*\)\.features\["--source"\]\.default/this.params.source=(\2.features["--source"].hubForceDefault?null:\1.params.source)||\2.features["--source"].default/' /usr/lib/scanservjs/client/assets/index-*.js \
    && grep -q 'hubForceDefault' /usr/lib/scanservjs/client/assets/index-*.js

# Use sane-airscan only for driverless scanners: SANE's older built-in "escl"
# backend finds the same devices again, so every scanner was listed 2-3 times.
RUN sed -i 's/^escl$/#escl  (disabled: sane-airscan handles eSCL)/' /etc/sane.d/dll.conf \
    && ! grep -qx escl /etc/sane.d/dll.conf

# Hover descriptions for the scan settings, and the feeder status shown under
# the Device field.
COPY scanner/hub-tooltips.js scanner/hub-feeder.js /usr/lib/scanservjs/client/
RUN sed -i 's#</body>#<script src="hub-tooltips.js"></script><script src="hub-feeder.js"></script></body>#' /usr/lib/scanservjs/client/index.html \
    && grep -q 'hub-feeder.js' /usr/lib/scanservjs/client/index.html

# Same browser-tab icon as the hub portal (replaces scanservjs' own icon).
COPY hub/portal/static/icon.svg /usr/lib/scanservjs/client/hub-icon.svg
COPY hub/portal/static/apple-touch-icon.png /usr/lib/scanservjs/client/hub-apple-touch-icon.png
RUN sed -i -E 's#<link rel="(icon|shortcut icon|apple-touch-icon)"[^>]*>##g; s#</head>#<link rel="icon" type="image/svg+xml" href="hub-icon.svg"><link rel="apple-touch-icon" href="hub-apple-touch-icon.png"></head>#' /usr/lib/scanservjs/client/index.html \
    && grep -q 'hub-icon.svg' /usr/lib/scanservjs/client/index.html

COPY shared/hub-driver /usr/local/bin/hub-driver
COPY scanner/entrypoint-wrapper.sh /entrypoint-wrapper.sh
RUN sed -i 's/\r$//' /usr/local/bin/hub-driver /entrypoint-wrapper.sh \
    && chmod +x /usr/local/bin/hub-driver /entrypoint-wrapper.sh

ENTRYPOINT ["/entrypoint-wrapper.sh"]
