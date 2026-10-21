#!/usr/bin/env python3
"""
SkyWatch — live flight radar + AI flight analyst.
  • Live aircraft positions: OpenSky ADS-B feed, fetched through Bright Data Web Unlocker
  • Click a plane: Claude + Bright Data (SERP API + Web Unlocker) identify airline, route, aircraft, status
  • Ask anything about the traffic on screen

Setup:
  pip install anthropic requests
  export ANTHROPIC_API_KEY=...
  export BRIGHTDATA_API_TOKEN=...
  export BRIGHTDATA_SERP_ZONE=<SERP API zone name>         # default: serp_api1
  export BRIGHTDATA_UNLOCKER_ZONE=<Web Unlocker zone name>  # default: web_unlocker1
  export DEMO=1   # optional: simulated planes if the live feed is down
Run:
  python app.py   -> open http://localhost:8000
"""
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
import os
import random
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic
import requests

BD_TOKEN = os.environ.get("BRIGHTDATA_API_TOKEN")
SERP_ZONE = os.environ.get("BRIGHTDATA_SERP_ZONE", "serp_api1")
UNLOCKER_ZONE = os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "web_unlocker1")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
PORT = int(os.environ.get("PORT", "8000"))
DEMO = os.environ.get("DEMO") == "1"
MAX_STEPS = 8
INDEX = Path(__file__).with_name("index.html")
OPENSKY = "https://opensky-network.org/api/states/all"


# ---------- Bright Data ----------

def bd_request(zone, url, timeout=(10, 40), **extra):
    r = requests.post(
        "https://api.brightdata.com/request",
        headers={"Authorization": f"Bearer {BD_TOKEN}"},
        json={"zone": zone, "url": url, "format": "raw", **extra},
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f"Bright Data {r.status_code}: {r.text[:300]}")
    return r.text


def web_search(query):
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query) + "&brd_json=1"
    raw = bd_request(SERP_ZONE, url)
    try:
        data = json.loads(raw)
        items = [{"title": o.get("title"), "url": o.get("link"), "snippet": o.get("description")}
                 for o in data.get("organic", [])[:6]]
        return json.dumps(items, indent=1) if items else raw[:4000]
    except json.JSONDecodeError:
        return raw[:4000]


def fetch_page(url):
    try:
        text = bd_request(UNLOCKER_ZONE, url, data_format="markdown")
    except RuntimeError:
        html = bd_request(UNLOCKER_ZONE, url)
        html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()[:8000]


# ---------- Live flights ----------

_cache = {}
_demo = []


def demo_flights(lamin, lomin, lamax, lomax):
    """Simulated traffic that moves between refreshes."""
    global _demo
    airlines = ["UAL", "SWA", "DAL", "AAL", "ASA", "JBU", "SKW", "FDX", "UPS", "BAW", "ANA", "QFA"]
    countries = {"BAW": "United Kingdom", "ANA": "Japan", "QFA": "Australia"}
    if not _demo:
        for i in range(70):
            a = random.choice(airlines)
            _demo.append({
                "id": f"a{i:05x}", "cs": f"{a}{random.randint(10, 2999)}",
                "country": countries.get(a, "United States"),
                "lat": random.uniform(36.8, 38.6), "lon": random.uniform(-123.4, -121.0),
                "alt": random.choice([random.uniform(300, 3000), random.uniform(3000, 12500)]),
                "spd": random.uniform(80, 260), "hdg": random.uniform(0, 360),
                "vr": random.choice([-8, -4, 0, 0, 0, 5, 10]), "ground": False,
            })
    for f in _demo:
        d = f["spd"] * 15 / 111_000
        f["lat"] += d * math.cos(math.radians(f["hdg"]))
        f["lon"] += d * math.sin(math.radians(f["hdg"])) / math.cos(math.radians(f["lat"]))
        f["hdg"] = (f["hdg"] + random.uniform(-3, 3)) % 360
        if not (36.5 < f["lat"] < 39 and -124 < f["lon"] < -120.5):
            f["hdg"] = (f["hdg"] + 180) % 360
    return [f for f in _demo if lamin <= f["lat"] <= lamax and lomin <= f["lon"] <= lomax], "demo"


WORLD_TTL = int(os.environ.get("WORLD_TTL", "30"))   # seconds between worldwide fetches
_world = {"fetched": 0, "t": 0, "flights": [], "source": "", "error": None, "busy": False}
_wlock = threading.Lock()


def _parse_states(data):
    flights = []
    for s in data.get("states") or []:
        if s[5] is None or s[6] is None:
            continue
        flights.append({
            "id": s[0], "cs": (s[1] or "").strip() or s[0].upper(), "country": s[2],
            "lon": s[5], "lat": s[6], "alt": s[7] if s[7] is not None else s[13],
            "ground": s[8], "spd": s[9], "hdg": s[10] or 0, "vr": s[11],
        })
    return flights


CACHE = Path(__file__).with_name("world_cache.json")


def fetch_world():
    """One worldwide snapshot -> (flights, data_time, source)."""
    try:
        data = json.loads(bd_request(UNLOCKER_ZONE, OPENSKY, timeout=(10, 120)))
        source = "OpenSky via Bright Data"
    except Exception as ex:
        print(f"  Bright Data world feed failed ({ex}); trying direct", flush=True)
        data, source = requests.get(OPENSKY, timeout=90).json(), "OpenSky direct"
    return _parse_states(data), data.get("time") or int(time.time()), source


def save_cache(flights, t, source):
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"t": t, "source": source, "flights": flights}, separators=(",", ":")))
    tmp.replace(CACHE)                     # atomic swap, never a half-written file


def load_cache():
    if not CACHE.exists():
        print("No world_cache.json — run `python3 preload.py` first for an instant start.", flush=True)
        return
    try:
        c = json.loads(CACHE.read_text())
        age = int(time.time() - c["t"])
        _world.update(t=c["t"], flights=c["flights"], source=f"cached snapshot ({age // 60}m old) · updating live…")
        print(f"Loaded {len(c['flights']):,} aircraft from world_cache.json ({age // 60} min old)", flush=True)
    except Exception as ex:
        print(f"Could not read world_cache.json: {ex}", flush=True)


def _refresh_world():
    try:
        flights, t, source = fetch_world()
        _world.update(t=t, flights=flights, source=source, error=None)
        print(f"  world refresh: {len(flights):,} aircraft ({source})", flush=True)
        save_cache(flights, t, source)
    except Exception as ex:
        _world["error"] = str(ex)
        print(f"  world refresh failed: {ex}", flush=True)
    finally:
        _world["fetched"] = time.time()
        _world["busy"] = False


def get_flights(lamin, lomin, lamax, lomax):
    if DEMO:
        fl, src = demo_flights(lamin, lomin, lamax, lomax)
        return {"flights": fl, "source": src, "time": int(time.time()), "total": len(_demo)}
    with _wlock:
        start = time.time() - _world["fetched"] > WORLD_TTL and not _world["busy"]
        if start:
            _world["busy"] = True
    if start:
        if not _world["flights"]:
            _refresh_world()                                   # first load: wait for it
        else:
            threading.Thread(target=_refresh_world, daemon=True).start()   # serve cache, refresh behind
    if not _world["flights"] and _world["error"]:
        raise RuntimeError(_world["error"])
    fl = [f for f in _world["flights"] if lamin <= f["lat"] <= lamax and lomin <= f["lon"] <= lomax]
    return {"flights": fl, "source": _world["source"], "time": _world["t"], "total": len(_world["flights"])}


# ---------- Claude agent ----------

class ClientGone(Exception):
    """The browser closed the connection (reload, tab closed); stop working on its request."""


HANDLERS = {"web_search": web_search, "fetch_page": fetch_page}
TOOLS = [
    {"name": "web_search",
     "description": "Search Google live via Bright Data. Returns top results with title, url, snippet.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "fetch_page",
     "description": "Fetch readable text of a web page via Bright Data Web Unlocker (handles blocks/CAPTCHAs).",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]

FLIGHT_SYSTEM = """You are an aviation analyst with live web access, briefing someone watching a live radar map.
You get one aircraft from live ADS-B data. Identify it:
- The callsign uses the ICAO airline code (UAL=United/UA, SWA=Southwest/WN, DAL=Delta/DL, AAL=American/AA,
  ASA=Alaska/AS, JBU=JetBlue/B6, SKW=SkyWest, FDX=FedEx, UPS=UPS, etc.). Search both forms, e.g. "UAL1234" and "UA1234 flight".
- Fetch 1-2 flight-tracker or airline pages to find today's route, aircraft type, and status.
- Max 4 tool calls total. Be fast.
Answer in short markdown, no preamble:
**Airline:** ...
**Route:** origin → destination (airport codes + cities)
**Aircraft:** ...
**Status:** ...
**Fun fact:** one line about the airline, route or aircraft
Then "Sources:" with 1-3 links. If you could not confirm something, say so plainly instead of guessing."""

CHAT_SYSTEM = """You answer questions about the live air traffic on the user's radar map.
You get a snapshot of visible aircraft (callsign, country, altitude ft, speed kt, heading deg, vertical rate ft/min).
Answer from the snapshot first. Use the web tools only when the question needs outside info (routes, airlines,
airports, aircraft, news) — max 4 tool calls, and issue independent searches in the SAME turn so they run in parallel.
The snapshot has no origin/destination. For route questions (e.g. "which flight is from Atlanta to SF"):
pick 2-3 likely candidates from the snapshot (airline that serves the route, e.g. DAL/UAL/FFT for ATL-SFO;
near SFO and descending or westbound), search their callsigns in parallel, and say which one matches.
If none can be confirmed, say so plainly and give the best candidates. Be concise and specific, use markdown, and list sources when you used the web."""


def run_agent(system, prompt, emit):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    t_start = time.time()
    def run_tool(b):
        arg = next(iter(b.input.values()), "")
        emit({"type": "step", "id": b.id, "tool": b.name, "arg": arg})
        t0 = time.time()
        try:
            out, ok = HANDLERS[b.name](**b.input), True
        except Exception as ex:
            out, ok = f"ERROR: {ex}", False
        emit({"type": "step_done", "id": b.id, "chars": len(out), "ok": ok,
              "ms": int((time.time() - t0) * 1000)})
        print(f"  {b.name}: {arg} -> {len(out)} chars in {time.time() - t0:.1f}s", flush=True)
        return {"type": "tool_result", "tool_use_id": b.id, "content": out}

    for _ in range(MAX_STEPS):
        emit({"type": "thinking"})
        resp = client.messages.create(model=MODEL, max_tokens=1500, system=system,
                                      tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            emit({"type": "answer", "text": text, "seconds": round(time.time() - t_start, 1)})
            return
        calls = [x for x in resp.content if x.type == "tool_use"]
        with ThreadPoolExecutor(max_workers=6) as pool:   # run searches/fetches in parallel
            results = list(pool.map(run_tool, calls))
        messages.append({"role": "user", "content": results})
    emit({"type": "error", "message": "Reached the step limit without an answer."})


def fmt_flight(f):
    alt = f"{f['alt'] * 3.281:,.0f} ft" if f.get("alt") is not None else "alt n/a"
    spd = f"{f['spd'] * 1.944:,.0f} kt" if f.get("spd") is not None else "spd n/a"
    vr = f"{f['vr'] * 196.85:+,.0f} ft/min" if f.get("vr") is not None else ""
    return (f"{f.get('cs')} | {f.get('country')} | {alt} | {spd} | hdg {f.get('hdg', 0):.0f} | {vr}"
            f" | {f.get('lat', 0):.3f},{f.get('lon', 0):.3f}")


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/flights":
            q = urllib.parse.parse_qs(u.query)
            try:
                box = [float(q[k][0]) for k in ("lamin", "lomin", "lamax", "lomax")]
                self._json(get_flights(*box))
            except Exception as ex:
                self._json({"error": str(ex)}, 502)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/ask":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            req = {}
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        lock = threading.Lock()
        gone = threading.Event()

        def emit(event):
            if gone.is_set():
                raise ClientGone()
            with lock:
                try:
                    self.wfile.write((json.dumps(event) + "\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    gone.set()
                    raise ClientGone()

        try:
            if req.get("flight"):
                f = req["flight"]
                print(f"Briefing: {f.get('cs')}", flush=True)
                run_agent(FLIGHT_SYSTEM, "Identify and brief me on this live aircraft:\n" + fmt_flight(f), emit)
            elif req.get("question", "").strip():
                snap = "\n".join(fmt_flight(f) for f in (req.get("flights") or [])[:300])
                print(f"Question: {req['question']}", flush=True)
                run_agent(CHAT_SYSTEM, f"Visible aircraft right now ({req.get('visible_count') or len(req.get('flights') or [])} in view; snapshot below):\n{snap}"
                                       f"\n\nQuestion: {req['question']}", emit)
            else:
                emit({"type": "error", "message": "Ask a question or pick a plane."})
        except ClientGone:
            print("  (browser closed the request — stopped)", flush=True)
        except Exception as ex:
            try:
                emit({"type": "error", "message": str(ex)})
            except ClientGone:
                pass

    def log_message(self, *args):
        pass


def main():
    missing = [k for k in ("ANTHROPIC_API_KEY", "BRIGHTDATA_API_TOKEN") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")
    if not DEMO:
        load_cache()
        _world["busy"] = True                                         # kick off first live refresh now,
        threading.Thread(target=_refresh_world, daemon=True).start()  # in the background
    print(f"SkyWatch running → http://localhost:{PORT}" + ("   (DEMO mode: simulated planes)" if DEMO else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
