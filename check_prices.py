"""
بوت متابعة أسعار أمازون السعودية (amazon.sa) عبر تيليجرام.

الفكرة:
- يقرأ قائمة المنتجات من products.json
- يفتح كل رابط ويحاول يستخرج السعر الحالي من صفحة أمازون
- يقارن السعر بالسعر المحفوظ من آخر مرة (في prices_history.json)
- لو السعر اتغيّر (خصوصًا لو نزل) يبعت رسالة على تيليجرام
- يحدّث prices_history.json بالسعر الجديد

يشتغل عادة عن طريق GitHub Actions على جدول زمني (مثلاً كل 6 ساعات).
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
HISTORY_FILE = Path("prices_history.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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

    # محاولة أخيرة: دور على أي نص فيه "ر.س" أو "SAR" جنب رقم
    match = re.search(r"([\d.,]+)\s*(?:ر\.?س|SAR)", html)
    if match:
        return clean_price(match.group(1))

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


def fetch_price(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        return None, f"خطأ في الاتصال: {exc}"

    if resp.status_code != 200:
        return None, f"استجابة غير متوقعة من أمازون (كود {resp.status_code})"

    price = extract_price(resp.text)
    if price is None:
        return None, "لم أستطع إيجاد السعر في الصفحة (ربما تغيّر شكل الصفحة أو طُلب تحقق أمني)"

    return price, None


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("تحذير: لم يتم ضبط TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID، سيتم طباعة الرسالة فقط.")
        print(text)
        return

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            api_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"فشل إرسال رسالة تيليجرام: {exc}")


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    if not products:
        print("لا توجد منتجات في products.json")
        return

    messages = []

    for product in products:
        name = product.get("name", "منتج بدون اسم")
        url = product.get("url")
        if not url:
            continue

        price, error = fetch_price(url)
        time.sleep(2)  # فاصل بسيط بين الطلبات عشان منضغطش على أمازون

        if error:
            print(f"[{name}] {error}")
            continue

        old_price = history.get(url, {}).get("price")

        if old_price is None:
            messages.append(
                f"🆕 <b>{name}</b>\n"
                f"بدأت متابعة السعر: {price:.2f} ر.س\n"
                f"{url}"
            )
        elif price < old_price:
            diff = old_price - price
            pct = (diff / old_price) * 100
            messages.append(
                f"🔻 <b>{name}</b>\n"
                f"نزل السعر من {old_price:.2f} إلى {price:.2f} ر.س "
                f"(خصم {pct:.0f}%)\n"
                f"{url}"
            )
        elif price > old_price:
            messages.append(
                f"🔺 <b>{name}</b>\n"
                f"ارتفع السعر من {old_price:.2f} إلى {price:.2f} ر.س\n"
                f"{url}"
            )
        # لو السعر زي ما هو، منبعتش رسالة

        history[url] = {"name": name, "price": price}

    save_json(HISTORY_FILE, history)

    if messages:
        full_message = "\n\n".join(messages)
        send_telegram_message(full_message)
        print("تم إرسال التحديثات.")
    else:
        print("لا توجد تغييرات في الأسعار هذه المرة.")


if __name__ == "__main__":
    sys.exit(main())
