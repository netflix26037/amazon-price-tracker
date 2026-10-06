"""
بوت متابعة أسعار أمازون السعودية (amazon.sa) عبر تيليجرام.

الفكرة:
- يقرأ قائمة المنتجات من products.json
- يفتح كل رابط ويحاول يستخرج: السعر الحالي، السعر الأصلي قبل الخصم (المشطوب
  إن وُجد)، وصورة المنتج
- يقارن السعر بالسعر المحفوظ من آخر مرة (في prices_history.json)، بالاعتماد
  على رقم المنتج (ASIN) وليس الرابط الكامل -- لأن الرابط ممكن يتغيّر شكله
  لنفس المنتج بالضبط.
- منتج جديد (ASIN غير موجود بالتاريخ): يُسجَّل كخط أساس بصمت، بدون أي رسالة.
- منتج معروف: يُرسل إشعار فقط لو السعر **نزل** فعليًا (مو لو ارتفع)، مع نسبة
  الخصم عن السعر الأصلي إن كانت متوفرة، وإشارة لو السعر وصل لأدنى مستوى له
  على الإطلاق أو أقل من المعتاد. لو السعر ارتفع، يُحدَّث السجل بصمت بدون
  أي إشعار.
- يحدّث لكل منتج: أقل سعر وأعلى سعر ومتوسط سعر تراكمي (min_price/max_price/
  avg_price) -- هذا هو الأساس اللي تعتمد عليه لوحة عرض الأسعار لاحقًا لحساب
  "أقل من المعتاد" الحقيقي، مو بس مقارنة بآخر مرة.

يشتغل عادة عن طريق GitHub Actions على جدول زمني (مثلاً كل 6 ساعات).

⚠️ مهم: الـ workflow لازم يسوي commit + push لملف prices_history.json بعد
كل تشغيلة، وإلا التاريخ (وبالتالي أقل/أعلى/متوسط سعر) يضيع.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PRODUCTS_FILE = Path("products.json")
HISTORY_FILE = Path("prices_history.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# إفصاح إلزامي حسب اتفاقية تشغيل برنامج أمازون أسوشييتس.
AFFILIATE_DISCLOSURE = "📌 بصفتي شريك أمازون أسوشييتس، أكسب عمولة من المشتريات المؤهلة."

# السعر الحالي يُعتبر "أقل من المعتاد" لو كان أقل من هذه النسبة من المتوسط
# التاريخي (0.95 يعني أقل بـ5% على الأقل من المتوسط المسجّل قبل هذه القراءة)
BELOW_USUAL_RATIO = 0.95

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

PRICE_SELECTORS = [
    {"id": "priceblock_ourprice"},
    {"id": "priceblock_dealprice"},
    {"id": "priceblock_saleprice"},
    {"class_": "a-price-whole"},
    {"class_": "a-offscreen"},
]

IMAGE_SELECTORS = [
    {"id": "landingImage"},
    {"id": "imgBlkFront"},
    {"class_": "a-dynamic-image"},
]


def extract_asin(url: str):
    m = re.search(r"/dp/([A-Z0-9]{10})", url or "")
    return m.group(1) if m else None


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path: Path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def migrate_history(history: dict) -> dict:
    """يهاجر أي مدخلات قديمة (مفتاحها الرابط الكامل، أو بالشكل القديم
    {price: X} بدون min/max/avg) إلى الشكل الجديد الكامل بمفاتيح ASIN،
    بدون ما نخسر أي بيانات متراكمة سابقًا."""
    migrated = {}
    for key, value in history.items():
        asin = extract_asin(key) if key.startswith("http") else key
        if not asin:
            continue
        price = value.get("price")
        entry = {
            "name": value.get("name", ""),
            "url": value.get("url", ""),
            "image_url": value.get("image_url"),
            "price": price,
            "list_price": value.get("list_price"),
            "min_price": value.get("min_price", price),
            "max_price": value.get("max_price", price),
            "avg_price": value.get("avg_price", price),
            "samples": value.get("samples", 1 if price is not None else 0),
            "last_checked": value.get("last_checked"),
        }
        migrated[asin] = entry
    return migrated


def clean_price(text: str):
    cleaned = re.sub(r"[^\d.,]", "", text or "")
    cleaned = cleaned.replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_price(html: str):
    soup = BeautifulSoup(html, "html.parser")

    for selector in PRICE_SELECTORS:
        tag = (
            soup.find(class_=selector["class_"])
            if "class_" in selector
            else soup.find(attrs=selector)
        )
        if tag and tag.text.strip():
            price = clean_price(tag.text)
            if price is not None:
                return price

    match = re.search(r"([\d.,]+)\s*(?:ر\.?س|SAR)", html)
    if match:
        return clean_price(match.group(1))

    return None


def extract_list_price(html: str):
    """يحاول يستخرج السعر الأصلي (قبل الخصم / المشطوب) من صفحة أمازون.
    شكل هذا العنصر يتغيّر كثير بين المنتجات؛ لو ما لقينا شي نرجع None
    (يعني: ما نقدر نحسب نسبة خصم حقيقية لهذا المنتج)."""
    soup = BeautifulSoup(html, "html.parser")

    strike = soup.find(attrs={"data-a-strike": "true"})
    if strike:
        offscreen = strike.find(class_="a-offscreen")
        if offscreen and offscreen.text.strip():
            price = clean_price(offscreen.text)
            if price is not None:
                return price

    tag = soup.find(class_="a-text-strike")
    if tag and tag.text.strip():
        price = clean_price(tag.text)
        if price is not None:
            return price

    match = re.search(r"(?:كان|List Price)[^\d]{0,15}([\d.,]+)", html)
    if match:
        price = clean_price(match.group(1))
        if price is not None:
            return price

    return None


def extract_image(html: str):
    soup = BeautifulSoup(html, "html.parser")

    for selector in IMAGE_SELECTORS:
        tag = (
            soup.find(class_=selector["class_"])
            if "class_" in selector
            else soup.find(attrs=selector)
        )
        if not tag:
            continue

        img_url = tag.get("data-old-hires") or tag.get("src")

        if not img_url:
            dynamic_data = tag.get("data-a-dynamic-image")
            if dynamic_data:
                try:
                    images = json.loads(dynamic_data)
                    if images:
                        img_url = next(iter(images))
                except (json.JSONDecodeError, StopIteration):
                    img_url = None

        if img_url and img_url.startswith("http"):
            return img_url

    return None


def fetch_product_details(url: str):
    """يرجع (السعر, السعر الأصلي, رابط الصورة, رسالة خطأ)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        return None, None, None, f"خطأ في الاتصال: {exc}"

    if resp.status_code != 200:
        return None, None, None, f"استجابة غير متوقعة من أمازون (كود {resp.status_code})"

    price = extract_price(resp.text)
    list_price = extract_list_price(resp.text)
    image_url = extract_image(resp.text)

    if price is None:
        return None, None, image_url, "لم أستطع إيجاد السعر في الصفحة (ربما تغيّر شكل الصفحة أو طُلب تحقق أمني)"

    if list_price is not None and list_price <= price:
        list_price = None

    return price, list_price, image_url, None


def send_telegram_message(text: str, image_url: str = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("تحذير: لم يتم ضبط TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID، سيتم طباعة الرسالة فقط.")
        print(text)
        return

    if image_url:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": image_url,
            "caption": text[:1024],
            "parse_mode": "HTML",
        }
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    try:
        resp = requests.post(api_url, data=payload, timeout=15)
        if resp.status_code != 200 and image_url:
            print(f"فشل إرسال الصورة (كود {resp.status_code})، سيتم الإرسال كنص.")
            send_telegram_message(text, image_url=None)
    except requests.RequestException as exc:
        print(f"فشل إرسال رسالة تيليجرام: {exc}")


def build_change_message(name, url, old_price, price, list_price, is_below_usual, is_all_time_low):
    # هذه الدالة تُستدعى فقط عند انخفاض السعر (شوف main) -- ما فيه حالة
    # ارتفاع هنا لأن البوت ما يرسل إشعار إلا لما السعر ينزل.
    diff = old_price - price
    pct = (diff / old_price) * 100
    lines = []
    lines.append(f"🔻 <b>{name}</b>")
    lines.append(f"نزل السعر من {old_price:.2f} إلى {price:.2f} ر.س (خصم {pct:.0f}%)")

    if list_price:
        discount_pct = (list_price - price) / list_price * 100
        lines.append(f"السعر الأصلي: {list_price:.2f} ر.س (خصم {discount_pct:.0f}% عن السعر الأصلي)")

    if is_all_time_low:
        lines.append("🏆 أقل سعر تم رصده على الإطلاق لهذا المنتج")
    elif is_below_usual:
        lines.append("📉 أقل من متوسط سعره المعتاد")

    lines.append(url)
    lines.append("")
    lines.append(AFFILIATE_DISCLOSURE)
    return "\n".join(lines)


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = migrate_history(load_json(HISTORY_FILE, {}))

    if not products:
        print("لا توجد منتجات في products.json")
        return

    new_count = 0
    changed_count = 0
    price_up_count = 0
    unchanged_count = 0
    skipped_count = 0

    for product in products:
        name = product.get("name", "منتج بدون اسم")
        url = product.get("url")
        if not url:
            continue

        asin = extract_asin(url)
        if not asin:
            print(f"[{name}] تخطي: ما قدرت أستخرج ASIN من الرابط {url}")
            skipped_count += 1
            continue

        price, list_price, image_url, error = fetch_product_details(url)
        time.sleep(15)

        if error:
            print(f"[{name}] {error}")
            skipped_count += 1
            continue

        existing = history.get(asin)
        old_price = existing.get("price") if existing else None
        old_avg = existing.get("avg_price") if existing else None
        old_min = existing.get("min_price") if existing else None
        old_samples = existing.get("samples", 0) if existing else 0

        is_below_usual = (
            old_avg is not None
            and old_samples >= 2
            and price < old_avg * BELOW_USUAL_RATIO
        )
        is_all_time_low = old_min is not None and price < old_min

        if old_price is None:
            new_count += 1
            new_min = price
            new_max = price
            new_avg = price
            new_samples = 1
        else:
            new_min = min(old_min, price) if old_min is not None else price
            new_max = max(existing.get("max_price", price), price)
            new_avg = ((old_avg or price) * old_samples + price) / (old_samples + 1)
            new_samples = old_samples + 1

            if price < old_price:
                # السعر نزل -- هذا الحال الوحيد اللي نرسل فيه إشعار
                message = build_change_message(
                    name, url, old_price, price, list_price, is_below_usual, is_all_time_low
                )
                send_telegram_message(message, image_url=image_url)
                changed_count += 1
            elif price > old_price:
                # السعر ارتفع -- نحدّث السجل بصمت بدون أي إشعار
                price_up_count += 1
            else:
                unchanged_count += 1

        history[asin] = {
            "name": name,
            "url": url,
            "image_url": image_url,
            "price": price,
            "list_price": list_price,
            "min_price": new_min,
            "max_price": new_max,
            "avg_price": round(new_avg, 2),
            "samples": new_samples,
            "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    save_json(HISTORY_FILE, history)

    print(
        f"تم: منتجات جديدة (بدون إشعار)={new_count}، "
        f"أسعار نزلت (تم الإشعار)={changed_count}، "
        f"أسعار ارتفعت (بدون إشعار)={price_up_count}، "
        f"بدون تغيير={unchanged_count}، تم تخطيها={skipped_count}"
    )


if __name__ == "__main__":
    sys.exit(main())
