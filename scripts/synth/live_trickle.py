#!/usr/bin/env python3
"""Simulated live traffic into a Logs node: every 2 s, 200 events stamped now, dataset=live-9 (change as needed), seq=N.
usage: LI_PASS=... live_trickle.py <host> <minutes>   prints the final sent count."""
import json, os, ssl, sys, time, urllib.request
ctx = ssl._create_unverified_context(); host, minutes = sys.argv[1], float(sys.argv[2])
def post(p, b, tok=None):
    r = urllib.request.Request(f"https://{host}:9543/api/v2{p}", data=json.dumps(b).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
    return json.load(urllib.request.urlopen(r, context=ctx, timeout=60)) if not tok else urllib.request.urlopen(r, context=ctx, timeout=60).status
tok = post("/sessions", {"username": "admin", "password": os.environ["LI_PASS"], "provider": "Local"})["sessionId"]
sent = fails = 0; end = time.time() + minutes * 60; seq = 0
while time.time() < end:
    now = int(time.time() * 1000)
    msgs = [{"id": i + 1, "text": f"live heartbeat seq={seq + i} from app-live01", "timestamp": now,
             "fields": [{"name": "dataset", "content": "live-9"}, {"name": "seq", "content": str(seq + i)}]} for i in range(200)]
    try: post("/events/ingest/live9", {"timestampValidation": False, "messages": msgs}, tok); sent += 200; seq += 200
    except Exception as e: fails += 1; print("fail", e, flush=True)
    time.sleep(2)
print(f"SENT {sent} fails {fails}", flush=True)
