# SkyWatch

A live flight radar with an AI flight analyst. Built with Claude and Bright Data.

- Live aircraft over the map area (OpenSky ADS-B feed, fetched through Bright Data Web Unlocker), refreshed every 15s
- Click a plane, then **Brief me**: Claude searches and reads the live web through Bright Data and returns the airline, route, aircraft and status with sources
- Ask questions about the traffic on screen

## Run

1. `pip3 install anthropic requests`
2. Set keys:
   ```
   export ANTHROPIC_API_KEY=sk-ant-...
   export BRIGHTDATA_API_TOKEN=...
   export BRIGHTDATA_SERP_ZONE=<SERP API zone name>
   export BRIGHTDATA_UNLOCKER_ZONE=<Web Unlocker zone name>
   ```
3. `python3 preload.py` — caches the worldwide snapshot to `world_cache.json` (~30–60s)
4. `python3 app.py`, then open http://localhost:8000 — starts instantly from the cache, then updates live in the background
5. Backup if the live feed fails: `DEMO=1 python3 app.py` (simulated planes)
