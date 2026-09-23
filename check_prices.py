"""
بوت متابعة أسعار أمازون السعودية (amazon.sa) عبر تيليجرام.

الفكرة:
- يقرأ قائمة المنتجات من products.json
- يفتح كل رابط ويحاول يستخرج السعر الحالي وصورة المنتج من صفحة أمازون
- يقارن السعر بالسعر المحفوظ من آخر مرة، بالاعتماد على رقم المنتج (ASIN)
  وليس الرابط الكامل -- لأن الرابط ممكن يتغيّر شكله لنفس المنتج بالضبط.
- منتج جديد (ASIN غير موجود بالتاريخ): يُسجَّل السعر كخط أساس بصمت، بدون أي
  رسالة تيليجرام.
- منتج معروف: يُرسل إشعار فقط لو السعر تغيّر فعليًا (طلع أو نزل).

--- نظام الدفعات (Batching) ---
عشان قائمة منتجات كبيرة (آلاف المنتجات) ما تتجاوز الحد الأقصى لوقت تشغيل
GitHub Actions (6 ساعات)، السكريبت يدعم تقسيم المنتجات لعدة دفعات تشتغل
بالتوازي، كل دفعة لها متغيرين بيئة:

  BATCH_INDEX : رقم الدفعة (يبدأ من 0)
  BATCH_COUNT : العدد الكلي للدفعات

كل دفعة تاخذ فقط المنتجات اللي رقمها (index % BATCH_COUNT == BATCH_INDEX)،
بحيث توزيع المنتجات متساوي تقريبًا بين كل الدفعات، وكل دفعة لها ملف تاريخ
أسعار خاص فيها (prices_history_batchN.json) عشان ما يصير تعارض (Conflict)
لما أكثر من دفعة تحاول تحفظ نفس الملف بنفس الوقت.

لو ما تم ضبط BATCH_COUNT (أو كانت قيمته 1)، السكريبت يشتغل عادي على كل
المنتجات بملف تاريخ واحد (prices_history.json) -- يعني التوافق للخلف
محفوظ ولو حبيت ترجع تشغيل بدون تقسيم.

⚠️ مهم: الـ workflow (GitHub Actions) لازم يسوي commit + push لملف
(ات) prices_history بعد كل تشغيلة، وإلا التاريخ يضيع.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PRODUCTS_FILE = Path("products.json")

# --- إعدادات الدفعات ---
BATCH_INDEX = int(os.environ.get("BATCH_INDEX", "0"))
BATCH_COUNT = int(os.environ.get("BATCH_COUNT", "1"))

if BATCH_COUNT > 1:
    HISTORY_FILE = Path(f"prices_history_batch{BATCH_INDEX}.json")
else:
    HISTORY_FILE = Path("prices_history.json")

# كل كم منتج نحفظ التاريخ على القرص أثناء التشغيل (حماية إضافية لو صار
# أي انقطاع غير متوقع في نص الطريق)
SAVE_EVERY = 20

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# إفصاح إلزامي حسب اتفاقية تشغيل برنامج أمازون أسوشييتس.
# يُضاف تلقائيًا في نهاية كل رسالة تحتوي على رابط منتج.
AFFILIATE_DISCLOSURE = "📌 بصفتي شريك أمازون أسوشييتس، أكسب عمولة من المشتريات المؤهلة."

# نتظاهر بأننا متصفح حقيقي عشان نقلل فرصة الحظر
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

# محاولات مختلفة لاستخراج السعر لأن أمازون بيغيّر شكل الصفحة أحيانًا
PRICE_SELECTORS = [
    {"id": "priceblock_ourprice"},
    {"id": "priceblock_dealprice"},
    {"id": "priceblock_saleprice"},
    {"class_": "a-price-whole"},
    {"class_": "a-offscreen"},
]

# محاولات مختلفة لاستخراج صورة المنتج
IMAGE_SELECTORS = [
    {"id": "landingImage"},
    {"id": "imgBlkFront"},
    {"class_": "a-dynamic-image"},
]


def extract_asin(url: str):
    """يستخرج رقم المنتج (ASIN) من الرابط. هذا هو المفتاح الثابت للمنتج
    بغض النظر عن شكل الرابط (مع/بدون tag أو باراميترات إضافية)."""
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


def migrate_history_to_asin_keys(history: dict) -> dict:
    """يهاجر أي مدخلات قديمة كانت مفتاحها الرابط الكامل (بدل ASIN) إلى
    مفاتيح ASIN، بحيث ما نخسر تاريخ الأسعار المتراكم من قبل."""
    migrated = {}
    changed = False
    for key, value in history.items():
        asin = extract_asin(key) if key.startswith("http") else key
        if asin:
            if asin != key:
                changed = True
            migrated[asin] = value
        else:
            changed = True
    if changed:
        print("تم ترحيل prices_history.json إلى مفاتيح ASIN.")
    return migrated


def extract_price(html: str):
    """يحاول يستخرج السعر من صفحة أمازون بأكثر من طريقة."""
    soup = BeautifulSoup(html, "html.parser")

    for selector in PRICE_SELECTORS:
        tag = soup.find(attrs=selector) if "class_" not in selector else soup.find(
            class_=selector["class_"]
        )
        if tag and tag.text.strip():
            price = clean_price(tag.text)
            if price is not None:
                return price

    match = re.search(r"([\d.,]+)\s*(?:ر\.?س|SAR)", html)
    if match:
        return clean_price(match.group(1))

    return None


def extract_image(html: str):
    """يحاول يستخرج رابط صورة المنتج الرئيسية من صفحة أمازون."""
    soup = BeautifulSoup(html, "html.parser")

    for selector in IMAGE_SELECTORS:
        tag = soup.find(attrs=selector) if "class_" not in selector else soup.find(
            class_=selector["class_"]
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


def clean_price(text: str):
    """يحوّل نص السعر (فيه فواصل/رموز) لرقم عشري."""
    cleaned = re.sub(r"[^\d.,]", "", text)
    cleaned = cleaned.replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_product_details(url: str):
    """يرجع (السعر, رابط الصورة, رسالة خطأ)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        return None, None, f"خطأ في الاتصال: {exc}"

    if resp.status_code != 200:
        return None, None, f"استجابة غير متوقعة من أمازون (كود {resp.status_code})"

    price = extract_price(resp.text)
    image_url = extract_image(resp.text)

    if price is None:
        return None, image_url, "لم أستطع إيجاد السعر في الصفحة (ربما تغيّر شكل الصفحة أو طُلب تحقق أمني)"

    return price, image_url, None


def send_telegram_message(text: str, image_url: str = None):
    """يرسل رسالة تيليجرام. لو فيه صورة يرسلها مع كابشن، وإلا نص عادي."""
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


def select_batch(products: list) -> list:
    """يرجع فقط المنتجات اللي تخص هذه الدفعة (توزيع متساوي عبر الدفعات
    بدل تقسيم متتالٍ، عشان لو فيه فئات منتجات مرتبة ورا بعض بالملف،
    كل دفعة توخذ خليط متنوع منها مو فئة وحدة بس)."""
    if BATCH_COUNT <= 1:
        return products
    return [p for i, p in enumerate(products) if i % BATCH_COUNT == BATCH_INDEX]


def main():
    products = load_json(PRODUCTS_FILE, [])
    raw_history = load_json(HISTORY_FILE, {})
    history = migrate_history_to_asin_keys(raw_history)

    if not products:
        print("لا توجد منتجات في products.json")
        return

    batch_products = select_batch(products)

    if BATCH_COUNT > 1:
        print(
            f"دفعة {BATCH_INDEX + 1}/{BATCH_COUNT}: "
            f"{len(batch_products)} منتج من إجمالي {len(products)}"
        )

    new_count = 0
    changed_count = 0
    unchanged_count = 0
    skipped_count = 0

    for idx, product in enumerate(batch_products, start=1):
        name = product.get("name", "منتج بدون اسم")
        url = product.get("url")
        if not url:
            continue

        asin = extract_asin(url)
        if not asin:
            print(f"[{name}] تخطي: ما قدرت أستخرج ASIN من الرابط {url}")
            skipped_count += 1
            continue

        price, image_url, error = fetch_product_details(url)
        time.sleep(15)  # فاصل بين الطلبات عشان منضغطش على أمازون

        if error:
            print(f"[{name}] {error}")
            skipped_count += 1
            continue

        old_price = history.get(asin, {}).get("price")
        message = None

        if old_price is None:
            new_count += 1
        elif price < old_price:
            diff = old_price - price
            pct = (diff / old_price) * 100
            message = (
                f"🔻 <b>{name}</b>\n"
                f"نزل السعر من {old_price:.2f} إلى {price:.2f} ر.س "
                f"(خصم {pct:.0f}%)\n"
                f"{url}\n\n"
                f"{AFFILIATE_DISCLOSURE}"
            )
            changed_count += 1
        elif price > old_price:
            message = (
                f"🔺 <b>{name}</b>\n"
                f"ارتفع السعر من {old_price:.2f} إلى {price:.2f} ر.س\n"
                f"{url}\n\n"
                f"{AFFILIATE_DISCLOSURE}"
            )
            changed_count += 1
        else:
            unchanged_count += 1

        if message:
            send_telegram_message(message, image_url=image_url)

        history[asin] = {"name": name, "url": url, "price": price}

        # حفظ دوري: حماية إضافية لو صار انقطاع غير متوقع في نص الطريق
        if idx % SAVE_EVERY == 0:
            save_json(HISTORY_FILE, history)

    save_json(HISTORY_FILE, history)

    print(
        f"تم: منتجات جديدة (بدون إشعار)={new_count}، "
        f"أسعار تغيّرت (تم الإشعار)={changed_count}، "
        f"بدون تغيير={unchanged_count}، تم تخطيها={skipped_count}"
    )


if __name__ == "__main__":
    sys.exit(main())
