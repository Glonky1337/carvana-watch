import os, json, re, time, requests
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
CARVANA_VDP = "https://www.carvana.com/vehicle/{}"
CARFAX_URL = "https://www.carfax.com/VehicleHistory/p/Report.cfx?partner=CVN_0&vin={}"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
DROP_THRESHOLD = 500
PRIORITY = {"UNICORN": 1, "GRAB": 0, "FAIR": -1}
NY_TAX_RATE = 0.0875
TITLE_REG_EST = 250
ANNUAL_MILES = 6000
MAX_PAGES = 10
PAGE_SIZE = 100

VDP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

FILTERS = {
    "makes": [
        {
            "name": "Toyota",
            "parentModels": [
                {"name": "Camry"},
                {"name": "Corolla"},
                {"name": "Tacoma"},
                {"name": "RAV4"},
            ],
        },
        {
            "name": "Honda",
            "parentModels": [
                {"name": "Civic"},
                {"name": "Accord"},
                {"name": "CR-V"},
            ],
        },
        {
            "name": "Mazda",
            "parentModels": [
                {"name": "Mazda3"},
            ],
        },
    ],
    "price": {"max": 16000},
    "mileage": {"max": 110000},
}

def search():
    all_vehicles = []
    for page in range(1, MAX_PAGES + 1):
        resp = requests.post(
            CARVANA_URL,
            json={
                "filters": FILTERS,
                "pagination": {"page": page, "pageSize": PAGE_SIZE},
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
        inventory = data.get("inventory", {})
        vehicles = inventory.get("vehicles", [])

        if page == 1:
            pg = inventory.get("pagination", {})
            total = pg.get("totalMatchedInventory", "?")
            print(f"Total matched inventory: {total}")

        if not vehicles:
            break

        all_vehicles.extend(vehicles)

        if len(vehicles) < PAGE_SIZE:
            break

        time.sleep(0.5)

    print(f"Fetched {len(all_vehicles)} vehicles across {page} pages")
    return all_vehicles

def find_report_url(html, vin):
    m = re.search(r'href="(https://www\.carfax\.com/[^"]*vin=[^"]*)"', html)
    if m:
        return "CarFax", m.group(1)
    m = re.search(r'href="(https://www\.autocheck\.com/[^"]*vin=[^"]*)"', html, re.IGNORECASE)
    if m:
        return "AutoCheck", m.group(1)
    if vin:
        return "CarFax", f"https://www.carfax.com/VehicleHistory/p/Report.cfx?partner=CVN_0&vin={vin}"
    return None, None

def fetch_vdp_details(vehicle_id, vin):
    try:
        resp = requests.get(CARVANA_VDP.format(vehicle_id), headers=VDP_HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text

        provider, report_url = find_report_url(html, vin)

        if "No reported accidents" in html:
            return {"clean": True, "summary": "Clean history — no reported accidents",
                    "provider": provider, "report_url": report_url}

        m = re.search(r"(\d+)\s+accidents?\s+reported", html, re.IGNORECASE)
        if m:
            return {"clean": False,
                    "summary": f"{m.group(1)} accident(s) reported on {provider or 'history report'}",
                    "provider": provider, "report_url": report_url}
        if "accident reported" in html.lower():
            return {"clean": False,
                    "summary": f"Accident reported on {provider or 'history report'}",
                    "provider": provider, "report_url": report_url}

        return {"clean": None,
                "summary": f"History not verified — check {provider or 'history report'} link",
                "provider": provider, "report_url": report_url}
    except Exception as e:
        print(f"    VDP fetch error for {vehicle_id}: {e}")
        fallback_provider, fallback_url = (None, None)
        if vin:
            fallback_provider = "CarFax"
            fallback_url = CARFAX_URL.format(vin)
        return {"clean": None,
                "summary": "History not verified — check report link",
                "provider": fallback_provider, "report_url": fallback_url}

def unavailable_reason(v):
    if v.get("isPurchasePending"):
        return "purchase in progress"
    if str(v.get("vehiclePurchaseType") or "").lower() == "reservable":
        return "pre-order"
    if v.get("vehicleReservableReasons"):
        return "pre-order"
    return None

def lifetime_miles(model):
    if "tacoma" in model:
        return 300
    if "rav4" in model or "cr-v" in model or "crv" in model:
        return 250
    return 220

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

    lifetime = lifetime_miles(model)
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
    elif miles < 110_000 and per_1k <= 130 and kbb_gap <= 3000 and effective <= 16_000:
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

def ai_review(v, verdict, stats, history):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return ""
    make = v.get("make") or "Toyota"
    prompt = f"""Kevin is looking at a used car on Carvana. He lives in Depew NY, drives about 6k miles a year for leisure (not commuting), and wants something reliable and cheap to insure.

Car: {v.get('year')} {make} {v.get('parentModel')} {v.get('trim', '')} ({v.get('color')})
Miles: {stats['miles']:,} ({stats['life_pct']}% through its typical {stats['lifetime']}k-mile life; would last him ~{stats['years_left']:.0f} more years at his usage)
Out-the-door price: ${stats['out_the_door']:,} (vehicle + shipping + NY tax + fees)
Kelly Blue Book: ${stats['kbb']:,}
Vehicle history: {history['summary']}
His rubric flagged this as: {verdict}

Give Kevin a plain-English take in 2-3 sentences. No jargon. Talk to him like a friend who knows cars. Flag any known problems with this specific year/model (transmission issues, oil consumption, etc.). If there's an accident on record, weight that heavily. End with one line that's just BUY, MAYBE, or SKIP."""

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

def test_gemini():
    if not os.environ.get("GEMINI_API_KEY"):
        print("=== GEMINI TEST: skipped (no key) ===")
        return
    print("=== GEMINI TEST ===")
    test_v = {"year": 2014, "make": "Toyota", "parentModel": "Corolla", "trim": "LE", "color": "Silver"}
    test_stats = {"price": 13990, "ship": 500, "kbb": 14000, "miles": 85000,
                  "effective": 14490, "out_the_door": 15800, "per_1k": 107,
                  "kbb_gap": 490, "life_pct": 39, "lifetime": 220, "years_left": 22}
    test_history = {"summary": "Clean history — no reported accidents"}
    result = ai_review(test_v, "GRAB", test_stats, test_history)
    if result:
        print(result)
    else:
        print("(no response — check GEMINI_API_KEY secret or Gemini quota)")
    print("=== END GEMINI TEST ===")

def main():
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        test_gemini()

    try:
        vehicles = search()
    except Exception as e:
        notify("", "Carvana Watch broken", f"search() failed: {e}", priority=1)
        raise

    sent = 0
    for v in vehicles:
        k = key(v)
        p = int((v.get("price") or {}).get("total") or 0)
        prev = SEEN.get(k)
        verdict, rubric, stats = analyze(v)

        yr = v.get("year")
        make = v.get("make") or ""
        mdl = v.get("parentModel") or v.get("model")
        trim = v.get("trim", "")
        miles = stats["miles"]

        skip = unavailable_reason(v)
        if skip:
            print(f"  {yr} {make} {mdl} {trim} ${p:,} @{miles:,}mi -> SKIP: {skip}")
            SEEN[k] = p
            continue

        is_new = prev is None
        drop = (prev - p) if prev is not None else 0
        alert_new = is_new and verdict != "PASS"
        alert_drop = (not is_new) and drop >= DROP_THRESHOLD and verdict != "PASS"

        print(f"  {yr} {make} {mdl} {trim} ${p:,} @{miles:,}mi -> {verdict}"
              f"{' [NEW]' if alert_new else ''}"
              f"{f' [DROP -${drop:,}]' if alert_drop else ''}")

        if alert_new or alert_drop:
            vid = v.get("vehicleId") or v.get("stockNumber")
            vin = v.get("vin", "")

            history = fetch_vdp_details(vid, vin)
            print(f"    history: {history['summary']}")

            if history["clean"] is False:
                print(f"    -> skipping alert (accidents on record)")
                SEEN[k] = p
                continue

            headline = {
                "UNICORN": "UNICORN: rare find, buy now",
                "GRAB": "GRAB: good deal",
                "FAIR": "FAIR: worth a look",
            }.get(verdict, verdict)

            body = f"{headline}\n{yr} {make} {mdl} {trim}".strip() + "\n\n" + rubric
            body += f"\n\n{history['summary']}"

            if alert_drop:
                body = f"PRICE DROP: -${drop:,} (was ${prev:,})\n\n" + body

            if verdict in ("UNICORN", "GRAB", "FAIR"):
                review = ai_review(v, verdict, stats, history)
                if review:
                    body += "\n\n" + review

            if history.get("report_url"):
                body += f"\n\nFull {history['provider']} report: {history['report_url']}"
            elif vin:
                body += f"\n\nFull CarFax: {CARFAX_URL.format(vin)}"

            prefix = "NEW" if alert_new else f"DROP -${drop:,}"
            title = f"{prefix}: {yr} {make} {mdl} - ${p:,}"
            notify(vid, title, body, priority=PRIORITY.get(verdict, 0))
            sent += 1

        SEEN[k] = p

    print(f"{len(vehicles)} matches, {sent} notifications sent")
    SEEN_FILE.write_text(json.dumps(SEEN, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
