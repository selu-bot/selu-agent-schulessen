import json
import unittest

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "capabilities" / "schulessen" / "container"))

from schulessen_client import (  # noqa: E402
    _decode_possible_json,
    _extract_hidden_fields,
    _normalize_cart,
    _normalize_day_offers,
)


class SchulessenClientTests(unittest.TestCase):
    def test_extract_hidden_fields(self):
        html = """
        <input type="hidden" id="__VIEWSTATE" value="viewstate-value" />
        <input type="hidden" id="__VIEWSTATEGENERATOR" value="generator-value" />
        <input type="hidden" id="__EVENTVALIDATION" value="event-value" />
        <input type="hidden" id="ESSID" value="essid-value" />
        """
        self.assertEqual(
            _extract_hidden_fields(html),
            {
                "__VIEWSTATE": "viewstate-value",
                "__VIEWSTATEGENERATOR": "generator-value",
                "__EVENTVALIDATION": "event-value",
                "ESSID": "essid-value",
            },
        )

    def test_decode_possible_json_unwraps_nested_strings(self):
        raw = json.dumps({"success": True, "parameter": json.dumps({"dayoffer": []})})
        decoded = _decode_possible_json(raw)
        self.assertTrue(decoded["success"])
        self.assertEqual(_decode_possible_json(decoded["parameter"]), {"dayoffer": []})

    def test_normalize_menu_response(self):
        payload = {
            "success": True,
            "parameter": json.dumps(
                {
                    "dayoffer": [
                        {
                            "datum": "2026-03-30",
                            "is_todeliver": False,
                            "menus": [
                                {
                                    "id_menuline": 407,
                                    "name_menulinie": "Menue 1",
                                    "gastro_text": "Nudeln mit Sosse",
                                    "is_active": True,
                                    "is_orderable": True,
                                    "price_personal": {"value": 385, "currency": "EUR"},
                                }
                            ],
                        }
                    ]
                }
            ),
        }
        normalized = _normalize_day_offers(payload, include_inactive=False)
        self.assertEqual(normalized["day_count"], 1)
        self.assertEqual(normalized["meal_count"], 1)
        meal = normalized["days"][0]["meals"][0]
        self.assertEqual(meal["meal_id"], 407)
        self.assertEqual(meal["price_cents"], 385)
        self.assertTrue(meal["is_orderable"])

    def test_normalize_cart_response(self):
        payload = {
            "success": True,
            "parameter": json.dumps(
                {
                    "shoppingcard": [
                        {
                            "id_transaction": "12345",
                            "id_menuline": 407,
                            "date_delivery": "2026-03-30",
                            "name_menuline": "Menue 1",
                            "units_ordered": 1,
                            "price_per_unit": {"value": 385, "currency": "EUR"},
                            "payable_amount": {"value": 0, "currency": "EUR"},
                            "is_cancelcation_allowed": True,
                        }
                    ],
                    "saldo": {"value": 1240, "currency": "EUR"},
                }
            ),
        }
        normalized = _normalize_cart(payload)
        self.assertEqual(normalized["item_count"], 1)
        self.assertEqual(normalized["active_item_count"], 1)
        self.assertEqual(normalized["cancelled_item_count"], 0)
        self.assertEqual(normalized["items"][0]["transaction_id"], "12345")
        self.assertTrue(normalized["items"][0]["is_cancellation_allowed"])
        self.assertEqual(normalized["balance_cents"], 1240)

    def test_normalize_cancelled_cart_entry(self):
        payload = {
            "success": True,
            "parameter": json.dumps(
                {
                    "items": [
                        {
                            "id_transaction": "cancelled-1",
                            "id_menuline": 407,
                            "date_delivery": "2026-03-30",
                            "name_menuline": "Menue 1",
                            "units_ordered": 0,
                            "payable_amount": {"value": -385, "currency": "EUR"},
                            "price_per_unit": {"value": 385, "currency": "EUR"},
                        }
                    ]
                }
            ),
        }
        normalized = _normalize_cart(payload)
        self.assertEqual(normalized["active_item_count"], 0)
        self.assertEqual(normalized["cancelled_item_count"], 1)
        self.assertEqual(normalized["cancelled_items"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
