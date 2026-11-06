#!/usr/bin/env python3
"""Per-day and per-month event counts on an Operations for Logs node (8.x or 9.x).
usage: LI_PASS=... [LI_OUT=file.json] count-events.py <host> [days-back, default 300] [field=value]
One aggregated-events query, timestamp >= now-days, 1-day bins, summed per month (UTC).
Traps: view=SIMPLE returns 0 bins; one query caps at 100 bins, so query 30-day windows."""
import json, os, ssl, sys, time, urllib.request, urllib.parse, collections, datetime as dt
ctx = ssl._create_unverified_context()
host = sys.argv[1]; days = int(sys.argv[2]) if len(sys.argv) > 2 else 300
flt = ""
if len(sys.argv) > 3:
    k, v = sys.argv[3].split("=", 1); flt = f"/{urllib.parse.quote(k)}/{urllib.parse.quote(v)}"
base = f"https://{host}:9543/api/v1"
def call(path, body=None, tok=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
    with urllib.request.urlopen(req, context=ctx, timeout=600) as r: return json.load(r)
tok = call("/sessions", {"username": "admin", "password": os.environ["LI_PASS"], "provider": "Local"})["sessionId"]
day = 86400000; now = int(time.time() * 1000); start = (now - days * day) // day * day
# chunked in 30-day windows: one query returns at most 100 bins (newest first)
daily = {}; complete = True; bins = []
for a in range(start, now + day, 30 * day):
    r = call(f"/aggregated-events/timestamp/%3E%3D{a}/timestamp/%3C{a + 30 * day}{flt}?bin-width={day}&timeout=580000", tok=tok)
    complete = complete and r.get("complete", True); bins += r.get("bins", [])
for b in bins:
    d = dt.datetime.fromtimestamp(b["minTimestamp"] / 1000, dt.timezone.utc).date().isoformat()
    daily[d] = daily.get(d, 0) + int(b.get("value", 0))
months = collections.OrderedDict()
for d in sorted(daily): months[d[:7]] = months.get(d[:7], 0) + daily[d]
for m, n in months.items(): print(f"{m} {n:>10,}")
nz = [d for d in sorted(daily) if daily[d]]
print(f"total   {sum(daily.values()):>10,}  days with data {len(nz)}  first {nz[0] if nz else '-'}  last {nz[-1] if nz else '-'}  complete={complete}")
if os.environ.get("LI_OUT"):
    json.dump({"host": host, "complete": complete, "daily": daily, "months": months,
               "total": sum(daily.values()), "at": dt.datetime.now().isoformat(timespec="seconds")},
              open(os.environ["LI_OUT"], "w"), indent=1)
