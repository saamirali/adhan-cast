"""Adhan service: local prayer times + cast adhan MP3s to Google Home / Chromecast."""
from contextlib import asynccontextmanager
import json, logging, os, re, socket, threading, time, uuid
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from adhanpy.PrayerTimes import PrayerTimes
from adhanpy.calculation.CalculationMethod import CalculationMethod
from adhanpy.calculation.CalculationParameters import CalculationParameters
from adhanpy.calculation.Madhab import Madhab

DATA_DIR = Path(os.environ.get("ADHAN_DATA_DIR", Path.home() / ".local/share/adhan"))
MEDIA_DIR = DATA_DIR / "media"
SETTINGS_FILE = DATA_DIR / "settings.json"
MOCK_CAST = os.environ.get("ADHAN_MOCK_CAST") == "1"
PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]
METHODS = {m.name: m for m in CalculationMethod if m.name != "NONE"}
ALADHAN_METHOD = {"NORTH_AMERICA": 2, "MUSLIM_WORLD_LEAGUE": 3, "EGYPTIAN": 5, "KARACHI": 1,
                  "UMM_AL_QURA": 4, "DUBAI": 16, "MOON_SIGHTING_COMMITTEE": 15, "KUWAIT": 9,
                  "QATAR": 10, "SINGAPORE": 11, "UOIF": 12}

DEFAULTS = {
    "location_name": "Milton, ON", "latitude": 43.5183, "longitude": -79.8774,
    "timezone": "America/Toronto", "method": "NORTH_AMERICA", "madhab": "HANAFI",
    "source": "local",            # "local" (offline, adhanpy) or "aladhan" (API, falls back to local)
    "offsets": {p: 0 for p in PRAYERS},  # minutes
    "port": int(os.environ.get("ADHAN_PORT", 8080)),
    "host_ip_override": "",
    "links": [],                  # [{"id": "abc123", "name": "Makkah adhan", "url": "https://..."}]
    "speakers": [],               # [{"name": "...", "host": "192.168.x.y" or ""}]
    "default_media": "", "global_volume": 0.5,
    "prayers": {p: {"enabled": True, "media": "", "volume": None} for p in PRAYERS},
}

log = logging.getLogger("adhan")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
EVENTS = deque(maxlen=200)
_lock = threading.RLock()


def event(kind, msg):
    tz = ZoneInfo(load_settings()["timezone"]) if SETTINGS_FILE.exists() else None
    EVENTS.appendleft({"time": datetime.now(tz).isoformat(timespec="seconds"), "kind": kind, "msg": msg})
    (log.error if kind == "error" else log.info)(msg)


# ---------- settings ----------
def _merge(d, base):
    out = dict(base)
    for k, v in (d or {}).items():
        out[k] = _merge(v, base[k]) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_settings():
    with _lock:
        try:
            return _merge(json.loads(SETTINGS_FILE.read_text()), DEFAULTS)
        except Exception:
            return json.loads(json.dumps(DEFAULTS))


def save_settings(s):
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2))
        tmp.replace(SETTINGS_FILE)


# ---------- prayer times ----------
_times_cache = {}


def compute_times(day: date, s=None):
    s = s or load_settings()
    key = (day.isoformat(), json.dumps({k: s[k] for k in ("latitude", "longitude", "timezone", "method", "madhab", "source", "offsets")}, sort_keys=True))
    if key in _times_cache:
        return _times_cache[key]
    tz = ZoneInfo(s["timezone"])
    times, source = None, "local"
    if s["source"] == "aladhan":
        try:
            r = requests.get(f"https://api.aladhan.com/v1/timings/{day.strftime('%d-%m-%Y')}", timeout=10, params={
                "latitude": s["latitude"], "longitude": s["longitude"], "method": ALADHAN_METHOD.get(s["method"], 2),
                "school": 1 if s["madhab"] == "HANAFI" else 0, "timezonestring": s["timezone"]})
            t = r.json()["data"]["timings"]
            times = {p: datetime.combine(day, datetime.strptime(t[p.capitalize()][:5], "%H:%M").time(), tz) for p in PRAYERS}
            times["sunrise"] = datetime.combine(day, datetime.strptime(t["Sunrise"][:5], "%H:%M").time(), tz)
            source = "aladhan"
        except Exception as e:
            event("error", f"Aladhan API failed ({e}); using local calculation")
    if times is None:
        params = CalculationParameters(method=METHODS[s["method"]])
        params.madhab = Madhab[s["madhab"]]
        pt = PrayerTimes((s["latitude"], s["longitude"]), datetime(day.year, day.month, day.day),
                         calculation_parameters=params, time_zone=tz)
        times = {p: getattr(pt, p).astimezone(tz) for p in PRAYERS + ["sunrise"]}
    for p in PRAYERS:
        times[p] = (times[p] + timedelta(minutes=int(s["offsets"].get(p, 0) or 0))).replace(second=0, microsecond=0)
    times["_source"] = source
    if len(_times_cache) > 50:
        _times_cache.clear()
    _times_cache[key] = times
    return times


# ---------- network / casting ----------
def lan_ip(s=None):
    s = s or load_settings()
    if s.get("host_ip_override"):
        return s["host_ip_override"]
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 1))  # no packets sent
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def media_url(name, s=None):
    s = s or load_settings()
    return f"http://{lan_ip(s)}:{s['port']}/media/{requests.utils.quote(name)}"


def discover_speakers(timeout=8):
    if MOCK_CAST:
        return [{"name": "Mock Speaker", "host": "192.0.2.10", "model": "Mock", "type": "audio"}]
    import pychromecast
    services, browser = pychromecast.discovery.discover_chromecasts(timeout=timeout)
    pychromecast.discovery.stop_discovery(browser)
    return sorted([{"name": c.friendly_name, "host": c.host, "model": c.model_name, "type": c.cast_type} for c in services],
                  key=lambda x: x["name"])


CAST_CALLS = []  # recorded in mock mode (used by tests)


def cast_to(speaker, url, volume):
    label = speaker.get("name") or speaker.get("host")
    if MOCK_CAST:
        CAST_CALLS.append({"speaker": label, "url": url, "volume": volume})
        event("play", f"[mock] cast {url} to {label} at volume {volume}")
        return
    import pychromecast
    browser = None
    try:
        if speaker.get("host"):
            casts, browser = pychromecast.get_listed_chromecasts(known_hosts=[speaker["host"]], timeout=10) \
                if not speaker.get("name") else pychromecast.get_listed_chromecasts(
                    friendly_names=[speaker["name"]], known_hosts=[speaker["host"]], timeout=10)
            if not casts and speaker.get("name"):
                casts, b2 = pychromecast.get_listed_chromecasts(known_hosts=[speaker["host"]], timeout=10)
                pychromecast.discovery.stop_discovery(b2)
        else:
            casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[speaker["name"]], timeout=10)
        if not casts:
            raise RuntimeError("speaker not found on network")
        cc = casts[0]
        cc.wait(timeout=10)
        if volume is not None:
            cc.set_volume(float(volume))
        mc = cc.media_controller
        mc.play_media(url, "audio/mpeg", title="Adhan")
        mc.block_until_active(timeout=15)
        event("play", f"Casting {url} to {label} (vol {volume})")
        cc.disconnect(timeout=5)
    except Exception as e:
        event("error", f"Cast to {label} failed: {e}")
    finally:
        if browser:
            pychromecast.discovery.stop_discovery(browser)


LINK_PREFIX = "link:"


def resolve_media(ref, s=None):
    """ref is an uploaded filename (legacy/plain) or 'link:<id>'. Returns (url, label) or (None, reason)."""
    s = s or load_settings()
    if not ref:
        return None, "no MP3 selected"
    if ref.startswith(LINK_PREFIX):
        link = next((l for l in s.get("links", []) if l["id"] == ref[len(LINK_PREFIX):]), None)
        return (link["url"], f"link '{link['name']}'") if link else (None, f"link {ref} no longer exists")
    if (MEDIA_DIR / ref).exists():
        return media_url(ref, s), ref
    return None, f"file {ref!r} not found"


def check_link(url):
    """Best-effort reachability check. Returns a warning string or None. Never blocks saving."""
    try:
        r = requests.head(url, timeout=6, allow_redirects=True)
        if r.status_code >= 400 or r.status_code == 405:
            r = requests.get(url, timeout=6, stream=True, headers={"Range": "bytes=0-1023"})
            r.close()
        if r.status_code >= 400:
            return f"URL returned HTTP {r.status_code}"
        ct = r.headers.get("content-type", "")
        if ct and not (ct.startswith("audio/") or "octet-stream" in ct or "mpeg" in ct):
            return f"content-type is {ct!r}, may not be playable audio"
    except Exception as e:
        return f"could not reach URL ({e.__class__.__name__})"
    return None


def play(prayer=None, media=None, speakers=None, volume=None):
    s = load_settings()
    if prayer:
        cfg = s["prayers"][prayer]
        media = media or cfg.get("media") or s["default_media"]
        volume = volume if volume is not None else (cfg.get("volume") if cfg.get("volume") is not None else s["global_volume"])
    media = media or s["default_media"]
    volume = volume if volume is not None else s["global_volume"]
    speakers = speakers or s["speakers"]
    url, label = resolve_media(media, s)
    if not url:
        event("error", f"{prayer or 'test'}: {label}")
        return False
    if not speakers:
        event("error", f"{prayer or 'test'}: no speakers selected")
        return False
    event("info", f"Playing {label} for {prayer or 'test'} on {len(speakers)} speaker(s)")
    for sp in speakers:
        threading.Thread(target=cast_to, args=(sp, url, volume), daemon=True).start()
    return True


# ---------- scheduler ----------
class Scheduler(threading.Thread):
    """Checks every few seconds; fires any enabled prayer whose time passed since last check.
    Uses tz-aware datetimes so DST transitions and midnight rollover are handled naturally."""

    def __init__(self, interval=5):
        super().__init__(daemon=True)
        self.interval = interval
        self.fired = set()
        self.last = None
        self.stop_evt = threading.Event()

    def tick(self, now=None):
        s = load_settings()
        tz = ZoneInfo(s["timezone"])
        now = now or datetime.now(tz)
        last = self.last or now - timedelta(seconds=self.interval)
        if now - last > timedelta(minutes=5):  # suspend/clock jump: don't fire stale adhans
            last = now - timedelta(seconds=self.interval)
        for d in {last.date(), now.date()}:
            t = compute_times(d, s)
            for p in PRAYERS:
                key = (d.isoformat(), p)
                if last < t[p] <= now and key not in self.fired and s["prayers"][p]["enabled"]:
                    self.fired.add(key)
                    play(prayer=p)
        self.fired = {k for k in self.fired if k[0] >= (now.date() - timedelta(days=1)).isoformat()}
        self.last = now

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.tick()
            except Exception as e:
                event("error", f"Scheduler error: {e}")
            self.stop_evt.wait(self.interval)


scheduler = Scheduler()

# ---------- API ----------
@asynccontextmanager
async def lifespan(app):
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_FILE.exists():
        save_settings(load_settings())
    if os.environ.get("ADHAN_NO_SCHEDULER") != "1":
        scheduler.start()
    event("info", f"Adhan service started; media served at http://{lan_ip()}:{load_settings()['port']}/media/ (mock={MOCK_CAST})")
    yield
    scheduler.stop_evt.set()


app = FastAPI(title="Adhan Service", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"




@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/media/{name}")
def media(name: str):
    p = (MEDIA_DIR / name).resolve()
    if p.parent != MEDIA_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/mpeg")


@app.get("/api/times")
def api_times(day: str = None):
    s = load_settings()
    tz = ZoneInfo(s["timezone"])
    now = datetime.now(tz)
    d = date.fromisoformat(day) if day else now.date()
    t = compute_times(d, s)
    upcoming = [(p, t[p]) for p in PRAYERS if t[p] > now] if d == now.date() else []
    if not upcoming and d == now.date():
        tm = compute_times(d + timedelta(days=1), s)
        upcoming = [("fajr", tm["fajr"])]
    nxt = upcoming[0] if upcoming else None
    return {"date": d.isoformat(), "timezone": s["timezone"], "location": s["location_name"], "source": t["_source"],
            "method": s["method"], "madhab": s["madhab"], "now": now.isoformat(timespec="seconds"),
            "times": {p: t[p].isoformat() for p in PRAYERS + ["sunrise"]},
            "next": {"prayer": nxt[0], "time": nxt[1].isoformat(), "seconds": int((nxt[1] - now).total_seconds())} if nxt else None}


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    return {**s, "detected_ip": lan_ip({**s, "host_ip_override": ""}), "methods": list(METHODS), "mock": MOCK_CAST}


@app.put("/api/settings")
def put_settings(new: dict):
    s = _merge({k: v for k, v in new.items() if k in DEFAULTS}, load_settings())
    try:
        ZoneInfo(s["timezone"]); METHODS[s["method"]]; Madhab[s["madhab"]]
        s["latitude"], s["longitude"] = float(s["latitude"]), float(s["longitude"])
        s["global_volume"] = min(1.0, max(0.0, float(s["global_volume"])))
    except Exception as e:
        raise HTTPException(400, f"invalid settings: {e}")
    save_settings(s)
    _times_cache.clear()  # recalc on settings change
    event("info", "Settings updated")
    return get_settings()


@app.get("/api/media")
def list_media():
    return [{"name": p.name, "size": p.stat().st_size, "url": f"/media/{p.name}"}
            for p in sorted(MEDIA_DIR.glob("*")) if p.is_file()]


@app.post("/api/media")
async def upload(file: UploadFile = File(...)):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename or "").name).strip("._")
    if not name.lower().endswith(".mp3"):
        raise HTTPException(400, "Only .mp3 files are accepted")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "File too large (50 MB max)")
    if not (data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)):
        raise HTTPException(400, "File does not look like an MP3")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(data)
    s = load_settings()
    if not s["default_media"]:
        s["default_media"] = name
        save_settings(s)
    event("info", f"Uploaded {name}")
    return {"name": name, "size": len(data)}


@app.delete("/api/media/{name}")
def delete_media(name: str):
    p = (MEDIA_DIR / name).resolve()
    if p.parent != MEDIA_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    p.unlink()
    s = load_settings()
    if s["default_media"] == name:
        s["default_media"] = ""
    for cfg in s["prayers"].values():
        if cfg.get("media") == name:
            cfg["media"] = ""
    save_settings(s)
    event("info", f"Deleted {name}")
    return {"ok": True}


@app.get("/api/links")
def list_links():
    return load_settings().get("links", [])


@app.post("/api/links")
def add_link(body: dict):
    name, url = str(body.get("name", "")).strip(), str(body.get("url", "")).strip()
    if not re.match(r"^https?://[^\s/]+", url):
        raise HTTPException(400, "URL must start with http:// or https://")
    link = {"id": uuid.uuid4().hex[:8], "name": name or url.rsplit("/", 1)[-1][:60] or "Audio link", "url": url}
    s = load_settings()
    s.setdefault("links", []).append(link)
    if not s["default_media"]:
        s["default_media"] = LINK_PREFIX + link["id"]
    save_settings(s)
    warning = check_link(url)
    event("error" if warning else "info", f"Added link {link['name']}" + (f" - warning: {warning}" if warning else ""))
    return {**link, "warning": warning}


@app.post("/api/links/check")
def recheck_link(body: dict):
    return {"warning": check_link(str(body.get("url", "")))}


@app.delete("/api/links/{link_id}")
def delete_link(link_id: str):
    s = load_settings()
    before = len(s["links"])
    s["links"] = [l for l in s["links"] if l["id"] != link_id]
    if len(s["links"]) == before:
        raise HTTPException(404)
    ref = LINK_PREFIX + link_id
    if s["default_media"] == ref:
        s["default_media"] = ""
    for cfg in s["prayers"].values():
        if cfg.get("media") == ref:
            cfg["media"] = ""
    save_settings(s)
    event("info", f"Deleted link {link_id}")
    return {"ok": True}


@app.get("/api/speakers/discover")
def api_discover():
    try:
        return discover_speakers()
    except Exception as e:
        event("error", f"Discovery failed: {e}")
        raise HTTPException(500, str(e))


@app.post("/api/test")
def api_test(body: dict = None):
    body = body or {}
    ok = play(prayer=body.get("prayer"), media=body.get("media"), speakers=body.get("speakers"), volume=body.get("volume"))
    return {"ok": ok}


@app.post("/api/stop")
def api_stop():
    if MOCK_CAST:
        return {"ok": True}
    import pychromecast
    for sp in load_settings()["speakers"]:
        try:
            casts, b = pychromecast.get_listed_chromecasts(
                friendly_names=[sp["name"]] if sp.get("name") else None,
                known_hosts=[sp["host"]] if sp.get("host") else None, timeout=8)
            for c in casts:
                c.wait(timeout=8); c.media_controller.stop(); c.disconnect()
            pychromecast.discovery.stop_discovery(b)
        except Exception as e:
            event("error", f"Stop failed on {sp}: {e}")
    return {"ok": True}


@app.get("/api/log")
def api_log():
    return list(EVENTS)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    port = int(os.environ.get("ADHAN_PORT") or load_settings()["port"])
    uvicorn.run(app, host=os.environ.get("ADHAN_BIND", "0.0.0.0"), port=port)


if __name__ == "__main__":
    main()
