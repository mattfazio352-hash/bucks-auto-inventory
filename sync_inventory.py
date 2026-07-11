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
ANCHOR_ZIPS = ["18951", "18901", "18940", "19020"]

# Full Bucks County ZIP list - only used when MC_BUCKS_ONLY=1 to filter results.
BUCKS_COUNTY_ZIPS = {
    "18901","18902","18912","18913","18914","18917","18920","18923","18925",
    "18929","18930","18932","18938","18940","18942","18944","18947","18950",
    "18951","18954","18955","18960","18962","18964","18966","18972","18974",
    "18976","18977","19007","19020","19021","19030","19047","19048","19053",
    "19054","19055","19056","19057","19067",
}

API_KEY     = os.environ.get("MARKETCHECK_API_KEY", "").strip()
RADIUS      = int(os.environ.get("MC_RADIUS", "25"))
ROWS        = min(int(os.environ.get("MC_ROWS", "50")), 50)      # 50 is the API max
MAX_VEHICLES= int(os.environ.get("MC_MAX_VEHICLES", "6000"))
MAX_CALLS   = int(os.environ.get("MC_MAX_CALLS", "250"))
HOST        = os.environ.get("MC_HOST", "api.marketcheck.com").strip()
BUCKS_ONLY  = os.environ.get("MC_BUCKS_ONLY", "0").strip() == "1"

OUT = os.path.join(os.path.dirname(__file__), "inventory.json")
BASE = f"https://{HOST}/v2/search/car/active"

_calls = 0


def _get(zip_code, start):
    """One page of results for a ZIP. Returns (num_found, listings)."""
    global _calls
    params = urllib.parse.urlencode({
        "api_key": API_KEY,
        "zip": zip_code,
        "radius": RADIUS,
        "rows": ROWS,
        "start": start,
    })
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


def fetch():
    dealers, vehicles, seen_vins, seen_dealers = {}, [], set(), set()
    for zip_code in ANCHOR_ZIPS:
        if len(vehicles) >= MAX_VEHICLES or _calls >= MAX_CALLS:
            break
        start = 0
        num_found = None
        while True:
            if len(vehicles) >= MAX_VEHICLES or _calls >= MAX_CALLS:
                break
            try:
                num_found, listings = _get(zip_code, start)
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
                if did not in seen_dealers:
                    seen_dealers.add(did)
                    dealers[did] = dmap
                vehicles.append(_map_vehicle(item, did))
                added += 1
                if len(vehicles) >= MAX_VEHICLES:
                    break
            print(f"  zip {zip_code}: start {start:>4}  +{added:>2} new  "
                  f"(total {len(vehicles)}, calls {_calls}, found ~{num_found})")
            start += ROWS
            # MarketCheck caps deep paging at 10000/rows; also stop at num_found.
            if start >= num_found or start >= 10000:
                break
            time.sleep(0.2)  # be polite to the API
    return list(dealers.values()), vehicles


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
