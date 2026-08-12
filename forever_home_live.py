#!/usr/bin/env python3
"""
Forever Home OS — LIVE pipeline (GitHub Actions edition).

Pulls real active for-sale listings from RentCast (free tier), scores them,
computes drive-time-to-ocean tiers, and publishes into ./site/ :
    site/index.html                 -> your weekly report (served by GitHub Pages)
    site/Forever_Home_Live.kmz      -> the map (open in Google Earth)
    site/Forever_Home_Live.kml      -> raw KML
    site/ForeverHome_AutoRefresh.kml-> open ONCE in Google Earth; auto-updates weekly

The RentCast API key is read from the RENTCAST_API_KEY environment variable
(a GitHub Actions secret) — it is never stored in this file.
Stays inside RentCast's free 50-requests/month limit via REQUEST_BUDGET.
"""
import math, zipfile, html, datetime, sys, os, time

# ======================================================================
# CONFIG
# ======================================================================
STATES         = ["MD", "VA", "DE", "PA", "NC"]
PROPERTY_TYPES = ["Land", "Single Family"]   # lots + homes (redevelopment derived from homes)
MAX_PRICE      = 500000
PER_QUERY      = 500
REQUEST_BUDGET = 12                           # hard cap per run -> ~4 runs/month stays free
BASE           = "https://api.rentcast.io/v1/listings/sale"

API_KEY = os.environ.get("RENTCAST_API_KEY")
if not API_KEY and os.path.exists("rentcast_key.txt"):
    API_KEY = open("rentcast_key.txt").read().strip()
if not API_KEY:
    sys.exit("ERROR: set RENTCAST_API_KEY (GitHub secret) or a local rentcast_key.txt")

SITE = "site"
os.makedirs(SITE, exist_ok=True)
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "").rstrip("/")

try:
    import requests
except ImportError:
    sys.exit("Please run:  pip install requests")

# ======================================================================
# 1. INGEST — real RentCast pull, budget-capped
# ======================================================================
def ingest_live():
    out, used = [], 0
    for st in STATES:
        for pt in PROPERTY_TYPES:
            if used >= REQUEST_BUDGET:
                print(f"  ! request budget ({REQUEST_BUDGET}) reached — stopping to stay free")
                return out, used
            params = {"state": st, "propertyType": pt, "status": "Active",
                      "maxPrice": MAX_PRICE, "limit": PER_QUERY}
            try:
                r = requests.get(BASE, headers={"X-Api-Key": API_KEY},
                                 params=params, timeout=40)
                used += 1
                if r.status_code != 200:
                    print(f"  {st}/{pt}: HTTP {r.status_code} {r.text[:120]}")
                    continue
                rows = r.json()
                for x in rows:
                    out.append(normalize(x, st, pt))
                print(f"  {st}/{pt}: {len(rows)} listings  (request {used}/{REQUEST_BUDGET})")
            except Exception as e:
                print(f"  {st}/{pt}: ERROR {type(e).__name__} {e}")
            time.sleep(1)
    return out, used

def normalize(x, st, pt):
    price = x.get("price") or 0
    acres = round((x.get("lotSize") or 0) / 43560.0, 2)
    ptype = "Lot" if pt == "Land" else "Home"
    yb = x.get("yearBuilt") or 0
    if ptype == "Home" and price < 200000 and 0 < yb < 1970 and acres >= 0.25:
        ptype = "Redevelopment"
    return dict(
        id=x.get("id") or f"{st}-{x.get('formattedAddress','?')}",
        state=st, county=x.get("county") or "",
        address=x.get("formattedAddress") or x.get("addressLine1") or "Unknown",
        lat=x.get("latitude"), lon=x.get("longitude"),
        ptype=ptype, price=price, acres=acres,
        dom=x.get("daysOnMarket") or 0, drop=0,
    )

# ======================================================================
# 2. ENRICH — drive-time to Atlantic coast (approx)
# ======================================================================
COAST = [(36.851,-75.977),(38.336,-75.084),(38.720,-75.076),(36.030,-75.670),
         (35.225,-75.529),(37.130,-75.966),(34.720,-76.660)]
def hav(a,b,c,d):
    R=3958.8; p1,p2=math.radians(a),math.radians(c)
    x=math.sin(math.radians(c-a)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(math.radians(d-b)/2)**2
    return 2*R*math.asin(math.sqrt(x))
def ocean(lat,lon):
    if lat is None or lon is None: return 9999,999,4,"unknown"
    d=min(hav(lat,lon,cy,cx) for cy,cx in COAST); m=(d*1.3)/45.0*60.0
    t=1 if m<60 else 2 if m<120 else 3 if m<180 else 4
    return round(d,1),round(m),t,{1:"<1 hr",2:"1-2 hr",3:"2-3 hr",4:"3+ hr"}[t]

# ======================================================================
# 3. SCORE
# ======================================================================
def fhi(p):
    o={1:30,2:20,3:10,4:3}[p["ocean_tier"]]; a=min(p["acres"]/10.0,1.0)*25
    pr=max(0,25*(1-abs(p["price"]-150000)/300000)) if p["price"] else 0
    t={"Lot":20,"Home":16,"Redevelopment":14}[p["ptype"]]
    return round(min(100,o+a+pr+t))
def opp(p):
    dm=min(p["dom"]/90.0,1.0)*25; ch=max(0,35*(1-p["price"]/400000.0)) if p["price"] else 0
    return round(min(100,dm+ch))
def band(pr):
    return "Under $100k" if pr<100000 else "$100k - $200k" if pr<200000 else "$200k - $300k" if pr<300000 else "$300k+"

# ======================================================================
# run
# ======================================================================
print("Pulling live listings from RentCast...")
props, used = ingest_live()
seen={}
for p in props: seen[p["id"]]=p
props=list(seen.values())
for p in props:
    p["ocean_mi"],p["ocean_min"],p["ocean_tier"],p["ocean_label"]=ocean(p["lat"],p["lon"])
    p["fhi"]=fhi(p); p["opp"]=opp(p); p["band"]=band(p["price"])
props=[p for p in props if p["lat"] and p["lon"]]
props.sort(key=lambda x:(x["fhi"],x["opp"]),reverse=True)
print(f"\n{len(props)} geocoded listings after dedupe. Requests used: {used}/{REQUEST_BUDGET}")

# ---- KMZ ----
TIER={1:"ff2ca02c",2:"ffe6a817",3:"ff0d7fff",4:"ff3333cc"}
BANDS=["Under $100k","$100k - $200k","$200k - $300k","$300k+"]
def pm(p):
    d=f"""<![CDATA[<b>{html.escape(str(p['address']))}</b><br/>
    <b>Price:</b> ${p['price']:,} &nbsp; <b>Type:</b> {p['ptype']}<br/>
    <b>Acres:</b> {p['acres']} &nbsp; <b>DOM:</b> {p['dom']}<br/>
    <b>Ocean drive:</b> ~{p['ocean_min']} min (Tier {p['ocean_tier']}, {p['ocean_label']})<br/>
    <b>Forever Home Index:</b> {p['fhi']}/100 &nbsp; <b>Opportunity:</b> {p['opp']}/100]]>"""
    return f"""<Placemark><name>{html.escape(str(p['address']).split(',')[0])} — ${p['price']:,}</name>
    <description>{d}</description><styleUrl>#t{p['ocean_tier']}</styleUrl>
    <Point><coordinates>{p['lon']},{p['lat']},0</coordinates></Point></Placemark>"""
styles="".join(f'<Style id="t{t}"><IconStyle><color>{c}</color><scale>1.1</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/homegardenbusiness.png</href></Icon></IconStyle></Style>' for t,c in TIER.items())
folders=""
for b in BANDS:
    pms="".join(pm(p) for p in props if p["band"]==b); n=sum(1 for p in props if p["band"]==b)
    if pms: folders+=f'<Folder><name>{b}  ({n})</name><open>0</open>{pms}</Folder>'
kml=f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Forever Home — Live Map</name>{styles}{folders}</Document></kml>'
open(f"{SITE}/Forever_Home_Live.kml","w").write(kml)
with zipfile.ZipFile(f"{SITE}/Forever_Home_Live.kmz","w",zipfile.ZIP_DEFLATED) as z:
    z.write(f"{SITE}/Forever_Home_Live.kml","doc.kml")

# ---- auto-refresh NetworkLink (open once in Google Earth; refreshes weekly) ----
if SITE_BASE_URL:
    nl=f'''<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2">
<NetworkLink><name>Forever Home — Live (auto-refresh)</name><Link>
<href>{SITE_BASE_URL}/Forever_Home_Live.kmz</href>
<refreshMode>onInterval</refreshMode><refreshInterval>86400</refreshInterval>
</Link></NetworkLink></kml>'''
    open(f"{SITE}/ForeverHome_AutoRefresh.kml","w").write(nl)

# ---- report / index.html ----
today=datetime.date.today().strftime("%B %d, %Y")
top=props[:15]; tier1=[p for p in props if p["ocean_tier"]==1]; fresh=[p for p in props if p["dom"]<=30]
kmz_link=f'{SITE_BASE_URL}/Forever_Home_Live.kmz' if SITE_BASE_URL else 'Forever_Home_Live.kmz'
nl_link=f'{SITE_BASE_URL}/ForeverHome_AutoRefresh.kml' if SITE_BASE_URL else 'ForeverHome_AutoRefresh.kml'
def row(p): return f"<tr><td>{html.escape(str(p['address']))}</td><td>${p['price']:,}</td><td>{p['ptype']}</td><td>{p['acres']}</td><td>~{p['ocean_min']}m (T{p['ocean_tier']})</td><td><b>{p['fhi']}</b></td><td>{p['opp']}</td></tr>"
rep=f"""<!doctype html><meta charset="utf-8"><title>Forever Home — Weekly Report</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:940px;margin:32px auto;padding:0 20px;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;font-size:14px;margin:12px 0}}th,td{{border:1px solid #ddd;padding:7px 9px;text-align:left}}
th{{background:#f4f6f8}}tr:nth-child(even){{background:#fafbfc}}.g{{color:#2ca02c;font-weight:600}}
a.btn{{display:inline-block;background:#2ca02c;color:#fff;padding:9px 15px;border-radius:7px;text-decoration:none;margin:4px 8px 4px 0}}</style>
<h1>Forever Home — Weekly Report</h1><p style="color:#666">{today} · MD·VA·DE·PA·NC · live RentCast data · updates every Monday</p>
<p><a class="btn" href="{kmz_link}">⬇ Download map (.kmz)</a><a class="btn" href="{nl_link}">🌎 Auto-refresh map for Google Earth</a></p>
<h3>Executive Summary</h3><table>
<tr><td><b>Active listings tracked</b></td><td class="g">{len(props)}</td></tr>
<tr><td><b>Tier-1 (under 1 hr to ocean)</b></td><td>{len(tier1)}</td></tr>
<tr><td><b>Fresh (&le;30 days on market)</b></td><td>{len(fresh)}</td></tr>
<tr><td><b>Top pick</b></td><td>{html.escape(str(top[0]['address'])) if top else '—'}</td></tr></table>
<h3>Top Candidates (by Forever Home Index)</h3>
<table><tr><th>Property</th><th>Price</th><th>Type</th><th>Acres</th><th>Ocean drive</th><th>FHI</th><th>Opp</th></tr>{''.join(row(p) for p in top)}</table>
<p style="color:#888;font-size:12px">FHI = fit to your criteria · Opp = deal quality · pin color on map = ocean drive-time tier (green &lt;1hr).</p>"""
open(f"{SITE}/index.html","w").write(rep)
print(f"Wrote {SITE}/index.html, Forever_Home_Live.kmz, ForeverHome_AutoRefresh.kml")
