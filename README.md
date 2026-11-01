# Adhan Service (Google Home / Chromecast)

Self-hosted service for Ubuntu that calculates prayer times locally (offline, `adhanpy`, ISNA by default, for Milton ON)
and casts your own adhan MP3s to Google Home / Nest / Chromecast speakers on the LAN at Fajr, Dhuhr, Asr, Maghrib and Isha.

## Install (Ubuntu 22.04+)
```bash
tar xzf adhan-service.tar.gz && cd adhan-service
sudo ./install.sh          # or: sudo ./install.sh 8090 for a different port
```
This creates the `adhan` system user, copies the app to `/opt/adhan-service`, builds a venv, and enables `adhan.service`
(`Restart=on-failure`). Data (settings.json + MP3s) lives in `/var/lib/adhan`.
Then open `http://<ubuntu-ip>:8080` from your phone or PC:
1. Upload an adhan MP3 (and optionally a separate Fajr adhan).
2. **Discover speakers** → Add (or add by IP). Speaker groups work too.
3. Pick an MP3 / volume per prayer, turn prayers on/off, then **Save** and **▶ Test now**.

Manage: `systemctl status adhan`, `journalctl -u adhan -f`, `sudo systemctl restart adhan`.
Update to a new version (keeps settings + MP3s in /var/lib/adhan):
```bash
tar xzf adhan-service.tar.gz && cd adhan-service && sudo ./update.sh
```
Uninstall: `sudo ./uninstall.sh` (keeps data) or `sudo ./uninstall.sh --purge`.

Dev run without installing: `ADHAN_MOCK_CAST=1 ./run-dev.sh` (mock logs casts instead of playing). Tests: `./venv/bin/pip install pytest httpx && ./venv/bin/pytest -q`.

## Audio sources: uploads and links
- **Upload** an MP3 (from a PC or straight from your phone's file picker; max 50 MB). It is served to speakers from this PC.
- **Add link**: a name + http/https URL to an MP3/audio stream. The URL is checked for reachability when added (and via *Check*),
  but a failing check only warns – the link is still saved. The speaker streams the URL itself, so it must be reachable from the speaker
  (public HTTPS works best; Google speakers may refuse some hosts or redirects).
- Every prayer picker lists both, labelled 📁 file / 🔗 link. Links are stored in settings.json as `links: [{id,name,url}]` and referenced as
  `link:<id>`; old settings files (plain filenames, no `links`) keep working unchanged.

## Phone / home screen
The UI is mobile-first (44px touch targets, 16px inputs so iOS doesn't zoom, safe-area aware, sticky header). Open it on your phone and use
*Add to Home Screen* (iOS Safari / Android Chrome) – it ships a local web-app manifest and icon, no internet needed. Dark is the default;
the ☀️/🌙 button switches theme and is remembered per browser.

## Settings
Location/lat/lon/timezone, method (NORTH_AMERICA = ISNA, MWL, Egyptian, Karachi, …), Asr Hanafi/Shafi, per-prayer minute offsets
(`offsets` in settings.json, e.g. to match your masjid), source = local or Aladhan API (falls back to local if offline),
host LAN IP override. Times are recalculated each day and immediately on settings change; tz-aware times handle DST.
If the PC was asleep/suspended for >5 minutes, missed adhans are skipped rather than played late.
**The PC must be awake** at prayer times — disable automatic suspend in Settings → Power.

## Firewall
The speaker downloads the MP3 from this machine, so port 8080 must be reachable on the LAN; discovery uses mDNS.
```bash
sudo ufw allow 8080/tcp
sudo ufw allow 5353/udp     # mDNS
```
(install.sh does this automatically if ufw is active.)

## Troubleshooting
- **No speakers discovered**: the Ubuntu box and speakers must be on the same subnet. Many routers split 2.4 GHz / 5 GHz or
  guest networks into separate segments, or have **AP/client isolation** enabled — turn it off or put both on the same SSID/VLAN.
  mDNS doesn't cross VLANs without an mDNS reflector (e.g. avahi `enable-reflector=yes`).
- **Enter the speaker IP manually**: Google Home app → speaker → Settings → Device information → IP address. Add it with "Add by IP".
  Give it a DHCP reservation in your router so it doesn't change.
- **Speaker chimes but no sound / "Cast failed"**: check the "Speakers fetch audio from http://…" URL in the UI is this PC's LAN IP
  (not Docker/VPN, e.g. 172.x or tun0). If wrong, set *Host LAN IP override*. Open that URL from your phone to confirm it plays.
- **Wrong times**: check lat/lon, timezone, method and Asr school; use offsets for masjid adjustments.
- Logs: the "Recent activity" panel in the UI and `journalctl -u adhan`.
