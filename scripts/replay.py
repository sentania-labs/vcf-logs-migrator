#!/usr/bin/env python3
"""Replay events from one Operations for Logs instance into another, keeping timestamps.

Source: GET /api/v1/events/timestamp/>=A/timestamp/<B?limit=20000 on :9543 (session Bearer).
Target: POST /api/v2/events/ingest/<agentId> on :9543 with a Bearer session and the agent wire format
        {"timestampValidation": false, "messages": [...]}  -- PROVEN on 9.0.2.0200 to keep the original timestamps.
        (The public schema {"events":[...]} on v1 or v2 clamps every timestamp to receipt time, Bearer or not.)

Usage: logs_replay.py --src HOST --src-user U --dst HOST --dst-user U --start ISO --end ISO
       [--window-min 60] [--agent-id replay] [--extra-field migrated_from=src] [--dry-run]
Passwords from SRC_PASS / DST_PASS env.
"""
import argparse, json, os, sys, time, datetime as dt
import requests, urllib3
urllib3.disable_warnings()
MAX_EVENTS = 20000          # server max per query
MAX_POST_BYTES = 3_500_000  # stay under the 4 MB ingest cap
STATIC_SKIP = {"source", "hostname", "event_type", "timestamp", "text"}  # fields the target derives itself

def session(host, user, pw, provider="Local"):
    r = requests.post(f"https://{host}:9543/api/v1/sessions", json={"username": user, "password": pw, "provider": provider}, verify=False, timeout=30)
    r.raise_for_status()
    return r.json()["sessionId"]

def query(host, tok, a_ms, b_ms, limit=MAX_EVENTS):
    url = f"https://{host}:9543/api/v1/events/timestamp/%3E%3D{a_ms}/timestamp/%3C{b_ms}"
    r = requests.get(url, params={"limit": limit, "timeout": 120, "order-by-direction": "ASC"}, headers={"Authorization": f"Bearer {tok}"}, verify=False, timeout=180)
    r.raise_for_status()
    j = r.json()
    return j.get("events", []), j.get("complete", True)

def to_ingest(ev, extra):
    fields = [{"name": f["name"], "content": str(f["content"])} for f in ev.get("fields", []) if f.get("name") not in STATIC_SKIP and f.get("content") not in (None, "")]
    # keep the original hostname as a normal field so it survives (the target would stamp its own 'source')
    for f in ev.get("fields", []):
        if f.get("name") == "hostname" and f.get("content"):
            fields.append({"name": "hostname", "content": str(f["content"])})
    fields += [{"name": k, "content": v} for k, v in extra.items()]
    return {"text": ev["text"], "timestamp": int(ev["timestamp"]), "fields": fields}

def session_v2(host, user, pw, provider="Local"):
    r = requests.post(f"https://{host}:9543/api/v2/sessions", json={"username": user, "password": pw, "provider": provider}, verify=False, timeout=30)
    r.raise_for_status()
    return r.json()["sessionId"]

def post_batches(host, tok, agent, events, dry):
    sent = 0; batch = []; size = 2
    def flush():
        nonlocal batch, size, sent
        if not batch: return
        if not dry:
            for attempt in range(6):
                msgs = [dict(e, id=i + 1) for i, e in enumerate(batch)]
                r = requests.post(f"https://{host}:9543/api/v2/events/ingest/{agent}", json={"timestampValidation": False, "messages": msgs}, headers={"Authorization": f"Bearer {tok}"}, verify=False, timeout=120)
                if r.status_code == 503:
                    time.sleep(int(r.headers.get("Retry-After", "5"))); continue
                r.raise_for_status(); break
            else:
                raise SystemExit("ingest kept returning 503")
        sent += len(batch); batch = []; size = 2
    for e in events:
        s = len(json.dumps(e)) + 1
        if size + s > MAX_POST_BYTES: flush()
        batch.append(e); size += s
    flush()
    return sent

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True); ap.add_argument("--src-user", default="admin")
    ap.add_argument("--dst", required=True); ap.add_argument("--dst-user", default="admin")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--window-min", type=int, default=60); ap.add_argument("--agent-id", default="replay")
    ap.add_argument("--extra-field", action="append", default=[]); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", help="json file to resume from")
    ap.add_argument("--export-dir", help="write one Importer-ready file per window instead of posting to --dst (timestamps survive only through the Importer)")
    a = ap.parse_args()
    extra = dict(x.split("=", 1) for x in a.extra_field)
    st = session(a.src, a.src_user, os.environ["SRC_PASS"])
    dt_ = None if (a.dry_run or a.export_dir) else session_v2(a.dst, a.dst_user, os.environ["DST_PASS"])
    if a.export_dir: os.makedirs(a.export_dir, exist_ok=True)
    t0 = int(dt.datetime.fromisoformat(a.start).timestamp() * 1000)
    t1 = int(dt.datetime.fromisoformat(a.end).timestamp() * 1000)
    if a.state and os.path.exists(a.state):
        t0 = max(t0, json.load(open(a.state))["next_ms"])
    win = a.window_min * 60_000
    total = 0
    while t0 < t1:
        lo, hi = t0, min(t0 + win, t1)
        evs, complete = query(a.src, st, lo, hi)
        if (len(evs) >= MAX_EVENTS or not complete) and hi - lo > 1000:
            win = max(1000, win // 2)      # window too dense or the source timed out: halve and retry
            continue
        if not complete:
            raise SystemExit(f"source query still incomplete at a {hi-lo} ms window; source too slow, retry later")
        if a.export_dir and evs:
            # one line per event: ISO-8601 UTC timestamp, original hostname, original text (newlines folded)
            fn = os.path.join(a.export_dir, f"{dt.datetime.fromtimestamp(lo/1000, dt.UTC):%Y%m%dT%H%M%SZ}.log")
            with open(fn, "a") as fh:
                for e in evs:
                    hn = next((f["content"] for f in e.get("fields", []) if f.get("name") == "hostname"), "unknown")
                    fh.write(f"{dt.datetime.fromtimestamp(e['timestamp']/1000, dt.UTC):%Y-%m-%dT%H:%M:%S.%f}"[:-3] + f"Z {hn} " + e["text"].replace("\n", " ") + "\n")
            n = len(evs)
        else:
            n = post_batches(a.dst, dt_, a.agent_id, [to_ingest(e, extra) for e in evs], a.dry_run) if evs else 0
        total += n
        print(f"{dt.datetime.fromtimestamp(lo/1000):%Y-%m-%d %H:%M} .. {dt.datetime.fromtimestamp(hi/1000):%H:%M}  src={len(evs):6d} sent={n:6d} total={total}", flush=True)
        t0 = hi
        if a.state: json.dump({"next_ms": t0}, open(a.state, "w"))
        if len(evs) < MAX_EVENTS // 4 and win < a.window_min * 60_000:
            win = min(a.window_min * 60_000, win * 2)   # grow back
    print("done", total)

if __name__ == "__main__":
    main()
