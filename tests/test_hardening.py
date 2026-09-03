"""Regression cases reconstructed from first-party UI/JS, without account data."""

import copy
import io
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "capabilities/schulessen/container")
)
from schulessen_client import (
    ApiError,
    AuthenticationError,
    BERLIN,
    MAX_RESPONSE_BYTES,
    SameOriginRedirectHandler,
    SchulessenClient,
    UncertainMutationError,
    _default_date_range,
    _ensure_date,
    _normalize_cart,
    _normalize_day_offers,
    _to_bool,
    _extract_hidden_fields,
)

NOW = datetime(2026, 9, 3, 12, tzinfo=BERLIN)
DAY = "2026-09-04"
API = "/vorbesteller/OrderForm.aspx/"


def menu_payload():
    return {
        "dayoffer": [
            {
                "datum": DAY,
                "is_todeliver": True,
                "is_closed": False,
                "reason_closed": "",
                "units_max_orderable": 2,
                "menus": [
                    {
                        "id_menuline": 407,
                        "name_menulinie": "Buffet",
                        "gastro_text": "<p>Lunch</p>",
                        "is_active": True,
                        "is_orderable": True,
                        "last_order": int(
                            (NOW + timedelta(hours=2)).timestamp() * 1000
                        ),
                        "last_cancel": int(
                            (NOW + timedelta(days=1)).timestamp() * 1000
                        ),
                        "price_personal": {"value": 495, "currency": "EUR"},
                    }
                ],
            }
        ]
    }


def cart_row(**overrides):
    row = {
        "id_transaction": "test-1",
        "id_menuline": 407,
        "date_delivery": DAY,
        "units_ordered": 1,
        "units_saved": 1,
        "units_paid": 1,
        "payable_amount": {"value": 0},
        "is_cancelcation_allowed": True,
    }
    row.update(overrides)
    return row


def cart(*rows):
    return _normalize_cart({"items": list(rows)})


class AvailabilityTests(unittest.TestCase):
    def normalize(self, payload):
        return _normalize_day_offers(payload, False, now=NOW)

    def test_september_3_closed_day_never_advertises_hidden_dish(self):
        payload = menu_payload()
        day = payload["dayoffer"][0]
        day.update(datum="2026-09-03", is_closed=True, reason_closed="Kein Essen")
        day["menus"][0]["gastro_text"] = "Hähnchenbrust Piccata Milanese"
        result = self.normalize(payload)
        day = result["days"][0]
        meal = day["meals"][0]
        self.assertEqual(day["reason_closed"], "Kein Essen")
        self.assertEqual(meal["availability"], "no_service")
        self.assertFalse(meal["can_order"])
        self.assertIsNone(meal["description"])
        self.assertIsNone(meal["price_cents"])
        self.assertNotIn("Piccata", json.dumps(result))
        self.assertEqual(result["orderable_meal_count"], 0)

    def test_reason_closed_overrides_contradictory_open_flags(self):
        payload = menu_payload()
        payload["dayoffer"][0]["reason_closed"] = "Sommerferien"
        self.assertFalse(self.normalize(payload)["days"][0]["can_order"])

    def test_available_meal_preserves_description_and_unknown_order_status(self):
        meal = self.normalize(menu_payload())["days"][0]["meals"][0]
        self.assertTrue(meal["can_order"])
        self.assertEqual(meal["description"], "Lunch")
        self.assertEqual(meal["price_cents"], 495)
        self.assertIsNone(meal["is_ordered"])

    def test_deadline_is_inclusive_and_in_berlin(self):
        payload = menu_payload()
        payload["dayoffer"][0]["menus"][0]["last_order"] = int(NOW.timestamp() * 1000)
        meal = self.normalize(payload)["days"][0]["meals"][0]
        self.assertEqual(meal["availability"], "deadline_passed")
        self.assertEqual(meal["order_deadline"], "2026-09-03T12:00:00+02:00")

    def test_unknown_flags_and_deadline_do_not_enable_ordering(self):
        for field in ["is_active", "is_orderable", "last_order"]:
            with self.subTest(field=field):
                payload = menu_payload()
                del payload["dayoffer"][0]["menus"][0][field]
                self.assertFalse(self.normalize(payload)["days"][0]["can_order"])

    def test_inactive_and_missing_offer_and_quota(self):
        for field, value, expected in [
            ("is_orderable", False, "unavailable"),
            ("gastro_text", "", "no_offer"),
            ("quota", {"id": 1, "units_remain": 0}, "sold_out"),
        ]:
            payload = menu_payload()
            payload["dayoffer"][0]["menus"][0][field] = value
            self.assertEqual(
                self.normalize(payload)["days"][0]["meals"][0]["availability"], expected
            )
        payload = menu_payload()
        payload["dayoffer"][0]["menus"][0]["is_active"] = False
        self.assertEqual(self.normalize(payload)["meal_count"], 0)

    def test_malformed_menu_is_not_an_empty_menu(self):
        for payload in [
            {},
            {"other": []},
            {"dayoffer": [None]},
            {"dayoffer": [{"datum": DAY}]},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                self.normalize(payload)

    def test_valid_empty_menu(self):
        self.assertEqual(self.normalize({"dayoffer": []})["days"], [])


class CartSafetyTests(unittest.TestCase):
    def test_pending_cancellation_is_not_cancelled_history(self):
        result = cart(cart_row(units_ordered=0, payable_amount={"value": -495}))
        self.assertEqual(result["items"][0]["status"], "pending_cancellation")
        self.assertEqual(result["cancelled_item_count"], 0)
        self.assertIsNone(result["has_active_order"])

    def test_quantity_reduction_does_not_disappear_as_cancelled(self):
        result = cart(
            cart_row(units_ordered=1, units_saved=2, payable_amount={"value": -495})
        )
        self.assertEqual(result["items"][0]["status"], "pending")

    def test_free_unsaved_meal_is_pending(self):
        result = cart(cart_row(units_saved=0, units_paid=0))
        self.assertEqual(result["pending_item_count"], 1)
        self.assertEqual(result["active_item_count"], 0)

    def test_component_changes_require_checkout_even_with_equal_units(self):
        result = cart(cart_row(components=[{"has_changed": True}]))
        self.assertEqual(result["pending_item_count"], 1)

    def test_settled_cancellation_and_active_order(self):
        result = cart(
            cart_row(),
            cart_row(
                id_transaction="test-2", units_ordered=0, units_saved=0, units_paid=0
            ),
        )
        self.assertEqual(result["active_item_count"], 1)
        self.assertEqual(result["cancelled_item_count"], 1)

    def test_unknown_status_is_not_reported_as_no_orders(self):
        result = cart(cart_row(units_saved=None, units_paid=None))
        self.assertEqual(result["unknown_item_count"], 1)
        self.assertIsNone(result["has_active_order"])

    def test_balance_only_uses_account_field(self):
        result = _normalize_cart(
            {"items": [cart_row(amount=999)], "bank_balance": {"value": 4245}}
        )
        self.assertEqual(result["balance_cents"], 4245)
        self.assertIsNone(
            _normalize_cart({"items": [cart_row(amount=999)]})["balance_cents"]
        )

    def test_nested_component_metadata_is_not_an_order(self):
        result = cart(cart_row(components=[{"date": DAY, "has_changed": False}]))
        self.assertEqual(result["item_count"], 1)

    def test_malformed_response_never_means_no_orders(self):
        for payload in [
            None,
            "",
            {},
            {"orders": []},
            {"items": [None]},
            {"success": "false", "parameter": "[]"},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                _normalize_cart(payload)


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.client = SchulessenClient()
        self.today = patch("schulessen_client._today", return_value=date(2026, 9, 3))
        self.today.start()
        self.addCleanup(self.today.stop)
        self.client.get_menu = MagicMock(
            return_value=_normalize_day_offers(menu_payload(), False, now=NOW)
        )
        self.client.get_cart_for_range = MagicMock()
        self.client._call_api = MagicMock(return_value={})

    def test_no_service_cannot_reach_mutating_endpoint(self):
        self.client.get_menu.return_value["days"][0]["meals"][0]["can_order"] = False
        with self.assertRaises(ApiError):
            self.client.place_order(DAY, 407)
        self.client._call_api.assert_not_called()

    def test_duplicate_order_does_not_write(self):
        self.client.get_cart_for_range.return_value = cart(cart_row())
        with self.assertRaises(ApiError):
            self.client.place_order(DAY, 407)
        self.client._call_api.assert_not_called()

    def test_pending_change_on_another_day_blocks_shared_checkout(self):
        self.client.get_cart_for_range.return_value = cart(
            cart_row(date_delivery="2026-09-07", units_saved=0)
        )
        with self.assertRaises(ApiError):
            self.client.place_order(DAY, 407)
        self.client._call_api.assert_not_called()
        self.client.get_cart_for_range.assert_called_once_with(
            "2026-08-31", "2026-11-29"
        )

    def test_success_only_after_verified_saved_order(self):
        self.client.get_cart_for_range.side_effect = [
            cart(),
            cart(cart_row(units_saved=0)),
            cart(cart_row()),
        ]
        result = self.client.place_order(DAY, 407)
        self.assertEqual(result["status"], "ordered")
        self.assertTrue(result["verified"])
        self.assertEqual(
            [c.args[0] for c in self.client._call_api.call_args_list],
            [API + "ShoppingCardAdd", API + "ShoppingCardPay"],
        )

    def test_add_failure_stops_before_pay_and_is_not_retried(self):
        self.client.get_cart_for_range.return_value = cart()
        self.client._call_api.side_effect = ApiError("rejected")
        with self.assertRaises(UncertainMutationError):
            self.client.place_order(DAY, 407)
        self.assertEqual(self.client._call_api.call_count, 1)

    def test_checkout_timeout_reports_uncertainty(self):
        self.client.get_cart_for_range.side_effect = [
            cart(),
            cart(cart_row(units_saved=0)),
        ]
        self.client._call_api.side_effect = [{}, ApiError("timeout")]
        with self.assertRaises(UncertainMutationError):
            self.client.place_order(DAY, 407)
        self.assertEqual(self.client._call_api.call_count, 2)

    def test_unverified_success_is_not_claimed(self):
        self.client.get_cart_for_range.side_effect = [
            cart(),
            cart(cart_row(units_saved=0)),
            cart(),
        ]
        with self.assertRaises(UncertainMutationError):
            self.client.place_order(DAY, 407)

    def test_concurrent_cart_change_prevents_checkout(self):
        self.client.get_cart_for_range.side_effect = [
            cart(),
            cart(
                cart_row(units_saved=0),
                cart_row(id_transaction="other", id_menuline=999, units_saved=0),
            ),
        ]
        with self.assertRaises(UncertainMutationError):
            self.client.place_order(DAY, 407)
        self.assertEqual(self.client._call_api.call_count, 1)

    def test_false_string_never_enables_existing_cart_checkout(self):
        with self.assertRaises(ValueError):
            self.client.place_order(DAY, 407, allow_checkout_existing_cart="false")
        self.client._call_api.assert_not_called()

    def test_component_edit_with_unchanged_total_prevents_checkout(self):
        other = cart_row(
            id_transaction="other",
            id_menuline=999,
            components=[{"id_component": 1, "has_changed": False}],
        )
        changed = dict(other, components=[{"id_component": 2, "has_changed": False}])
        self.client.get_cart_for_range.side_effect = [
            cart(other),
            cart(cart_row(units_saved=0), changed),
        ]
        with self.assertRaises(UncertainMutationError):
            self.client.place_order(DAY, 407)
        self.assertEqual(self.client._call_api.call_count, 1)

    def test_existing_cart_override_cannot_bypass_unknown_quantity(self):
        self.client.get_cart_for_range.return_value = cart(
            cart_row(id_menuline=999, units_ordered=None, payable_amount={"value": 495})
        )
        with self.assertRaises(ApiError):
            self.client.place_order(DAY, 407, allow_checkout_existing_cart=True)
        self.client._call_api.assert_not_called()

    def test_invalid_quantities_and_dates_never_write(self):
        for qty in [0, -1, True, 1.5, "1"]:
            with self.subTest(qty=qty), self.assertRaises(ValueError):
                self.client.place_order(DAY, 407, quantity=qty)
        with self.assertRaises(ValueError):
            self.client.place_order("2026-02-30", 407)
        self.client._call_api.assert_not_called()

    def test_cancel_requires_matching_transaction_and_permission(self):
        for row, transaction in [
            (cart_row(), "wrong-id"),
            (cart_row(is_cancelcation_allowed=False), "test-1"),
        ]:
            self.client.get_cart_for_range.return_value = cart(row)
            with self.assertRaises(ApiError):
                self.client.cancel_order(DAY, 407, transaction)
        self.client._call_api.assert_not_called()

    def test_cancel_ignores_already_cancelled_history(self):
        before = cart(
            cart_row(),
            cart_row(
                id_transaction="old", units_ordered=0, units_saved=0, units_paid=0
            ),
        )
        staged = copy.deepcopy(before)
        staged["items"][0].update(quantity=0, status="pending_cancellation")
        self.client.get_cart_for_range.side_effect = [before, staged, cart()]
        self.assertEqual(self.client.cancel_order(DAY, 407)["status"], "cancelled")
        self.assertEqual(
            self.client._call_api.call_args_list[0].args[1]["id_transaction"], "test-1"
        )

    def test_unconfirmed_cancellation_is_uncertain(self):
        self.client.get_cart_for_range.side_effect = [
            cart(cart_row()),
            cart(cart_row(units_ordered=0, payable_amount={"value": -495})),
            cart(cart_row()),
        ]
        with self.assertRaises(UncertainMutationError):
            self.client.cancel_order(DAY, 407)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = SchulessenClient()
        self.client.is_authenticated = MagicMock(return_value=True)

    def test_write_session_failure_is_never_replayed(self):
        for endpoint in ["ShoppingCardAdd", "ShoppingCardPay", "ShoppingCardDelete"]:
            with (
                self.subTest(endpoint=endpoint),
                patch.object(
                    self.client,
                    "_request_text",
                    side_effect=AuthenticationError("expired"),
                ) as request,
                patch.object(self.client, "login") as login,
            ):
                with self.assertRaises(AuthenticationError):
                    self.client._call_api(API + endpoint, {})
                request.assert_called_once()
                login.assert_not_called()

    def test_write_must_explicitly_succeed(self):
        for response in [{"success": False}, {}, [], {"success": "false"}]:
            with (
                self.subTest(response=response),
                patch.object(
                    self.client,
                    "_request_text",
                    return_value=json.dumps({"d": json.dumps(response)}),
                ),
            ):
                with self.assertRaises(ApiError):
                    self.client._call_api(API + "ShoppingCardPay", {})

    def test_sensitive_http_body_is_not_in_errors(self):
        error = HTTPError(
            "https://www.schulessen.net", 500, "bad", {}, io.BytesIO(b"password SECRET")
        )
        with patch.object(self.client.opener, "open", side_effect=error):
            with self.assertRaises(ApiError) as ctx:
                self.client._request_text("GET", "/test")
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_large_response_is_bounded(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * (
            MAX_RESPONSE_BYTES + 1
        )
        with (
            patch.object(self.client.opener, "open", return_value=response),
            self.assertRaises(ApiError),
        ):
            self.client._request_text("GET", "/test")
        response.__enter__.return_value.read.assert_called_once_with(
            MAX_RESPONSE_BYTES + 1
        )

    def test_foreign_destinations_and_redirects_blocked(self):
        with self.assertRaises(ValueError):
            SchulessenClient("http://www.schulessen.net")
        for url in ["https://example.com/", "http://www.schulessen.net/"]:
            with self.subTest(url=url), self.assertRaises(ApiError):
                SameOriginRedirectHandler().redirect_request(
                    Request("https://www.schulessen.net/"),
                    None,
                    302,
                    "redirect",
                    {},
                    url,
                )
        with self.assertRaises(ValueError):
            self.client._request_text("POST", "https://example.com/")

    def test_login_html_detection_is_case_independent(self):
        for text in ["<!doctype html><html>", "<HTML>sign in</HTML>"]:
            with self.assertRaises(AuthenticationError):
                self.client._decode_api_response(text)

    def test_date_validation_and_berlin_default(self):
        for value in ["2026-02-30", "2026-9-3", "2026-09-03\n", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ensure_date(value)
        with self.assertRaises(ValueError):
            _default_date_range("2026-01-01", "2026-12-31")
        with patch("schulessen_client._today", return_value=date(2026, 9, 3)):
            self.assertEqual(
                _default_date_range(None, None), ("2026-09-03", "2026-09-04")
            )

    def test_html_login_attribute_order_and_entities(self):
        fields = ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "ESSID"]
        html = "".join(f"<input value='x&amp;y' name='{key}'>" for key in fields)
        self.assertEqual(_extract_hidden_fields(html), {key: "x&y" for key in fields})

    def test_unrecognized_boolean_is_unknown(self):
        for value in ["unknown", "", {}, [], 2]:
            self.assertIsNone(_to_bool(value))


if __name__ == "__main__":
    unittest.main()
