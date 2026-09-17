"""
track_products.py

يشتغل هذا السكربت داخل GitHub Actions (أو محليًا) ووظيفته:
  1) يقارن products.json الحالي مع state.json (آخر حالة معروفة)
  2) يرسل إشعار تلغرام "منتج جديد" فقط للمنتجات اللي ما كانت موجودة من قبل
  3) للمنتجات القديمة، يتأكد من السعر عبر PA-API ويرسل إشعار فقط لو تغيّر السعر
  4) يحدّث state.json بالحالة الجديدة (يحتاج الـ workflow يسوي commit له بعد التشغيل)

المتطلبات (تحطها كـ GitHub Secrets في المستودع):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  AMAZON_PAAPI_ACCESS_KEY
  AMAZON_PAAPI_SECRET_KEY
  AMAZON_PAAPI_PARTNER_TAG   (مثال: mohammedala02-21)
"""

import json
import os
import re
import sys
import time
import requests

PRODUCTS_FILE = "products.json"
STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# فترة انتظار بسيطة بين طلبات PA-API لتجنب تجاوز الحد المسموح
PAAPI_DELAY_SECONDS = 1.1


def extract_asin(url: str):
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("تحذير: بيانات تلغرام غير مكتملة، تم تخطي الإرسال.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"فشل إرسال رسالة تلغرام: {e}")


def get_price_via_paapi(asin: str):
    """
    يجلب السعر الحالي للمنتج عبر Amazon Product Advertising API (الطريقة الرسمية والآمنة).
    استبدل هذي الدالة بمكتبة PA-API الرسمية (paapi5-python-sdk) أو استدعاء موقّع يدويًا
    حسب مفاتيحك. تركتها هنا كنقطة وصل (placeholder) لأن التوقيع الفعلي لـ PA-API
    يحتاج مكتبة SDK معتمدة (paapi5-python-sdk) بدل استدعاء يدوي بسيط.

    ترجع: (price: float | None, currency: str | None)
    """
    access_key = os.environ.get("AMAZON_PAAPI_ACCESS_KEY")
    secret_key = os.environ.get("AMAZON_PAAPI_SECRET_KEY")
    partner_tag = os.environ.get("AMAZON_PAAPI_PARTNER_TAG")

    if not (access_key and secret_key and partner_tag):
        print(f"تخطي جلب السعر لـ {asin}: مفاتيح PA-API غير مكتملة.")
        return None, None

    # --- استبدل هذا الجزء باستدعاء paapi5-python-sdk الفعلي ---
    # from paapi5_python_sdk.api.default_api import DefaultApi
    # from paapi5_python_sdk.models.get_items_request import GetItemsRequest
    # ... الخ حسب توثيق أمازون الرسمي
    #
    # مثال مبسّط جدًا (غير فعّال فعليًا بدون SDK):
    raise NotImplementedError(
        "اربط هذي الدالة بمكتبة paapi5-python-sdk الرسمية قبل التشغيل الفعلي."
    )


def main():
    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    current_asins = {}
    for p in products:
        asin = extract_asin(p.get("url", ""))
        if asin:
            current_asins[asin] = p.get("name", "")

    new_count = 0
    changed_count = 0
    checked_count = 0

    for asin, name in current_asins.items():
        if asin not in state:
            # منتج جديد بالكامل -> إشعار "منتج جديد" فقط، بدون فحص سعر الآن
            send_telegram(
                f"🆕 <b>منتج جديد أُضيف</b>\n{name}\n"
                f"https://www.amazon.sa/dp/{asin}"
            )
            state[asin] = {"name": name, "price": None}
            new_count += 1
            continue

        # منتج معروف من قبل -> تحقق من السعر فقط
        try:
            price, currency = get_price_via_paapi(asin)
        except NotImplementedError:
            print("get_price_via_paapi غير مفعّلة بعد؛ إيقاف فحص الأسعار.")
            break
        except Exception as e:
            print(f"خطأ في جلب سعر {asin}: {e}")
            continue

        checked_count += 1
        old_price = state[asin].get("price")

        if price is not None and old_price is not None and price != old_price:
            direction = "⬇️ انخفض" if price < old_price else "⬆️ ارتفع"
            send_telegram(
                f"{direction} <b>سعر منتج</b>\n{name}\n"
                f"السعر القديم: {old_price} {currency or ''}\n"
                f"السعر الجديد: {price} {currency or ''}\n"
                f"https://www.amazon.sa/dp/{asin}"
            )
            changed_count += 1

        if price is not None:
            state[asin]["price"] = price
            state[asin]["name"] = name

        time.sleep(PAAPI_DELAY_SECONDS)

    save_json(STATE_FILE, state)

    print(
        f"تم: منتجات جديدة={new_count}، أسعار متغيرة={changed_count}، "
        f"تم فحصها={checked_count}، الإجمالي في الملف={len(current_asins)}"
    )


if __name__ == "__main__":
    main()
