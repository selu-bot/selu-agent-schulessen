from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


class SchulessenError(RuntimeError):
    pass


class AuthenticationError(SchulessenError):
    pass


class ApiError(SchulessenError):
    pass


LOGIN_PATH = "/Vorbesteller/Default.aspx"
LOGIN_REFERER = "https://www.schulessen.net/Vorbesteller/Default.aspx"
HOME_URL = "https://www.schulessen.net/vorbesteller/"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _ensure_date(value: str) -> str:
    if not DATE_RE.match(value):
        raise ValueError(f"Expected date in YYYY-MM-DD format, got '{value}'")
    return value


def _default_date_range(from_date: str | None, to_date: str | None) -> tuple[str, str]:
    if from_date is None:
        start = date.today()
    else:
        start = datetime.strptime(_ensure_date(from_date), "%Y-%m-%d").date()

    if to_date is None:
        end = start + timedelta(days=max(0, 4 - start.weekday()))
    else:
        end = datetime.strptime(_ensure_date(to_date), "%Y-%m-%d").date()

    if end < start:
        raise ValueError("to_date must not be earlier than from_date")

    return start.isoformat(), end.isoformat()


def _extract_hidden_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "ESSID"):
        match = re.search(
            rf'id="{re.escape(key)}"[^>]*value="([^"]*)"',
            html,
            flags=re.IGNORECASE,
        )
        if not match:
            raise AuthenticationError(f"Login page is missing hidden field '{key}'")
        fields[key] = match.group(1)
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


def _find_first_list(obj: Any, *candidate_keys: str) -> list[Any]:
    hit = _find_value(obj, *candidate_keys)
    if isinstance(hit, list):
        return hit
    return []


def _extract_price(value: Any) -> tuple[int | None, str | None]:
    data = _maybe_dict(_decode_possible_json(value))
    if data:
        amount = _find_value(data, "value", "preis", "price", "betrag", "amount")
        currency = _find_value(data, "currency", "waehrung", "curr")
        try:
            amount_int = int(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount_int = None
        return amount_int, currency if isinstance(currency, str) else None

    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, None


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "ja", "y"}:
            return True
        if lowered in {"false", "0", "no", "nein", "n", ""}:
            return False
    return bool(value)


def _normalize_day_offers(payload: Any, include_inactive: bool) -> dict[str, Any]:
    decoded = _decode_possible_json(payload)
    if isinstance(decoded, dict) and decoded.get("success") is False:
        raise ApiError(decoded.get("message") or "schulessen.net rejected the menu request")

    parameter = _find_value(decoded, "parameter") if isinstance(decoded, dict) else None
    container = _decode_possible_json(parameter if parameter is not None else decoded)
    if not isinstance(container, (dict, list)):
        raise ApiError("Menu response was not in the expected format")

    day_offers = (
        container
        if isinstance(container, list)
        else _find_first_list(container, "dayoffer", "dayoffers", "days", "angebote")
    )

    normalized_days: list[dict[str, Any]] = []
    for raw_day in day_offers:
        if not isinstance(raw_day, dict):
            continue

        raw_menus = _find_first_list(raw_day, "menus", "menu", "menues", "mealoffers")
        meals: list[dict[str, Any]] = []
        for raw_menu in raw_menus:
            if not isinstance(raw_menu, dict):
                continue

            is_active = _find_value(raw_menu, "is_active", "active", "aktiv")
            is_active_bool = _to_bool(is_active, default=True)
            if not include_inactive and not is_active_bool:
                continue

            meal_id = _find_value(raw_menu, "id_menuline", "id_mline", "meal_id", "menu_id")
            try:
                meal_id_int = int(meal_id) if meal_id is not None else None
            except (TypeError, ValueError):
                meal_id_int = None

            price_cents, currency = _extract_price(
                _find_value(raw_menu, "price_personal", "price", "preis")
            )

            meals.append(
                {
                    "meal_id": meal_id_int,
                    "name": _find_value(raw_menu, "name_menulinie", "name", "title"),
                    "description": _find_value(raw_menu, "gastro_text", "description", "text"),
                    "price_cents": price_cents,
                    "currency": currency,
                    "is_active": is_active_bool,
                    "is_orderable": _to_bool(
                        _find_value(raw_menu, "is_orderable", "orderable")
                    ),
                    "is_ordered": _to_bool(
                        _find_value(raw_menu, "is_ordered", "ordered", "selected")
                    ),
                }
            )

        normalized_days.append(
            {
                "date": _find_value(raw_day, "datum", "date", "day"),
                "is_delivery": _to_bool(
                    _find_value(raw_day, "is_todeliver", "to_deliver", "deliver")
                ),
                "meals": meals,
            }
        )

    result = {
        "from_date": normalized_days[0]["date"] if normalized_days else None,
        "to_date": normalized_days[-1]["date"] if normalized_days else None,
        "days": normalized_days,
        "day_count": len(normalized_days),
        "meal_count": sum(len(day["meals"]) for day in normalized_days),
    }
    if not normalized_days:
        result["raw_payload"] = container
    return result


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_iter_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_iter_dicts(child))
    return found


def _looks_like_cart_item(obj: dict[str, Any]) -> bool:
    keys = {_normalized_key(key) for key in obj.keys()}
    return bool(
        {
            "idtransaction",
            "transactionid",
            "idmline",
            "idmenuline",
            "oidmlinie",
            "datum",
            "date",
        }
        & keys
    )


def _normalize_cart(payload: Any) -> dict[str, Any]:
    decoded = _decode_possible_json(payload)
    if isinstance(decoded, dict) and decoded.get("success") is False:
        raise ApiError(decoded.get("message") or "schulessen.net rejected the cart request")

    parameter = _find_value(decoded, "parameter") if isinstance(decoded, dict) else None
    container = _decode_possible_json(parameter if parameter is not None else decoded)
    dicts = [item for item in _iter_dicts(container) if _looks_like_cart_item(item)]

    items: list[dict[str, Any]] = []
    for raw in dicts:
        transaction_id = _find_value(raw, "id_transaction", "transaction_id", "idtransaction")
        meal_id = _find_value(raw, "id_mline", "id_menuline", "oid_mlinie", "meal_id")
        quantity = _find_value(raw, "anzahl", "quantity", "count", "units_ordered", "units_saved")
        price_cents, currency = _extract_price(
            _find_value(raw, "price", "preis", "amount", "price_per_unit", "gross_amount")
        )
        payable_amount_cents, _ = _extract_price(
            _find_value(raw, "payable_amount", "payable", "offen")
        )

        try:
            meal_id_int = int(meal_id) if meal_id is not None else None
        except (TypeError, ValueError):
            meal_id_int = None

        try:
            quantity_int = int(quantity) if quantity is not None else None
        except (TypeError, ValueError):
            quantity_int = None

        items.append(
            {
                "transaction_id": str(transaction_id) if transaction_id is not None else None,
                "meal_id": meal_id_int,
                "date": _find_value(raw, "date_delivery", "datum", "date"),
                "name": _find_value(raw, "name_menuline", "name_menu", "name", "title"),
                "description": _find_value(raw, "gastro_text", "description", "text"),
                "quantity": quantity_int,
                "price_cents": price_cents,
                "currency": currency,
                "payable_amount_cents": payable_amount_cents,
                "units_paid": _find_value(raw, "units_paid"),
                "units_saved": _find_value(raw, "units_saved"),
                "is_cancellation_allowed": _to_bool(
                    _find_value(raw, "is_cancelcation_allowed", "is_cancellation_allowed")
                ),
                "status": "cancelled"
                if (quantity_int is not None and quantity_int <= 0)
                or (payable_amount_cents is not None and payable_amount_cents < 0)
                else "active",
            }
        )

    balance_cents = None
    for raw in _iter_dicts(container):
        maybe_balance = _find_value(raw, "saldo", "balance", "kartenbetrag", "amount")
        if maybe_balance is None:
            continue
        extracted, _currency = _extract_price(maybe_balance)
        if extracted is not None:
            balance_cents = extracted
            break

    active_items = [item for item in items if item.get("status") == "active"]
    cancelled_items = [item for item in items if item.get("status") == "cancelled"]

    result = {
        "items": items,
        "item_count": len(items),
        "active_items": active_items,
        "active_item_count": len(active_items),
        "cancelled_items": cancelled_items,
        "cancelled_item_count": len(cancelled_items),
        "has_active_order": len(active_items) > 0,
        "has_cancelled_history": len(cancelled_items) > 0,
        "balance_cents": balance_cents,
    }
    if not items:
        result["raw_payload"] = container
    return result


@dataclass
class LoginCredentials:
    username: str
    password: str


class SchulessenClient:
    def __init__(self, base_url: str = "https://www.schulessen.net") -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self._credentials: LoginCredentials | None = None

    def set_credentials(self, username: str, password: str) -> None:
        creds = LoginCredentials(username=username, password=password)
        if self._credentials != creds:
            self.cookie_jar.clear()
            self._credentials = creds

    def login(self) -> dict[str, Any]:
        if not self._credentials:
            raise AuthenticationError("Missing schulessen.net credentials")

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
                f"ESSID={hidden['ESSID']}",
            ]
        ).encode("utf-8")

        response = self._request_text(
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

        if not self.is_authenticated() and "window.close()" not in response:
            raise AuthenticationError("Couldn't sign in to schulessen.net. Check username and password.")

        return {"authenticated": True}

    def is_authenticated(self) -> bool:
        return any("ASPXAUTH" in cookie.name.upper() for cookie in self.cookie_jar)

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
        return result

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
        before = self.get_cart_for_range(meal_date, meal_date)
        duplicate_items = [
            item
            for item in before["items"]
            if item.get("date") == meal_date
            and item.get("meal_id") == int(meal_id)
            and item.get("status") != "cancelled"
        ]
        if duplicate_items:
            raise ApiError("That meal is already ordered for that day.")

        pending_items = [
            item
            for item in before["items"]
            if (item.get("payable_amount_cents") or 0) > 0
        ]
        if pending_items and not allow_checkout_existing_cart:
            raise ApiError(
                "The cart already contains pending items. Check the cart first or set allow_checkout_existing_cart=true."
            )

        add_result = self._call_api(
            "/vorbesteller/OrderForm.aspx/ShoppingCardAdd",
            {
                "id_transaction": 0,
                "datum": meal_date,
                "id_mline": str(int(meal_id)),
                "id_outlet_slot": int(outlet_slot_id),
                "anzahl": int(quantity),
                "components": components or [],
            },
        )
        pay_result = self._call_api("/vorbesteller/OrderForm.aspx/ShoppingCardPay", {})
        after = self.get_cart_for_range(meal_date, meal_date)
        return {
            "status": "ordered",
            "date": meal_date,
            "meal_id": int(meal_id),
            "quantity": int(quantity),
            "cart_before": before,
            "cart_after": after,
            "add_result": _decode_possible_json(add_result),
            "pay_result": _decode_possible_json(pay_result),
        }

    def cancel_order(
        self,
        meal_date: str,
        meal_id: int,
        transaction_id: str | None = None,
    ) -> dict[str, Any]:
        meal_date = _ensure_date(meal_date)
        resolved_transaction = transaction_id
        cart = self.get_cart_for_range(meal_date, meal_date)

        if not resolved_transaction:
            matches = [
                item
                for item in cart["items"]
                if item.get("date") == meal_date and item.get("meal_id") == int(meal_id)
            ]
            if len(matches) != 1 or not matches[0].get("transaction_id"):
                raise ApiError(
                    "I couldn't resolve a unique transaction for that meal. Check the cart first and provide transaction_id."
                )
            resolved_transaction = matches[0]["transaction_id"]

        delete_result = self._call_api(
            "/vorbesteller/OrderForm.aspx/ShoppingCardDelete",
            {
                "id_transaction": str(resolved_transaction),
                "datum": meal_date,
                "oid_mlinie": str(int(meal_id)),
            },
        )
        after = self.get_cart()
        return {
            "status": "cancelled",
            "date": meal_date,
            "meal_id": int(meal_id),
            "transaction_id": str(resolved_transaction),
            "cart_after": after,
            "delete_result": _decode_possible_json(delete_result),
        }

    def _call_api(self, path: str, payload: dict[str, Any], retry: bool = True) -> Any:
        if not self.is_authenticated():
            self.login()

        try:
            text = self._request_text(
                "POST",
                path,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json; charset=UTF-8",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": f"{self.base_url}/vorbesteller/OrderForm.aspx",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except AuthenticationError:
            if retry:
                self.cookie_jar.clear()
                self.login()
                return self._call_api(path, payload, retry=False)
            raise

        raw = self._decode_api_response(text)
        return raw

    def _decode_api_response(self, text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            raise ApiError("schulessen.net returned an empty response")
        if stripped.startswith("<!DOCTYPE html") or "<form" in stripped:
            raise AuthenticationError("schulessen.net returned the login page instead of data")

        try:
            outer = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ApiError(f"schulessen.net returned invalid JSON: {exc}") from exc

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
        url = path if path.startswith("http") else f"{self.base_url}{path}"
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
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403):
                raise AuthenticationError("schulessen.net rejected the session") from exc
            raise ApiError(f"schulessen.net request failed with HTTP {exc.code}: {body[:200]}") from exc
        except URLError as exc:
            raise ApiError(f"schulessen.net is currently unreachable: {exc.reason}") from exc
