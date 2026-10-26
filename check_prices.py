"""
بوت متابعة أسعار أمازون السعودية (amazon.sa) عبر تيليجرام.

ماذا يسوي:
- يقرأ products.json ويفحص المنتجات على دفعات متتالية: في كل تشغيلة يبدأ
  بالمنتجات اللي ما انفحصت أبدًا، ثم الأقدم فحصًا، ويكمل لين تخلص ميزانية
  الوقت (افتراضيًا 5 ساعات و10 دقائق). يعني التشغيلة الجاية تكمل من حيث وقفت
  بدل ما تعيد نفس أول المنتجات كل مرة.
- يقرأ كل ملفات السجل الموجودة (prices_history.json + prices_history_batch0..N.json)
  ويدمجها، ثم يكتب دائمًا في ملف واحد: prices_history.json (كل 25 منتج وفي النهاية،
  حتى لو انقطع التشغيل). ملفات الدفعات القديمة تبقى للقراءة فقط وتقدر تحذفها بعد
  أول تشغيلة ناجحة.
- المفتاح الثابت لكل منتج: رقم ASIN، أو كود الرابط المختصر (amzn.eu/d/XXXX)
  للروابط اللي ما فيها ASIN -- فما يتجاهل أي منتج.
- يرسل إشعار تيليجرام فقط لما السعر ينزل فعليًا، وبشروط تمنع التكرار:
    * الانخفاض لازم يكون >= MIN_DROP_PERCENT من آخر سعر مسجّل.
    * لا يكرر الإشعار لنفس المنتج بنفس السعر (أو أعلى) خلال
      ALERT_COOLDOWN_HOURS؛ يرسل مرة ثانية بس لو نزل لسعر أقل من اللي أُرسل.
    * قبل الإرسال يعيد جلب الصفحة مرة ثانية ويتأكد إن السعر المنخفض ثابت
      (يتجنب القراءات الغلط من أمازون).
- ارتفاع السعر أو ثباته: يحدّث السجل بصمت بدون أي رسالة.
- منتج جديد (أول قراءة ناجحة): يسجّل السعر كخط أساس بصمت.
- يحفظ لكل منتج أقل/أعلى/متوسط سعر ليستخدمها لوحة العروض.

متغيرات بيئة اختيارية (كلها لها قيم افتراضية):
  MAX_RUNTIME_SECONDS, DELAY_MIN_SECONDS, DELAY_MAX_SECONDS,
  MIN_DROP_PERCENT, ALERT_COOLDOWN_HOURS
بدون TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID يطبع الرسائل بدل ما يرسلها (للتجربة).

⚠️ الـ workflow لازم يسوي commit + push لـ prices_history.json بعد كل تشغيلة.
"""

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

PRODUCTS_FILE = Path("products.json")
HISTORY_FILE = Path("prices_history.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# إفصاح إلزامي حسب اتفاقية تشغيل برنامج أمازون أسوشييتس.
AFFILIATE_DISCLOSURE = "📌 بصفتي شريك أمازون أسوشييتس، أكسب عمولة من المشتريات المؤهلة."


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


MAX_RUNTIME_SECONDS = _env_float("MAX_RUNTIME_SECONDS", 5 * 3600 + 10 * 60)
DELAY_MIN = _env_float("DELAY_MIN_SECONDS", 4.0)
DELAY_MAX = _env_float("DELAY_MAX_SECONDS", 8.0)
MIN_DROP_PERCENT = _env_float("MIN_DROP_PERCENT", 2.0)
ALERT_COOLDOWN_HOURS = _env_float("ALERT_COOLDOWN_HOURS", 72.0)

SAVE_EVERY = 25
MAX_CONSECUTIVE_FAILURES = 15
# السعر الحالي "أقل من المعتاد" لو كان أقل من هذه النسبة من المتوسط التاريخي
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

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
SHORT_RE = re.compile(r"(?:amzn\.eu/d|amzn\.to|a\.co/d)/([A-Za-z0-9]+)")

CORE_PRICE_CONTAINER_IDS = [
    "corePriceDisplay_desktop_feature_div",
    "corePrice_feature_div",
    "apex_desktop",
    "apex_offerDisplay_desktop",
    "buybox",
]
LEGACY_PRICE_IDS = ["priceblock_ourprice", "priceblock_dealprice", "priceblock_saleprice"]
IMAGE_SELECTORS = [
    {"id": "landingImage"},
    {"id": "imgBlkFront"},
    {"class_": "a-dynamic-image"},
]

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


# --------------------------------------------------------------------------
# أدوات عامة
# --------------------------------------------------------------------------
def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def product_key(url):
    """المفتاح الثابت للمنتج: ASIN، أو كود الرابط المختصر. None لو ما عرفنا."""
    if not url:
        return None
    m = ASIN_RE.search(url)
    if m:
        return m.group(1)
    m = SHORT_RE.search(url)
    if m:
        return "short:" + m.group(1)
    return None


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def migrate_history(history: dict) -> dict:
    """يوحّد شكل السجل: مفاتيح (ASIN/short) بدل الروابط الكاملة، ويضيف الحقول
    الناقصة، بدون ما نخسر أي بيانات سابقة."""
    out = {}
    for key, value in history.items():
        if not isinstance(value, dict):
            continue
        k = product_key(key) if key.startswith("http") else key
        if not k:
            continue
        price = value.get("price")
        out[k] = {
            "name": value.get("name", ""),
            "url": value.get("url") or (key if key.startswith("http") else ""),
            "image_url": value.get("image_url"),
            "price": price,
            "list_price": value.get("list_price"),
            "min_price": value.get("min_price", price),
            "max_price": value.get("max_price", price),
            "avg_price": value.get("avg_price", price),
            "samples": value.get("samples", 1 if price is not None else 0),
            "last_checked": value.get("last_checked"),
            "last_attempt": value.get("last_attempt") or value.get("last_checked"),
            "last_alert_price": value.get("last_alert_price"),
            "last_alert_at": value.get("last_alert_at"),
        }
    return out



def load_all_history():
    """يقرأ كل ملفات السجل الموجودة (prices_history.json وأي prices_history_batchN.json)
    ويدمجها بدون أي خسارة: لكل منتج نأخذ أحدث قراءة (حسب last_checked).
    الكتابة بعدها تكون دائمًا في ملف واحد: prices_history.json."""
    merged = {}
    for path in sorted(Path(".").glob("prices_history*.json")):
        raw = load_json(path, {})
        if not isinstance(raw, dict):
            print(f"تنبيه: تجاهلت {path.name} (شكله مو قاموس منتجات).")
            continue
        for key, value in migrate_history(raw).items():
            current = merged.get(key)
            if current is None:
                merged[key] = value
                continue
            newer = (value.get("last_checked") or "") > (current.get("last_checked") or "")
            only_has_price = value.get("price") is not None and current.get("price") is None
            if newer or only_has_price:
                merged[key] = value
    return merged


def empty_entry(name, url):
    return {
        "name": name,
        "url": url,
        "image_url": None,
        "price": None,
        "list_price": None,
        "min_price": None,
        "max_price": None,
        "avg_price": None,
        "samples": 0,
        "last_checked": None,
        "last_attempt": None,
        "last_alert_price": None,
        "last_alert_at": None,
    }


# --------------------------------------------------------------------------
# استخراج البيانات من صفحة أمازون
# --------------------------------------------------------------------------
def clean_price(text):
    """يستخرج أول رقم سعر من النص (يدعم الأرقام العربية والفواصل)."""
    if not text:
        return None
    text = text.translate(_ARABIC_DIGITS)
    m = re.search(r"\d[\d.,]*", text)
    if not m:
        return None
    s = m.group(0).replace(",", "").rstrip(".")
    try:
        value = float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def _first_unstruck_price(node):
    for ap in node.find_all(class_="a-price"):
        if ap.get("data-a-strike") == "true":
            continue
        off = ap.find(class_="a-offscreen")
        if off and off.get_text(strip=True):
            p = clean_price(off.get_text())
            if p:
                return p
    return None


def extract_price(soup, html):
    # 1) حاوية السعر الأساسية (صندوق الشراء) -- أدق مصدر
    for cid in CORE_PRICE_CONTAINER_IDS:
        node = soup.find(id=cid)
        if not node:
            continue
        ptp = node.find(class_="priceToPay")
        if ptp:
            off = ptp.find(class_="a-offscreen")
            if off:
                p = clean_price(off.get_text())
                if p:
                    return p
        p = _first_unstruck_price(node)
        if p:
            return p

    # 2) الأشكال القديمة
    for pid in LEGACY_PRICE_IDS:
        tag = soup.find(id=pid)
        if tag and tag.get_text(strip=True):
            p = clean_price(tag.get_text())
            if p:
                return p

    # 3) أي سعر غير مشطوب بالصفحة
    p = _first_unstruck_price(soup)
    if p:
        return p

    # 4) آخر محاولة: نص فيه "ر.س" أو "SAR" جنب رقم
    m = re.search(r"([\d٠-٩.,٫٬]+)\s*(?:ر\.?\s?س|SAR)", html)
    if m:
        return clean_price(m.group(1))
    return None


def extract_list_price(soup, html):
    """السعر الأصلي قبل الخصم (المشطوب) إن وُجد، وإلا None."""
    strike = soup.find(attrs={"data-a-strike": "true"})
    if strike:
        off = strike.find(class_="a-offscreen")
        if off and off.get_text(strip=True):
            p = clean_price(off.get_text())
            if p:
                return p

    tag = soup.find(class_="a-text-strike")
    if tag and tag.get_text(strip=True):
        p = clean_price(tag.get_text())
        if p:
            return p

    m = re.search(r"(?:كان|List Price)[^\d]{0,15}([\d.,]+)", html)
    if m:
        return clean_price(m.group(1))
    return None


def extract_image(soup):
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
            dynamic = tag.get("data-a-dynamic-image")
            if dynamic:
                try:
                    images = json.loads(dynamic)
                    if images:
                        img_url = next(iter(images))
                except (json.JSONDecodeError, StopIteration):
                    img_url = None
        if img_url and img_url.startswith("http"):
            return img_url
    return None


def fetch_product_details(session, url):
    """يرجع (بيانات, نوع_الخطأ, رسالة_الخطأ). البيانات = dict أو None."""
    try:
        resp = session.get(url, timeout=25, allow_redirects=True)
    except requests.RequestException as exc:
        return None, "network", f"خطأ في الاتصال: {exc}"

    if resp.status_code in (429, 503):
        return None, "blocked", f"أمازون رفض الطلب مؤقتًا (كود {resp.status_code})"
    if resp.status_code == 404:
        return None, "gone", "الصفحة غير موجودة (404)"
    if resp.status_code != 200:
        return None, "http", f"استجابة غير متوقعة (كود {resp.status_code})"

    html = resp.text
    low = html.lower()
    if "captcha" in low and ("robot" in low or "validatecaptcha" in low):
        return None, "blocked", "أمازون طلب تحقق (CAPTCHA)"

    soup = BeautifulSoup(html, "html.parser")
    price = extract_price(soup, html)
    if price is None:
        return None, "parse", "ما قدرت أجد السعر بالصفحة (ممكن المنتج غير متوفر أو تغيّر شكل الصفحة)"

    list_price = extract_list_price(soup, html)
    if list_price is not None and list_price <= price:
        list_price = None

    return {"price": price, "list_price": list_price, "image_url": extract_image(soup)}, None, None


# --------------------------------------------------------------------------
# تيليجرام
# --------------------------------------------------------------------------
def send_telegram_message(text, image_url=None):
    """يرجع True لو وصلت الرسالة (أو لو ما فيه إعداد تيليجرام وتم الطباعة فقط)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("تحذير: لم يتم ضبط TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID، سيتم طباعة الرسالة فقط:")
        print(text)
        return True

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

    for _ in range(2):
        try:
            resp = requests.post(api_url, data=payload, timeout=20)
        except requests.RequestException as exc:
            print(f"فشل إرسال رسالة تيليجرام: {exc}")
            return False
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            time.sleep(min(wait + 1, 60))
            continue
        if resp.status_code != 200 and image_url:
            print(f"فشل إرسال الصورة (كود {resp.status_code})، سيتم الإرسال كنص.")
            return send_telegram_message(text, image_url=None)
        if resp.status_code != 200:
            print(f"فشل تيليجرام (كود {resp.status_code}): {resp.text[:200]}")
        return resp.status_code == 200
    return False


def build_drop_message(name, url, old_price, price, list_price, is_below_usual, is_all_time_low):
    pct = (old_price - price) / old_price * 100
    lines = [
        f"🔻 <b>{name}</b>",
        f"نزل السعر من {old_price:.2f} إلى {price:.2f} ر.س (خصم {pct:.0f}%)",
    ]
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


# --------------------------------------------------------------------------
# منطق الإشعار
# --------------------------------------------------------------------------
def should_alert(old_price, price, existing, now):
    """هل هذا الانخفاض يستاهل إشعار؟ (يمنع التكرار والضجيج)."""
    if price >= old_price:
        return False
    drop_pct = (old_price - price) / old_price * 100
    if drop_pct < MIN_DROP_PERCENT:
        return False

    last_alert_price = existing.get("last_alert_price")
    last_alert_at = existing.get("last_alert_at")
    if last_alert_price is not None and last_alert_at:
        try:
            hours = (now - datetime.fromisoformat(last_alert_at)).total_seconds() / 3600
        except ValueError:
            hours = ALERT_COOLDOWN_HOURS + 1
        # خلال فترة التهدئة: نرسل فقط لو السعر أقل من آخر سعر أُرسل عنه
        if hours < ALERT_COOLDOWN_HOURS and price >= last_alert_price:
            return False
    return True


def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# --------------------------------------------------------------------------
# التشغيل الرئيسي
# --------------------------------------------------------------------------
def main():
    start = time.monotonic()
    products = load_json(PRODUCTS_FILE, [])
    history = load_all_history()

    if not products:
        print("لا توجد منتجات في products.json")
        return 0

    # منتجات فريدة حسب المفتاح (يتجاهل التكرار بروابط مختلفة لنفس المنتج)
    unique = {}
    no_key = 0
    for p in products:
        key = product_key(p.get("url"))
        if not key:
            no_key += 1
            continue
        unique.setdefault(key, p)

    total = len(unique)
    if no_key:
        print(f"تنبيه: {no_key} رابط ما قدرت أستخرج له مفتاح (ASIN/رابط مختصر) وتم تجاهله.")

    # الترتيب: اللي ما انفحص أبدًا أولًا، ثم الأقدم محاولة
    queue = sorted(unique.keys(), key=lambda k: (history.get(k) or {}).get("last_attempt") or "")

    session = requests.Session()
    session.headers.update(HEADERS)

    stats = {
        "checked": 0, "failed": 0, "drops": 0, "drops_suppressed": 0,
        "unconfirmed": 0, "raised": 0, "new": 0, "unchanged": 0,
    }
    consecutive_failures = 0
    stop_reason = None
    processed = 0

    try:
        for key in queue:
            if time.monotonic() - start > MAX_RUNTIME_SECONDS:
                stop_reason = "انتهت ميزانية الوقت (التشغيلة الجاية تكمل من هنا)"
                break

            product = unique[key]
            name = product.get("name", "منتج بدون اسم")
            url = product["url"]
            existing = history.get(key)
            now = utc_now()
            now_iso = iso(now)

            data, kind, msg = fetch_product_details(session, url)
            processed += 1

            if data is None:
                stats["failed"] += 1
                consecutive_failures += 1
                print(f"[{name}] {msg}")
                entry = existing or empty_entry(name, url)
                entry["last_attempt"] = now_iso
                history[key] = entry
                if kind == "blocked":
                    time.sleep(min(60 * consecutive_failures, 300))
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stop_reason = "فشل متتالي كثير (غالبًا حظر مؤقت من أمازون)، تم الإيقاف مبكرًا"
                    break
                if processed % SAVE_EVERY == 0:
                    save_json(HISTORY_FILE, history)
                polite_sleep()
                continue

            consecutive_failures = 0
            price = data["price"]
            list_price = data["list_price"]
            image_url = data["image_url"]
            stats["checked"] += 1

            old_price = existing.get("price") if existing else None
            alert_sent = False

            if old_price is None:
                # أول قراءة ناجحة: خط أساس بصمت
                stats["new"] += 1
                new_min = new_max = new_avg = price
                new_samples = 1
            else:
                old_avg = existing.get("avg_price")
                old_min = existing.get("min_price")
                old_max = existing.get("max_price")
                old_samples = existing.get("samples", 0)

                is_below_usual = (
                    old_avg is not None and old_samples >= 2 and price < old_avg * BELOW_USUAL_RATIO
                )
                is_all_time_low = old_min is not None and price < old_min

                if price < old_price:
                    if should_alert(old_price, price, existing, now):
                        # تأكيد: نعيد الجلب مرة وحدة ونتأكد إن السعر المنخفض ثابت
                        time.sleep(random.uniform(2.0, 4.0))
                        c_data, _, _ = fetch_product_details(session, url)
                        if c_data is None or abs(c_data["price"] - price) > 0.01:
                            stats["unconfirmed"] += 1
                            print(f"[{name}] انخفاض غير مؤكد ({old_price} -> {price})، تم تجاهله هالمرة.")
                            existing["last_attempt"] = now_iso
                            history[key] = existing
                            polite_sleep()
                            continue

                        message = build_drop_message(
                            name, url, old_price, price, list_price, is_below_usual, is_all_time_low
                        )
                        if send_telegram_message(message, image_url=image_url or existing.get("image_url")):
                            alert_sent = True
                            stats["drops"] += 1
                            time.sleep(1.2)  # احترام حد معدل تيليجرام
                        else:
                            # فشل الإرسال: نترك السعر القديم بالسجل عشان نعيد المحاولة بالجاية
                            existing["last_attempt"] = now_iso
                            history[key] = existing
                            polite_sleep()
                            continue
                    else:
                        stats["drops_suppressed"] += 1
                elif price > old_price:
                    stats["raised"] += 1
                else:
                    stats["unchanged"] += 1

                new_min = min(old_min, price) if old_min is not None else price
                new_max = max(old_max, price) if old_max is not None else price
                new_avg = ((old_avg if old_avg is not None else price) * old_samples + price) / (old_samples + 1)
                new_samples = old_samples + 1

            history[key] = {
                "name": name,
                "url": url,
                "image_url": image_url or (existing.get("image_url") if existing else None),
                "price": price,
                "list_price": list_price,
                "min_price": new_min,
                "max_price": new_max,
                "avg_price": round(new_avg, 2),
                "samples": new_samples,
                "last_checked": now_iso,
                "last_attempt": now_iso,
                "last_alert_price": price if alert_sent else (existing.get("last_alert_price") if existing else None),
                "last_alert_at": now_iso if alert_sent else (existing.get("last_alert_at") if existing else None),
            }

            if processed % SAVE_EVERY == 0:
                save_json(HISTORY_FILE, history)
            polite_sleep()
    finally:
        save_json(HISTORY_FILE, history)

    never_left = sum(1 for k in unique if not (history.get(k) or {}).get("last_attempt"))
    if stop_reason:
        print(f"توقف: {stop_reason}")
    print(
        f"تم: فُحص={stats['checked']}، فشل={stats['failed']}، "
        f"انخفاضات تم إشعارها={stats['drops']}، انخفاضات متجاهلة (صغيرة/مكررة)={stats['drops_suppressed']}، "
        f"غير مؤكدة={stats['unconfirmed']}، ارتفاعات (بدون إشعار)={stats['raised']}، "
        f"جديدة={stats['new']}، بدون تغيير={stats['unchanged']}"
    )
    print(
        f"checked={stats['checked']} failed={stats['failed']} drops={stats['drops']} "
        f"raised={stats['raised']} new={stats['new']} never_checked_left={never_left} total={total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
