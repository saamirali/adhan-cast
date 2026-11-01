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
