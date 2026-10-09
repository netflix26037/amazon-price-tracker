"""فحص أسعار منتجات amazon.sa وإرسال تنبيه على تيليجرام عند تغيّر السعر."""
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
PRODUCTS_FILE = BASE / "products.json"
HISTORY_FILE = BASE / "prices_history.json"
MAX_HISTORY = 200
# تجاهل التغيّر الأقل من هذه النسبة (%) لتقليل تنبيهات تذبذب الأسعار
MIN_CHANGE_PCT = float(os.environ.get("MIN_CHANGE_PCT", "1"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PRICE_SELECTORS = [
    "#corePrice_feature_div span.a-price:not(.a-text-price) span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-price:not(.a-text-price) span.a-offscreen",
    "span.a-price:not(.a-text-price) span.a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#price_inside_buybox",
]

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def parse_price(text: str):
    """يحوّل نص مثل 'SAR 15.99' أو '١٥٫٩٩ ر.س' إلى رقم."""
    text = text.translate(ARABIC_DIGITS).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def extract_price(page_html: str):
    low = page_html.lower()
    if "captcha" in low or "api-services-support@amazon" in low:
        raise RuntimeError("أمازون رجّع صفحة تحقق (CAPTCHA)")
    soup = BeautifulSoup(page_html, "html.parser")
    for sel in PRICE_SELECTORS:
        for el in soup.select(sel):
            price = parse_price(el.get_text(" ", strip=True))
            if price:
                return price
    raise RuntimeError("ما لقيت السعر في الصفحة (ممكن المنتج غير متوفر أو تغيّر التصميم)")


def fetch_price(session: requests.Session, url: str):
    last_err = None
    for attempt in range(3):
        try:
            r = session.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return extract_price(r.text)
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last_err = e
        except RuntimeError as e:
            last_err = e
        time.sleep(3 + attempt * 4 + random.random() * 2)
    raise last_err


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("تنبيه: TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوطين، ما أرسلت شيء.")
        print(text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not r.ok:
        print(f"فشل إرسال تيليجرام: {r.status_code} {r.text}")


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"تحذير: {path.name} تالف، سيُعاد إنشاؤه.")
    return default


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = requests.Session()
    failures = 0
    drops = []

    for i, p in enumerate(products):
        name, url = p["name"], p["url"]
        if "XXXXXXXXXX" in url:
            print(f"تخطيت '{name}': لسا ما حطيت رمز المنتج الحقيقي.")
            continue
        if i:
            time.sleep(random.uniform(4, 9))  # فاصل بين المنتجات لتقليل احتمال الحظر

        try:
            price = fetch_price(session, url)
        except Exception as e:
            failures += 1
            print(f"✗ {name}: {e}")
            continue

        entry = history.setdefault(url, {"name": name, "last_price": None, "history": []})
        entry["name"] = name

        # أقل سعر سُجّل للمنتج (يدعم السجلات القديمة اللي ما فيها lowest_price)
        lowest = entry.get("lowest_price")
        if lowest is None:
            known = [h["price"] for h in entry["history"]]
            if entry["last_price"] is not None:
                known.append(entry["last_price"])
            lowest = min(known) if known else None

        if lowest is None:
            entry["lowest_price"] = price  # أول تسجيل بدون إرسال
        else:
            pct = (price - lowest) / lowest * 100
            if price < lowest and abs(pct) >= MIN_CHANGE_PCT:
                link = f'<a href="{html.escape(url)}">{html.escape(name)}</a>'
                drops.append(f"📉 {link}\nأقل سعر سابق {lowest:.2f} ← <b>{price:.2f}</b> ريال ({pct:.0f}%)")
                entry["lowest_price"] = price
            else:
                entry["lowest_price"] = lowest

        if entry["last_price"] != price:
            entry["history"].append({"t": now, "price": price})
            entry["history"] = entry["history"][-MAX_HISTORY:]
        entry["last_price"] = price
        entry["updated"] = now
        print(f"✓ {name}: {price:.2f} (أقل سعر مسجّل: {entry['lowest_price']:.2f})")

    if drops:
        send_telegram("🔔 <b>انخفضت أسعار</b>\n\n" + "\n\n".join(drops))

    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    if failures:
        print(f"{failures} منتج/منتجات فشل فحصها.")
        sys.exit(1)


if __name__ == "__main__":
    main()
