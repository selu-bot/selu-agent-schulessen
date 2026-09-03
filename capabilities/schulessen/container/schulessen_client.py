from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPCookieProcessor,
    Request,
    build_opener,
)
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class SchulessenError(RuntimeError):
    pass


class AuthenticationError(SchulessenError):
    pass


class ApiError(SchulessenError):
    pass


class UncertainMutationError(SchulessenError):
    """The server may have applied a write. Never automatically replay it."""


BERLIN = ZoneInfo("Europe/Berlin")
MAX_RANGE_DAYS = 93
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
READ_ENDPOINTS = {"MenuOffer", "ShoppingCard"}
WRITE_ENDPOINTS = {"ShoppingCardAdd", "ShoppingCardDelete", "ShoppingCardPay"}


def _today() -> date:
    return datetime.now(BERLIN).date()


LOGIN_PATH = "/Vorbesteller/Default.aspx"
LOGIN_REFERER = "https://www.schulessen.net/Vorbesteller/Default.aspx"
HOME_URL = "https://www.schulessen.net/vorbesteller/"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _ensure_date(value: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError("Expected a calendar date in YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            "Expected a valid calendar date in YYYY-MM-DD format"
        ) from None
    return value


def _default_date_range(from_date: str | None, to_date: str | None) -> tuple[str, str]:
    if from_date is None:
        start = _today()
    else:
        start = datetime.strptime(_ensure_date(from_date), "%Y-%m-%d").date()

    if to_date is None:
        end = start + timedelta(days=max(0, 4 - start.weekday()))
    else:
        end = datetime.strptime(_ensure_date(to_date), "%Y-%m-%d").date()

    if end < start:
        raise ValueError("to_date must not be earlier than from_date")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise ValueError(f"Date range must not exceed {MAX_RANGE_DAYS} days")

    return start.isoformat(), end.isoformat()


def _extract_hidden_fields(html: str) -> dict[str, str]:
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.fields = {}

        def handle_starttag(self, tag, attrs):
            if tag.lower() == "input":
                attrs = dict(attrs)
                self.fields[attrs.get("id") or attrs.get("name")] = attrs.get(
                    "value", ""
                )

    parser = Inputs()
    parser.feed(html)
    fields = {}
    for key in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "ESSID"):
        if key not in parser.fields:
            raise AuthenticationError(f"Login page is missing hidden field '{key}'")
        fields[key] = parser.fields[key]
    return fields


def _decode_possible_json(value: Any) -> Any:
    current = value
    while isinstance(current, str):
        text = current.strip()
        if not text:
            return text
        if text[0] not in "[{":
            return current
        try:
            current = json.loads(text)
        except json.JSONDecodeError:
            return current
    return current


def _maybe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _find_value(obj: Any, *candidate_keys: str) -> Any:
    if not isinstance(obj, dict):
        return None

    normalized = {_normalized_key(k): v for k, v in obj.items()}
    for key in candidate_keys:
        hit = normalized.get(_normalized_key(key))
        if hit is not None:
            return hit
    return None


def _integer(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _extract_price(value: Any) -> tuple[int | None, str | None]:
    data = _maybe_dict(_decode_possible_json(value))
    if data:
        amount = _find_value(data, "value", "preis", "price", "betrag", "amount")
        currency = _find_value(data, "currency", "waehrung", "curr")
        return _integer(amount), currency if isinstance(currency, str) else None
    return _integer(value), None


def _to_bool(value: Any, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        if value.strip().lower() in {"true", "1", "yes", "ja", "y"}:
            return True
        if value.strip().lower() in {"false", "0", "no", "nein", "n"}:
            return False
    return default


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = Text()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split()) or None


def _unwrap_payload(payload: Any) -> Any:
    current = payload
    for _ in range(8):
        current = _decode_possible_json(current)
        if not isinstance(current, dict):
            return current
        if "success" in current and _to_bool(current["success"]) is not True:
            # Server messages can contain HTML, identifiers, or credentials.
            raise ApiError("schulessen.net rejected the request")
        if "d" in current:
            current = current["d"]
        elif "parameter" in current:
            current = current["parameter"]
        else:
            return current
    raise ApiError("schulessen.net returned too many response wrappers")


def _required_list(container: Any, *keys: str) -> list:
    value = container if isinstance(container, list) else _find_value(container, *keys)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ApiError(
            "schulessen.net response schema changed; status cannot be determined"
        )
    return value


def _deadline(value: Any) -> datetime | None:
    # The website passes these values to JavaScript Date: milliseconds since epoch.
    millis = _integer(value)
    if millis is None:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, BERLIN)
    except (ValueError, OverflowError, OSError):
        return None


def _normalize_day_offers(
    payload: Any, include_inactive: bool, *, now: datetime | None = None
) -> dict[str, Any]:
    container = _unwrap_payload(payload)
    days = _required_list(container, "dayoffer", "dayoffers", "days", "angebote")
    now = now or datetime.now(BERLIN)
    normalized_days = []
    for raw_day in days:
        day_date = _ensure_date(_find_value(raw_day, "datum", "date", "day"))
        delivery = _to_bool(
            _find_value(raw_day, "is_todeliver", "to_deliver", "deliver")
        )
        closed = _to_bool(_find_value(raw_day, "is_closed"))
        reason = _plain_text(_find_value(raw_day, "reason_closed"))
        no_service = closed is True or bool(reason) or delivery is False
        meals = []
        for raw_menu in _required_list(
            raw_day, "menus", "menu", "menues", "mealoffers"
        ):
            active = _to_bool(_find_value(raw_menu, "is_active", "active", "aktiv"))
            if not include_inactive and active is False:
                continue
            description = _plain_text(
                _find_value(raw_menu, "gastro_text", "description", "text")
            )
            orderable = _to_bool(_find_value(raw_menu, "is_orderable", "orderable"))
            last_order = _deadline(_find_value(raw_menu, "last_order"))
            last_cancel = _deadline(_find_value(raw_menu, "last_cancel"))
            meal_id = _integer(
                _find_value(raw_menu, "id_menuline", "id_mline", "meal_id", "menu_id")
            )
            quota = _maybe_dict(_find_value(raw_menu, "quota"))
            quota_id = _integer(quota.get("id"))
            quota_remaining = (
                _integer(quota.get("units_remain"))
                if quota_id not in (None, 0)
                else None
            )
            maximum = _integer(_find_value(raw_day, "units_max_orderable"))
            if no_service:
                availability = "no_service"
            elif not description:
                availability = "no_offer"
            elif active is False or orderable is False:
                availability = "unavailable"
            elif last_order is not None and now >= last_order:
                availability = "deadline_passed"
            elif (quota_remaining is not None and quota_remaining <= 0) or maximum == 0:
                availability = "sold_out"
            elif (
                active is not True
                or orderable is not True
                or delivery is not True
                or closed is not False
                or last_order is None
                or meal_id is None
                or meal_id <= 0
                or (quota_id not in (None, 0) and quota_remaining is None)
            ):
                availability = "unknown"
            else:
                availability = "orderable"
            price, currency = _extract_price(
                _find_value(raw_menu, "price_personal", "price", "preis")
            )
            meals.append(
                {
                    "meal_id": meal_id,
                    "name": _plain_text(
                        _find_value(raw_menu, "name_menulinie", "name", "title")
                    ),
                    # Mirror the website: never expose a hidden dish as today's meal.
                    "description": None if no_service else description,
                    "price_cents": None if no_service else price,
                    "currency": currency,
                    "is_active": active,
                    "is_orderable": orderable,
                    "can_order": availability == "orderable",
                    "availability": availability,
                    "availability_reason": reason if no_service else None,
                    "order_deadline": last_order.isoformat() if last_order else None,
                    "cancellation_deadline": last_cancel.isoformat()
                    if last_cancel
                    else None,
                    "max_quantity": maximum,
                    "quota_remaining": quota_remaining,
                    "is_ordered": _to_bool(
                        _find_value(raw_menu, "is_ordered", "ordered", "selected")
                    ),
                }
            )
        normalized_days.append(
            {
                "date": day_date,
                "is_delivery": delivery,
                "is_closed": closed,
                "reason_closed": reason,
                "availability": "no_service"
                if no_service
                else ("listed" if meals else "no_offer"),
                "can_order": any(meal["can_order"] for meal in meals),
                "meals": meals,
            }
        )
    normalized_days.sort(key=lambda day: day["date"])
    return {
        "from_date": normalized_days[0]["date"] if normalized_days else None,
        "to_date": normalized_days[-1]["date"] if normalized_days else None,
        "days": normalized_days,
        "day_count": len(normalized_days),
        "meal_count": sum(len(day["meals"]) for day in normalized_days),
        "orderable_meal_count": sum(
            meal["can_order"] for day in normalized_days for meal in day["meals"]
        ),
        "timezone": "Europe/Berlin",
    }


def _normalize_cart(payload: Any) -> dict[str, Any]:
    container = _unwrap_payload(payload)
    # Known envelopes only. Recursively guessing at objects with dates can turn
    # metadata into orders, or malformed responses into an apparently empty cart.
    nested = _find_value(container, "shoppingcard", "shopping_card")
    if isinstance(nested, dict):
        container = nested
    rows = _required_list(container, "items", "shoppingcard", "shopping_card")
    items = []
    for raw in rows:
        transaction_id = _find_value(
            raw, "id_transaction", "transaction_id", "idtransaction"
        )
        meal_id = _integer(
            _find_value(raw, "id_mline", "id_menuline", "oid_mlinie", "meal_id")
        )
        meal_date = _ensure_date(_find_value(raw, "date_delivery", "datum", "date"))
        if (
            meal_id is None
            or meal_id <= 0
            or not isinstance(transaction_id, (str, int))
            or isinstance(transaction_id, bool)
        ):
            raise ApiError(
                "Cart item is missing a valid meal or transaction identifier"
            )
        quantity = _integer(
            _find_value(raw, "units_ordered", "anzahl", "quantity", "count")
        )
        saved = _integer(_find_value(raw, "units_saved"))
        paid = _integer(_find_value(raw, "units_paid"))
        price, currency = _extract_price(
            _find_value(raw, "price_per_unit", "price", "preis", "gross_amount")
        )
        payable, _ = _extract_price(
            _find_value(raw, "payable_amount", "payable", "offen")
        )
        components = _find_value(raw, "components")
        if components is not None and (
            not isinstance(components, list)
            or any(not isinstance(c, dict) for c in components)
        ):
            raise ApiError("Cart component schema changed; status cannot be determined")
        component_changes = any(
            _to_bool(c.get("has_changed")) is not False for c in (components or [])
        )
        pending = (
            (payable is not None and payable != 0)
            or component_changes
            or (quantity is not None and saved is not None and quantity != saved)
        )
        if (
            quantity is None
            or quantity < 0
            or payable is None
            or (saved is None and paid is None)
            or (saved is not None and saved < 0)
            or (paid is not None and paid < 0)
            or any(_to_bool(c.get("has_changed")) is None for c in (components or []))
        ):
            status = "unknown"
        elif pending:
            status = "pending_cancellation" if quantity == 0 else "pending"
        elif quantity == saved or (saved is None and quantity == paid):
            status = "active" if quantity > 0 else "cancelled"
        else:
            status = "unknown"
        items.append(
            {
                "transaction_id": str(transaction_id),
                "meal_id": meal_id,
                "date": meal_date,
                "name": _plain_text(
                    _find_value(raw, "name_menuline", "name_menu", "name", "title")
                ),
                "description": _plain_text(
                    _find_value(raw, "gastro_text", "description", "text")
                ),
                "quantity": quantity,
                "price_cents": price,
                "currency": currency,
                "payable_amount_cents": payable,
                "units_paid": paid,
                "units_saved": saved,
                # Detect selection edits even when quantity and total stay the same;
                # do not expose raw component payloads to the conversation.
                "selection_digest": hashlib.sha256(
                    json.dumps(
                        {
                            "components": components,
                            "slot": _find_value(raw, "id_outlet_slot"),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "is_cancellation_allowed": _to_bool(
                    _find_value(
                        raw, "is_cancelcation_allowed", "is_cancellation_allowed"
                    )
                ),
                "status": status,
            }
        )
    active = [item for item in items if item["status"] == "active"]
    cancelled = [item for item in items if item["status"] == "cancelled"]
    pending = [
        item for item in items if item["status"] in {"pending", "pending_cancellation"}
    ]
    unknown = [item for item in items if item["status"] == "unknown"]
    # Never infer account balance from a line item's amount.
    balance, _ = _extract_price(
        _find_value(container, "saldo", "balance", "kartenbetrag", "bank_balance")
    )
    return {
        "items": items,
        "item_count": len(items),
        "active_items": active,
        "active_item_count": len(active),
        "cancelled_items": cancelled,
        "cancelled_item_count": len(cancelled),
        "pending_items": pending,
        "pending_item_count": len(pending),
        "unknown_items": unknown,
        "unknown_item_count": len(unknown),
        "has_active_order": True if active else (None if pending or unknown else False),
        "has_cancelled_history": bool(cancelled),
        "balance_cents": balance,
        "status_known": not bool(pending or unknown),
    }


class SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if (
            urlsplit(newurl).scheme != "https"
            or urlsplit(newurl).netloc != "www.schulessen.net"
        ):
            raise ApiError("Blocked redirect outside schulessen.net")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class LoginCredentials:
    username: str
    password: str = field(repr=False)


SESSION_MAX_AGE_SECONDS = (
    15 * 60
)  # 15 minutes – well within ASP.NET's default 20-min timeout


class SchulessenClient:
    def __init__(self, base_url: str = "https://www.schulessen.net") -> None:
        if base_url.rstrip("/") != "https://www.schulessen.net":
            raise ValueError("Only https://www.schulessen.net is supported")
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = CookieJar()
        self.opener = build_opener(
            SameOriginRedirectHandler(), HTTPCookieProcessor(self.cookie_jar)
        )
        self._credentials: LoginCredentials | None = None
        self._authenticated_at: float | None = None

    def set_credentials(self, username: str, password: str) -> None:
        creds = LoginCredentials(username=username, password=password)
        if self._credentials != creds:
            self.cookie_jar.clear()
            self._authenticated_at = None
            self._credentials = creds

    def login(self) -> dict[str, Any]:
        if not self._credentials:
            raise AuthenticationError("Missing schulessen.net credentials")

        self.cookie_jar.clear()
        self._authenticated_at = None
        html = self._request_text("GET", LOGIN_PATH)
        hidden = _extract_hidden_fields(html)

        self._request_text(
            "GET",
            "/vorbesteller/Pages.aspx?src=ESS.js",
            headers={"Referer": LOGIN_REFERER, "Accept": "*/*"},
        )
        self._request_text(
            "GET",
            "/vorbesteller/Pages.aspx?src=MINTEC_FO.js",
            headers={"Referer": LOGIN_REFERER, "Accept": "*/*"},
        )

        username = quote(self._credentials.username, safe="")
        password = quote(self._credentials.password, safe="")

        body = "&".join(
            [
                "__EVENTTARGET=",
                "__EVENTARGUMENT=",
                f"__VIEWSTATE={quote(hidden['__VIEWSTATE'], safe='')}",
                f"__VIEWSTATEGENERATOR={quote(hidden['__VIEWSTATEGENERATOR'], safe='')}",
                f"__EVENTVALIDATION={quote(hidden['__EVENTVALIDATION'], safe='')}",
                f"Login1%24UserName={username}",
                f"Login1%24Password={password}",
                "Login1%24LoginButton=Anmelden",
                "TransferValues=",
                "txtKartenbetrag=",
                "txtLocation=",
                "txtTouch=",
                "WebStyle=Default",
                "InfoPortal=",
                f"HomeUrl={quote(HOME_URL, safe='')}",
                "IFilialeHost=",
                "ISIFrame=",
                "ESSLanguage=",
                "ESSTranslation=",
                f"ESSID={quote(hidden['ESSID'], safe='')}",
            ]
        ).encode("utf-8")

        self._request_text(
            "POST",
            LOGIN_PATH,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": LOGIN_REFERER,
                "Origin": self.base_url,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        if not self.is_authenticated():
            raise AuthenticationError(
                "Couldn't sign in to schulessen.net. Check username and password."
            )

        self._authenticated_at = time.time()
        logger.info("Successfully authenticated to schulessen.net")
        return {"authenticated": True}

    def is_authenticated(self) -> bool:
        now = time.time()

        # Guard 1: proactive time-based expiry.
        # ASP.NET session cookies often lack an "expires" attribute, so the
        # CookieJar never considers them stale.  We track when we last
        # successfully logged in and treat the session as dead once
        # SESSION_MAX_AGE_SECONDS have elapsed.
        if self._authenticated_at is not None:
            age = now - self._authenticated_at
            if age >= SESSION_MAX_AGE_SECONDS:
                logger.info(
                    "Session age %.0fs exceeds max %ds, clearing session",
                    age,
                    SESSION_MAX_AGE_SECONDS,
                )
                self.cookie_jar.clear()
                self._authenticated_at = None
                return False

        # Guard 2: cookie-level expiry (for cookies that *do* carry an
        # explicit expiry timestamp).
        for cookie in self.cookie_jar:
            if "ASPXAUTH" in cookie.name.upper():
                if cookie.expires is not None and cookie.expires <= now:
                    logger.info(
                        "ASPXAUTH cookie expired (expires=%s, now=%s), clearing session",
                        cookie.expires,
                        now,
                    )
                    self.cookie_jar.clear()
                    self._authenticated_at = None
                    return False
                return True
        return False

    def get_menu(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        start, end = _default_date_range(from_date, to_date)
        raw = self._call_api(
            "/vorbesteller/OrderForm.aspx/MenuOffer",
            {"vonDatum": start, "bisDatum": end, "idx_splan": 0},
        )
        result = _normalize_day_offers(raw, include_inactive=include_inactive)
        if any(not start <= day["date"] <= end for day in result["days"]):
            raise ApiError("Menu response contains dates outside the requested range")
        result["from_date"] = start
        result["to_date"] = end
        return result

    def get_cart(self) -> dict[str, Any]:
        return self.get_cart_for_range()

    def get_cart_for_range(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        start, end = _default_date_range(from_date, to_date)
        raw = self._call_api(
            "/vorbesteller/OrderForm.aspx/ShoppingCard",
            {"vonDatum": start, "bisDatum": end},
        )
        result = _normalize_cart(raw)
        result["from_date"] = start
        result["to_date"] = end
        if any(not start <= item["date"] <= end for item in result["items"]):
            raise ApiError("Cart response contains dates outside the requested range")
        return result

    def _checkout_cart(self, meal_date: str) -> dict[str, Any]:
        # Match the first-party checkout UI's current-week + 90-day window.
        today = _today()
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=90)
        if not start <= date.fromisoformat(meal_date) <= end:
            raise ValueError(
                "Mutation date must be within the website's current checkout window"
            )
        return self.get_cart_for_range(start.isoformat(), end.isoformat())

    @staticmethod
    def _assert_clean_cart(cart: dict, allow_existing: bool = False) -> None:
        if cart["unknown_item_count"]:
            raise ApiError(
                "Cart status is incomplete. Check the website before changing orders."
            )
        if cart["pending_item_count"] and not allow_existing:
            raise ApiError(
                "The cart contains pending changes, possibly on another day. Review them on the website first."
            )

    @staticmethod
    def _other_items(cart: dict, meal_date: str, meal_id: int) -> list[dict]:
        return sorted(
            [
                item
                for item in cart["items"]
                if not (item["date"] == meal_date and item["meal_id"] == meal_id)
            ],
            key=lambda item: (item["date"], item["meal_id"], item["transaction_id"]),
        )

    def place_order(
        self,
        meal_date: str,
        meal_id: int,
        quantity: int = 1,
        outlet_slot_id: int = 1,
        allow_checkout_existing_cart: bool = False,
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        meal_date = _ensure_date(meal_date)
        meal_id = _positive_integer(meal_id, "meal_id")
        quantity = _positive_integer(quantity, "quantity")
        outlet_slot_id = _positive_integer(outlet_slot_id, "outlet_slot_id")
        if type(allow_checkout_existing_cart) is not bool:
            raise ValueError("allow_checkout_existing_cart must be a boolean")
        if components is not None and (
            not isinstance(components, list)
            or any(not isinstance(c, dict) for c in components)
        ):
            raise ValueError("components must be a list of objects")
        menu = self.get_menu(meal_date, meal_date, include_inactive=True)
        matches = [
            meal
            for day in menu["days"]
            for meal in day["meals"]
            if meal["meal_id"] == meal_id
        ]
        if len(matches) != 1 or matches[0]["can_order"] is not True:
            raise ApiError(
                "This meal is not currently orderable. Check the live menu and closure notice."
            )
        for limit in (matches[0]["max_quantity"], matches[0]["quota_remaining"]):
            if limit is not None and quantity > limit:
                raise ValueError("quantity exceeds the current menu limit")
        before = self._checkout_cart(meal_date)
        self._assert_clean_cart(before, allow_checkout_existing_cart)
        if any(
            item["date"] == meal_date
            and item["meal_id"] == meal_id
            and item["status"] != "cancelled"
            for item in before["items"]
        ):
            raise ApiError("That meal is already ordered or pending for that day.")
        try:
            self._call_api(
                "/vorbesteller/OrderForm.aspx/ShoppingCardAdd",
                {
                    "id_transaction": 0,
                    "datum": meal_date,
                    "id_mline": str(meal_id),
                    "id_outlet_slot": outlet_slot_id,
                    "anzahl": quantity,
                    "components": components or [],
                },
            )
            staged = self._checkout_cart(meal_date)
            if self._other_items(staged, meal_date, meal_id) != self._other_items(
                before, meal_date, meal_id
            ):
                raise ApiError("Other cart items changed during ordering")
            targets = [
                item
                for item in staged["items"]
                if item["date"] == meal_date
                and item["meal_id"] == meal_id
                and item["status"] != "cancelled"
            ]
            if (
                len(targets) != 1
                or targets[0]["quantity"] != quantity
                or targets[0]["status"] not in {"active", "pending"}
            ):
                raise ApiError("The staged meal could not be verified")
            self._call_api("/vorbesteller/OrderForm.aspx/ShoppingCardPay", {})
            after = self.get_cart_for_range(meal_date, meal_date)
            confirmed = [
                item
                for item in after["items"]
                if item["meal_id"] == meal_id and item["status"] != "cancelled"
            ]
            if (
                len(confirmed) != 1
                or confirmed[0]["status"] != "active"
                or confirmed[0]["quantity"] != quantity
            ):
                raise ApiError("The completed order could not be verified")
        except (SchulessenError, ValueError) as exc:
            raise UncertainMutationError(
                "Ordering did not finish with a verified result. The cart or order may have changed. "
                "Check get_cart and the website before retrying; do not automatically repeat the order."
            ) from exc
        return {
            "status": "ordered",
            "verified": True,
            "date": meal_date,
            "meal_id": meal_id,
            "quantity": quantity,
            "cart_after": after,
        }

    def cancel_order(
        self, meal_date: str, meal_id: int, transaction_id: str | None = None
    ) -> dict[str, Any]:
        meal_date = _ensure_date(meal_date)
        meal_id = _positive_integer(meal_id, "meal_id")
        if transaction_id is not None and (
            not isinstance(transaction_id, str) or not transaction_id.strip()
        ):
            raise ValueError("transaction_id must be a non-empty string")
        before = self._checkout_cart(meal_date)
        self._assert_clean_cart(before)
        matches = [
            item
            for item in before["items"]
            if item["date"] == meal_date
            and item["meal_id"] == meal_id
            and item["status"] == "active"
            and (transaction_id is None or item["transaction_id"] == transaction_id)
        ]
        if len(matches) != 1 or not matches[0]["transaction_id"]:
            raise ApiError(
                "Could not resolve a unique active transaction for that date and meal. Check get_cart."
            )
        target = matches[0]
        if target["is_cancellation_allowed"] is not True:
            raise ApiError("Cancellation is not currently allowed for this order.")
        resolved = target["transaction_id"]
        try:
            self._call_api(
                "/vorbesteller/OrderForm.aspx/ShoppingCardDelete",
                {
                    "id_transaction": resolved,
                    "datum": meal_date,
                    "oid_mlinie": str(meal_id),
                },
            )
            staged = self._checkout_cart(meal_date)
            untouched_before = sorted(
                [i for i in before["items"] if i["transaction_id"] != resolved],
                key=lambda i: i["transaction_id"],
            )
            untouched_after = sorted(
                [i for i in staged["items"] if i["transaction_id"] != resolved],
                key=lambda i: i["transaction_id"],
            )
            if untouched_before != untouched_after:
                raise ApiError("Other cart items changed during cancellation")
            remaining = [
                item for item in staged["items"] if item["transaction_id"] == resolved
            ]
            if remaining and (
                len(remaining) != 1
                or remaining[0]["quantity"] != 0
                or remaining[0]["status"] not in {"pending_cancellation", "cancelled"}
            ):
                raise ApiError("The staged cancellation could not be verified")
            self._call_api("/vorbesteller/OrderForm.aspx/ShoppingCardPay", {})
            after = self.get_cart_for_range(meal_date, meal_date)
            remaining = [
                item for item in after["items"] if item["transaction_id"] == resolved
            ]
            if remaining and (
                len(remaining) != 1 or remaining[0]["status"] != "cancelled"
            ):
                raise ApiError("The completed cancellation could not be verified")
        except (SchulessenError, ValueError) as exc:
            raise UncertainMutationError(
                "Cancellation did not finish with a verified result. The cart or order may have changed. "
                "Check get_cart and the website before retrying; do not automatically repeat cancellation."
            ) from exc
        return {
            "status": "cancelled",
            "verified": True,
            "date": meal_date,
            "meal_id": meal_id,
            "transaction_id": resolved,
            "cart_after": after,
        }

    def _call_api(self, path: str, payload: dict[str, Any], retry: bool = True) -> Any:
        endpoint = path.rsplit("/", 1)[-1]
        if (
            path != f"/vorbesteller/OrderForm.aspx/{endpoint}"
            or endpoint not in READ_ENDPOINTS | WRITE_ENDPOINTS
        ):
            raise ValueError("Unsupported schulessen.net endpoint")
        readonly = endpoint in READ_ENDPOINTS
        if not self.is_authenticated():
            self.login()
        for attempt in range(2 if readonly and retry else 1):
            try:
                text = self._request_text(
                    "POST",
                    path,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json; charset=UTF-8",
                        "Accept": "application/json",
                        "Referer": f"{self.base_url}/vorbesteller/OrderForm.aspx",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                decoded = self._decode_api_response(text)
                if not readonly and (
                    not isinstance(decoded, dict)
                    or _to_bool(decoded.get("success")) is not True
                ):
                    raise ApiError("schulessen.net did not confirm the change")
                return _unwrap_payload(decoded)
            except AuthenticationError:
                self.cookie_jar.clear()
                self._authenticated_at = None
                if not readonly or not retry or attempt:
                    raise
                logger.info("Session expired; re-authenticating for %s", endpoint)
                self.login()
        raise AuthenticationError("Could not restore the schulessen.net session")

    def _decode_api_response(self, text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            raise ApiError("schulessen.net returned an empty response")
        if stripped.startswith("<"):
            raise AuthenticationError(
                "schulessen.net returned the login page instead of data"
            )

        try:
            outer = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ApiError("schulessen.net returned invalid JSON") from exc

        if isinstance(outer, dict) and "d" in outer:
            return _decode_possible_json(outer["d"])
        return _decode_possible_json(outer)

    def _request_text(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only local schulessen.net paths are supported")
        url = f"{self.base_url}{path}"
        request = Request(url=url, method=method.upper(), data=data)
        merged_headers = {
            "User-Agent": "Selu Schulessen Capability/1.0",
        }
        if headers:
            merged_headers.update(headers)
        for key, value in merged_headers.items():
            request.add_header(key, value)

        try:
            with self.opener.open(request, timeout=20) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ApiError("schulessen.net response exceeds the size limit")
                return body.decode("utf-8", errors="replace")
        except HTTPError as exc:
            exc.close()
            if exc.code in (401, 403):
                raise AuthenticationError(
                    "schulessen.net rejected the session"
                ) from exc
            raise ApiError(
                f"schulessen.net request failed with HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise ApiError(
                "schulessen.net is currently unreachable or timed out"
            ) from exc
