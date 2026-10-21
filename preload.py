#!/usr/bin/env python3
"""
Pre-load worldwide flight data into world_cache.json so SkyWatch starts instantly.
Run before starting the server:
    python3 preload.py && python3 app.py
"""
import sys
import time

from app import BD_TOKEN, fetch_world, save_cache, CACHE

if not BD_TOKEN:
    sys.exit("Missing env var BRIGHTDATA_API_TOKEN")

t0 = time.time()
print("Fetching worldwide aircraft…", flush=True)
flights, t, source = fetch_world()
save_cache(flights, t, source)
airborne = sum(1 for f in flights if not f["ground"])
print(f"Saved {len(flights):,} aircraft ({airborne:,} airborne) to {CACHE.name} "
      f"in {time.time() - t0:.1f}s via {source}")
