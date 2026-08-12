#!/usr/bin/env python3
"""
Forever Home OS — LIVE pipeline (GitHub Actions edition).

Pulls real active for-sale listings from RentCast (free tier), scores them,
computes drive-time to the nearest LARGE WATER BODY (Atlantic OR major bays/lakes),
and publishes into ./site/ :
    site/index.html                  -> weekly report (served by GitHub Pages)
    site/Forever_Home_Live.kmz       -> the map (open in Google Earth)
    site/Forever_Home_Live.kml       -> raw KML
    site/ForeverHome_AutoRefresh.kml -> open ONCE in Google Earth; auto-updates weekly

PIN COLOR = overall match to your criteria (Forever Home Index):
    green = strong (80+), yellow = good (65-79), orange = moderate (50-64), red = weaker.

The RentCast API key is read from the RENTCAST_API_KEY environment variable
(a GitHub Actions secret) — it is never stored in this file.
Stays inside RentCast's free 50-requests/month limit via REQUEST_BUDGET.
"""
import math, zipfile, html, datetime, sys, os, time, urllib.parse

# ======================================================================
# CONFIG
# ======================================================================
STATES         = ["MD", "VA", "DE", "PA", "NC"]
PROPERTY_TYPES = ["Land", "Single Family"]   # lots + homes (redevelopment derived from homes)
MAX_PRICE      = 300000                       # hard ceiling: nothing above $300k
MIN_ACRES      = 3                            # hard floor: nothing under 3 acres
PER_QUERY      = 500
REQUEST_BUDGET = 12
BASE           = "https://api.rentcast.io/v1/listings/sale"

API_KEY = os.environ.get("RENTCAST_API_KEY")
if not API_KEY and os.path.exists("rentcast_key.txt"):
    API_KEY = open("rentcast_key.txt").read().strip()
if not API_KEY:
    sys.exit("ERROR: set RENTCAST_API_KEY (GitHub secret) or a local rentcast_key.txt")

SITE = "site"; os.makedirs(SITE, exist_ok=True)
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
                r = requests.get(BASE, headers={"X-Api-Key": API_KEY}, params=params, timeout=40)
                used += 1
                if r.status_code != 200:
                    print(f"  {st}/{pt}: HTTP {r.status_code} {r.text[:120]}"); continue
                rows = r.json()
                for x in rows: out.append(normalize(x, st, pt))
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
# 2. ENRICH — drive-time to nearest LARGE WATER BODY
#    (Atlantic coastline + major bays, sounds, and large lakes)
# ======================================================================
# Atlantic / bay / sound coastline reference points (Mid-Atlantic)
COAST = [(36.851,-75.977),(38.336,-75.084),(38.720,-75.076),(36.030,-75.670),
         (35.225,-75.529),(37.130,-75.966),(34.720,-76.660)]
# Chesapeake & Delaware Bays, NC sounds
BAYS = [(39.20,-76.24),(38.60,-76.40),(38.00,-76.30),(37.30,-76.10),   # Chesapeake Bay (upper->lower)
        (39.10,-75.30),(38.85,-75.10),                                  # Delaware Bay
        (36.05,-76.05),(35.45,-76.05),(36.40,-75.92)]                   # Albemarle / Pamlico / Currituck sounds
# Major inland lakes across the 5 states
LAKES = [(36.60,-78.30),(36.51,-77.90),   # Kerr (Buggs Island) / Lake Gaston  (VA-NC)
         (37.10,-79.60),(38.02,-77.80),   # Smith Mountain Lake / Lake Anna     (VA)
         (39.50,-79.30),                  # Deep Creek Lake                     (MD)
         (42.10,-80.10),(40.42,-78.06),(41.40,-75.20),  # Lake Erie / Raystown / Wallenpaupack (PA)
         (35.50,-80.95),(35.72,-79.02),(36.00,-78.68)]  # Norman / Jordan / Falls              (NC)
WATER = COAST + BAYS + LAKES

def hav(a,b,c,d):
    R=3958.8; p1,p2=math.radians(a),math.radians(c)
    x=math.sin(math.radians(c-a)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(math.radians(d-b)/2)**2
    return 2*R*math.asin(math.sqrt(x))

def water(lat,lon):
    if lat is None or lon is None: return 9999,999,4,"unknown"
    d=min(hav(lat,lon,cy,cx) for cy,cx in WATER); m=(d*1.3)/45.0*60.0
    t=1 if m<60 else 2 if m<120 else 3 if m<180 else 4
    return round(d,1),round(m),t,{1:"<1 hr",2:"1-2 hr",3:"2-3 hr",4:"3+ hr"}[t]

# ---- distance to nearest major interstate (you want to be FAR from these) ----
# Route waypoints for the big interstates through the 5 states, densified below.
INTERSTATES = {
 "I-95":[(39.95,-75.16),(39.74,-75.55),(39.29,-76.61),(38.90,-77.04),(38.30,-77.46),
         (37.54,-77.44),(37.23,-77.40),(36.69,-77.54),(35.94,-77.79),(35.05,-78.88)],
 "I-64":[(36.85,-76.29),(37.08,-76.47),(37.27,-76.71),(37.54,-77.44),(38.03,-78.48),(38.15,-79.07)],
 "I-81":[(36.60,-82.18),(36.95,-81.09),(37.29,-80.05),(38.15,-79.07),(38.45,-78.87),
         (39.19,-78.16),(39.94,-77.66),(40.27,-76.88),(41.41,-75.66)],
 "I-83":[(39.29,-76.61),(39.96,-76.73),(40.27,-76.88)],
 "I-70":[(39.64,-77.72),(39.41,-77.41),(39.99,-78.25)],
 "I-76":[(40.00,-75.30),(40.27,-76.88),(40.44,-80.00)],
 "I-79":[(42.13,-80.08),(41.40,-80.05),(40.44,-80.00)],
 "I-80":[(41.16,-80.32),(41.05,-78.00),(41.04,-75.75)],
 "I-40":[(35.78,-78.64),(36.07,-79.79),(35.92,-80.25),(35.59,-82.55)],
 "I-85":[(37.23,-77.40),(36.58,-78.42),(36.07,-79.79),(35.22,-80.84)],
 "I-77":[(35.22,-80.84),(35.78,-80.89),(36.95,-81.09)],
 "I-97":[(39.29,-76.61),(38.97,-76.50)],
}
def _interp(a,b,mi=2.0):
    d=hav(a[0],a[1],b[0],b[1]); n=max(1,int(d/mi))
    return [(a[0]+(b[0]-a[0])*i/n, a[1]+(b[1]-a[1])*i/n) for i in range(n+1)]
HIGHWAY_POINTS=[]
for _nm,_wps in INTERSTATES.items():
    for _i in range(len(_wps)-1):
        for _p in _interp(_wps[_i],_wps[_i+1]): HIGHWAY_POINTS.append((_p[0],_p[1],_nm))
def highway_near(lat,lon):
    if lat is None or lon is None: return 999.0,None
    best=min(((hav(lat,lon,py,px),nm) for py,px,nm in HIGHWAY_POINTS), key=lambda x:x[0])
    return round(best[0],1),best[1]

# ======================================================================
# 3. SCORE  — Forever Home Index = overall match to your criteria
# ======================================================================
def hwy_pts(d):   # reward being FAR from an interstate (up to 15)
    return 15 if d>=3 else 11 if d>=2 else 6 if d>=1 else 2 if d>=0.5 else 0
def fhi(p):
    # water 30 · acreage 22 · price 18 · type 15 · away-from-highway 15
    w={1:30,2:19,3:9,4:3}[p["water_tier"]]
    a=min(p["acres"]/10.0,1.0)*22
    pr=max(0,18*(1-abs(p["price"]-150000)/300000)) if p["price"] else 0
    t={"Lot":15,"Home":12,"Redevelopment":10}[p["ptype"]]
    q=hwy_pts(p["hwy_mi"])
    return round(min(100,w+a+pr+t+q))
def opp(p):
    dm=min(p["dom"]/90.0,1.0)*25; ch=max(0,35*(1-p["price"]/400000.0)) if p["price"] else 0
    return round(min(100,dm+ch))
def band(pr):
    return "Under $100k" if pr<100000 else "$100k - $200k" if pr<200000 else "$200k - $300k" if pr<300000 else "$300k+"

# pin color by overall match (FHI)
def match_key(v):
    return ("excellent" if v>=80 else "strong" if v>=65 else "moderate" if v>=50
            else "fair" if v>=35 else "weak")
# 5 clearly distinct colors, best -> worst, single green then warm ramp (KML aabbggrr)
MATCH_COLOR = {"excellent":"ff50981a","strong":"ff00d4ff","moderate":"ff3c8dfd",
               "fair":"ff334ae3","weak":"ff150fa5"}
MATCH_LABEL = {"excellent":"Excellent (80+)","strong":"Strong (65-79)","moderate":"Moderate (50-64)",
               "fair":"Fair (35-49)","weak":"Weak (<35)"}
# matching CSS hex for the web report legend
MATCH_HEX = {"excellent":"#1a9850","strong":"#ffd400","moderate":"#fd8d3c",
             "fair":"#e34a33","weak":"#a50f15"}

# Icon SHAPE = type (house = home on the land, tree = land only); COLOR = match tier
ICON_HREF = {"house":"http://maps.google.com/mapfiles/kml/shapes/homegardenbusiness.png",
             "land": "http://maps.google.com/mapfiles/kml/shapes/parks.png"}
def icon_type(ptype): return "house" if ptype in ("Home","Redevelopment") else "land"

def listing_link(p):
    # RentCast gives no public listing URL, so link to a web search that lands on the real listing
    return "https://www.google.com/search?q=" + urllib.parse.quote(f"{p['address']} for sale")

def lot_map_link(p):
    # best-effort parcel/plat map: routes to the county GIS / parcel viewer for this lot
    parts=[str(p['address'])]
    if p.get('county'): parts.append(f"{p['county']} County")
    parts.append(f"{p.get('state','')} parcel map plat lot lines".strip())
    return "https://www.google.com/search?q=" + urllib.parse.quote(" ".join(parts))

def lot_ring(p):
    # area-accurate square (not the exact survey boundary) sized to the listing's acreage
    a=p.get("acres") or 0
    if a<=0 or p["lat"] is None or p["lon"] is None: return None
    side=math.sqrt(a*43560.0)                 # feet
    half=side/2.0
    dlat=half/364320.0                         # ~ft per degree latitude
    dlon=half/(364320.0*max(0.2,math.cos(math.radians(p["lat"]))))
    la,lo=p["lat"],p["lon"]
    corners=[(lo-dlon,la-dlat),(lo+dlon,la-dlat),(lo+dlon,la+dlat),(lo-dlon,la+dlat),(lo-dlon,la-dlat)]
    return " ".join(f"{x:.6f},{y:.6f},0" for x,y in corners)

def pros_cons(p):
    # score each factor against your preferences; + is good, - is bad
    f=[]
    t,m=p["water_tier"],p["water_min"]
    if   t==1: f.append(( 3.0, f"~{m} min to water — under your 1-hour target"))
    elif t==2: f.append(( 1.0, f"~{m} min to water (1–2 hrs)"))
    elif t==3: f.append((-2.0, f"~{m} min to water (2–3 hrs)"))
    else:      f.append((-3.0, f"~{m} min to water (over 3 hrs away)"))
    a=p["acres"]
    if   a>=10: f.append(( 3.0, f"{a} acres — very large parcel"))
    elif a>=5:  f.append(( 2.0, f"{a} acres — roomy"))
    else:       f.append(( 0.5, f"{a} acres — meets your 3-acre minimum"))
    pr=p["price"]
    if   pr<100000: f.append(( 3.0, f"${pr:,} — well under your budget"))
    elif pr<200000: f.append(( 1.5, f"${pr:,} — comfortably in budget"))
    elif pr<280000: f.append(( 0.5, f"${pr:,} — mid-upper price range"))
    else:           f.append((-1.0, f"${pr:,} — near your $300k ceiling"))
    if   p["ptype"]=="Lot":  f.append(( 1.0, "Vacant lot — build to your own design"))
    elif p["ptype"]=="Home": f.append(( 0.5, "Existing home on the land"))
    else:                    f.append((-1.0, "Redevelopment — likely needs significant work"))
    hd,hn=p["hwy_mi"],(p["hwy_name"] or "an interstate")
    if   hd>=3:    f.append(( 2.0, f"{hd} mi from nearest interstate — quiet"))
    elif hd>=1.5:  f.append(( 0.5, f"{hd} mi from {hn}"))
    elif hd>=0.75: f.append((-1.5, f"only {hd} mi from {hn} — some road noise"))
    else:          f.append((-3.0, f"~{hd} mi from {hn} — likely significant highway noise"))
    d=p["dom"]
    if   d>=75: f.append(( 1.0, f"{d} days on market — likely room to negotiate"))
    elif d<=7:  f.append((-0.5, f"only {d} days on market — may move fast"))
    pros=[txt for s,txt in sorted(f,key=lambda x:-x[0]) if s>0][:3]
    cons=[txt for s,txt in sorted(f,key=lambda x: x[0]) if s<0][:3]
    return pros,cons

# ======================================================================
# run
# ======================================================================
print("Pulling live listings from RentCast...")
props, used = ingest_live()
seen={}
for p in props: seen[p["id"]]=p
props=list(seen.values())
for p in props:
    p["water_mi"],p["water_min"],p["water_tier"],p["water_label"]=water(p["lat"],p["lon"])
    p["hwy_mi"],p["hwy_name"]=highway_near(p["lat"],p["lon"])
    p["fhi"]=fhi(p); p["opp"]=opp(p); p["band"]=band(p["price"]); p["mkey"]=match_key(p["fhi"])
props=[p for p in props if p["lat"] and p["lon"] and p["acres"]>=MIN_ACRES and p["price"]<=MAX_PRICE]
props.sort(key=lambda x:(x["fhi"],x["opp"]),reverse=True)
print(f"\n{len(props)} listings after filters (>= {MIN_ACRES} ac, <= ${MAX_PRICE:,}). Requests used: {used}/{REQUEST_BUDGET}")

# ---- KMZ ----
BANDS=["Under $100k","$100k - $200k","$200k - $300k"]
def pm(p):
    pros,cons=pros_cons(p)
    prohtml="".join(f"&nbsp;&nbsp;✓ {html.escape(t)}<br/>" for t in pros)
    conhtml="".join(f"&nbsp;&nbsp;✗ {html.escape(t)}<br/>" for t in cons) or "&nbsp;&nbsp;— no major drawbacks vs your criteria<br/>"
    d=f"""<![CDATA[<b>{html.escape(str(p['address']))}</b><br/>
    <b>Price:</b> ${p['price']:,} &nbsp; <b>Type:</b> {p['ptype']}<br/>
    <b>Acres:</b> {p['acres']} &nbsp; <b>Days on market:</b> {p['dom']}<br/>
    <b>Nearest water:</b> ~{p['water_min']} min drive ({p['water_label']})<br/>
    <b>Nearest interstate:</b> ~{p['hwy_mi']} mi{(' (' + p['hwy_name'] + ')') if p['hwy_name'] else ''}<br/>
    <b>Match:</b> {p['fhi']}/100 &nbsp; <b>Opportunity:</b> {p['opp']}/100<br/>
    <br/><b>Top pros:</b><br/>{prohtml}<b>Top cons:</b><br/>{conhtml}
    <br/><a href="{listing_link(p)}">🔍 Find this listing online</a>
    <br/><a href="{lot_map_link(p)}">🗺 View lot / parcel map</a>]]>"""
    pt=f"<Point><coordinates>{p['lon']},{p['lat']},0</coordinates></Point>"
    ring=lot_ring(p)
    geom=(f"<MultiGeometry>{pt}<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon></MultiGeometry>" if ring else pt)
    return f"""<Placemark><name>{p['ptype']} · ${p['price']:,} · {html.escape(str(p['address']).split(',')[0])}</name>
    <description>{d}</description><styleUrl>#m_{p['mkey']}_{icon_type(p['ptype'])}</styleUrl>
    {geom}</Placemark>"""
styles="".join(f'<Style id="m_{k}_{it}"><IconStyle><color>{c}</color><scale>1.2</scale><Icon><href>{href}</href></Icon></IconStyle><LineStyle><color>{c}</color><width>2</width></LineStyle><PolyStyle><color>40{c[2:]}</color></PolyStyle></Style>' for k,c in MATCH_COLOR.items() for it,href in ICON_HREF.items())
# Nested folders for filtering in Google Earth: State > Match tier > Price band. Toggle checkboxes to show/hide.
MATCH_ORDER=["excellent","strong","moderate","fair","weak"]
folders=""
for st in STATES:
    sprops=[p for p in props if p["state"]==st]
    if not sprops: continue
    msub=""
    for mk in MATCH_ORDER:
        mprops=[p for p in sprops if p["mkey"]==mk]
        if not mprops: continue
        psub=""
        for b in BANDS:
            pms="".join(pm(p) for p in mprops if p["band"]==b); n=sum(1 for p in mprops if p["band"]==b)
            if pms: psub+=f'<Folder><name>{b} ({n})</name><open>0</open>{pms}</Folder>'
        msub+=f'<Folder><name>{MATCH_LABEL[mk]} ({len(mprops)})</name><open>0</open>{psub}</Folder>'
    folders+=f'<Folder><name>{st} ({len(sprops)})</name><open>0</open>{msub}</Folder>'
desc=("Pin COLOR = overall match (Forever Home Index), best to worst: "
      "green Excellent 80+, yellow Strong 65-79, orange Moderate 50-64, red Fair 35-49, dark red Weak <35. "
      "Only listings >= 3 acres and <= $300k are shown. "
      "TO FILTER: use the sidebar checkboxes, nested State > Match tier > Price band. "
      "Each pin is labeled Type - Price - Address. "
      "The colored square around each pin shows the APPROXIMATE lot size (area-accurate, centered on the listing; "
      "not the exact survey boundary — use the parcel-map link in the popup for true lines). "
      "Match rewards being near any large water (ocean, bay, sound, or major lake), acreage, price, and type.")
kml=f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Forever Home — Live Map</name><description><![CDATA[{desc}]]></description>{styles}{folders}</Document></kml>'
open(f"{SITE}/Forever_Home_Live.kml","w").write(kml)
with zipfile.ZipFile(f"{SITE}/Forever_Home_Live.kmz","w",zipfile.ZIP_DEFLATED) as z:
    z.write(f"{SITE}/Forever_Home_Live.kml","doc.kml")

# ---- auto-refresh NetworkLink ----
if SITE_BASE_URL:
    # viewFormat appends the current map view coords as a query string, which busts
    # any GitHub/Earth cache on each refresh so you always pull the freshest map.
    nl=f'''<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2">
<NetworkLink><name>Forever Home — Live (auto-refresh)</name>
<flyToView>0</flyToView>
<Link>
<href>{SITE_BASE_URL}/Forever_Home_Live.kmz</href>
<refreshMode>onInterval</refreshMode><refreshInterval>3600</refreshInterval>
<viewRefreshMode>onStop</viewRefreshMode><viewRefreshTime>1</viewRefreshTime>
<viewFormat>cb=[bboxWest]_[bboxSouth]_[bboxEast]_[bboxNorth]</viewFormat>
</Link></NetworkLink></kml>'''
    open(f"{SITE}/ForeverHome_AutoRefresh.kml","w").write(nl)

# ---- report / index.html ----
today=datetime.date.today().strftime("%B %d, %Y")
top=props[:15]; tier1=[p for p in props if p["water_tier"]==1]; fresh=[p for p in props if p["dom"]<=30]
topmatch=[p for p in props if p["fhi"]>=80]
kmz_link=f'{SITE_BASE_URL}/Forever_Home_Live.kmz' if SITE_BASE_URL else 'Forever_Home_Live.kmz'
nl_link=f'{SITE_BASE_URL}/ForeverHome_AutoRefresh.kml' if SITE_BASE_URL else 'ForeverHome_AutoRefresh.kml'
def row(p): return f"<tr><td><a href='{listing_link(p)}' target='_blank'>{html.escape(str(p['address']))}</a></td><td>${p['price']:,}</td><td>{p['ptype']}</td><td>{p['acres']}</td><td>~{p['water_min']}m ({p['water_label']})</td><td><b>{p['fhi']}</b></td><td>{p['opp']}</td></tr>"
STATE_NAMES={"MD":"Maryland","VA":"Virginia","DE":"Delaware","PA":"Pennsylvania","NC":"North Carolina"}
def state_block(st):
    sp=[p for p in props if p["state"]==st][:10]   # props already sorted by match desc
    if not sp:
        return f"<h3>{STATE_NAMES[st]}</h3><p style='color:#888'>No qualifying listings this week.</p>"
    hdr="<tr><th>Property</th><th>Price</th><th>Type</th><th>Acres</th><th>Nearest water</th><th>Match</th><th>Opp</th></tr>"
    return f"<h3>{STATE_NAMES[st]} — Top {len(sp)} (by match)</h3><table>{hdr}{''.join(row(p) for p in sp)}</table>"
state_sections="".join(state_block(st) for st in STATES)
rep=f"""<!doctype html><meta charset="utf-8"><title>Forever Home — Weekly Report</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:960px;margin:32px auto;padding:0 20px;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;font-size:14px;margin:12px 0}}th,td{{border:1px solid #ddd;padding:7px 9px;text-align:left}}
th{{background:#f4f6f8}}tr:nth-child(even){{background:#fafbfc}}.g{{color:#2ca02c;font-weight:600}}
a.btn{{display:inline-block;background:#2ca02c;color:#fff;padding:9px 15px;border-radius:7px;text-decoration:none;margin:4px 8px 4px 0}}
.legend span{{display:inline-block;margin-right:14px;font-size:13px}}.dot{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:middle}}</style>
<h1>Forever Home — Weekly Report</h1><p style="color:#666">{today} · MD·VA·DE·PA·NC · live RentCast data · updates every Monday</p>
<p><a class="btn" href="{kmz_link}">⬇ Download map (.kmz)</a><a class="btn" href="{nl_link}">🌎 Auto-refresh map for Google Earth</a></p>
<p class="legend"><b>Pin color = overall match (best → worst):</b>
<span><span class="dot" style="background:#1a9850"></span>Excellent 80+</span>
<span><span class="dot" style="background:#ffd400"></span>Strong 65-79</span>
<span><span class="dot" style="background:#fd8d3c"></span>Moderate 50-64</span>
<span><span class="dot" style="background:#e34a33"></span>Fair 35-49</span>
<span><span class="dot" style="background:#a50f15"></span>Weak &lt;35</span></p>
<p class="legend" style="color:#555">Showing only listings ≥ 3 acres and ≤ $300k · in Google Earth, filter by <b>Homes / Land only</b> and price using the sidebar checkboxes.</p>
<h3>Executive Summary</h3><table>
<tr><td><b>Active listings tracked</b></td><td class="g">{len(props)}</td></tr>
<tr><td><b>Excellent matches (FHI 80+)</b></td><td>{len(topmatch)}</td></tr>
<tr><td><b>Within 1 hr of large water</b></td><td>{len(tier1)}</td></tr>
<tr><td><b>Fresh (&le;30 days on market)</b></td><td>{len(fresh)}</td></tr>
<tr><td><b>Top pick</b></td><td>{html.escape(str(top[0]['address'])) if top else '—'}</td></tr></table>
<h2>Top 10 by State</h2>
{state_sections}
<p style="color:#888;font-size:12px">Match (Forever Home Index) rewards proximity to any large water body — ocean, bay, sound, or major lake — plus acreage, price fit, and property type. Opp = deal quality.</p>"""
open(f"{SITE}/index.html","w").write(rep)
print(f"Wrote {SITE}/index.html + KMZ. Excellent matches: {len(topmatch)}, within-1hr-water: {len(tier1)}")
