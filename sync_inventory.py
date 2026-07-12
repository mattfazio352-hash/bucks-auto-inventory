"""
Bucks Auto Market - inventory collector (MarketCheck).

WITHOUT an API key: exits without changes (your manually-managed listings stay put).
WITH a MarketCheck API key: pulls active dealer listings across the Bucks County
area, paginating deeply and de-duplicating by VIN, then rewrites inventory.json
in the exact format the Android app reads.

This is the same collector used both locally (run it yourself) and by the nightly
GitHub Action. To reach thousands of vehicles it pages through results 50 at a
time (MarketCheck's per-request max) from several anchor ZIPs that blanket the
county, merging everything into one de-duplicated file.

Sign up for a key: https://www.marketcheck.com/apis/pricing/
  - Free tier   = 500 API calls / month  (enough for one full ~5,000-car pull)
  - Basic tier  = 5,000 calls / month     (needed for a nightly auto-refresh)

Auto.dev is a drop-in alternative; adjust fetch() to its endpoint/fields.

------------------------------------------------------------------------------
Configuration (all optional, via environment variables):
  MARKETCHECK_API_KEY   your key (REQUIRED to do anything)
  MC_RADIUS             search radius in miles per anchor ZIP   (default 25)
  MC_ROWS               rows per request, MarketCheck max is 50 (default 50)
  MC_MAX_VEHICLES       stop after collecting this many         (default 6000)
  MC_MAX_CALLS          hard cap on API calls (protects quota)  (default 250)
  MC_HOST               API host                    (default api.marketcheck.com)
  MC_BUCKS_ONLY         "1" = keep only dealers in Bucks ZIPs   (default 0)
------------------------------------------------------------------------------
"""
import json, os, sys, time, urllib.parse, urllib.request

# Anchor ZIPs spread across Bucks County (north / central / east / south). With a
# ~25-mile radius these overlap and blanket the whole county plus nearby metro
# inventory; the VIN de-dupe stitches them into one clean list.
ANCHOR_ZIPS = ["18901", "19020", "18951", "19124", "18103", "19401", "19380"]

# Full Bucks County ZIP list - only used when MC_BUCKS_ONLY=1 to filter results.
BUCKS_COUNTY_ZIPS = {
    "18901","18902","18912","18913","18914","18917","18920","18923","18925",
    "18929","18930","18932","18938","18940","18942","18944","18947","18950",
    "18951","18954","18955","18960","18962","18964","18966","18972","18974",
    "18976","18977","19007","19020","19021","19030","19047","19048","19053",
    "19054","19055","19056","19057","19067",
}

API_KEY     = os.environ.get("MARKETCHECK_API_KEY", "").strip()
RADIUS      = int(os.environ.get("MC_RADIUS", "50"))
ROWS        = min(int(os.environ.get("MC_ROWS", "50")), 50)      # 50 is the API max
MAX_VEHICLES= int(os.environ.get("MC_MAX_VEHICLES", "6000"))
MAX_CALLS   = int(os.environ.get("MC_MAX_CALLS", "250"))
HOST        = os.environ.get("MC_HOST", "api.marketcheck.com").strip()
BUCKS_ONLY  = os.environ.get("MC_BUCKS_ONLY", "0").strip() == "1"

# Affordable-first composition: aim for ~80% of listings at/under $15k
# (everyday used cars), the rest newer/pricier. Pulled across a wider PA
# radius so there are enough budget cars to fill the board.
CHEAP_MAX    = int(os.environ.get("MC_CHEAP_MAX", "15000"))
TARGET_TOTAL = int(os.environ.get("MC_TARGET_TOTAL", "2000"))
CHEAP_FRAC   = float(os.environ.get("MC_CHEAP_FRAC", "0.80"))

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_OUT_HERE = os.path.join(_SCRIPT_DIR, "inventory.json")
_OUT_UP = os.path.join(_SCRIPT_DIR, "..", "inventory.json")
# Write next to the script (repo-root layout) unless inventory.json only lives
# one level up (scripts/ layout). Prevents writing outside the repo checkout.
OUT = _OUT_UP if (os.path.exists(_OUT_UP) and not os.path.exists(_OUT_HERE)) else _OUT_HERE
BASE = f"https://{HOST}/v2/search/car/active"

# Seller listings submitted through the app carry this dealer id. They live in
# inventory.json next to the MarketCheck cars; we carry them over on every
# refresh so a paid listing is never wiped by the weekly pull.
SELLER_DEALER_ID = "app-sellers"


def _load_preserved_sellers():
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return [], []
    d = [x for x in old.get("dealers", []) if x.get("id") == SELLER_DEALER_ID]
    v = [x for x in old.get("vehicles", []) if x.get("dealerId") == SELLER_DEALER_ID]
    return d, v


_calls = 0


def _get(zip_code, start, price_range=None, sort_order=None):
    """One page of results for a ZIP. Returns (num_found, listings).
    price_range like "0-15000" filters by price; sort_order "asc"/"desc"
    sorts by price so we can pull the cheapest cars first."""
    global _calls
    q = {
        "api_key": API_KEY,
        "zip": zip_code,
        "radius": RADIUS,
        "rows": ROWS,
        "start": start,
    }
    if price_range:
        q["price_range"] = price_range
    if sort_order:
        q["sort_by"] = "price"
        q["sort_order"] = sort_order
    params = urllib.parse.urlencode(q)
    _calls += 1
    with urllib.request.urlopen(f"{BASE}?{params}", timeout=45) as r:
        data = json.load(r)
    return int(data.get("num_found", 0)), data.get("listings", []) or []


def _map_dealer(dealer):
    did = str(dealer.get("id", "unknown"))
    return did, {
        "id": did,
        "name": dealer.get("name", "Unknown Dealer"),
        "address": dealer.get("street", "") or "",
        "city": dealer.get("city", "") or "",
        "zip": str(dealer.get("zip", "") or ""),
        "phone": dealer.get("phone", "") or "",
        "website": dealer.get("website", "") or "",
        "inHouseFinancing": False,
        "financePartners": [],
    }


def _map_vehicle(item, did):
    build = item.get("build", {}) or {}
    media = item.get("media", {}) or {}
    photos = media.get("photo_links", []) or [""]
    return {
        "vin": item.get("vin", ""),
        "year": int(build.get("year") or 0),
        "make": build.get("make", "") or "",
        "model": build.get("model", "") or "",
        "trim": build.get("trim", "") or "",
        "price": int(item.get("price") or 0),
        "mileage": int(item.get("miles") or 0),
        "condition": "New" if item.get("inventory_type") == "new" else "Used",
        "bodyStyle": build.get("body_type", "") or "",
        "exteriorColor": item.get("exterior_color", "") or "",
        "interiorColor": item.get("interior_color", "") or "",
        "engine": build.get("engine", "") or "",
        "transmission": build.get("transmission", "") or "",
        "drivetrain": build.get("drivetrain", "") or "",
        "fuelType": build.get("fuel_type", "") or "",
        "mpgCity": int(build.get("city_mpg") or 0),
        "mpgHighway": int(build.get("highway_mpg") or 0),
        "dealerId": did,
        "sellerType": "Dealer",
        "description": item.get("heading", "") or "",
        "photoUrl": photos[0] if photos else "",
    }


def _collect(dealers, seen_vins, bucket, target, price_range, sort_order):
    """Page every anchor ZIP, adding new VINs to `bucket` until it hits target."""
    for zip_code in ANCHOR_ZIPS:
        if len(bucket) >= target or _calls >= MAX_CALLS:
            break
        start = 0
        while True:
            if len(bucket) >= target or _calls >= MAX_CALLS:
                break
            try:
                num_found, listings = _get(zip_code, start, price_range, sort_order)
            except Exception as e:
                print(f"  zip {zip_code} start {start}: {e}", file=sys.stderr)
                break
            if not listings:
                break
            added = 0
            for item in listings:
                vin = item.get("vin")
                if not vin or vin in seen_vins:
                    continue
                dealer = item.get("dealer", {}) or {}
                did, dmap = _map_dealer(dealer)
                if BUCKS_ONLY and dmap["zip"] not in BUCKS_COUNTY_ZIPS:
                    continue
                seen_vins.add(vin)
                if did not in dealers:
                    dealers[did] = dmap
                bucket.append(_map_vehicle(item, did))
                added += 1
                if len(bucket) >= target:
                    break
            print(f"  [{price_range}] zip {zip_code} start {start:>4} +{added:>2} "
                  f"(bucket {len(bucket)}/{target}, calls {_calls}, found ~{num_found})")
            start += ROWS
            if start >= num_found or start >= 10000:
                break
            time.sleep(0.2)


def fetch():
    dealers, seen_vins = {}, set()
    cheap, premium = [], []
    target_cheap = int(TARGET_TOTAL * CHEAP_FRAC)
    target_premium = TARGET_TOTAL - target_cheap
    print(f"Affordable pass: up to {target_cheap} cars at/under ${CHEAP_MAX:,} "
          f"(cheapest first), radius {RADIUS}mi ...")
    _collect(dealers, seen_vins, cheap, target_cheap, f"0-{CHEAP_MAX}", "asc")
    print(f"Premium pass: up to {target_premium} cars over ${CHEAP_MAX:,} ...")
    _collect(dealers, seen_vins, premium, target_premium, f"{CHEAP_MAX + 1}-1000000", "desc")
    vehicles = cheap + premium
    print(f"Collected {len(cheap)} affordable + {len(premium)} premium "
          f"= {len(vehicles)} raw vehicles.")
    return list(dealers.values()), vehicles

def dedup_variety(vehicles):
    """Collapse the same car offered in multiple colors (same dealer + year +
    make + model + trim) down to a single listing, so the app shows variety
    instead of 34 identical Volvos. Prefers a listing that has a photo, then the
    lowest price. Also drops junk/placeholder prices."""
    groups = {}
    for v in vehicles:
        price = v.get("price") or 0
        if price < 1500:
            continue
        key = (v.get("dealerId"), v.get("year"), v.get("make"), v.get("model"), v.get("trim"))
        cur = groups.get(key)
        if cur is None:
            groups[key] = v
            continue
        def score(x):
            return (1 if x.get("photoUrl") else 0, -(x.get("price") or 0))
        if score(v) > score(cur):
            groups[key] = v
    return list(groups.values())


def main():
    if not API_KEY:
        print("No MARKETCHECK_API_KEY set - leaving inventory.json unchanged.")
        print("Get a key at https://www.marketcheck.com/apis/pricing/ then set")
        print("MARKETCHECK_API_KEY and re-run.")
        return
    print(f"Collecting from {HOST}  radius={RADIUS}mi  rows={ROWS}  "
          f"cap={MAX_VEHICLES} vehicles / {MAX_CALLS} calls")
    dealers, vehicles = fetch()
    if not vehicles:
        print("API returned nothing; keeping existing file to avoid wiping listings.")
        return
    before = len(vehicles)
    vehicles = dedup_variety(vehicles)
    print(f"De-duplicated color/near-identical copies: {before} -> {len(vehicles)} unique.")
    # Carry over paid/app-submitted seller listings so the weekly pull never wipes them.
    s_dealers, s_vehicles = _load_preserved_sellers()
    have = {v["vin"] for v in vehicles}
    for v in s_vehicles:
        if v.get("vin") and v["vin"] not in have:
            vehicles.append(v)
    for d in s_dealers:
        if not any(x["id"] == d["id"] for x in dealers):
            dealers.append(d)
    if s_vehicles:
        print(f"Preserved {len(s_vehicles)} seller listing(s) across the refresh.")
    used_ids = {v["dealerId"] for v in vehicles}
    dealers = [d for d in dealers if d["id"] in used_ids]
    payload = {
        "note": (f"Auto-generated by sync_inventory.py from MarketCheck. "
                 f"{len(vehicles)} vehicles / {len(dealers)} dealers, "
                 f"radius {RADIUS}mi around Bucks County. Used {_calls} API calls."),
        "dealers": dealers,
        "vehicles": vehicles,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"\nDone. Wrote {len(vehicles)} vehicles from {len(dealers)} dealers "
          f"to {os.path.abspath(OUT)}  ({_calls} API calls used).")


if __name__ == "__main__":
    main()
