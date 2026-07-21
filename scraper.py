import os, json, requests
from pathlib import Path

SEEN_FILE = Path("seen.json")

def load_seen():
    if not SEEN_FILE.exists():
        return {}
    try:
        raw = json.loads(SEEN_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    if isinstance(raw, list):
        return {vid: 0 for vid in raw}
    return raw

SEEN = load_seen()

CARVANA_URL = "https://apik.carvana.io/merch/search/api/v2/search"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
DROP_THRESHOLD = 500
PRIORITY = {"UNICORN": 1, "GRAB": 0, "FAIR": -1}
NY_TAX_RATE = 0.0875
TITLE_REG_EST = 250
ANNUAL_MILES = 6000
TARGET_IDS = {"4531716", "4536523"}

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

def unavailable_reason(v):
    if v.get("isPurchasePending"):
        return "purchase in progress"

    tag_blob = " ".join(
        f"{t.get('tagKey','')} {t.get('tagName','')}".lower()
        for t in (v.get("vehicleTags") or [])
    )
    if "preorder" in tag_blob.replace("-", "").replace(" ", ""):
        return "pre-order"
    if "purchaseinprogress" in tag_blob.replace(" ", ""):
        return "purchase in progress"

    fulfillment = str(v.get("fulfillmentType") or "").lower().replace("-", "").replace("_", "")
    if "preorder" in fulfillment:
        return f"fulfillmentType={v.get('fulfillmentType')}"

    return None

def analyze(v):
    p = v.get("price") or {}
    price = int(p.get("total") or 0)
    ship = int(p.get("transportCost") or 0)
    kbb = int(p.get("kbbValue") or 0)
    miles = int(v.get("mileage") or 0)
    model = (v.get("parentModel") or v.get("model") or "").lower()

    effective = price + ship
    sales_tax = int(price * NY_TAX_RATE)
    fees = sales_tax + TITLE_REG_EST
    out_the_door = effective + fees

    lifetime = 300 if "tacoma" in model else 220
    remaining_miles = max(lifetime * 1000 - miles, 20_000)
    life_pct = min(int(miles / (lifetime * 1000) * 100), 99)
    years_left = remaining_miles / ANNUAL_MILES
    per_1k = effective / (remaining_miles / 1000)
    kbb_gap = effective - kbb if kbb else 0

    if kbb and kbb_gap <= -500 and miles < 90_000 and per_1k <= 90:
        verdict = "UNICORN"
    elif kbb_gap <= 500 and ((miles < 80_000 and effective <= 14_000) or
                              (miles < 100_000 and effective <= 13_000)):
        verdict = "GRAB"
    elif miles < 110_000 and per_1k <= 130 and kbb_gap <= 2000 and effective <= 15_500:
        verdict = "FAIR"
    else:
        verdict = "PASS"

    if kbb and kbb_gap <= -100:
        kbb_line = f"${abs(kbb_gap):,} UNDER Kelly Blue Book"
    elif kbb and kbb_gap > 100:
        kbb_line = f"${kbb_gap:,} over Kelly Blue Book"
    elif kbb:
        kbb_line = "Right at Kelly Blue Book"
    else:
        kbb_line = ""

    lines = [
        f"Vehicle: ${price:,}",
        f"Shipping: ${ship:,}",
        f"Est. NY tax + fees: ${fees:,}",
        f"OUT THE DOOR: ${out_the_door:,}",
        "",
        f"Mileage: {miles:,} ({life_pct}% used)",
    ]
    if kbb_line:
        lines.append(kbb_line)
    lines.append(f"At {ANNUAL_MILES:,} mi/yr: should last ~{years_left:.0f} more years")
    rubric = "\n".join(lines)

    return verdict, rubric, {
        "price": price, "ship": ship, "kbb": kbb, "miles": miles,
        "effective": effective, "out_the_door": out_the_door,
        "per_1k": per_1k, "kbb_gap": kbb_gap,
        "life_pct": life_pct, "lifetime": lifetime, "years_left": years_left,
    }

def ai_review(v, verdict, stats):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return ""
    prompt = f"""Kevin is looking at a used Toyota on Carvana. He lives in Depew NY, drives about 6k miles a year, and wants something reliable and cheap to insure.

Car: {v.get('year')} Toyota {v.get('parentModel')} {v.get('trim', '')} ({v.get('color')})
Miles: {stats['miles']:,} ({stats['life_pct']}% through its typical {stats['lifetime']}k-mile life; would last him ~{stats['years_left']:.0f} more years at his usage)
Out-the-door price: ${stats['out_the_door']:,} (vehicle + shipping + NY tax + fees)
Kelly Blue Book: ${stats['kbb']:,}
His rubric flagged this as: {verdict}

Give Kevin a plain-English take in 2-3 sentences. No jargon. Talk to him like a friend who knows cars. End with one line that's just BUY, MAYBE, or SKIP."""

    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return f"---\nWhat I think:\n{text}"
    except Exception as e:
        print(f"    Gemini error: {e}")
        return ""

def notify(vid, title, body, priority=0):
    resp = requests.post(
        PUSHOVER_URL,
        data={
            "token": os.environ["PUSHOVER_TOKEN"],
            "user": os.environ["PUSHOVER_USER"],
            "title": title,
            "message": body,
            "url": f"https://www.carvana.com/vehicle/{vid}" if vid else "",
            "url_title": "View listing" if vid else "",
            "priority": priority,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"    Pushover error: {resp.status_code} {resp.text[:200]}")

def key(v):
    return str(v.get("vehicleId") or v.get("stockNumber"))

def main():
    try:
        vehicles = search()
    except Exception as e:
        notify("", "Carvana Watch broken", f"search() failed: {e}", priority=1)
        raise

    print("=== TARGET CHECK ===")
    target_hits = {str(v.get("vehicleId")): v for v in vehicles if str(v.get("vehicleId")) in TARGET_IDS}
    for tid in TARGET_IDS:
        if tid in target_hits:
            v = target_hits[tid]
            print(f"  FOUND {tid} ({v.get('year')} {v.get('parentModel')} {v.get('trim')}):")
            print(f"    tags={v.get('vehicleTags')}")
            print(f"    inventoryType={v.get('vehicleInventoryType')} "
                  f"purchaseType={v.get('vehiclePurchaseType')} "
                  f"lockType={v.get('vehicleLockType')}")
            print(f"    fulfillmentType={v.get('fulfillmentType')} "
                  f"pending={v.get('isPurchasePending')} "
                  f"onDemand={v.get('isOnDemand')}")
            print(f"    reservable={v.get('vehicleReservableReasons')} "
                  f"days={v.get('analyticsOnlyGetItByDays')}")
        else:
            print(f"  NOT IN SEARCH RESULTS: {tid}")
    print("=== END TARGET CHECK ===")

    all_tags = set()
    for v in vehicles:
        for t in (v.get("vehicleTags") or []):
            all_tags.add((t.get("tagKey"), t.get("tagName")))
    print(f"ALL UNIQUE TAGS ACROSS BATCH: {sorted(all_tags)}")

    sent = 0
    for v in vehicles:
        k = key(v)
        p = int((v.get("price") or {}).get("total") or 0)
        prev = SEEN.get(k)
        verdict, rubric, stats = analyze(v)

        is_new = prev is None
        drop = (prev - p) if prev is not None else 0
        alert_new = is_new and verdict != "PASS"
        alert_drop = (not is_new) and drop >= DROP_THRESHOLD and verdict != "PASS"

        yr = v.get("year")
        mdl = v.get("parentModel") or v.get("model")
        trim = v.get("trim", "")
        skip = unavailable_reason(v)
        print(f"  {yr} {mdl} {trim} ${p:,} @{stats['miles']:,}mi -> {verdict}"
              f"{f' [SKIP: {skip}]' if skip else ''}"
              f"{' [NEW]' if alert_new and not skip else ''}"
              f"{f' [DROP -${drop:,}]' if alert_drop and not skip else ''}")

        if skip:
            SEEN[k] = p
            continue

        if alert_new or alert_drop:
            headline = {
                "UNICORN": "UNICORN: rare find, buy now",
                "GRAB": "GRAB: good deal",
                "FAIR": "FAIR: worth a look",
            }.get(verdict, verdict)

            body = f"{headline}\n{yr} Toyota {mdl} {trim}".strip() + "\n\n" + rubric
            if alert_drop:
                body = f"PRICE DROP: -${drop:,} (was ${prev:,})\n\n" + body
            if verdict in ("UNICORN", "GRAB"):
                review = ai_review(v, verdict, stats)
                if review:
                    body += "\n\n" + review

            prefix = "NEW" if alert_new else f"DROP -${drop:,}"
            title = f"{prefix}: {yr} {mdl} - ${p:,}"
            vid = v.get("vehicleId") or v.get("stockNumber")
            notify(vid, title, body, priority=PRIORITY.get(verdict, 0))
            sent += 1

        SEEN[k] = p

    print(f"{len(vehicles)} matches, {sent} notifications sent")
    SEEN_FILE.write_text(json.dumps(SEEN, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
