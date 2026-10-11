"""
check_prices.py

يعالج ثلاث مشاكل:
  1) تغطية كل المنتجات بالتناوب: كل تشغيل يفحص "الأقدم فحصًا" أولًا (round-robin)،
     فبعد عدة تشغيلات كل منتج في products.json ياخذ دوره.
  2) الحالة تنحفظ في state.json (السعر + وقت آخر فحص) والـ workflow يسوي commit لها،
     فما يتكرر نفس المنتج ولا نفس السعر.
  3) إشعار تلغرام فقط لما السعر ينخفض. الزيادة وعدم التغيّر بدون أي رسالة.

جلب السعر: عدّل الدالة get_prices() فقط. لازم ترجع {asin: (price, currency)}
والمنتج اللي فشل جلب سعره ما يدخل في الناتج (ما يتحدّث ولا ينرسل عنه شي).

متغيرات البيئة (GitHub Secrets / env):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  MAX_PER_RUN     عدد المنتجات في كل تشغيل (افتراضي 500)
  BATCH_SIZE      حجم الدفعة لكل طلب (افتراضي 10، حد PA-API)
  MIN_DROP_PCT    أقل نسبة انخفاض تستحق إشعار (افتراضي 1)
  NOTIFY_NEW      1 لإرسال إشعار للمنتجات الجديدة (افتراضي 0)
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests

PRODUCTS_FILE = os.environ.get("PRODUCTS_FILE", "products.json")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "500"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
MIN_DROP_PCT = float(os.environ.get("MIN_DROP_PCT", "1"))
NOTIFY_NEW = os.environ.get("NOTIFY_NEW", "0") == "1"
DELAY_BETWEEN_BATCHES = float(os.environ.get("DELAY_BETWEEN_BATCHES", "1.2"))

ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")


def extract_asin(url):
    m = ASIN_RE.search(url or "")
    return m.group(1) if m else None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_prices(asins):
    """
    اربطها بمصدر الأسعار الخاص فيك.
    ترجع {asin: (price: float, currency: str)} وتتجاهل أي منتج فشل جلبه.
    الموصى فيه: Amazon PA-API (GetItems) فيه 10 منتجات لكل طلب.
    """
    raise NotImplementedError("اربط get_prices بمصدر الأسعار (PA-API).")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram skipped]\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except Exception as e:
        print(f"telegram error: {e}")


def format_drop(name, asin, old, new, currency):
    pct = (old - new) / old * 100
    return (
        f"⬇️ <b>{name}</b>\n"
        f"من {old:g} إلى {new:g} {currency or ''} (−{pct:.0f}%)\n"
        f"https://www.amazon.sa/dp/{asin}"
    )


def send_in_chunks(lines, per_message=8):
    for i in range(0, len(lines), per_message):
        send_telegram("\n\n".join(lines[i:i + per_message]))
        time.sleep(1.1)  # حد تلغرام للرسائل


def sync_state(products, state):
    """يضيف المنتجات الجديدة للـ state بدون أي إشعار، ولا يحذف القديمة."""
    new_asins = []
    for p in products:
        asin = extract_asin(p.get("url"))
        if not asin:
            continue
        if asin not in state:
            state[asin] = {
                "name": p.get("name", ""),
                "price": None,
                "currency": None,
                "last_checked": None,
            }
            new_asins.append(asin)
        else:
            state[asin]["name"] = p.get("name", state[asin].get("name", ""))
    return new_asins


def pick_batch(state, valid_asins, limit):
    """الأقدم فحصًا أولًا. اللي ما انفحص أبدًا يجي قبل الكل."""
    candidates = [a for a in state if a in valid_asins]
    candidates.sort(key=lambda a: state[a].get("last_checked") or "")
    return candidates[:limit]


def run(products=None, state=None, price_fn=None, notify=send_in_chunks):
    products = products if products is not None else load_json(PRODUCTS_FILE, [])
    state = state if state is not None else load_json(STATE_FILE, {})
    price_fn = price_fn or get_prices

    valid_asins = {extract_asin(p.get("url")) for p in products} - {None}
    new_asins = sync_state(products, state)

    if NOTIFY_NEW and new_asins:
        notify([f"🆕 <b>{state[a]['name']}</b>\nhttps://www.amazon.sa/dp/{a}"
                for a in new_asins[:50]])

    batch = pick_batch(state, valid_asins, MAX_PER_RUN)
    drops, checked, failed = [], 0, 0

    for i in range(0, len(batch), BATCH_SIZE):
        chunk = batch[i:i + BATCH_SIZE]
        try:
            prices = price_fn(chunk)
        except NotImplementedError:
            print("get_prices غير مربوطة بمصدر أسعار بعد. توقف.")
            break
        except Exception as e:
            print(f"batch failed: {e}")
            failed += len(chunk)
            continue

        for asin in chunk:
            if asin not in prices:
                failed += 1
                continue  # ما نلمس last_checked، يبقى أولوية للتشغيل الجاي
            price, currency = prices[asin]
            entry = state[asin]
            old = entry.get("price")
            entry["last_checked"] = now_iso()
            checked += 1

            if old is not None and price < old:
                if (old - price) / old * 100 >= MIN_DROP_PCT:
                    drops.append(format_drop(entry["name"], asin, old, price, currency))

            entry["price"] = price
            entry["currency"] = currency

        time.sleep(DELAY_BETWEEN_BATCHES)

    save_json(STATE_FILE, state)
    if drops:
        notify(drops)

    unchecked = sum(1 for a in valid_asins if not state.get(a, {}).get("last_checked"))
    print(f"checked={checked} failed={failed} drops={len(drops)} "
          f"new={len(new_asins)} never_checked_left={unchecked} total={len(valid_asins)}")
    return {"checked": checked, "failed": failed, "drops": len(drops)}


if __name__ == "__main__":
    run()
