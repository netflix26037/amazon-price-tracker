"""جلب الأسعار من الـ API الرسمية لأمازون (Creators API) بدل قراءة صفحات الموقع.

ليش؟ أمازون يحظر عناوين خوادم GitHub عند قراءة الصفحات، فتتعطل التشغيلات.
الـ API الرسمية ما تتعرض لهالحظر، وتجيب 10 منتجات بطلب واحد.

يحتاج 3 أسرار في GitHub (Settings > Secrets and variables > Actions):
  CREATORS_API_CREDENTIAL_ID      معرّف الاعتماد
  CREATORS_API_CREDENTIAL_SECRET  سر الاعتماد
  CREATORS_API_VERSION            نسخة الـ API (مثل 3.2) كما تظهر في Associates Central
اختياريًا:
  CREATORS_API_TAG      (الافتراضي mohammedala02-21)
  CREATORS_API_COUNTRY  (الافتراضي SA)

لو الأسرار ناقصة أو المكتبة غير مثبتة، يرجع build_client() بـ None
وسكربت الفحص يكمل بالطريقة القديمة (قراءة الصفحات).
"""

import os

try:  # المكتبة اختيارية: لو غير موجودة نرجع للطريقة القديمة
    from amazon_creatorsapi import AmazonCreatorsApi
    from amazon_creatorsapi.errors import (
        AccessDeniedError,
        AssociateValidationError,
        AuthenticationError,
        InvalidArgumentError,
        ItemsNotFoundError,
    )
    from creatorsapi_python_sdk.models.get_items_resource import GetItemsResource

    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False

BATCH_SIZE = 10  # حد أمازون: 10 منتجات بالطلب الواحد


class FatalApiError(Exception):
    """خطأ ما ينفع معه إعادة المحاولة (اعتماد خاطئ أو ما لك صلاحية)."""


class TemporaryApiError(Exception):
    """خطأ مؤقت (ازدحام، انقطاع شبكة...)؛ ممكن ينجح لاحقًا."""


def _pick_listing(item):
    offers = getattr(item, "offers_v2", None)
    listings = list(getattr(offers, "listings", None) or [])
    listings = [
        li for li in listings
        if getattr(getattr(getattr(li, "price", None), "money", None), "amount", None) is not None
    ]
    if not listings:
        return None
    for li in listings:
        if getattr(li, "is_buy_box_winner", False):
            return li
    return listings[0]


def parse_item(item):
    """يحوّل منتج الـ API إلى {price, list_price, image_url} أو None لو ما له عرض."""
    listing = _pick_listing(item)
    if listing is None:
        return None
    price = float(listing.price.money.amount)
    if price <= 0:
        return None

    list_price = None
    basis = getattr(listing.price, "saving_basis", None)
    basis_amount = getattr(getattr(basis, "money", None), "amount", None)
    if basis_amount is not None and float(basis_amount) > price:
        list_price = float(basis_amount)

    image_url = None
    primary = getattr(getattr(item, "images", None), "primary", None)
    for size in ("large", "medium", "small"):
        image_url = getattr(getattr(primary, size, None), "url", None)
        if image_url:
            break

    return {"price": price, "list_price": list_price, "image_url": image_url}


class Client:
    def __init__(self, api):
        self._api = api
        self._resources = [
            GetItemsResource.OFFERS_V2_DOT_LISTINGS_DOT_PRICE,
            GetItemsResource.OFFERS_V2_DOT_LISTINGS_DOT_IS_BUY_BOX_WINNER,
            GetItemsResource.IMAGES_DOT_PRIMARY_DOT_LARGE,
        ]

    def lookup(self, asins):
        """يرجع {asin: بيانات أو None}. None = المنتج موجود لكن بدون عرض حالي.
        المنتج اللي ما رجع من أمازون أبدًا ما يظهر بالنتيجة."""
        asins = list(dict.fromkeys(asins))
        results = {}
        for i in range(0, len(asins), BATCH_SIZE):
            chunk = asins[i:i + BATCH_SIZE]
            try:
                items = self._api.get_items(chunk, resources=self._resources)
            except ItemsNotFoundError:
                # أمازون ما لقي ولا منتج من هالدفعة: نعتبرها بدون عرض
                for a in chunk:
                    results[a] = None
                continue
            except (AuthenticationError, AccessDeniedError, AssociateValidationError, InvalidArgumentError) as exc:
                raise FatalApiError(f"{type(exc).__name__}: {exc}") from exc
            except Exception as exc:  # ازدحام/شبكة/غيره
                raise TemporaryApiError(f"{type(exc).__name__}: {exc}") from exc

            returned = set()
            for item in items:
                asin = getattr(item, "asin", None)
                if not asin:
                    continue
                returned.add(asin)
                results[asin] = parse_item(item)
            for a in chunk:
                if a not in returned:
                    results[a] = None
        return results


def build_client():
    """يرجع Client جاهز، أو None لو الإعدادات ناقصة."""
    if not _AVAILABLE:
        print("مكتبة Creators API غير مثبتة؛ سأستخدم قراءة الصفحات.")
        return None
    cred_id = os.environ.get("CREATORS_API_CREDENTIAL_ID", "").strip()
    secret = os.environ.get("CREATORS_API_CREDENTIAL_SECRET", "").strip()
    version = os.environ.get("CREATORS_API_VERSION", "").strip()
    if not (cred_id and secret and version):
        print("أسرار Creators API غير مضبوطة؛ سأستخدم قراءة الصفحات.")
        return None
    api = AmazonCreatorsApi(
        credential_id=cred_id,
        credential_secret=secret,
        version=version,
        tag=os.environ.get("CREATORS_API_TAG", "mohammedala02-21").strip(),
        country=os.environ.get("CREATORS_API_COUNTRY", "SA").strip(),
    )
    return Client(api)
