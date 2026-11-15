# Scan inbox: an FTP (and optionally SMB) drop folder so printers/MFPs can
# "Scan to FTP / Scan to Network" straight into ./scans, where the scan page
# lists them for download.
FROM alpine:3.20

RUN apk add --no-cache bash vsftpd samba-server samba-common-tools tzdata

COPY inbox/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

VOLUME ["/scans"]
EXPOSE 21 445
ENTRYPOINT ["/entrypoint.sh"]
