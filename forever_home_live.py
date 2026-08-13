#!/usr/bin/env python3
"""
Forever Home OS — LIVE pipeline (GitHub Actions edition).  v2 — reconciled criteria.

Pulls real active for-sale listings from RentCast (free tier), scores them against
your reconciled Forever Home criteria, and publishes into ./site/ :
    site/index.html                  -> weekly report (served by GitHub Pages)
    site/Forever_Home_Live.kmz       -> the map (open in Google Earth)
    site/Forever_Home_Live.kml       -> raw KML
    site/ForeverHome_AutoRefresh.kml -> open ONCE in Google Earth; auto-updates weekly

WHAT CHANGED IN v2 (matches Forever_Home_Search_Criteria_v1):
  * Forever Home Index reweighted to 7 axes you tuned (see WEIGHTS below).
  * Water: ANY recreational body (lake/bay/river/reservoir/ocean) treated equally;
    full points within ~90 min drive ("day trip"). Ocean no longer privileged.
  * Acreage: 3-acre hard floor kept; score has DIMINISHING RETURNS and PLATEAUS at 15 acres.
  * Property type: neutral (homes and land compete on merits).
  * NEW: drive time to your four anchor people, shown per property + a "people & family" axis.
  * NEW: raw-land cost & timeline estimator (low-likely-high band) for buildable lots.
  * NEW: expanded de-risking research links (flood, wetlands, perc/septic, power/broadband,
    hospital) plus non-traditional-school search — the interim for axes that have no free
    per-property data feed yet (schools, safety, living-off-the-land: see NEUTRAL_BASELINE).

Everything you'd want to tune lives in the CONFIG block. The RentCast key is read from the
RENTCAST_API_KEY environment variable (a GitHub Actions secret). Stays inside RentCast's free
50-requests/month limit via REQUEST_BUDGET.
"""
import math, zipfile, html, datetime, sys, os, time, urllib.parse

# ======================================================================
# CONFIG  — change anything here; the map/report rebuild on the next run
# ======================================================================
STATES         = ["MD", "VA", "DE", "PA", "NC"]
PROPERTY_TYPES = ["Land", "Single Family"]   # lots + homes (redevelopment derived from homes)
MIN_ACRES      = 2                            # hard floor
PER_QUERY      = 500
REQUEST_BUDGET = 12
BASE           = "https://api.rentcast.io/v1/listings/sale"
AVM_URL        = "https://api.rentcast.io/v1/avm/value"

# --- PRICE CEILING driven by the home you'll sell ---------------------------
# The ceiling is your current home's estimated value (fetched from RentCast each run,
# so it self-updates). Every property must fit its ALL-IN cost under this ceiling:
#   move-in home  -> list price
#   raw lot       -> lot price + estimated cost to build a 3-4 br house
#   fixer/redev   -> price + estimated cost of improvements
HOME_ADDRESS        = "9383 Steeple Court, Laurel, MD 20723"
CEILING_OVERRIDE    = None      # set a dollar number to hardcode the ceiling; None = fetch AVM
FALLBACK_CEILING    = 300000    # used only if the AVM lookup fails
# Optional realism knobs (defaults = use gross home value as the ceiling, as requested).
# Your true usable budget is net proceeds: value - mortgage payoff - selling costs.
NET_PROCEEDS_FACTOR = 1.00      # e.g. 0.93 to net out ~7% realtor/closing costs
MORTGAGE_PAYOFF     = 0         # subtract remaining mortgage balance
EXTRA_FUNDS         = 170000    # added to RentCast's value (Zillow/Realtor run higher; +$170k per your call)
# Cost model knobs for the all-in math:
BUILD_COST_FLOOR        = 300000  # (unused now that land is shown up to the ceiling)
HOME_IMPROVEMENT_BUFFER = 0       # added to a move-in home's all-in (0 = assume move-in ready)
RENO_PSF                = 110     # $/sqft heavy renovation for fixer / redevelopment

# --- Forever Home Index weights (must sum to 100). Your reconciled v1 numbers. ---
WEIGHTS = {
    "schools":  16,   # school quality incl. non-traditional options
    "quiet":    16,   # away from highways / traffic
    "safety":   16,   # neighborhood safety
    "off_land": 16,   # living off the land (garden-led)
    "water":    12,   # near recreational water, <=90 min
    "people":   12,   # close to town & the four anchor people
    "acreage":  12,   # land, diminishing returns, plateau at 15 ac
}
WATER_FULL_MIN = 90    # minutes: full water points at/under this ("day trip")
ACRE_PLATEAU   = 15    # acres: no additional land credit beyond this

# Axes with no reliable FREE per-property data feed yet. Rather than fake precision,
# they contribute a neutral baseline (so they don't distort the ranking) and are
# surfaced as prominent research links/flags on every property. Wire a data source
# later to score them for real (that's the paid-tier upgrade).
NEUTRAL_BASELINE = 0.60
UNSCORED_AXES = ("schools", "safety", "off_land")

# Your four anchor people (locality-accurate coords; refine to exact house-level anytime).
ANCHORS = [
    ("Best friends · West Chester PA", 39.9850, -75.6200),
    ("Her grandma · Fallston MD",      39.5115, -76.4155),
    ("Your parents · Crofton MD",      39.0060, -76.6760),
    ("Her mom · Gambrills MD",         39.0640, -76.6600),
]

# Raw-land build-cost model (order-of-magnitude planning estimate, NOT a bid; excludes land price).
STATE_COST_MULT = {"MD": 1.08, "VA": 1.02, "DE": 1.03, "PA": 1.00, "NC": 0.95}
BUILD_SQFT      = 1800     # assumed modest single-level forever home
BUILD_PSF       = 165      # $/sqft base, mid finish (before regional multiplier)

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
def ingest_live(start_used=0):
    out, used = [], start_used
    for st in STATES:
        for pt in PROPERTY_TYPES:
            if used >= REQUEST_BUDGET:
                print(f"  ! request budget ({REQUEST_BUDGET}) reached — stopping to stay free")
                return out, used
            # Homes: list price must fit under the ceiling. Land: leave room for the build,
            # so only fetch lots cheap enough that lot + a floor-cost build could fit.
            maxp = SF_MAXPRICE if pt != "Land" else LAND_MAXPRICE
            params = {"state": st, "propertyType": pt, "status": "Active",
                      "maxPrice": maxp, "limit": PER_QUERY}
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
        state=st, county=x.get("county") or "", city=x.get("city") or "", zip=x.get("zipCode") or "",
        address=x.get("formattedAddress") or x.get("addressLine1") or "Unknown",
        lat=x.get("latitude"), lon=x.get("longitude"),
        ptype=ptype, price=price, acres=acres, bsqft=x.get("squareFootage") or 0,
        dom=x.get("daysOnMarket") or 0, drop=0,
    )

# ======================================================================
# 1b. CEILING — your home's value (RentCast AVM), self-updating each run
# ======================================================================
def compute_ceiling():
    """Returns (ceiling_dollars, source_text, requests_used)."""
    if CEILING_OVERRIDE:
        return int(CEILING_OVERRIDE), f"manual override ${int(CEILING_OVERRIDE):,}", 0
    try:
        r = requests.get(AVM_URL, headers={"X-Api-Key": API_KEY},
                         params={"address": HOME_ADDRESS}, timeout=40)
        if r.status_code == 200:
            v = r.json().get("price") or 0
            if v > 0:
                ceil = int(v*NET_PROCEEDS_FACTOR - MORTGAGE_PAYOFF + EXTRA_FUNDS)
                src = f"RentCast AVM of {HOME_ADDRESS}: ${int(v):,}"
                if NET_PROCEEDS_FACTOR != 1.0 or MORTGAGE_PAYOFF or EXTRA_FUNDS:
                    src += f" → adjusted ceiling ${ceil:,}"
                return ceil, src, 1
        print(f"  ! AVM lookup HTTP {r.status_code} — using fallback ceiling ${FALLBACK_CEILING:,}")
        return FALLBACK_CEILING, f"fallback ${FALLBACK_CEILING:,} (AVM HTTP {r.status_code})", 1
    except Exception as e:
        print(f"  ! AVM error {type(e).__name__} {e} — using fallback ceiling")
        return FALLBACK_CEILING, f"fallback ${FALLBACK_CEILING:,} (AVM error)", 0

# ======================================================================
# 2. ENRICH — geography from free data
# ======================================================================
def hav(a,b,c,d):
    R=3958.8; p1,p2=math.radians(a),math.radians(c)
    x=math.sin(math.radians(c-a)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(math.radians(d-b)/2)**2
    return 2*R*math.asin(math.sqrt(x))

def drive_min(mi):            # crude drive-time model: +30% for road routing, 45 mph avg
    return (mi*1.3)/45.0*60.0

# --- nearest RECREATIONAL water (ocean, bay, sound, OR major lake — all equal) ---
COAST = [(36.851,-75.977),(38.336,-75.084),(38.720,-75.076),(36.030,-75.670),
         (35.225,-75.529),(37.130,-75.966),(34.720,-76.660)]
BAYS  = [(39.20,-76.24),(38.60,-76.40),(38.00,-76.30),(37.30,-76.10),
         (39.10,-75.30),(38.85,-75.10),
         (36.05,-76.05),(35.45,-76.05),(36.40,-75.92)]
LAKES = [(36.60,-78.30),(36.51,-77.90),(37.10,-79.60),(38.02,-77.80),(39.50,-79.30),
         (42.10,-80.10),(40.42,-78.06),(41.40,-75.20),
         (35.50,-80.95),(35.72,-79.02),(36.00,-78.68)]
WATER = COAST + BAYS + LAKES

def water(lat,lon):
    if lat is None or lon is None: return 9999,999,4,"unknown"
    d=min(hav(lat,lon,cy,cx) for cy,cx in WATER); m=drive_min(d)
    t=1 if m<=90 else 2 if m<=150 else 3 if m<=210 else 4
    lbl={1:"≤90 min (day trip)",2:"90–150 min",3:"150–210 min",4:"3.5+ hr"}[t]
    return round(d,1),round(m),t,lbl

# --- distance to nearest major interstate (you want to be FAR from these) ---
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

# --- drive time to your four anchor people ---
def anchor_times(lat,lon):
    res=[]
    for nm,ay,ax in ANCHORS:
        if lat is None or lon is None: res.append((nm,None)); continue
        res.append((nm, round(drive_min(hav(lat,lon,ay,ax)))))
    return res
def nearest_kin(anchor_list):
    vals=[m for _,m in anchor_list if m is not None]
    return min(vals) if vals else None

# ======================================================================
# 3. SCORE — Forever Home Index (0-100) = overall match to your criteria
# ======================================================================
def s_water(m):
    if m is None: return 0.4
    return 1.0 if m<=WATER_FULL_MIN else max(0.0, 1-(m-WATER_FULL_MIN)/120.0)
def s_quiet(d):
    return 1.0 if d>=3 else 0.75 if d>=2 else 0.45 if d>=1 else 0.2 if d>=0.5 else 0.05
def s_acre(a):
    a=min(max(a,MIN_ACRES),ACRE_PLATEAU)                 # floor 3, plateau 15
    frac=(math.sqrt(a)-math.sqrt(MIN_ACRES))/(math.sqrt(ACRE_PLATEAU)-math.sqrt(MIN_ACRES))
    return 0.4+0.6*frac                                   # 3ac -> 0.40, 15ac -> 1.00, diminishing
def s_people(best_min):
    if best_min is None: return 0.4
    return 1.0 if best_min<=30 else max(0.0, 1-(best_min-30)/120.0)

def fhi(p):
    W=WEIGHTS
    variable = (W["water"]  * s_water(p["water_min"])
              + W["quiet"]  * s_quiet(p["hwy_mi"])
              + W["acreage"]* s_acre(p["acres"])
              + W["people"] * s_people(p["kin_min"]))
    baseline = NEUTRAL_BASELINE * sum(W[a] for a in UNSCORED_AXES)
    return round(min(100, variable + baseline))

def opp(p):
    dm=min(p["dom"]/90.0,1.0)*25; ch=max(0,35*(1-p["price"]/400000.0)) if p["price"] else 0
    return round(min(100,dm+ch))
def band(pr):
    lo=int((pr or 0)//100000)*100
    return f"${lo}k–${lo+100}k"

# ---- raw-land build cost & timeline estimator (3-4 br house on a lot) ----
def build_estimate(p):
    if p["ptype"] not in ("Lot","Redevelopment"): return None
    a=p["acres"] or MIN_ACRES
    m=STATE_COST_MULT.get(p["state"],1.0)
    cleared=min(a,1.0)                                   # ~1 acre cleared per her preference
    site_prep=8000*cleared + 1500*max(0,min(a,4)-1)     # clearing + grading
    well=12000; septic=18000; driveway=6000; power=10000
    soft=12000                                          # survey, perc, design/eng, permits
    structure=BUILD_SQFT*BUILD_PSF*m                    # 3-4 br single-level, mid finish
    subtotal=(site_prep+well+septic+driveway+power+soft)*m + structure
    likely=subtotal*1.18                                # +18% contingency
    low=likely*0.80; high=likely*1.35                   # engineered septic / long power run / surprises
    rnd=lambda v:int(round(v,-3))
    return dict(low=rnd(low), likely=rnd(likely), high=rnd(high),
                months=(12,24), confidence="low–moderate")

# ---- renovation estimate for a fixer / redevelopment ----
def reno_estimate(p):
    m=STATE_COST_MULT.get(p["state"],1.0)
    sqft=p.get("bsqft") or 1500                          # building sqft if known, else assume ~1500
    return int(round(sqft*RENO_PSF*m + 15000, -3))       # heavy reno + permits/soft

# ---- ALL-IN cost = what it truly costs to end up living there ----
def added_cost(p):
    if p["ptype"]=="Lot":
        e=p.get("est"); return e["likely"] if e else 0   # lot + build a 3-4 br house
    if p["ptype"]=="Redevelopment":
        return reno_estimate(p)                          # price + improvements
    return HOME_IMPROVEMENT_BUFFER                        # move-in home
def all_in(p):
    return int((p["price"] or 0) + added_cost(p))

# ---- pin color by RELATIVE match rank (assigned in run section) ----
MATCH_COLOR = {"excellent":"ff50981a","strong":"ff00d4ff","moderate":"ff3c8dfd",
               "fair":"ff334ae3","weak":"ff150fa5"}
MATCH_LABEL = {"excellent":"Best 20%","strong":"Upper 20%","moderate":"Middle 20%",
               "fair":"Lower 20%","weak":"Bottom 20%"}
MATCH_HEX = {"excellent":"#1a9850","strong":"#ffd400","moderate":"#fd8d3c",
             "fair":"#e34a33","weak":"#a50f15"}
ICON_HREF = {"house":"http://maps.google.com/mapfiles/kml/shapes/homegardenbusiness.png",
             "land": "http://maps.google.com/mapfiles/kml/shapes/parks.png"}
def icon_type(ptype): return "house" if ptype in ("Home","Redevelopment") else "land"

# ---- research / de-risking links (interim for not-yet-scored axes) ----
def _gsearch(s): return "https://www.google.com/search?q=" + urllib.parse.quote(s)
def _place(p):   return f"{(p.get('city') or p.get('county') or '')} {p.get('state','')}".strip()
def listing_link(p): return _gsearch(f"{p['address']} for sale")
def lot_map_link(p):
    parts=[str(p['address'])]
    if p.get('county'): parts.append(f"{p['county']} County")
    parts.append(f"{p.get('state','')} parcel map plat lot lines".strip())
    return _gsearch(" ".join(parts))
def schools_link(p): return _gsearch(f"{_place(p)} best school districts AND charter, magnet, private, Montessori, homeschool co-op options")
def crime_link(p):   return _gsearch(f"{_place(p)} crime rate safety map")
def grow_link(p):    return _gsearch(f"{_place(p)} USDA growing zone, vegetable garden zoning HOA/deed restrictions, local farms CSA farmers market")
def meat_link(p):    return _gsearch(f"{_place(p)} local pasture-raised meat, farm eggs, raw dairy, butcher; backyard chickens/livestock zoning")
def flood_link(p):   return "https://msc.fema.gov/portal/search?AddressQuery=" + urllib.parse.quote(str(p['address']))
def wetland_link(p): return "https://www.fws.gov/wetlands/data/mapper.html"
def perc_link(p):    return _gsearch(f"{p.get('county','')} County {p.get('state','')} health department perc test septic well requirements")
def util_link(p):    return _gsearch(f"{_place(p)} electric utility service + broadband internet availability map")
def hospital_link(p):return _gsearch(f"nearest hospital emergency room to {p['address']}")

def lot_ring(p):
    a=p.get("acres") or 0
    if a<=0 or p["lat"] is None or p["lon"] is None: return None
    side=math.sqrt(a*43560.0); half=side/2.0
    dlat=half/364320.0
    dlon=half/(364320.0*max(0.2,math.cos(math.radians(p["lat"]))))
    la,lo=p["lat"],p["lon"]
    corners=[(lo-dlon,la-dlat),(lo+dlon,la-dlat),(lo+dlon,la+dlat),(lo-dlon,la+dlat),(lo-dlon,la-dlat)]
    return " ".join(f"{x:.6f},{y:.6f},0" for x,y in corners)

def pros_cons(p):
    f=[]
    t,m=p["water_tier"],p["water_min"]
    if   t==1: f.append(( 3.0, f"~{m} min to recreational water — an easy day trip"))
    elif t==2: f.append(( 1.0, f"~{m} min to water (90–150 min)"))
    elif t==3: f.append((-2.0, f"~{m} min to water (150–210 min)"))
    else:      f.append((-3.0, f"~{m} min to water (over 3.5 hrs)"))
    a=p["acres"]
    if   a>=15: f.append(( 2.5, f"{a} acres — at/above your 15-acre sweet-spot plateau"))
    elif a>=8:  f.append(( 2.0, f"{a} acres — roomy, strong land score"))
    elif a>=5:  f.append(( 1.2, f"{a} acres — comfortably above the {MIN_ACRES}-acre floor"))
    else:       f.append(( 0.5, f"{a} acres — meets your {MIN_ACRES}-acre minimum"))
    k=p["kin_min"]
    if   k is not None and k<=45: f.append(( 2.0, f"~{k} min to your nearest family/friends"))
    elif k is not None and k<=90: f.append(( 0.5, f"~{k} min to your nearest family/friends"))
    elif k is not None:           f.append((-1.5, f"~{k} min to your nearest family/friends — a hike"))
    hd,hn=p["hwy_mi"],(p["hwy_name"] or "an interstate")
    if   hd>=3:    f.append(( 2.0, f"{hd} mi from nearest interstate — quiet"))
    elif hd>=1.5:  f.append(( 0.5, f"{hd} mi from {hn}"))
    elif hd>=0.75: f.append((-1.5, f"only {hd} mi from {hn} — some road noise"))
    else:          f.append((-3.0, f"~{hd} mi from {hn} — likely significant highway noise"))
    ai=p.get("allin", p["price"]); head=CEILING-ai
    if   p["ptype"]=="Lot":
        f.append(( 1.0 if head>0 else -2.0,
                   f"All-in ~${ai:,} (lot + build) — {'$'+format(head,',')+' under' if head>=0 else '$'+format(-head,',')+' OVER'} your ${CEILING:,} ceiling"))
    elif p["ptype"]=="Redevelopment":
        f.append(( 0.5 if head>0 else -2.0,
                   f"All-in ~${ai:,} (price + reno) — {'$'+format(head,',')+' under' if head>=0 else '$'+format(-head,',')+' OVER'} ceiling"))
    else:
        if   head>=150000: f.append(( 2.0, f"${p['price']:,} — well under your ${CEILING:,} ceiling"))
        elif head>=40000:  f.append(( 1.0, f"${p['price']:,} — comfortably under ceiling"))
        else:              f.append((-1.0, f"${p['price']:,} — near your ${CEILING:,} ceiling"))
    d=p["dom"]
    if   d>=75: f.append(( 1.0, f"{d} days on market — likely room to negotiate"))
    elif d<=7:  f.append((-0.5, f"only {d} days on market — may move fast"))
    pros=[txt for s,txt in sorted(f,key=lambda x:-x[0]) if s>0][:3]
    cons=[txt for s,txt in sorted(f,key=lambda x: x[0]) if s<0][:3]
    return pros,cons

# ======================================================================
# run
# ======================================================================
print("Determining price ceiling from your home's value...")
CEILING, CEILING_SRC, used0 = compute_ceiling()
SF_MAXPRICE   = CEILING                                   # homes: list price must fit under ceiling
LAND_MAXPRICE = CEILING                                    # lots: shown up to the ceiling; over-budget all-in is flagged, not dropped
print(f"  Ceiling = ${CEILING:,}  ({CEILING_SRC})")
print(f"  Query caps -> homes <= ${SF_MAXPRICE:,}, land <= ${LAND_MAXPRICE:,}")

print("Pulling live listings from RentCast...")
props, used = ingest_live(start_used=used0)
seen={}
for p in props: seen[p["id"]]=p
props=list(seen.values())
for p in props:
    p["water_mi"],p["water_min"],p["water_tier"],p["water_label"]=water(p["lat"],p["lon"])
    p["hwy_mi"],p["hwy_name"]=highway_near(p["lat"],p["lon"])
    p["anchors"]=anchor_times(p["lat"],p["lon"])
    p["kin_min"]=nearest_kin(p["anchors"])
    p["est"]=build_estimate(p) if p["ptype"]=="Lot" else None
    p["reno"]=reno_estimate(p) if p["ptype"]=="Redevelopment" else 0
    p["addcost"]=added_cost(p)
    p["allin"]=all_in(p)
    p["over_budget"]=p["allin"]>CEILING
    p["fhi"]=fhi(p); p["opp"]=opp(p); p["band"]=band(p["price"])
found_by_state={st:sum(1 for p in props if p.get("state")==st) for st in STATES}
# Categorize every listing: keep it, or record WHY it was dropped (per state) so a
# state that zeroes out (e.g. PA) is self-diagnosing instead of a mystery.
UNDER_KEY=f"under_{MIN_ACRES}ac"
DROP_REASONS=["no_coords","no_lotsize",UNDER_KEY]
def drop_reason(p):
    if not (p["lat"] and p["lon"]): return "no_coords"
    if (p["acres"] or 0)<=0:        return "no_lotsize"      # RentCast returned no lotSize
    if p["acres"]<MIN_ACRES:        return UNDER_KEY
    return None   # all-in over ceiling is NOT dropped now — it's kept and flagged over-budget
drops_by_state={st:{r:0 for r in DROP_REASONS} for st in STATES}
kept=[]
for p in props:
    why=drop_reason(p)
    if why is None: kept.append(p)
    else: drops_by_state[p["state"]][why]+=1
props=kept
kept_by_state={st:sum(1 for p in props if p["state"]==st) for st in STATES}
# Affordable (all-in under ceiling) ranks above over-budget; then by match, then deal.
props.sort(key=lambda x:(0 if x["over_budget"] else 1, x["fhi"], x["opp"]),reverse=True)
N=len(props)
for i,p in enumerate(props):
    q=i/max(1,N-1)
    p["mkey"]=("excellent" if q<0.2 else "strong" if q<0.4 else "moderate" if q<0.6 else "fair" if q<0.8 else "weak")
n_fit=sum(1 for p in props if not p["over_budget"]); n_over=sum(1 for p in props if p["over_budget"])
print(f"\n{len(props)} listings after filters (>= {MIN_ACRES} ac, list <= ${CEILING:,}): {n_fit} fit all-in, {n_over} shown over-budget. Requests used: {used}/{REQUEST_BUDGET}")
print("found->kept by state: " + ", ".join(f"{st} {found_by_state[st]}->{kept_by_state[st]}" for st in STATES))
for st in STATES:
    d=drops_by_state[st]
    if sum(d.values()): print(f"  {st} drops: " + " · ".join(f"{r} {d[r]}" for r in DROP_REASONS if d[r]))

# ---- KMZ ----
_maxband=max([p["price"] for p in props], default=0)
BANDS=[f"${i}k–${i+100}k" for i in range(0, int(max(_maxband, CEILING)//100000)*100+100, 100)]
def cost_line(p):
    ai=p["allin"]; head=CEILING-ai
    tag=(f"<span style='color:#1a9850'>${head:,} under</span>" if head>=0
         else f"<span style='color:#c0392b'>${-head:,} OVER</span>")
    if p["ptype"]=="Lot" and p.get("est"):
        e=p["est"]
        return (f"<b>All-in: ${ai:,}</b> = lot ${p['price']:,} + build ~${e['likely']:,} "
                f"(${e['low']:,}–${e['high']:,}) &nbsp; vs ceiling ${CEILING:,} ({tag})<br/>"
                f"<span style='color:#666;font-size:12px'>~{e['months'][0]}–{e['months'][1]} mo to build · confidence {e['confidence']}</span><br/>")
    if p["ptype"]=="Redevelopment":
        return (f"<b>All-in: ${ai:,}</b> = price ${p['price']:,} + reno ~${p['reno']:,} "
                f"&nbsp; vs ceiling ${CEILING:,} ({tag})<br/>")
    return f"<b>All-in: ${ai:,}</b> (move-in) &nbsp; vs ceiling ${CEILING:,} ({tag})<br/>"
def anchors_line(p):
    parts=[f"{nm.split(' · ')[0]} ~{m}m" if m is not None else f"{nm}: ?" for nm,m in p.get("anchors",[])]
    return "<b>Drive to family:</b> " + " &nbsp;|&nbsp; ".join(parts) + "<br/>" if parts else ""
def pm(p):
    pros,cons=pros_cons(p)
    prohtml="".join(f"&nbsp;&nbsp;✓ {html.escape(t)}<br/>" for t in pros)
    conhtml="".join(f"&nbsp;&nbsp;✗ {html.escape(t)}<br/>" for t in cons) or "&nbsp;&nbsp;— no major drawbacks vs your criteria<br/>"
    d=f"""<![CDATA[<b>{html.escape(str(p['address']))}</b><br/>
    <b>List price:</b> ${p['price']:,} &nbsp; <b>Type:</b> {p['ptype']}<br/>
    <b>Acres:</b> {p['acres']} &nbsp; <b>Days on market:</b> {p['dom']}<br/>
    {cost_line(p)}
    <b>Nearest water:</b> ~{p['water_min']} min drive ({p['water_label']})<br/>
    <b>Nearest interstate:</b> ~{p['hwy_mi']} mi{(' (' + p['hwy_name'] + ')') if p['hwy_name'] else ''}<br/>
    {anchors_line(p)}
    <b>Match:</b> {p['fhi']}/100 &nbsp; <b>Opportunity:</b> {p['opp']}/100<br/>
    <span style="color:#666;font-size:12px">Match auto-scores water, quiet, acreage &amp; family drive time. Schools, safety &amp; living-off-the-land aren't auto-scored yet — check the links below.</span><br/>
    <br/><b>Top pros:</b><br/>{prohtml}<b>Top cons:</b><br/>{conhtml}
    <br/><b>Look into it:</b> <a href="{schools_link(p)}">\U0001F393 Schools</a> &nbsp; <a href="{crime_link(p)}">\U0001F693 Safety</a> &nbsp; <a href="{grow_link(p)}">\U0001F331 Garden</a> &nbsp; <a href="{meat_link(p)}">\U0001F95A Meat/Dairy</a><br/>
    <b>De-risk the land:</b> <a href="{flood_link(p)}">\U0001F30A Flood</a> &nbsp; <a href="{wetland_link(p)}">\U0001F4A7 Wetlands</a> &nbsp; <a href="{perc_link(p)}">\U0001F6BD Perc/Well</a> &nbsp; <a href="{util_link(p)}">⚡ Power/Internet</a> &nbsp; <a href="{hospital_link(p)}">\U0001F3E5 Hospital</a><br/>
    <a href="{listing_link(p)}">\U0001F50D Find this listing</a> &nbsp; <a href="{lot_map_link(p)}">\U0001F5FA Parcel map</a>]]>"""
    pt=f"<Point><coordinates>{p['lon']},{p['lat']},0</coordinates></Point>"
    ring=lot_ring(p)
    geom=(f"<MultiGeometry>{pt}<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon></MultiGeometry>" if ring else pt)
    return f"""<Placemark><name>{p['ptype']} · ${p['price']:,} · {html.escape(str(p['address']).split(',')[0])}</name>
    <description>{d}</description><styleUrl>#m_{p['mkey']}_{icon_type(p['ptype'])}</styleUrl>
    {geom}</Placemark>"""
styles="".join(f'<Style id="m_{k}_{it}"><IconStyle><color>{c}</color><scale>1.2</scale><Icon><href>{href}</href></Icon></IconStyle><LabelStyle><scale>0</scale></LabelStyle><LineStyle><color>{c}</color><width>2</width></LineStyle><PolyStyle><color>40{c[2:]}</color></PolyStyle></Style>' for k,c in MATCH_COLOR.items() for it,href in ICON_HREF.items())
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
desc=("Pin COLOR = match RANK among your current candidates, best to worst: "
      "green = best 20%, yellow = upper, orange = middle, red = lower, dark red = bottom 20%. "
      "Exact 0-100 match score is in each pin's popup. Listings >= 2 acres are shown; a property whose "
      "ALL-IN cost (lot + build, or price + reno, or move-in price) is over your home-sale ceiling is "
      "still shown but flagged OVER-budget in red, and ranked below the ones that fit. "
      "TO FILTER: sidebar checkboxes, nested State > Match tier > Price band. "
      "Match auto-scores nearness to any recreational water (<=90 min), quiet (distance from interstates), "
      "acreage (diminishing returns, plateau at 15 ac), and drive time to your four anchor people. "
      "Schools, safety, and living-off-the-land show as research links until a data feed scores them. "
      "The colored square is the APPROXIMATE lot size (area-accurate, not the exact survey boundary).")
kml=f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Forever Home — Live Map</name><description><![CDATA[{desc}]]></description>{styles}{folders}</Document></kml>'
open(f"{SITE}/Forever_Home_Live.kml","w").write(kml)
with zipfile.ZipFile(f"{SITE}/Forever_Home_Live.kmz","w",zipfile.ZIP_DEFLATED) as z:
    z.write(f"{SITE}/Forever_Home_Live.kml","doc.kml")

# ---- auto-refresh NetworkLink ----
if SITE_BASE_URL:
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
ests=[p["est"]["likely"] for p in props if p.get("est")]
avg_build=int(round(sum(ests)/len(ests),-3)) if ests else 0
diag=" · ".join(f"{st} {found_by_state[st]}→{kept_by_state[st]}" for st in STATES)
def _dropstr(st):
    d=drops_by_state[st]; parts=[f"{r} {d[r]}" for r in DROP_REASONS if d[r]]
    return f"{st}: " + (", ".join(parts) if parts else "none")
diag2=" &nbsp;|&nbsp; ".join(_dropstr(st) for st in STATES if sum(drops_by_state[st].values()))
kmz_link=f'{SITE_BASE_URL}/Forever_Home_Live.kmz' if SITE_BASE_URL else 'Forever_Home_Live.kmz'
nl_link=f'{SITE_BASE_URL}/ForeverHome_AutoRefresh.kml' if SITE_BASE_URL else 'ForeverHome_AutoRefresh.kml'
def row(p):
    area=(f"<a href='{schools_link(p)}' target='_blank'>Schools</a> · "
          f"<a href='{crime_link(p)}' target='_blank'>Safety</a> · "
          f"<a href='{flood_link(p)}' target='_blank'>Flood</a> · "
          f"<a href='{perc_link(p)}' target='_blank'>Perc</a>")
    kin=p.get("kin_min"); kincell=f"~{kin}m" if kin is not None else "—"
    head=CEILING-p["allin"]
    aicell=(f"${p['allin']:,}" + (f" <span style='color:#1a9850'>(${head:,}▼)</span>" if head>=0
            else f" <span style='color:#c0392b'>(${-head:,}▲)</span>"))
    return (f"<tr><td><a href='{listing_link(p)}' target='_blank'>{html.escape(str(p['address']))}</a></td>"
            f"<td>${p['price']:,}</td><td>{aicell}</td><td>{p['ptype']}</td><td>{p['acres']}</td>"
            f"<td>~{p['water_min']}m ({p['water_label']})</td><td>~{p['hwy_mi']}mi</td>"
            f"<td>{kincell}</td>"
            f"<td><b>{p['fhi']}</b></td><td>{p['opp']}</td><td style='font-size:12px'>{area}</td></tr>")
STATE_NAMES={"MD":"Maryland","VA":"Virginia","DE":"Delaware","PA":"Pennsylvania","NC":"North Carolina"}
def state_block(st):
    sp=[p for p in props if p["state"]==st][:25]
    if not sp:
        return f"<h3>{STATE_NAMES[st]}</h3><p style='color:#888'>No qualifying listings this week.</p>"
    hdr=("<tr><th>Property</th><th>List price</th><th>All-in vs ceiling</th><th>Type</th><th>Acres</th><th>Nearest water</th>"
         "<th>Interstate</th><th>Nearest kin</th><th>Match</th><th>Opp</th><th>Look into it</th></tr>")
    return f"<h3>{STATE_NAMES[st]} — Top {len(sp)} (by match)</h3><table>{hdr}{''.join(row(p) for p in sp)}</table>"
state_sections="".join(state_block(st) for st in STATES)
wlines=" · ".join(f"{k} {WEIGHTS[k]}" for k in ("schools","quiet","safety","off_land","water","people","acreage"))
rep=f"""<!doctype html><meta charset="utf-8"><title>Forever Home — Weekly Report</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1000px;margin:32px auto;padding:0 20px;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;font-size:13.5px;margin:12px 0}}th,td{{border:1px solid #ddd;padding:7px 9px;text-align:left}}
th{{background:#f4f6f8}}tr:nth-child(even){{background:#fafbfc}}.g{{color:#2ca02c;font-weight:600}}
a.btn{{display:inline-block;background:#2ca02c;color:#fff;padding:9px 15px;border-radius:7px;text-decoration:none;margin:4px 8px 4px 0}}
.legend span{{display:inline-block;margin-right:14px;font-size:13px}}.dot{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:middle}}</style>
<h1>Forever Home — Weekly Report</h1><p style="color:#666">{today} · MD·VA·DE·PA·NC · live RentCast data · updates every Monday</p>
<p><a class="btn" href="{kmz_link}">⬇ Download map (.kmz)</a><a class="btn" href="{nl_link}">\U0001F30E Auto-refresh map for Google Earth</a></p>
<p class="legend"><b>Pin color = match rank among your candidates (best → worst):</b>
<span><span class="dot" style="background:#1a9850"></span>Best 20%</span>
<span><span class="dot" style="background:#ffd400"></span>Upper 20%</span>
<span><span class="dot" style="background:#fd8d3c"></span>Middle 20%</span>
<span><span class="dot" style="background:#e34a33"></span>Lower 20%</span>
<span><span class="dot" style="background:#a50f15"></span>Bottom 20%</span>
&nbsp; <span style="color:#888">(exact 0–100 score in each pin's popup)</span></p>
<p class="legend" style="color:#555">Showing listings ≥ 2 acres. <b>All-in over your ${CEILING:,} ceiling is flagged in red, not hidden.</b> <b>Data check</b> — RentCast found → kept: {diag}</p>
<p class="legend" style="color:#777;font-size:12px"><b>Why listings were dropped</b> (diagnoses thin states): {diag2}</p>
<h3>Executive Summary</h3><table>
<tr><td><b>Price ceiling (your home's value)</b></td><td class="g">${CEILING:,}</td></tr>
<tr><td style="color:#888;font-size:12px" colspan="2">{html.escape(CEILING_SRC)}</td></tr>
<tr><td><b>Listings shown (≥ 2 acres)</b></td><td class="g">{len(props)}</td></tr>
<tr><td><b>&nbsp;&nbsp;· fit all-in under ceiling / shown over-budget</b></td><td>{n_fit} / {n_over}</td></tr>
<tr><td><b>&nbsp;&nbsp;· move-in homes / lots-to-build / fixers</b></td><td>{sum(1 for p in props if p['ptype']=='Home')} / {sum(1 for p in props if p['ptype']=='Lot')} / {sum(1 for p in props if p['ptype']=='Redevelopment')}</td></tr>
<tr><td><b>Listings scoring 80+/100</b></td><td>{len(topmatch)}</td></tr>
<tr><td><b>Within 90 min of recreational water</b></td><td>{len(tier1)}</td></tr>
<tr><td><b>Fresh (&le;30 days on market)</b></td><td>{len(fresh)}</td></tr>
<tr><td><b>Typical build cost (raw lots)</b></td><td>~${avg_build:,} <span style="color:#888;font-size:12px">(added to lot price for all-in)</span></td></tr>
<tr><td><b>Top pick</b></td><td>{html.escape(str(top[0]['address'])) if top else '—'}</td></tr></table>
<p style="color:#555;font-size:13px"><b>Index weights:</b> {wlines} &nbsp;(schools/safety/off-land currently a neutral baseline + research links until a data feed scores them).</p>
<h2>Top by State</h2>
{state_sections}
<p style="color:#888;font-size:12px">Ceiling = your current home's estimated value; every property's <b>all-in</b> cost (lot + build for a 3-4 br house, or price + reno for a fixer, or list price for a move-in home) must fit under it. Match auto-scores water (≤90 min), quiet, acreage (plateau at 15 ac), and drive time to your four anchor people. Opp = deal quality.</p>"""
open(f"{SITE}/index.html","w").write(rep)
print(f"Wrote {SITE}/index.html + KMZ. 80+ matches: {len(topmatch)}, within-90min-water: {len(tier1)}, avg build est: ${avg_build:,}")
