# scanservjs + the same driver manager as the print hub, so scanner drivers
# (brscan, epsonscan2, sane backends...) can be added from drivers/scanner/.
ARG SCANSERVJS_VERSION=latest
FROM sbs20/scanservjs:${SCANSERVJS_VERSION}

ENV DRIVERS_DIR=/drivers DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d

# Rename the main action button "Scan" -> "Scan now" so it isn't confused with
# the SCAN tab in scanservjs' top bar. The pattern ignores minified variable
# names; the grep makes the build fail loudly if a new scanservjs version
# changes the bundle so the patch no longer applies.
RUN sed -i 's/\("btn-scan":[^,]*(\["\)Scan"\]/\1Scan now"]/g' /usr/lib/scanservjs/client/assets/index-*.js \
    && grep -q '"btn-scan":[^,]*Scan now' /usr/lib/scanservjs/client/assets/index-*.js

COPY shared/hub-driver /usr/local/bin/hub-driver
COPY scanner/entrypoint-wrapper.sh /entrypoint-wrapper.sh
RUN sed -i 's/\r$//' /usr/local/bin/hub-driver /entrypoint-wrapper.sh \
    && chmod +x /usr/local/bin/hub-driver /entrypoint-wrapper.sh

ENTRYPOINT ["/entrypoint-wrapper.sh"]
