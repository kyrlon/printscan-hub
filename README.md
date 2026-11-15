# PrintScan Hub

One box on your network that holds all the printer and scanner drivers, so other
computers don't have to install anything.

| What | Where | Who |
|---|---|---|
| **Home**: big Print / Scan buttons, toner levels and printer status | `http://<server>/` | anyone on the LAN |
| **Upload & print** from any browser (PDF, images, text, Word/Excel/PowerPoint) | `http://<server>/print` | anyone on the LAN |
| **Scan** to PDF/JPG in the browser | `http://<server>/scan` | anyone on the LAN |
| **Scan from the printer's button** into a shared folder | FTP / SMB, see below | the printer |
| **Add as a network printer** without vendor drivers (IPP Everywhere / AirPrint / Mopria) | `http://<server>:631/printers/<queue>` | laptops/phones that are allowed to add printers |
| **Admin**: add printers, install drivers | `http://<server>/admin` | `ADMIN_USER` / `ADMIN_PASSWORD` |
| Full CUPS admin | `http://<server>:631/admin` | same login |

A locked-down work laptop can always use the upload page: it's just a website.

## Quick start

Best on a **Linux** host: a Raspberry Pi, mini PC, NAS or VM.

```bash
cp .env.example .env        # then set ADMIN_PASSWORD
docker compose up -d --build
```

**Docker Desktop (Windows/macOS)** works for network printers, but USB printers and
auto-discovery don't work (the admin page shows a warning). Containers run inside a
Linux VM that your PC's USB ports and LAN multicast don't reach. Add this line to `.env`
so the hub uses normal port mapping (on macOS, use `:` instead of `;`):

```
COMPOSE_FILE=docker-compose.yml;docker-compose.desktop.yml
```

Network scanners can't be auto-discovered there either, so list yours in `.env`
(most network MFPs support driverless eSCL at `http://<printer-ip>/eSCL`):

```
AIRSCAN_DEVICES=Office_MFC=http://192.168.1.50/eSCL,eSCL
```

On Windows, a USB printer can be passed into the VM with
[usbipd-win](https://github.com/dorssel/usbipd-win) (`usbipd bind` + `usbipd attach --wsl`).
The warning goes away once the container can see a USB bus.

Open `http://<server>/admin`, click **Scan network & USB for printers**, pick the
printer, leave the driver as `everywhere` (works for most network printers from
the last ~10 years), and click **Add printer**.

## Installing drivers

The image already has `printer-driver-all`, HPLIP, Gutenprint, Foomatic and the
OpenPrinting PPDs (about 17,000 models). Search for yours under **Admin → Add a printer → Pick a driver**
before installing anything.

If your printer isn't covered, there are three ways to add a driver. All three are
**remembered in `drivers/printer/` and re-installed automatically** whenever the
container is rebuilt or updated:

1. **Web upload**: Admin → Drivers → upload a vendor `.deb`, a `.ppd`/`.ppd.gz`, or a `.sh` install script.
2. **Debian package**: Admin → Drivers → type a package name (e.g. `printer-driver-brlaser`).
   Use *Search for a driver package* if you don't know the name.
3. **Drop files in the folder**, then click *Re-install all* (or restart the container):

   ```
   drivers/printer/
     apt-packages.txt    one Debian package per line
     debs/               vendor .deb files (Brother, Epson, Canon, Kyocera, Xerox...)
     ppd/                .ppd / .ppd.gz files
     scripts/            *.sh, run once per container (for vendors that ship installers)
   ```

From a shell:

```bash
docker exec printscan-hub hub-driver search brother
docker exec printscan-hub hub-driver apt printer-driver-brlaser
docker exec printscan-hub hub-driver add /drivers/debs/mfcl2710dwpdrv-4.0.0-1.i386.deb
docker exec printscan-hub hub-driver list
```

Notes:
- Download the vendor's **Linux → Debian/Ubuntu (.deb)** package. i386-only packages (common for
  Brother) are handled: 32-bit support is switched on automatically.
- The hub runs Debian 12 on whatever CPU your host has. On a Raspberry Pi (arm64), x86-only
  vendor binaries won't run, so use the open-source drivers (`printer-driver-*`, `hplip`, `everywhere`).
- Each install is logged to `drivers/printer/install.log`, which you can also read on the admin page.

### Scanner drivers

Most network scanners from the last ~8 years work with no driver through eSCL/AirScan
(`sane-airscan` is included). For others, use `drivers/scanner/` the same way
(`apt-packages.txt`, `debs/` e.g. Brother `brscan4`, `scripts/`) and run
`docker compose restart scanner`. If a network scanner isn't detected, set
`SANED_NET_HOSTS`, `AIRSCAN_DEVICES` or `PIXMA_HOSTS` in `.env`.

## Scan from the printer's own Scan button

The `inbox` service gives printers a place to save scans: FTP on every host, plus an
SMB share (`\\<hub>\scans`) on Linux hosts. Anything saved there shows up under **Files**
on the Scan page.

1. In `.env`, set `HUB_ADDRESS` (the hub's LAN IP) and `SCAN_INBOX_PASSWORD`, then run `docker compose up -d`.
2. Open **Admin → Scan to hub folder**. It lists the exact values to enter in the printer's
   "Scan to FTP" / "Scan to Network" profile, with steps for Brother.
3. On the printer: **Scan → FTP → (profile) → Start**.

Leave **Passive mode** on. On Docker Desktop only FTP is available, because Windows already uses port 445.

## Using it from client computers

- **Windows 10/11**: Settings → Printers & scanners → Add device. If the printer isn't listed, choose
  *Add manually* → *Select a shared printer by name* → `http://<server>:631/printers/<queue>`.
  If Windows asks for a driver, pick **Microsoft IPP Class Driver**. No vendor driver is needed.
- **macOS / iOS**: printers show up automatically (AirPrint).
- **Android / ChromeOS**: printers show up automatically (Mopria / built-in print service).
- **Not allowed to add a printer at all**: use `http://<server>/`.

Auto-discovery needs mDNS. The hub uses the host's `avahi-daemon` if one is running
(`sudo apt install avahi-daemon`). If the host has none and no D-Bus, the container runs its own.
Set `AVAHI=off` to turn discovery off.

## Tested hardware

✅ works · ⚠️ works with a caveat · ❌ doesn't work · ➖ not tested yet

### Printers

| Printer | Connection | Driver | Print from portal | Driverless clients (IPP) | Toner / status | Notes |
|---|---|---|---|---|---|---|
| Brother MFC-L3780CDW | Network (Wi-Fi/Ethernet) | `everywhere` (no driver) | ✅ text, Word (.docx), test page | ➖ | ✅ live CMYK levels | Added by IP address on Docker Desktop |

### Scanners

| Scanner | Connection | Backend | Browser scan | Scan from printer to hub folder (FTP) | Notes |
|---|---|---|---|---|---|
| Brother MFC-L3780CDW | Network | `sane-airscan` (eSCL, no driver) | ✅ flatbed | ➖ | Reports Flatbed, ADF, ADF Duplex at 100–600 dpi; ADF not tried yet. Needs `AIRSCAN_DEVICES` on Docker Desktop |

### Hosts

| Host | Network printing | USB devices | Auto-discovery | Notes |
|---|---|---|---|---|
| Windows 11 + Docker Desktop (WSL2) | ✅ | ❌ | ❌ | Use `docker-compose.desktop.yml`; add printers/scanners by IP |
| Linux (Raspberry Pi, mini PC, VM) | ➖ | ➖ | ➖ | Recommended host; not tested yet |

Tried a device? Add a row, including failures. Note the connection, driver/backend and
anything you had to change.

## What lives where

| Path | Contents |
|---|---|
| `data/cups/` | CUPS state: printers, their PPDs, `cupsd.conf`. Back this up. |
| `drivers/printer/`, `drivers/scanner/` | Drivers to re-install on every rebuild |
| `scans/` | Scanned files |
| `hub/` | Print hub image: CUPS + portal (`hub/portal/app.py`) |
| `scanner/` | scanservjs image with the same driver manager |
| `inbox/` | FTP/SMB drop folder for the printer's own Scan button |
| `docker-compose.desktop.yml` | Override for Docker Desktop (port mapping instead of host networking) |
| `shared/hub-driver` | The driver installer used by both images |

`hub/cupsd.conf` is only the default. After the first start, edit `data/cups/cupsd.conf` and
`docker compose restart hub`.

## Updating

```bash
docker compose build --pull && docker compose up -d
```

Your printers (`data/cups`) are kept, and your drivers are re-installed from `drivers/`.
The first start after a rebuild takes longer while the drivers install.

## Security

- Printing, scanning and the job list are open to the **local network** only (CUPS `@LOCAL`).
  Anyone on the LAN can also cancel jobs from the portal.
- Admin pages need `ADMIN_USER`/`ADMIN_PASSWORD`. Driver uploads run as root inside a
  privileged container, so pick a strong password.
- Don't expose ports 80/631/8080 to the internet.
- To shrink the image by ~400 MB, set `INSTALL_OFFICE=false` in `.env` and rebuild.
  Office files then have to be saved as PDF before uploading.
