"""Run: ADHAN_MOCK_CAST=1 ./venv/bin/pytest -q   (no real speaker needed)"""
import os, tempfile, time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
os.environ["ADHAN_DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADHAN_MOCK_CAST"] = "1"
os.environ["ADHAN_NO_SCHEDULER"] = "1"
from fastapi.testclient import TestClient
from app import main

TZ = ZoneInfo("America/Toronto")
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 500


def client():
    return TestClient(main.app).__enter__()


def test_ui_and_times():
    c = client()
    assert c.get("/").status_code == 200 and "<title>Adhan</title>" in c.get("/").text
    assert c.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert c.get("/static/icon-192.png").status_code == 200
    t = c.get("/api/times").json()
    times = {k: datetime.fromisoformat(v) for k, v in t["times"].items()}
    order = ["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]
    assert all(times[a] < times[b] for a, b in zip(order, order[1:]))
    assert 11 <= times["dhuhr"].hour <= 13 and t["next"]


def test_dst_and_madhab():
    s = main.load_settings()
    summer, winter = main.compute_times(date(2026, 7, 1), s), main.compute_times(date(2026, 12, 1), s)
    assert summer["dhuhr"].utcoffset() == timedelta(hours=-4) and winter["dhuhr"].utcoffset() == timedelta(hours=-5)
    s2 = dict(s, madhab="SHAFI")
    assert main.compute_times(date(2026, 7, 1), s2)["asr"] < summer["asr"]


def test_upload_serve_delete():
    c = client()
    assert c.post("/api/media", files={"file": ("bad.txt", b"hi")}).status_code == 400
    r = c.post("/api/media", files={"file": ("My Adhan.mp3", MP3, "audio/mpeg")}).json()
    assert r["name"] == "My_Adhan.mp3"
    assert c.get("/media/My_Adhan.mp3").content == MP3
    assert c.get("/api/settings").json()["default_media"] == "My_Adhan.mp3"
    c.post("/api/media", files={"file": ("fajr.mp3", MP3)})
    assert c.delete("/api/media/fajr.mp3").status_code == 200
    assert c.get("/media/fajr.mp3").status_code == 404


def test_scheduler_fires_mocked_cast():
    c = client()
    c.post("/api/media", files={"file": ("main.mp3", MP3)})
    c.post("/api/media", files={"file": ("fajr.mp3", MP3)})
    s = c.get("/api/settings").json()
    s["speakers"] = [{"name": "Kitchen", "host": "192.0.2.5"}]
    s["default_media"] = "main.mp3"
    s["prayers"]["fajr"] = {"enabled": True, "media": "fajr.mp3", "volume": 0.3}
    s["prayers"]["dhuhr"]["enabled"] = False
    s["host_ip_override"] = "192.168.1.20"
    assert c.put("/api/settings", json=s).status_code == 200
    main.CAST_CALLS.clear()
    d = date.today()
    t = main.compute_times(d)
    sch = main.Scheduler(interval=5)
    for p in main.PRAYERS:  # simulate ticks straddling each prayer time
        sch.last = t[p] - timedelta(seconds=3)
        sch.tick(now=t[p] + timedelta(seconds=2))
        sch.tick(now=t[p] + timedelta(seconds=7))  # must not double-fire
    time.sleep(0.5)
    urls = [x["url"] for x in main.CAST_CALLS]
    assert len(urls) == 4  # dhuhr disabled
    assert urls[0] == "http://192.168.1.20:8080/media/fajr.mp3" and main.CAST_CALLS[0]["volume"] == 0.3
    assert all(u.endswith("/media/main.mp3") for u in urls[1:])
    assert c.post("/api/test", json={}).json()["ok"]
    assert any(e["kind"] == "play" for e in c.get("/api/log").json())


def test_links_and_backcompat(monkeypatch):
    import json
    c = client()
    # legacy settings.json (no "links" key, plain filename refs) still loads
    s = json.loads(main.SETTINGS_FILE.read_text()); s.pop("links", None)
    main.SETTINGS_FILE.write_text(json.dumps(s))
    assert c.get("/api/settings").json()["links"] == []
    assert c.post("/api/links", json={"name": "x", "url": "ftp://nope"}).status_code == 400
    monkeypatch.setattr(main, "check_link", lambda url: "could not reach URL (ConnectTimeout)")
    r = c.post("/api/links", json={"name": "Makkah", "url": "https://example.invalid/adhan.mp3"}).json()
    assert r["warning"] and r["id"]  # warns but still saved
    assert any(l["id"] == r["id"] for l in c.get("/api/links").json())
    s = c.get("/api/settings").json()
    s["prayers"]["isha"]["media"] = "link:" + r["id"]
    c.put("/api/settings", json=s)
    main.CAST_CALLS.clear()
    assert c.post("/api/test", json={"prayer": "isha"}).json()["ok"]
    assert c.post("/api/test", json={"media": "link:" + r["id"]}).json()["ok"]
    time.sleep(0.3)
    assert [x["url"] for x in main.CAST_CALLS] == ["https://example.invalid/adhan.mp3"] * 2
    assert c.delete("/api/links/" + r["id"]).status_code == 200
    assert c.get("/api/settings").json()["prayers"]["isha"]["media"] == ""
    assert c.delete("/api/links/" + r["id"]).status_code == 404


def test_check_link_real_server():
    # live server from the TestClient isn't on a socket; use a tiny local http server
    import http.server, threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    assert main.check_link(f"http://127.0.0.1:{port}/does-not-exist.mp3").startswith("URL returned HTTP 404")
    assert "could not reach" in main.check_link("http://127.0.0.1:1/x.mp3")
    srv.shutdown()


def test_reminder_settings_backcompat_and_validation():
    import json
    c = client()
    s = json.loads(main.SETTINGS_FILE.read_text()); s.pop("reminders", None)
    main.SETTINGS_FILE.write_text(json.dumps(s))
    r = c.get("/api/settings").json()["reminders"]
    assert r["enabled"] is False and r["minutes"] == 15 and r["sound"] == "tts"
    assert set(r["prayers"]) == set(main.PRAYERS) and r["prayers"]["asr"] == {"enabled": True, "minutes": None}
    s = c.get("/api/settings").json()
    s["reminders"]["minutes"] = 0
    assert c.put("/api/settings", json=s).status_code == 400
    s["reminders"].update(minutes=10, enabled=True)
    s["reminders"]["prayers"]["fajr"]["minutes"] = 121
    assert c.put("/api/settings", json=s).status_code == 400
    s["reminders"]["prayers"]["fajr"]["minutes"] = 30
    out = c.put("/api/settings", json=s).json()["reminders"]
    assert out["prayers"]["fajr"]["minutes"] == 30 and out["minutes"] == 10
    t = c.get("/api/times").json()
    assert datetime.fromisoformat(t["times"]["fajr"]) - datetime.fromisoformat(t["reminders"]["fajr"]) == timedelta(minutes=30)
    assert datetime.fromisoformat(t["times"]["isha"]) - datetime.fromisoformat(t["reminders"]["isha"]) == timedelta(minutes=10)
    s["reminders"]["enabled"] = False
    c.put("/api/settings", json=s)
    assert all(v is None for v in c.get("/api/times").json()["reminders"].values())


def test_reminder_scheduler_tts_and_library(monkeypatch):
    c = client()
    monkeypatch.setattr(main, "fetch_tts", lambda text, lang="en": MP3)
    c.post("/api/media", files={"file": ("main.mp3", MP3)})
    c.post("/api/media", files={"file": ("chime.mp3", MP3)})
    s = c.get("/api/settings").json()
    s.update(speakers=[{"name": "Kitchen", "host": "192.0.2.5"}], default_media="main.mp3", host_ip_override="192.168.1.20")
    for p in main.PRAYERS:
        s["prayers"][p]["enabled"] = True
    s["reminders"] = {"enabled": True, "minutes": 15, "sound": "tts", "volume": 0.2, "language": "en",
                      "prayers": {p: {"enabled": p != "asr", "minutes": 5 if p == "isha" else None} for p in main.PRAYERS}}
    assert c.put("/api/settings", json=s).status_code == 200
    t = main.compute_times(date.today())
    main.CAST_CALLS.clear()
    sch = main.Scheduler(interval=5)
    # dhuhr: reminder fires 15 min before, once; adhan still fires at its time
    r = t["dhuhr"] - timedelta(minutes=15)
    sch.last = r - timedelta(seconds=3); sch.tick(now=r + timedelta(seconds=2)); sch.tick(now=r + timedelta(seconds=7))
    sch.last = t["dhuhr"] - timedelta(seconds=3); sch.tick(now=t["dhuhr"] + timedelta(seconds=2))
    # asr reminder disabled; isha uses 5 min override
    ra = t["asr"] - timedelta(minutes=15)
    sch.last = ra - timedelta(seconds=3); sch.tick(now=ra + timedelta(seconds=2))
    ri = t["isha"] - timedelta(minutes=5)
    sch.last = ri - timedelta(seconds=3); sch.tick(now=ri + timedelta(seconds=2))
    time.sleep(0.5)
    calls = main.CAST_CALLS
    rem = [x for x in calls if x["title"] == "Adhan reminder"]
    assert len(rem) == 2 and all(x["volume"] == 0.2 for x in rem)
    assert all(x["url"].startswith("http://192.168.1.20:8080/tts/") for x in rem)
    assert c.get(rem[0]["url"].split("8080")[1]).content == MP3
    assert [x["url"] for x in calls if x["title"] == "Adhan"] == ["http://192.168.1.20:8080/media/main.mp3"]
    msgs = [e["msg"] for e in c.get("/api/log").json() if e["kind"] == "reminder"]
    assert any("Dhuhr adhan in 15 minutes" in m for m in msgs) and any("Isha adhan in 5 minutes" in m for m in msgs)
    # stale reminder (service started late / clock jump) is skipped
    main.CAST_CALLS.clear()
    sch2 = main.Scheduler(interval=5)
    sch2.last = r - timedelta(minutes=30); sch2.tick(now=r + timedelta(minutes=1))
    time.sleep(0.2)
    assert not main.CAST_CALLS
    # library sound + test endpoint; deleting the file falls back to tts
    main.CAST_CALLS.clear()
    out = c.post("/api/reminder/test", json={"prayer": "maghrib", "minutes": 10, "sound": "chime.mp3"}).json()
    assert out["ok"] and out["text"] == "Maghrib adhan in 10 minutes"
    time.sleep(0.3)
    assert main.CAST_CALLS[-1]["url"].endswith("/media/chime.mp3")
    s = c.get("/api/settings").json(); s["reminders"]["sound"] = "chime.mp3"; c.put("/api/settings", json=s)
    c.delete("/api/media/chime.mp3")
    assert c.get("/api/settings").json()["reminders"]["sound"] == "tts"
    assert c.post("/api/reminder/test", json={"prayer": "nope"}).status_code == 400


def test_reminder_tts_fallback(monkeypatch):
    c = client()
    def boom(text, lang="en"):
        raise RuntimeError("offline")
    monkeypatch.setattr(main, "fetch_tts", boom)
    main.CAST_CALLS.clear()
    assert c.post("/api/reminder/test", json={"prayer": "fajr", "minutes": 7, "sound": "tts"}).json()["ok"]
    time.sleep(0.3)
    assert main.CAST_CALLS[-1]["url"].startswith("https://translate.google.com/translate_tts")
    assert "Fajr%20adhan%20in%207%20minutes" in main.CAST_CALLS[-1]["url"]
    # missing library item also falls back to spoken
    assert c.post("/api/reminder/test", json={"prayer": "fajr", "minutes": 1, "sound": "gone.mp3"}).json()["ok"]
    assert "Fajr%20adhan%20in%201%20minute" in main.CAST_CALLS[-1]["url"] if (time.sleep(0.3) or True) else 0
