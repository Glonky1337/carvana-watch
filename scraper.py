import os, json, requests
from pathlib import Path

SEEN_FILE = Path("seen.json")
SEEN = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()

CARVANA_URL = "https://apik.carvana.io/merch/search/api/v2/search"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

def search():
    resp = requests.post(
        CARVANA_URL,
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
    return resp.json().get("inventory", {}).get("vehicles", [])

def analyze(v):
    p = v.get("price") or {}
    price = int(p.get("total") or 0)
    ship = int(p.get("transportCost") or 0)
    kbb = int(p.get("kbbValue") or 0)
    miles = int(v.get("mileage") or 0)
    model = (v.get("parentModel") or v.get("model") or "").lower()

    effective = price + min(ship, 150)
    lifetime = 300 if "tacoma" in model else 220
    remaining_k = max(lifetime - miles / 1000, 20)
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
    year = v.get("year") or ""
    model = v.get("parentModel") or v.get("model") or ""
    price = int((v.get("price") or {}).get("total") or 0)
    resp = requests.post(
        PUSHOVER_URL,
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
    if resp.status_code != 200:
        print(f"    Pushover error: {resp.status_code} {resp.text[:200]}")

def key(v):
    return str(v.get("vehicleId") or v.get("stockNumber"))

def main():
    vehicles = search()
    new = [v for v in vehicles if key(v) not in SEEN]
    print(f"{len(vehicles)} matches, {len(new)} new")
    for v in new:
        verdict, msg = analyze(v)
        yr = v.get("year")
        mdl = v.get("parentModel") or v.get("model")
        pr = int((v.get("price") or {}).get("total") or 0)
        mi = v.get("mileage") or 0
        print(f"  {yr} {mdl} ${pr:,} @{mi:,}mi -> {verdict}")
        if verdict != "PASS":
            notify(v, msg)
        SEEN.add(key(v))
    SEEN_FILE.write_text(json.dumps(sorted(SEEN)))

if __name__ == "__main__":
    main()
