# PrintScan Hub - CUPS print server + web "upload & print" portal + driver manager
FROM debian:bookworm-slim

# Set to false to skip LibreOffice (~400 MB). Without it the portal can still
# print PDFs, images and plain text; Office documents will be rejected.
ARG INSTALL_OFFICE=true

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    DRIVERS_DIR=/drivers

# Stop package postinst scripts from trying to start/restart services.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d

RUN apt-get update && apt-get install -y --no-install-recommends \
        cups cups-bsd cups-client cups-filters cups-ipp-utils \
        avahi-daemon dbus libnss-mdns \
        # broad driver coverage out of the box
        printer-driver-all printer-driver-cups-pdf \
        hplip foomatic-db-compressed-ppds openprinting-ppds \
        ghostscript poppler-utils \
        # portal
        python3 python3-flask python3-cups python3-requests gunicorn \
        # needed by many vendor installers
        ca-certificates curl wget file procps psmisc lsb-base \
    && if [ "$INSTALL_OFFICE" = "true" ]; then \
         apt-get install -y --no-install-recommends \
           libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
           fonts-dejavu fonts-liberation2 ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Our default CUPS config; seeded into the /etc/cups volume on first start.
COPY hub/cupsd.conf /etc/cups/cupsd.conf
RUN sed -i 's/^#\?\s*SystemGroup.*/SystemGroup lpadmin root/' /etc/cups/cups-files.conf \
    && cp -a /etc/cups /opt/cups-defaults

COPY shared/hub-driver /usr/local/bin/hub-driver
COPY hub/entrypoint.sh /entrypoint.sh
COPY hub/portal /opt/portal
RUN sed -i 's/\r$//' /usr/local/bin/hub-driver /entrypoint.sh \
    && chmod +x /usr/local/bin/hub-driver /entrypoint.sh

VOLUME ["/etc/cups", "/drivers"]
EXPOSE 631 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD lpstat -r | grep -q "is running" && curl -fs "http://127.0.0.1:${PORTAL_PORT:-80}/healthz" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
