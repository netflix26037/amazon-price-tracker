"""
بوت متابعة أسعار أمازون السعودية (amazon.sa) عبر تيليجرام.

--- التحديث: استخدام Amazon Creators API الرسمي بدل الـ Scraping ---
بدل ما نفتح كل رابط منتج ونستخرج السعر يدويًا من HTML (بطيء، وعرضة لتغيّر
تصميم الصفحة أو الحظر)، صرنا نستخدم واجهة أمازون الرسمية (Creators API،
اللي حلّت محل Product Advertising API v5 القديم). هذي الواجهة ترجع لنا
حتى 10 منتجات بطلب واحد، فبدل 2334 طلب صار عندنا ~234 طلب بس -- تشغيل
كامل خلال دقائق قليلة بدل ساعات.

الفكرة نفس القديمة:
- يقرأ قائمة المنتجات من products.json ويستخرج ASIN من كل رابط.
- يجيب السعر الحالي لكل ASIN عبر Creators API على دفعات من 10.
- يقارن بالسعر المحفوظ من آخر مرة (في prices_history.json) بالاعتماد على
  ASIN، ويرسل إشعار تيليجرام فقط لو السعر تغيّر فعليًا.
- منتج جديد (أول مرة نشوفه): يُسجَّل كخط أساس بصمت، بدون إشعار.

المتطلبات (أضفها لـ requirements.txt):
    python-amazon-paapi
    requests

متغيرات البيئة المطلوبة (GitHub Secrets):
    AMAZON_CLIENT_ID       -- معرف بيانات الاعتماد من Creators API
    AMAZON_CLIENT_SECRET   -- السر من Creators API
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

⚠️ مهم: الـ workflow لازم يسوي commit + push لملف prices_history.json
بعد كل تشغيلة، وإلا التاريخ يضيع.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from amazon_creatorsapi import AmazonCreatorsApi, Country

PRODUCTS_FILE = Path("products.json")
HISTORY_FILE = Path("prices_history.json")

# كل كم "دفعة" (مو منتج) نحفظ التاريخ على القرص أثناء التشغيل -- حماية
# إضافية لو صار انقطاع غير متوقع في نص الطريق
SAVE_EVERY_BATCHES = 5

# الحد الأقصى لعدد المنتجات بالطلب الواحد لـ Creators API
BATCH_SIZE = 10

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

AMAZON_CLIENT_ID = os.environ.get("AMAZON_CLIENT_ID")
AMAZON_CLIENT_SECRET = os.environ.get("AMAZON_CLIENT_SECRET")
# الإصدار (Version) اللي تشوفه بجانب بيانات الاعتماد بصفحة Creators API
# (مثلاً v3.2) -- عدّله لو تغيّر، أو مرّره كـ Secret باسم AMAZON_CREDENTIAL_VERSION
AMAZON_CREDENTIAL_VERSION = os.environ.get("AMAZON_CREDENTIAL_VERSION", "3.2")
# رمز الشريك التسويقي (Partner Tag) الظاهر بروابطك الحالية
AMAZON_PARTNER_TAG = os.environ.get("AMAZON_PARTNER_TAG", "mohammedala02-21")

# إفصاح إلزامي حسب اتفاقية تشغيل برنامج أمازون أسوشييتس.
AFFILIATE_DISCLOSURE = "📌 بصفتي شريك أمازون أسوشييتس، أكسب عمولة من المشتريات المؤهلة."


def extract_asin(url: str):
    """يستخرج رقم المنتج (ASIN) من الرابط."""
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
    """يهاجر أي مدخلات قديمة كانت مفتاحها الرابط الكامل إلى مفاتيح ASIN."""
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


def get_price_from_item(item):
    """يرجع (السعر, رابط الصورة) من كائن المنتج اللي ترجعه الـ API،
    أو (None, None) لو ما فيه عروض متاحة حاليًا (نفذت الكمية مثلًا)."""
    price = None
    if item.offers_v2 and item.offers_v2.listings:
        listing = item.offers_v2.listings[0]
        if listing.price and listing.price.money:
            price = float(listing.price.money.amount)

    image_url = None
    if item.images and item.images.primary and item.images.primary.large:
        image_url = item.images.primary.large.url

    return price, image_url


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


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main():
    if not AMAZON_CLIENT_ID or not AMAZON_CLIENT_SECRET:
        print("خطأ: لازم تضبط AMAZON_CLIENT_ID و AMAZON_CLIENT_SECRET.")
        sys.exit(1)

    products = load_json(PRODUCTS_FILE, [])
    raw_history = load_json(HISTORY_FILE, {})
    history = migrate_history_to_asin_keys(raw_history)

    if not products:
        print("لا توجد منتجات في products.json")
        return

    # نبني خريطة ASIN -> بيانات المنتج (الاسم والرابط) من ملفنا
    asin_to_product = {}
    skipped_count = 0
    for product in products:
        url = product.get("url")
        asin = extract_asin(url) if url else None
        if not asin:
            skipped_count += 1
            continue
        asin_to_product[asin] = product  # لو فيه تكرار لنفس ASIN، ناخذ آخر واحد

    all_asins = list(asin_to_product.keys())
    print(f"إجمالي منتجات صالحة: {len(all_asins)} (تم تخطي {skipped_count} بدون ASIN صالح)")

    api = AmazonCreatorsApi(
        credential_id=AMAZON_CLIENT_ID,
        credential_secret=AMAZON_CLIENT_SECRET,
        version=AMAZON_CREDENTIAL_VERSION,
        tag=AMAZON_PARTNER_TAG,
        country=Country.SA,
        throttling=2,  # ثانيتين بين كل طلب دفعة، هامش أمان إضافي
    )

    new_count = 0
    changed_count = 0
    unchanged_count = 0
    unavailable_count = 0
    error_count = 0

    batches = list(chunked(all_asins, BATCH_SIZE))
    print(f"سيتم التنفيذ على {len(batches)} دفعة (كل دفعة حتى {BATCH_SIZE} منتجات)")

    for batch_idx, batch_asins in enumerate(batches, start=1):
        try:
            items = api.get_items(batch_asins)
        except Exception as exc:  # نلتقط أي خطأ عام من المكتبة/الشبكة
            print(f"دفعة {batch_idx}: فشل الطلب بالكامل: {exc}")
            error_count += len(batch_asins)
            continue

        items_by_asin = {item.asin: item for item in items if getattr(item, "asin", None)}

        for asin in batch_asins:
            product = asin_to_product[asin]
            name = product.get("name", "منتج بدون اسم")
            url = product.get("url")

            item = items_by_asin.get(asin)
            if item is None:
                print(f"[{name}] لم يُرجع أي بيانات لهذا الـ ASIN ({asin})")
                error_count += 1
                continue

            price, image_url = get_price_from_item(item)

            if price is None:
                print(f"[{name}] غير متوفر حاليًا (لا توجد عروض)")
                unavailable_count += 1
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

        if batch_idx % SAVE_EVERY_BATCHES == 0:
            save_json(HISTORY_FILE, history)
            print(f"تم حفظ التقدم بعد الدفعة {batch_idx}/{len(batches)}")

    save_json(HISTORY_FILE, history)

    print(
        f"تم: منتجات جديدة (بدون إشعار)={new_count}، "
        f"أسعار تغيّرت (تم الإشعار)={changed_count}، "
        f"بدون تغيير={unchanged_count}، غير متوفرة حاليًا={unavailable_count}، "
        f"أخطاء={error_count}"
    )


if __name__ == "__main__":
    sys.exit(main())
