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


def _after_setup(c, **after):
    c.post("/api/media", files={"file": ("main.mp3", MP3)})
    c.post("/api/media", files={"file": ("dua.mp3", MP3)})
    s = c.get("/api/settings").json()
    s.update(speakers=[{"name": "Kitchen", "host": "192.0.2.5"}], default_media="main.mp3", host_ip_override="192.168.1.20")
    s["reminders"]["enabled"] = False
    for p in main.PRAYERS:
        s["prayers"][p] = {"enabled": True, "media": "", "volume": None}
    s["after_adhan"] = {"enabled": True, "media": "dua.mp3", "gap": 0, "volume": 0.25,
                        "prayers": {p: True for p in main.PRAYERS}, **after}
    assert c.put("/api/settings", json=s).status_code == 200
    main.CAST_CALLS.clear()


def test_after_adhan_backcompat_and_scheduler():
    import json
    c = client()
    s = json.loads(main.SETTINGS_FILE.read_text()); s.pop("after_adhan", None)
    main.SETTINGS_FILE.write_text(json.dumps(s))
    a = c.get("/api/settings").json()["after_adhan"]
    assert a["enabled"] is False and a["gap"] == 2 and a["volume"] is None and all(a["prayers"].values())
    _after_setup(c, prayers={**{p: True for p in main.PRAYERS}, "asr": False})
    t = main.compute_times(date.today())
    sch = main.Scheduler(interval=5)
    for p in ("dhuhr", "asr"):
        sch.last = t[p] - timedelta(seconds=3); sch.tick(now=t[p] + timedelta(seconds=2))
    time.sleep(0.5)
    seq = [(x["title"], x["url"].rsplit("/", 1)[-1], x["volume"]) for x in main.CAST_CALLS]
    assert seq.count(("After adhan", "dua.mp3", 0.25)) == 1  # dhuhr only, asr toggled off
    assert seq.index(("After adhan", "dua.mp3", 0.25)) > 0 and seq[0][0] == "Adhan"
    assert len([x for x in seq if x[0] == "Adhan"]) == 2
    assert any("after adhan" in e["msg"] for e in c.get("/api/log").json())


def test_after_adhan_skipped_when_stopped():
    c = client()
    _after_setup(c)
    old = main.MOCK_ADHAN_SECONDS
    main.MOCK_ADHAN_SECONDS = 0.4
    try:
        assert c.post("/api/test", json={"prayer": "isha"}).json()["ok"]
        time.sleep(0.1)
        c.post("/api/stop")
        time.sleep(0.6)
    finally:
        main.MOCK_ADHAN_SECONDS = old
    assert [x["title"] for x in main.CAST_CALLS] == ["Adhan"]
    assert any("skipped" in e["msg"] for e in c.get("/api/log").json())


def test_after_adhan_test_endpoint_and_delete():
    c = client()
    _after_setup(c)
    assert c.post("/api/after/test", json={}).json()["ok"]
    time.sleep(0.3)
    assert [(x["title"], x["url"]) for x in main.CAST_CALLS] == [("After adhan", "http://192.168.1.20:8080/media/dua.mp3")]
    c.delete("/api/media/dua.mp3")
    a = c.get("/api/settings").json()["after_adhan"]
    assert a["media"] == "" and a["enabled"] is False
    assert not c.post("/api/after/test", json={}).json()["ok"]


def test_wait_adhan_done_real_logic(monkeypatch):
    from types import SimpleNamespace as NS
    url = "http://h/a.mp3"
    states = iter([NS(player_state="PLAYING", idle_reason=None, content_id=url, duration=100)] * 2 +
                  [NS(player_state="IDLE", idle_reason="FINISHED", content_id=url, duration=100)] * 5)
    class MC:
        @property
        def status(self): return next(states)
    assert main.wait_adhan_done(MC(), url, main.STOP_GEN[0], poll=0) == "finished"
    states = iter([NS(player_state="PLAYING", idle_reason=None, content_id=url, duration=100)] * 2 +
                  [NS(player_state="IDLE", idle_reason="CANCELLED", content_id=url, duration=100)] * 5)
    assert main.wait_adhan_done(MC(), url, main.STOP_GEN[0], poll=0) == "stopped"
    states = iter([NS(player_state="PLAYING", idle_reason=None, content_id=url, duration=None)] * 10**6)
    monkeypatch.setattr(main, "AFTER_MAX_WAIT", 0.05)
    assert main.wait_adhan_done(MC(), url, main.STOP_GEN[0], poll=0.01) == "timeout"
    states = iter([NS(player_state="PLAYING", idle_reason=None, content_id=url, duration=100)] * 100)
    assert main.wait_adhan_done(MC(), url, main.STOP_GEN[0] - 1, poll=0) == "stopped"


def _sched_setup(c):
    c.post("/api/media", files={"file": ("main.mp3", MP3)})
    c.post("/api/media", files={"file": ("kahf.mp3", MP3)})
    s = c.get("/api/settings").json()
    s.update(speakers=[{"name": "Kitchen", "host": "192.0.2.5"}], default_media="main.mp3", host_ip_override="192.168.1.20")
    s["reminders"]["enabled"] = False
    s["after_adhan"]["enabled"] = False
    c.put("/api/settings", json=s)
    for sc in c.get("/api/schedules").json():
        c.delete("/api/schedules/" + sc["id"])
    main.BUSY.clear(); main.CAST_CALLS.clear()


def test_schedule_crud_validation_and_next_run():
    c = client()
    _sched_setup(c)
    assert c.post("/api/schedules", json={"name": "x", "media": "kahf.mp3", "days": []}).status_code == 400
    assert c.post("/api/schedules", json={"name": "x", "media": "kahf.mp3", "days": [4], "time": "25:00"}).status_code == 400
    assert c.post("/api/schedules", json={"name": "x", "media": "", "days": [4]}).status_code == 400
    r = c.post("/api/schedules", json={"name": "Al-Kahf", "media": "kahf.mp3", "days": [4], "mode": "time", "time": "09:00"}).json()
    nr = datetime.fromisoformat(r["next_run"])
    assert nr.weekday() == 4 and (nr.hour, nr.minute) == (9, 0) and r["enabled"]
    r2 = c.put("/api/schedules/" + r["id"], json={"mode": "prayer", "prayer": "maghrib", "offset": -10, "days": [0, 1, 2, 3, 4, 5, 6]}).json()
    assert r2["id"] == r["id"] and r2["name"] == "Al-Kahf" and r2["mode"] == "prayer"
    # settings PUT never clobbers schedules
    s = c.get("/api/settings").json(); s["schedules"] = []
    c.put("/api/settings", json=s)
    assert len(c.get("/api/schedules").json()) == 1
    assert c.get("/api/times").json()["next_schedule"]["name"] == "Al-Kahf"
    c.delete("/api/media/kahf.mp3")
    assert c.get("/api/schedules").json()[0]["enabled"] is False
    assert c.delete("/api/schedules/" + r["id"]).status_code == 200
    assert c.delete("/api/schedules/" + r["id"]).status_code == 404


def test_schedule_day_of_week_and_prayer_relative():
    s = main.load_settings()
    tz = ZoneInfo(s["timezone"])
    fri, sat = date(2026, 11, 6), date(2026, 11, 7)
    sc = main.normalize_schedule({"name": "Kahf", "media": "k.mp3", "days": [4], "time": "09:00"})
    assert main.schedule_time(sc, fri, s) == datetime(2026, 11, 6, 9, 0, tzinfo=tz)
    assert main.schedule_time(sc, sat, s) is None
    assert main.next_run(sc, datetime(2026, 11, 6, 9, 30, tzinfo=tz), s) == datetime(2026, 11, 13, 9, 0, tzinfo=tz)
    rel = main.normalize_schedule({"name": "Mulk", "media": "k.mp3", "days": [5], "mode": "prayer", "prayer": "isha", "offset": 10})
    assert main.schedule_time(rel, sat, s) == main.compute_times(sat, s)["isha"] + timedelta(minutes=10)
    before = main.normalize_schedule({"name": "B", "media": "k.mp3", "days": [5], "mode": "prayer", "prayer": "maghrib", "offset": -15})
    assert main.schedule_time(before, sat, s) == main.compute_times(sat, s)["maghrib"] - timedelta(minutes=15)


def test_schedule_fires_and_skips_missed():
    c = client()
    _sched_setup(c)
    today = date.today()
    r = c.post("/api/schedules", json={"name": "Kahf", "media": "kahf.mp3", "days": [today.weekday()], "time": "09:00", "volume": 0.4}).json()
    tz = ZoneInfo(main.load_settings()["timezone"])
    t = datetime(today.year, today.month, today.day, 9, 0, tzinfo=tz)
    sch = main.Scheduler(interval=5)
    sch.last = t - timedelta(seconds=3); sch.tick(now=t + timedelta(seconds=2)); sch.tick(now=t + timedelta(seconds=7))
    time.sleep(0.3)
    calls = [x for x in main.CAST_CALLS if x["title"] == "Scheduled"]
    assert len(calls) == 1 and calls[0]["url"].endswith("/media/kahf.mp3") and calls[0]["volume"] == 0.4
    main.CAST_CALLS.clear(); main.BUSY.clear()
    sch2 = main.Scheduler(interval=5)
    sch2.last = t - timedelta(minutes=30); sch2.tick(now=t + timedelta(minutes=1))  # clock jump: missed
    time.sleep(0.2)
    assert not main.CAST_CALLS
    # test endpoint
    assert c.post(f"/api/schedules/{r['id']}/test").json()["ok"]


def test_schedule_conflict_with_adhan():
    c = client()
    _sched_setup(c)
    today = date.today()
    tz = ZoneInfo(main.load_settings()["timezone"])
    t = main.compute_times(today)["asr"]
    # schedule at exactly the adhan time: adhan fires first, schedule is delayed while adhan plays
    c.post("/api/schedules", json={"name": "Kahf", "media": "kahf.mp3", "days": [today.weekday()], "mode": "prayer", "prayer": "asr", "offset": 0})
    sch = main.Scheduler(interval=5)
    sch.last = t - timedelta(seconds=3); sch.tick(now=t + timedelta(seconds=2))
    time.sleep(0.2)
    assert [x["title"] for x in main.CAST_CALLS] == ["Adhan"]
    assert any("delayed" in e["msg"] for e in c.get("/api/log").json())
    # once the adhan is over, the delayed schedule plays on the next tick
    main.BUSY["Kitchen"]["until"] = time.monotonic() - 1
    sch.tick(now=t + timedelta(seconds=60))
    time.sleep(0.2)
    assert [x["title"] for x in main.CAST_CALLS] == ["Adhan", "Scheduled"]
    # adhan preempts a playing scheduled item
    main.mark_busy("Kitchen", "Scheduled", "Kahf")
    main.play(prayer="isha")
    time.sleep(0.2)
    assert any("takes priority" in e["msg"] for e in c.get("/api/log").json())
    # a schedule blocked for more than 10 min is skipped
    main.BUSY.clear(); main.CAST_CALLS.clear()
    main.mark_busy("Kitchen", "Adhan")
    sch3 = main.Scheduler(interval=5)
    sch3.pending = {c.get("/api/schedules").json()[0]["id"]: (t, True)}
    sch3.last = t + timedelta(minutes=11) - timedelta(seconds=5)
    sch3.tick(now=t + timedelta(minutes=11))
    assert not sch3.pending and not main.CAST_CALLS
