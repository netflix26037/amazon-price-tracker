"""
diagnose.py - فحص لمرة وحدة (طلب واحد لأمازون فقط) يحدد سبب توقف البوت.
يرسل النتيجة على تلغرام ويطبعها في سجل التشغيل.
"""
import os
import subprocess
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
SAMPLE_ASIN = os.environ.get("SAMPLE_ASIN", "B0DHXMQHPC")

CAPTCHA_MARKERS = ("captcha", "robot check", "validatecaptcha",
                   "api-services-support@amazon.com", "automated access")


def classify_amazon(status, body):
    low = (body or "").lower()
    if status in (429, 503):
        return "blocked", f"أمازون رجّعت HTTP {status} (حظر/ضغط طلبات)"
    if any(m in low for m in CAPTCHA_MARKERS):
        return "blocked", "أمازون رجّعت صفحة تحقق (CAPTCHA) بدل المنتج"
    if status != 200:
        return "error", f"رد غير متوقع HTTP {status}"
    if "a-price" in low or "priceblock" in low or "apexprice" in low:
        return "ok", "الصفحة وصلت وفيها بيانات سعر"
    return "unknown", "الصفحة وصلت لكن ما لقيت عنصر السعر (تغيّر تصميم الصفحة أو المنتج غير متوفر)"


def check_telegram():
    if not TOKEN or not CHAT:
        return False, "TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير موجودين في Secrets"
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=15)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "توكن البوت صالح"
        return False, f"تلغرام رفض التوكن (HTTP {r.status_code}) - غالبًا تغيّر أو انحذف"
    except Exception as e:
        return False, f"تعذر الاتصال بتلغرام: {e}"


def check_amazon():
    try:
        r = requests.get(
            f"https://www.amazon.sa/dp/{SAMPLE_ASIN}",
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ar,en;q=0.8"},
            timeout=25,
        )
        return classify_amazon(r.status_code, r.text)
    except Exception as e:
        return "error", f"تعذر الاتصال بأمازون: {e}"


def last_commits():
    try:
        out = subprocess.run(
            ["git", "log", "-5", "--format=%cI | %an | %s"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return out or "لا يوجد سجل"
    except Exception as e:
        return f"تعذر قراءة السجل: {e}"


def send(text):
    if not (TOKEN and CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": text}, timeout=15)
    except Exception:
        pass


if __name__ == "__main__":
    tg_ok, tg_msg = check_telegram()
    az_state, az_msg = check_amazon()
    commits = last_commits()
    report = (
        "🔎 تشخيص البوت\n"
        f"- تلغرام: {'✅' if tg_ok else '❌'} {tg_msg}\n"
        f"- أمازون (من سيرفر GitHub): {az_state} - {az_msg}\n"
        f"- آخر 5 عمليات commit:\n{commits}"
    )
    print(report)
    if tg_ok:
        send(report)
