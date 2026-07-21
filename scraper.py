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
    return data.get("vehicles") or data.get("results") or data.get("inventory") or []

def analyze(v):
    price = v.get("price", 0)
    miles = v.get("mileage", 0)
    ship = v.get("shippingFee", 0)
    accidents = v.get("hasAccidents", False)
    use = (v.get("priorUse") or "").lower()
    model = (v.get("model") or "").lower()

    effective = price + min(ship, 150)
    lifetime = 300 if "tacoma" in model else 220
    remaining_k = max(lifetime - miles/1000, 20)
    per_1k = effective / remaining_k

    if accidents or use in ("rental", "fleet"):
        verdict = "PASS"
    elif miles < 80_000 and effective <= 14_000:
        verdict = "GRAB"
    elif miles < 100_000 and effective <= 13_000:
        verdict = "GRAB"
    elif 80_000 <= miles <= 110_000 and 14_000 <= effective <= 15_000:
        verdict = "FAIR"
    else:
        verdict = "PASS"

    label = f"pickup ${effective:,}" if ship > 150 else f"ship ${effective:,}"
    msg = f"VERDICT: {verdict}\nEFFECTIVE: {label}\n$/1K REMAINING: ${per_1k:.0f}\nMILES: {miles:,}"
    return verdict, msg

def notify(v, msg):
    vid = v.get("vehicleId") or v.get("id") or v.get("stockNumber")
    requests.post(
        "https://api.pushover.net/1/messages.json",
        data={
            "token": os.environ["PUSHOVER_TOKEN"],
            "user": os.environ["PUSHOVER_USER"],
            "title": f"{v.get('year')} {v.get('model')} - ${v.get('price'):,}",
            "message": msg,
            "url": f"https://www.carvana.com/vehicle/{vid}",
            "url_title": "View listing",
        },
        timeout=10,
    )

def main():
    vehicles = search()
    key = lambda v: v.get("vin") or v.get("stockNumber")
    new = [v for v in vehicles if key(v) not in SEEN]
    print(f"{len(vehicles)} matches, {len(new)} new")
    if vehicles and not new:
        print("Sample keys:", list(vehicles[0].keys())[:15])
    for v in new:
        verdict, msg = analyze(v)
        print(f"{v.get('year')} {v.get('model')} ${v.get('price')} -> {verdict}")
        if verdict != "PASS":
            notify(v, msg)
        SEEN.append(key(v))
    SEEN_FILE.write_text(json.dumps(SEEN))

if __name__ == "__main__":
    main()
