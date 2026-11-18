# PrintScan Hub

One box on your network that holds all the printer and scanner drivers, so other
computers don't have to install anything.

| What | Where | Who |
|---|---|---|
| **Home**: big Print / Scan buttons, toner levels and printer status | `http://<server>/` | anyone on the LAN |
| **Upload & print** from any browser (PDF, images, text, Word/Excel/PowerPoint) | `http://<server>/print` | anyone on the LAN |
| **Scan** to PDF/JPG in the browser, shared or private | `http://<server>/scan` | anyone on the LAN; optional accounts |
| **Scan from the printer's button** into a shared folder | FTP / SMB, see below | the printer |
| **Add as a network printer** without vendor drivers (IPP Everywhere / AirPrint / Mopria) | `http://<server>:631/printers/<queue>` | laptops/phones that are allowed to add printers |
| **Admin**: add printers, install drivers | `http://<server>/admin` | `ADMIN_USER` / `ADMIN_PASSWORD` |
| Full CUPS admin | `http://<server>:631/admin` | same login |

631 is the default; set `CUPS_PORT` in `.env` if that port is taken (see [Linux host prep](#linux-host-prep-why-stop-the-hosts-cups)).

A locked-down work laptop can always use the upload page: it's just a website.

New to printer and scanner jargon (ADF, IPP, eSCL, PPD…)? See [Help: terms explained](#help-terms-explained).

## How it fits together

`docker compose up` starts three containers. The scanner and inbox share the `scans/` folder, so scans the printer sends to the inbox appear on the scan page.

```
      Browsers, laptops and phones on your LAN
      │ :80 portal + scan page  │ :631 IPP
      ▼                         ▼
 ┌──────────────────────────────────┐  ┌───────────────────┐  ┌───────────────────┐
 │ hub                              │  │ scanner           │  │ inbox             │
 │ scan page, private files ────────├─►│ scanservjs        │  │ FTP server        │
 │ web portal ──► CUPS + drivers    │  │ SANE + airscan    │  │ (+ SMB on Linux)  │
 └────────────────┬─────────────────┘  └─────────┬─────────┘  └─────────▲─────────┘
                  │ print (IPP/USB)              │ scan (eSCL)          │ Scan to FTP
                  ▼                              ▼                      │
 ┌──────────────────────────────────────────────────────────────────────┴─────────────┐
 │                              Printer / scanner (MFP)                               │
 └────────────────────────────────────────────────────────────────────────────────────┘
```

| Container | Base image | What runs inside |
|---|---|---|
| **hub** (`printscan-hub`) | Debian 12 (bookworm-slim) | **CUPS** print server and its drivers, the **web portal** (Python/Flask, served by gunicorn, talks to CUPS via pycups), **LibreOffice** to turn Office files into PDF, **Avahi** for printer discovery, the `hub-driver` installer, and the gateway to the scan page that keeps private scans private (SQLite, in `data/portal/`) |
| **scanner** (`printscan-scanner`) | [`sbs20/scanservjs`](https://hub.docker.com/r/sbs20/scanservjs) | **scanservjs** web scanning app on top of **SANE** and **sane-airscan**, plus the hub's additions: driver installer, feeder detection, Letter default, tooltips, "Scan now" label |
| **inbox** (`printscan-inbox`) | Alpine 3.20 | **vsftpd** FTP server (and **Samba** on Linux hosts) that saves the printer's own scans into `scans/` |

The scan app isn't reachable on its own port. The portal serves it at `/scan/app/` and shows it inside its **Scan** tab,
so it can decide who sees which scans. Ports above are the defaults; change them in `.env`.

### Built with

PrintScan Hub mostly wires together existing open-source projects. Many thanks to them:

| Project | Used for |
|---|---|
| [CUPS](https://openprinting.github.io/cups/) + [cups-filters](https://github.com/OpenPrinting/cups-filters) | Print server, print queues, IPP sharing, converting documents for printers |
| [OpenPrinting](https://openprinting.github.io/) drivers: Gutenprint, [HPLIP](https://developers.hp.com/hp-linux-imaging-and-printing), Foomatic, `printer-driver-*` | The roughly 17,000 printer models supported out of the box |
| [scanservjs](https://github.com/sbs20/scanservjs) by [sbs20](https://github.com/sbs20) | The web scanning interface (Scan page) |
| [SANE](http://www.sane-project.org/) + [sane-airscan](https://github.com/alexpevzner/sane-airscan) | Talking to scanners; driver-free network scanning over eSCL/WSD |
| [Flask](https://flask.palletsprojects.com/), [gunicorn](https://gunicorn.org/), [pycups](https://github.com/OpenPrinting/pycups) | The hub's web portal and its connection to CUPS |
| [LibreOffice](https://www.libreoffice.org/) | Printing Word/Excel/PowerPoint files (converted to PDF) |
| [Avahi](https://avahi.org/) | Bonjour/mDNS so printers and scanners can be discovered |
| [vsftpd](https://security.appspot.com/vsftpd.html), [Samba](https://www.samba.org/) | The scan inbox (Scan to FTP / network folder) |
| [Debian](https://www.debian.org/), [Alpine Linux](https://alpinelinux.org/), [Docker Compose](https://docs.docker.com/compose/) | Base images and running everything together |

Each project keeps its own license (see [License](#license)). The hub's own code is the portal (`hub/portal/`), the
entrypoints, `shared/hub-driver`, the scanner add-ons (`scanner/hub-*.js`,
`scanservjs/config/config.local.js`) and the compose files.

## Quick start

Best on a **Linux** host: a Raspberry Pi, mini PC, NAS or VM.

```bash
git clone https://github.com/kyrlon/printscan-hub.git && cd printscan-hub
scripts/linux-host.sh install-docker   # only if Docker isn't installed yet
scripts/linux-host.sh prepare          # stop the host's own CUPS (see below)
cp .env.example .env                   # set ADMIN_PASSWORD and HUB_ADDRESS (this machine's LAN IP)
docker compose up -d --build
```

### Linux host prep: why stop the host's CUPS?

On Linux the hub uses **host networking**, so its CUPS needs port **631** (or your `CUPS_PORT`) on the machine itself.
Desktop installs of Debian, Ubuntu and Raspberry Pi OS usually run their own CUPS there, and the
hub's CUPS can't start while it does. Every printer then lives in the hub instead. The helper script
does this for you:

| Command | What it does |
|---|---|
| `scripts/linux-host.sh status` | Read-only check: host CUPS/Avahi/ipp-usb/Docker, which hub ports are free, USB printers |
| `scripts/linux-host.sh prepare` | Stops and disables the host's `cups`/`cups-browsed`; installs and starts `avahi-daemon` and `ipp-usb` if missing |
| `scripts/linux-host.sh restore` | Turns the host's CUPS back on (run `docker compose down` first) |
| `scripts/linux-host.sh install-docker` | Installs Docker Engine + Compose from Docker's own repository and adds you to the `docker` group |

The commands that change the system ask for `sudo`. Keep **Avahi** (discovery) and **ipp-usb** running:
the hub uses both.

**Port 631 already taken, and you want to keep it that way?** Set `CUPS_PORT` in `.env` (e.g.
`CUPS_PORT=8631`) and run `docker compose up -d`. The hub's CUPS moves to that port and the host's CUPS
can stay. Things that follow automatically:
- AirPrint / Mopria / Windows discovery: they announce the new port.
- The *Add to your computer* addresses on the Print page.
- The *full CUPS admin* link.
- The Docker Desktop port mapping.
- `scripts/linux-host.sh status`.

Printers added **by hand** on laptops need the port in the address, e.g. `http://<server>:8631/printers/<queue>`.
Keep 631 if you can: it's what clients try by default. Running both CUPS servers also means two sets of
printers show up in discovery, so name the hub's queues clearly.

**USB printers on Linux:** most USB printers from the last ~8 years (including all-in-ones) support
*IPP over USB*. `ipp-usb` shares them at `http://localhost:60000`, so the hub can print **and scan**
with no vendor driver. In **Admin → Add a printer**, use the device URI `ipp://localhost:60000/ipp/print`
with the `everywhere` driver. `scripts/linux-host.sh status` shows whether ipp-usb found your printer.
Older USB printers without IPP over USB show up as `usb://…` devices and need a driver from the
*Installing drivers* section.

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

### Do you need a driver? Usually not: the `everywhere` driver

`everywhere` is CUPS's built-in driver for **IPP Everywhere**, the standard that lets a computer print
without a model-specific driver. It's the default when you add a printer. How it works:

1. When you add the printer, CUPS asks it over IPP what it can do: paper sizes, trays, two-sided,
   color, resolutions and the file formats it accepts.
2. From the answers, CUPS writes the printer's settings file (a PPD), the same thing a vendor driver
   would install, so the options match that exact printer.
3. When you print, CUPS converts the document to a standard format the printer understands
   (PWG Raster, Apple Raster or PDF).

It's the same technology as **AirPrint** and **Mopria**, so a printer that supports either almost
certainly works with `everywhere`. That covers most printers from the last ~10 years. USB printers
on Linux work too, through `ipp-usb`. CUPS itself is moving this way: classic per-model drivers are
being phased out.

**Limits:**
- The printer must answer over IPP when you add it, so use an `ipp://`, `ipps://` or `dnssd://…_ipp…`
  address. `socket://` and `lpd://` addresses can't use it.
- You get the standard features: paper, trays, two-sided, color, quality. Brand-only extras (toner save,
  some finishing options) may need the vendor driver.
- Printers from before about 2013 usually don't support it.

**Use a vendor driver instead** for an older printer, a non-IPP connection, or a printer that prints
wrongly with `everywhere` (bad margins, a missing feature you need).

### Finding and installing a driver

The image already has `printer-driver-all`, HPLIP, Gutenprint, Foomatic and the
OpenPrinting PPDs (about 17,000 models). Search for yours under **Admin → Add a printer → Driver → Search installed drivers…**
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

## How scanning works

```
Browser ──► hub portal (/scan/app/) ──► scanservjs ──► scanimage (SANE) ──► backends ──► scanner
             hides others' private      web UI + API    the scanning tool    airscan, USB, hpaio…
             files
```

- **scanservjs** is a small Node.js web app. It doesn't talk to scanners itself: it runs SANE's
  `scanimage` to list scanners, read their options and scan.
- **SANE backends** (listed in `/etc/sane.d/dll.conf`) do the talking: **sane-airscan** for driverless
  network and IPP-over-USB scanners (eSCL/WSD), USB backends such as `epson2` or `genesys`, `net` for
  scanners shared by another Linux machine, and `hpaio` if you add `hplip` to `drivers/scanner/`.
  SANE's older `escl` backend is turned off because it listed every driverless scanner a second time.
- **Scanning and printing find devices separately.** Printers come from CUPS in the `hub` container;
  scanners come from SANE in the `scanner` container. An all-in-one appears in both, and adding a
  printer on the Admin page has no effect on the Scan page.

### When are scanners detected?

Not continuously. The first time the scan page loads after the scanner container starts, scanservjs
runs `scanimage -L` and asks each scanner for its options. That takes a few seconds to half a minute,
and the result is cached. Later page loads use the cache. It searches again when you press **↻** next
to *Device*, or when the scanner container restarts. **Plugged in or switched on a scanner later? Press ↻.**

Whether network scanners are found automatically depends on the host:

| Host | Network scanners | How |
|---|---|---|
| Linux | ✅ found automatically | sane-airscan hears Bonjour/mDNS announcements (through the host's Avahi) and sends WSD probes. USB devices shared by `ipp-usb` show up with "(USB)" |
| Docker Desktop | ❌ list them yourself | Announcements don't reach Docker's VM: add `AIRSCAN_DEVICES` to `.env` |

Scanners listed in `AIRSCAN_DEVICES`, `SANED_NET_HOSTS` or `PIXMA_HOSTS` are always included.
In a name like `airscan:e1:…`, `e` means eSCL (`w` = WSD) and the number is just its position.

### What happens on Scan now

1. The page sends the settings (device, source, resolution, area, mode, format) to the scan API.
2. scanservjs runs `scanimage`; the raw image goes to a temporary folder.
3. A pipeline converts it to the chosen format (JPG, PNG, PDF, or PDF with text recognition).
4. The file is saved in `scans/` and its name returned. In a private mode, the portal records who owns it.

**Preview** is a quick low-resolution scan that isn't saved; drag the box on it to set the scan area.

The hub's additions (Letter default, automatic Source, the feeder status under *Device*) run in
scanservjs' settings hook (`scanservjs/config/config.local.js`) on **every** page load, not only after a
search. The feeder state is a live question to the scanner (`/eSCL/ScannerStatus`), so it updates
without pressing ↻.

## Glass or document feeder?

The **Source** list on the Scan page comes from each scanner, so it differs by device:
flatbed-only models show just *Flatbed*, and models with a feeder add *ADF*, *ADF Duplex*,
*ADF Front/Back* or similar. Hover over any setting for a short description; on a phone or tablet, press and hold it. This works
throughout the portal too.

`SCAN_DEFAULT_SOURCE` in `.env` sets where scans start:

| Value | Behaviour |
|---|---|
| `auto` (default) | Feeder if it has paper, otherwise the glass. Checked each time the scan page loads. The feeder state for the selected device shows under **Device**, with a **Switch to feeder/glass** button if you load or remove paper afterwards |
| `auto-duplex` | Same as `auto`, but both sides when using the feeder |
| `flatbed` / `adf` / `duplex` | Always start there |
| `remember` | Whatever was used last time in that browser |

You can still change Source by hand before each scan. Feeder detection uses eSCL, so it works for
scanners listed in `AIRSCAN_DEVICES` or found automatically on a Linux host.

## Who sees scans

The **Files:** switch at the top of the Scan page controls who sees scans under **FILES**:

| Mode | Who sees the files | Limits (`.env`) |
|---|---|---|
| **Shared** (default) | Everyone on the network. Scans sent from the printer's own Scan button always land here | none |
| **Only this browser** | Only the browser that made them. No account needed. Clearing the browser's cookies loses access | `PRIVATE_MAX_FILES=20`, `PRIVATE_KEEP_DAYS=7` |
| **Only me** (signed in) | The account holder, on any device they sign in on. Accounts start in this mode | `ACCOUNT_MAX_FILES=200`, `ACCOUNT_KEEP_DAYS=0` (keep) |

Past the file limit, the oldest private scans are removed; `0` means unlimited.

The **preview image** always belongs to the device that made it, whatever the mode. Someone else
pressing Preview or Scan doesn't change what you see, and your phone and computer each keep their
own preview even when signed in to the same account. Scan settings are also remembered per browser.
The admin can see every device's latest preview under **Admin → Scanners → Scan page sessions**.

While someone is previewing or scanning, everyone else's Scan page shows a **scanner in use** bar
(who started it and how long ago). Starting a scan on that same scanner is refused until it finishes;
other scanners stay free.

Accounts are optional: the admin creates them under **Admin → Accounts**, or set `ALLOW_SIGNUP=true`
so people can create their own. The **Sign in** link appears on the Scan page once accounts are on.
Deleting an account either keeps its scans (they become shared) or deletes them.

This keeps scans apart on a shared network. It isn't strong security: the hub uses plain HTTP, so
anyone who can watch your network traffic could see files in transit.

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
| HP ENVY Photo 7100 | USB, via `ipp-usb` on a Debian 13 host | `everywhere` (no driver), URI `ipp://localhost:60000/ipp/print` | ✅ test page (Print page button) | ➖ | ✅ tri-color + black ink; "Photo tray empty" shown per tray | Also on Wi-Fi; no Settings link when reached over USB. One early test page was reported "completed" by the printer but never came out, right as it flagged its empty photo tray |

### Scanners

| Scanner | Connection | Backend | Browser scan | Scan from printer to hub folder (FTP) | Notes |
|---|---|---|---|---|---|
| HP ENVY Photo 7100 | USB via `ipp-usb`, and network | `sane-airscan` (eSCL, no driver) | ➖ (detected, Flatbed only) | ➖ | Auto-discovered on a Linux host, both as "(USB)" and over Wi-Fi |
| Brother MFC-L3780CDW | Network | `sane-airscan` (eSCL, no driver) | ✅ flatbed | ➖ | Reports Flatbed, ADF, ADF Duplex at 100–600 dpi. Feeder detection ✅ (auto-selects ADF when loaded); scanning through the ADF not tried yet. Needs `AIRSCAN_DEVICES` on Docker Desktop |

### Hosts

| Host | Network printing | USB devices | Auto-discovery | Notes |
|---|---|---|---|---|
| Windows 11 + Docker Desktop (WSL2) | ✅ | ❌ | ❌ | Use `docker-compose.desktop.yml`; add printers/scanners by IP |
| Debian 13 (trixie), amd64 | ✅ | ✅ via `ipp-usb` | ✅ scanners found automatically | Run `scripts/linux-host.sh prepare` first (stops the host's CUPS) |

Tried a device? Add a row, including failures. Note the connection, driver/backend and
anything you had to change.

## What lives where

| Path | Contents |
|---|---|
| `data/cups/` | CUPS state: printers, their PPDs, `cupsd.conf`. Back this up. |
| `data/portal/` | Accounts and which private scan belongs to whom (`portal.db`). Back this up too. |
| `drivers/printer/`, `drivers/scanner/` | Drivers to re-install on every rebuild |
| `scans/` | Scanned files |
| `hub/` | Print hub image: CUPS + portal (`hub/portal/app.py`) |
| `scanner/` | scanservjs image with the same driver manager |
| `inbox/` | FTP/SMB drop folder for the printer's own Scan button |
| `docker-compose.desktop.yml` | Override for Docker Desktop (port mapping instead of host networking) |
| `shared/hub-driver` | The driver installer used by both images |
| `scripts/linux-host.sh` | Linux host helper: status, prepare/restore host CUPS, install Docker |

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
- The scan app only listens inside the host (or the containers' private network on Docker Desktop);
  everyone goes through the portal, which enforces private scans.
- Account passwords are stored hashed in `data/portal/portal.db`.
- Don't expose ports 80/631 (or the inbox's FTP ports) to the internet.
- To shrink the image by ~400 MB, set `INSTALL_OFFICE=false` in `.env` and rebuild.
  Office files then have to be saved as PDF before uploading.

## To-do: Home Assistant

Ideas for connecting the hub to [Home Assistant](https://www.home-assistant.io/). None of these are built or tested yet.

- [ ] **Printer status and toner sensors.** Try HA's built-in *Internet Printing Protocol (IPP)* integration
      pointed at a hub queue (`<server>`, port `631`, path `/printers/<queue>`), or at the printer directly. Document what works.
- [ ] **Hub status endpoint.** A JSON `/api/status` on the portal (printers, toner, alerts, feeder state, recent jobs)
      for HA `rest` sensors.
- [ ] **Alerts.** Example HA automations: notify a phone on *toner low*, *out of paper* or *paper jam*.
- [ ] **Feeder sensor.** A binary sensor for "paper in the document feeder", using the same eSCL check as the Scan page.
- [ ] **Scan from HA.** A `rest_command` or dashboard button that starts a scan through the scanservjs API
      (`POST /api/v1/scan`), e.g. "scan to PDF from the feeder".
- [ ] **New-scan notifications.** Notify HA (webhook) when a file lands in `scans/`, including scans sent from the printer's own Scan button.
- [ ] **Print from HA.** Send a file or generated text (shopping list, chore chart) to a printer through the portal.
- [ ] **Packaging.** An HA add-on (Supervisor) or MQTT discovery so the hub's entities appear automatically.

## Help: terms explained

### Scanning

| Term | What it means |
|---|---|
| **ADF** (Automatic Document Feeder) | The tray on top of many printers that pulls a stack of pages through one at a time. Load pages face up. Shown as *ADF* in the Source list |
| **Batch** | Putting several scanned pages into one file. *Manual* asks before each page (use it on the glass); *Auto* keeps going (use it with the feeder) |
| **Color / Gray / Lineart** | Scan modes. *Gray* is shades of gray (fine for black-and-white photos); *Lineart* is pure black and white with the smallest files, good for text |
| **DPI** (dots per inch) | How much detail a scan captures. 200–300 dpi for documents, 600 for photos. Doubling the dpi makes files about four times bigger |
| **Duplex** | Both sides of each page. *ADF Duplex* scans front and back in one pass |
| **eSCL / AirScan** | The standard, driver-free way modern network scanners talk to computers (Apple calls it AirScan). The hub uses it to scan and to check whether the feeder has paper |
| **Flatbed / Platen** | The glass under the scanner lid. One page at a time; best for photos, books and fragile pages |
| **Preview** | A quick low-resolution scan for checking the page and dragging the box to choose the scan area. It doesn't save a file |
| **SANE** | The Linux scanning system the scan page is built on. *sane-airscan* is its driver-free eSCL backend |
| **Scan area** | The rectangle that gets scanned (Top, Left, Width, Height in mm). Defaults to Letter, set by `SCAN_DEFAULT_PAPER` |
| **scanservjs** | The open-source web scanning app shown on the Scan page |
| **Source** | Where the paper is scanned from: glass or feeder. Each scanner reports its own list |

### Printing

| Term | What it means |
|---|---|
| **AirPrint / Mopria** | Apple's and Android's built-in driver-free printing. Devices see the hub's printers this way without installing anything |
| **CUPS** | The print server inside the hub. It holds the queues and drivers and sends jobs to the printers |
| **Device URI** | The address CUPS uses to reach a printer, e.g. `ipp://192.168.1.185/ipp/print` (network), `socket://…:9100` (raw network) or `usb://…` |
| **Driver** | Software that turns a document into the printer's own language. With `everywhere` (IPP Everywhere), no vendor driver is needed |
| **Driverless** | Printing or scanning with built-in, standard protocols (IPP Everywhere, AirPrint, eSCL) instead of vendor drivers |
| **IPP** (Internet Printing Protocol) | The standard network printing protocol. Clients add the hub's printers with `http://<server>:631/printers/<queue>` |
| **IPP Everywhere** (`everywhere`) | The driver-free flavour of IPP that most printers from the last ~10 years support (anything with AirPrint or Mopria). CUPS asks the printer what it can do and builds its settings from the answer, so nothing is installed. Needs an `ipp`/`ipps` address. See [Do you need a driver?](#do-you-need-a-driver-usually-not-the-everywhere-driver) |
| **Job** | One print request. It shows in the queue as pending, printing, completed, canceled or aborted |
| **PPD** (PostScript Printer Description) | A small file describing a printer's options (trays, duplex, paper sizes). Many Linux drivers come as PPDs |
| **Queue** | A printer as CUPS knows it, with a name like `home_printer`. One physical printer can have several queues with different settings |
| **Toner / marker levels** | Supply levels the printer reports (shown as colored bars). *Low* is the printer's own warning threshold |
| **Vendor `.deb`** | A Linux driver package from the printer maker (Debian/Ubuntu format). Upload it on the Admin page. Some are *i386* (32-bit), which the hub handles |

### Network & setup

| Term | What it means |
|---|---|
| **Bonjour / mDNS / Avahi** | How devices announce themselves on a local network so others can find them automatically. Avahi is the Linux version |
| **DHCP reservation** | A router setting that always gives a device the same IP address. Use it for the printer and the hub so their addresses don't change |
| **Docker Desktop / WSL2** | Docker for Windows and Mac. Containers run inside a small Linux virtual machine, which is why USB and auto-discovery don't work there |
| **`.env`** | The settings file for the hub (passwords, ports, defaults). Edit it, then run `docker compose up -d` |
| **FTP / passive mode** | A simple file-transfer protocol printers use for "Scan to FTP". *Passive mode* keeps it working through Docker and firewalls, so leave it on |
| **Host networking / port mapping** | Two ways containers reach the network. Host networking shares the computer's network directly (Linux). Port mapping forwards chosen ports (Docker Desktop) |
| **Hub address** (`HUB_ADDRESS`) | This computer's IP on your network, e.g. `192.168.1.101`. Printers need it to send scans; "localhost" only means "this same computer" |
| **LAN** | Your local network: the devices on the same router or Wi-Fi |
| **SMB** | Windows file sharing (`\\server\share`). An alternative "Scan to Network" target on Linux hosts |
| **USB/IP (usbipd-win)** | A tool that passes a USB device from Windows into Docker Desktop's virtual machine |

## License

PrintScan Hub's own code is released under the [MIT License](LICENSE).

The projects it builds on keep their own licenses. Notably, scanservjs is GPL-2.0: if you publish
a built `printscan-scanner` image, its scanservjs part falls under that license.
