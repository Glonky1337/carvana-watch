import os, json, requests
from pathlib import Path

SEEN_FILE = Path("seen.json")
SEEN = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []

def search():
    resp = requests.post(
        "https://apik.carvana.io/merch/search/api/v2/search",
        json={
            "filters": {
                "makes": [{
                    "name": "Toyota",
                    "parentModels": [
                        {"name": "Camry"},
                        {"name": "Corolla"},
                        {"name": "Tacoma"},
                    ],
                }],
                "price": {"max": 16000},
                "mileage": {"max": 110000},
            },
            "pagination": {"page": 1, "pageSize": 100},
            "sortBy": "MostPopular",
            "zip5": "14043",
        },
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://www.carvana.com",
            "Referer": "https://www.carvana.com/",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    vehicles = data.get("inventory", {}).get("vehicles", [])
    if vehicles:
        print(f"ALL FIELDS: {sorted(vehicles[0].keys())}")
    return vehicles

def analyze(v):
    p = v.get("price") or {}
    price = int(p.get("total") or 0)
    ship = int(p.get("transportCost") or 0)
    kbb = int(p.get("kbbValue") or 0)
    miles = int(v.get("mileage") or 0)
    model = (v.get("parentModel") or v.get("model") or "").lower()

    effective = price + min(ship, 150)
    lifetime = 300 if "tacoma" in model else 220
    remaining_k = max(lifetime - miles/1000, 20)
    per_1k = effective / remaining_k
    over_kbb = effective - kbb if kbb else 0

    if miles < 80_000 and effective <= 14_000:
        verdict = "GRAB"
    elif miles < 100_000 and effective <= 13_000:
        verdict = "GRAB"
    elif 80_000 <= miles <= 110_000 and 14_000 <= effective <= 15_000:
        verdict = "FAIR"
    else:
        verdict = "PASS"

    label = f"pickup ${effective:,}" if ship > 150 else f"ship ${effective:,}"
    kbb_note = f" (+${over_kbb:,} over KBB)" if over_kbb > 1000 else ""
    msg = f"VERDICT: {verdict}\nEFFECTIVE: {label}{kbb_note}\n$/1K REMAINING: ${per_1k:.0f}\nMILES: {miles:,}"
    return verdict, msg

def notify(v, msg):
    vid = v.get("vehicleId") or v.get("stockNumber")
    year = v.get("year") or v.get("modelYear") or ""
    model = v.get("parentModel") or v.get("model") or ""
    price = int((v.get("price") or {}).get("total") or 0)
    requests.post(
        "https://api.pushover.net/1/messages.json",
        data={
            "token": os.environ["PUSHOVER_TOKEN"],
            "user": os.environ["PUSHOVER_USER"],
            "title": f"{year} {model} - ${price:,}",
            "message": msg,
            "url": f"https://www.carvana.com/vehicle/{vid}",
            "url_title": "View listing",
        },
        timeout=10,
    )

def main():
    vehicles = search()
    key = lambda v: str(v.get("vehicleId") or v.get("stockNumber"))
    new = [v for v in vehicles if key(v) not in SEEN]
    print(f"{len(vehicles)} matches, {len(new)} new")
    for v in new:
        verdict, msg = analyze(v)
        yr = v.get("year") or v.get("modelYear")
        mdl = v.get("parentModel") or v.get("model")
        pr = (v.get("price") or {}).get("total")
        print(f"  {yr} {mdl} ${pr} @{v.get('mileage')}mi -> {verdict}")
        if verdict != "PASS":
            notify(v, msg)
        SEEN.append(key(v))
    SEEN_FILE.write_text(json.dumps(SEEN))

if __name__ == "__main__":
    main()
