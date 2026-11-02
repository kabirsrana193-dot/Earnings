"""
F&O Results Timing - web app (Flask)

Shows every company reporting results on a given day, separates F&O stocks
(those with an option chain), and lists the exchange release time of each F&O
stock's last 8 quarterly results (pre-market / market hours / post-market).

Run locally:   pip install -r requirements.txt && python app.py
Deploy:        Render / Railway / etc.  Start command:  gunicorn app:app
"""

import io
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
TIME_KEYS = ["exchdisstime", "broadcast_date", "broadcastDate", "broadCastDate",
             "filingDate", "filing_date"]
QUARTER_KEYS = ["toDate", "to_date", "qe_Date", "quarterEnded"]
DATE_FORMATS = [
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%b-%Y",
]

# ---------------- NSE access (one shared, rate-limited session) ----------------
_lock = threading.Lock()
_session = {"s": None, "t": 0}
_cache = {}


def cache_get(key, ttl):
    item = _cache.get(key)
    if item and time.time() - item[0] < ttl:
        return item[1]
    return None


def cache_set(key, val):
    _cache[key] = (time.time(), val)
    return val


def nse_session(force=False):
    if force or _session["s"] is None or time.time() - _session["t"] > 600:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(BASE, timeout=15)
        except requests.RequestException:
            pass
        _session["s"], _session["t"] = s, time.time()
    return _session["s"]


def nse_json(path, params=None, retries=3):
    with _lock:
        for attempt in range(retries):
            try:
                r = nse_session(force=attempt > 0).get(BASE + path, params=params, timeout=20)
                if r.status_code == 200 and r.text.strip():
                    return r.json()
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1.5 * (attempt + 1))
        return None


# ---------------- Data logic ----------------
def parse_ts(val):
    if not val:
        return None
    val = str(val).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def bucket(dt):
    if dt is None or (dt.hour == 0 and dt.minute == 0):
        return "unknown"
    t = (dt.hour, dt.minute)
    if t < MARKET_OPEN:
        return "pre-market"
    if t <= MARKET_CLOSE:
        return "market-hours"
    return "post-market"


def fno_symbols():
    cached = cache_get("fno", 6 * 3600)
    if cached:
        return cached
    try:
        r = requests.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
                         headers=HEADERS, timeout=20)
        if r.status_code == 200:
            df = pd.read_csv(io.StringIO(r.text), skipinitialspace=True)
            col = [c for c in df.columns if "SYMBOL" in c.upper()][0]
            syms = {str(x).strip().upper() for x in df[col].dropna()}
            if len(syms) > 100:
                return cache_set("fno", syms)
    except Exception:
        pass
    data = nse_json("/api/underlying-information")
    if data:
        lst = data.get("data", {}).get("UnderlyingList", [])
        syms = {x["symbol"].upper() for x in lst}
        if syms:
            return cache_set("fno", syms)
    return None


def todays_results(date_str):
    key = "cal:" + date_str
    cached = cache_get(key, 600)
    if cached is not None:
        return cached
    params = {"index": "equities", "from_date": date_str, "to_date": date_str}
    data = nse_json("/api/corporate-board-meetings", params) or \
        nse_json("/api/event-calendar", params)
    if not data:
        return None
    if isinstance(data, dict):
        data = data.get("data", [])
    out = {}
    for row in data:
        sym = row.get("bm_symbol") or row.get("symbol")
        name = row.get("sm_name") or row.get("company") or sym
        purpose = " ".join(str(row.get(k, "")) for k in
                           ("bm_purpose", "purpose", "bm_desc")).lower()
        if sym and "result" in purpose:
            out[sym.strip().upper()] = name
    return cache_set(key, out)


def history(symbol, n):
    key = f"hist:{symbol}:{n}"
    cached = cache_get(key, 6 * 3600)
    if cached is not None:
        return cached
    data = nse_json("/api/corporates-financial-results",
                    {"index": "equities", "symbol": symbol, "period": "Quarterly"})
    if not data:
        return None
    if isinstance(data, dict):
        data = data.get("data", [])
    per_q = {}
    for row in data:
        q = next((row.get(k) for k in QUARTER_KEYS if row.get(k)), None)
        ts = next((parse_ts(row.get(k)) for k in TIME_KEYS if parse_ts(row.get(k))), None)
        if q and ts and (q not in per_q or ts < per_q[q]):
            per_q[q] = ts
    quarters = sorted(per_q, key=lambda q: parse_ts(q) or datetime.min, reverse=True)[:n]
    out = [{"quarter_end": q, "released": per_q[q].strftime("%d-%b-%Y %H:%M:%S"),
            "time": per_q[q].strftime("%H:%M"), "bucket": bucket(per_q[q])}
           for q in quarters]
    return cache_set(key, out)


# ---------------- API routes ----------------
@app.route("/api/today")
def api_today():
    date_str = request.args.get("date") or datetime.now(IST).strftime("%d-%m-%Y")
    results = todays_results(date_str)
    if results is None:
        return jsonify(error="Could not reach NSE's results calendar (it may be blocking this server). Try again in a minute."), 502
    fno = fno_symbols()
    if fno is None:
        return jsonify(error="Could not fetch the F&O stock list from NSE."), 502
    return jsonify(
        date=date_str,
        fno=[{"symbol": s, "name": n} for s, n in sorted(results.items()) if s in fno],
        other=[{"symbol": s, "name": n} for s, n in sorted(results.items()) if s not in fno],
    )


@app.route("/api/history/<symbol>")
def api_history(symbol):
    n = min(max(int(request.args.get("q", 8)), 1), 12)
    hist = history(symbol.upper(), n)
    if hist is None:
        return jsonify(error="No history returned"), 502
    return jsonify(symbol=symbol.upper(), history=hist)


@app.route("/health")
def health():
    return "ok"


# ---------------- Frontend ----------------
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F&O Results Timing</title>
<style>
:root{--bg:#fafaf8;--card:#fff;--text:#1c1c1a;--muted:#6b6b66;--line:#e4e4de;
--pre:#1f6feb;--mkt:#1a7f37;--post:#9a6700;--unk:#6b6b66}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--text:#ecece8;--muted:#9a9a93;--line:#34342f;
--pre:#58a6ff;--mkt:#3fb950;--post:#d29922;--unk:#9a9a93}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 18px}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:18px}
input,select,button{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text)}
button{cursor:pointer;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.card h2{font-size:16px;margin:0 0 2px}
.sym{font-weight:700}
.muted{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:14px}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
.pre-market{color:var(--pre)}.market-hours{color:var(--mkt)}.post-market{color:var(--post)}.unknown{color:var(--unk)}
.sum{margin-top:8px;font-size:13px}
.err{color:#cf222e}
.chips span{display:inline-block;margin:2px 6px 2px 0;padding:1px 8px;border:1px solid var(--line);border-radius:99px;font-size:13px}
</style>
</head>
<body>
<main>
<h1>F&amp;O results timing</h1>
<p class="sub">Companies reporting results on the chosen day. F&amp;O stocks (with an option chain) are listed separately, with the exchange release time of their last quarters.</p>
<div class="bar">
  <input type="date" id="date">
  <select id="q"><option>4</option><option>6</option><option selected>8</option><option>12</option></select>
  <span class="muted">quarters</span>
  <button id="go">Load</button>
  <span id="status" class="muted"></span>
</div>
<div id="fno"></div>
<div class="card" id="otherCard" hidden>
  <h2>Non-F&amp;O (no option chain)</h2>
  <div class="chips" id="other"></div>
</div>
<p class="muted">Times are exchange dissemination times (IST). Market hours = 09:15 to 15:30. Data comes from NSE and can occasionally be missing or delayed.</p>
</main>
<script>
const $ = id => document.getElementById(id);
const labels = {"pre-market":"Pre-market","market-hours":"Market hours","post-market":"Post-market","unknown":"Unknown"};
function todayISO(){
  const d = new Date(new Date().toLocaleString("en-US",{timeZone:"Asia/Kolkata"}));
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
}
function toNSE(iso){const [y,m,d]=iso.split("-");return d+"-"+m+"-"+y;}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

async function load(){
  $("fno").innerHTML=""; $("otherCard").hidden=true;
  $("status").textContent="Loading calendar…"; $("status").className="muted";
  const n = $("q").value;
  let data;
  try{
    const r = await fetch("/api/today?date="+toNSE($("date").value));
    data = await r.json();
    if(!r.ok) throw new Error(data.error||"Request failed");
  }catch(e){ $("status").textContent=e.message; $("status").className="err"; return; }

  $("status").textContent = data.fno.length+" F&O, "+data.other.length+" non-F&O";
  $("other").innerHTML = data.other.map(o=>"<span title='"+esc(o.name)+"'>"+esc(o.symbol)+"</span>").join("") || "<span class='muted'>None</span>";
  $("otherCard").hidden=false;

  if(!data.fno.length){ $("fno").innerHTML="<div class='card muted'>No F&O stocks reporting results on this date.</div>"; return; }

  // create placeholder cards, then fill them one by one (keeps requests gentle on NSE)
  for(const s of data.fno){
    const div=document.createElement("div"); div.className="card"; div.id="c-"+s.symbol;
    div.innerHTML="<h2><span class='sym'>"+esc(s.symbol)+"</span> <span class='muted'>"+esc(s.name)+"</span></h2><div class='muted'>Loading history…</div>";
    $("fno").appendChild(div);
  }
  for(const s of data.fno){
    const div=$("c-"+s.symbol);
    try{
      const r = await fetch("/api/history/"+encodeURIComponent(s.symbol)+"?q="+n);
      const h = await r.json();
      if(!r.ok) throw new Error(h.error||"No history");
      const counts={};
      h.history.forEach(x=>counts[x.bucket]=(counts[x.bucket]||0)+1);
      const sum=Object.entries(counts).map(([k,v])=>"<span class='badge "+k+"'>"+labels[k]+": "+v+"</span>").join(" ");
      const rows=h.history.map(x=>"<tr><td>"+esc(x.quarter_end)+"</td><td>"+esc(x.released)+"</td><td><span class='badge "+x.bucket+"'>"+labels[x.bucket]+"</span></td></tr>").join("");
      div.innerHTML="<h2><span class='sym'>"+esc(s.symbol)+"</span> <span class='muted'>"+esc(s.name)+"</span></h2>"
        +"<div class='sum'>"+sum+"</div>"
        +"<table><thead><tr><th>Quarter ended</th><th>Released (IST)</th><th>Timing</th></tr></thead><tbody>"+rows+"</tbody></table>";
    }catch(e){
      div.innerHTML="<h2><span class='sym'>"+esc(s.symbol)+"</span> <span class='muted'>"+esc(s.name)+"</span></h2><div class='err'>"+esc(e.message)+"</div>";
    }
  }
}
$("date").value=todayISO();
$("go").onclick=load;
load();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
