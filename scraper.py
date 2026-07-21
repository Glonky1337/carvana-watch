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

    label = f"pickup ${effective:,}" if ship > 150 else f"ship ${effective:,}"
    if kbb and kbb_gap <= -100:
        kbb_note = f" (${abs(kbb_gap):,} UNDER KBB)"
    elif kbb and kbb_gap > 1000:
        kbb_note = f" (+${kbb_gap:,} over KBB)"
    elif kbb:
        kbb_note = " (near KBB)"
    else:
        kbb_note = ""

    rubric = f"VERDICT: {verdict}\nEFFECTIVE: {label}{kbb_note}\n$/1K REMAINING: ${per_1k:.0f}\nMILES: {miles:,}"
    return verdict, rubric, {
        "price": price, "ship": ship, "kbb": kbb, "miles": miles,
        "effective": effective, "per_1k": per_1k, "kbb_gap": kbb_gap,
    }

def ai_review(v, verdict, stats):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return ""
    prompt = f"""You are helping Kevin decide on a used car listing. He lives in Depew NY, drives ~6k mi/year, and wants an occasional-use vehicle that's cheap to insure (Camry LE, Corolla LE, or Tacoma).

CAR: {v.get('year')} Toyota {v.get('parentModel')} {v.get('trim', '')}
COLOR: {v.get('color')}
MILEAGE: {stats['miles']:,}
PRICE: ${stats['price']:,}
SHIPPING: ${stats['ship']:,} (Kevin can pickup from Latham NY for ~$150)
KBB VALUE: ${stats['kbb']:,}
EFFECTIVE PRICE: ${stats['effective']:,}
$ PER 1K REMAINING MILES: ${stats['per_1k']:.0f}
KEVIN'S RUBRIC SAYS: {verdict}

Give a 2-3 sentence recommendation. Consider: is the KBB gap reasonable? Is the mileage sweet-spot for Toyota longevity? Any red flags in the trim or price positioning? End with a single word on its own line: BUY, MAYBE, or SKIP."""

    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return f"\n---\nAI REVIEW:\n{text}"
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
        print(f"  {yr} {mdl} ${p:,} @{stats['miles']:,}mi -> {verdict}"
              f"{' [NEW]' if alert_new else ''}"
              f"{f' [DROP -${drop:,}]' if alert_drop else ''}")

        if alert_new or alert_drop:
            body = rubric
            if alert_drop:
                body = f"PRICE DROP: -${drop:,} (was ${prev:,})\n\n{body}"
            if verdict in ("UNICORN", "GRAB"):
                body += ai_review(v, verdict, stats)
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
