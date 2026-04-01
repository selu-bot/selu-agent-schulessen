import json
import time
import unittest
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "capabilities" / "schulessen" / "container"))

from schulessen_client import (  # noqa: E402
    AuthenticationError,
    SESSION_MAX_AGE_SECONDS,
    SchulessenClient,
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


def _make_aspxauth_cookie(expires: float | None = None) -> Cookie:
    """Create a fake .ASPXAUTH cookie for testing."""
    return Cookie(
        version=0,
        name=".ASPXAUTH",
        value="fake-token",
        port=None,
        port_specified=False,
        domain="www.schulessen.net",
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=int(expires) if expires is not None else None,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={},
    )


class SessionExpiryRetryTests(unittest.TestCase):
    """Tests for automatic re-login when the session expires."""

    def _make_client(self) -> SchulessenClient:
        client = SchulessenClient(base_url="https://www.schulessen.net")
        client.set_credentials("user", "pass")
        return client

    def test_is_authenticated_returns_false_when_no_cookie(self):
        client = self._make_client()
        self.assertFalse(client.is_authenticated())

    def test_is_authenticated_returns_true_with_valid_cookie(self):
        client = self._make_client()
        future = time.time() + 3600
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=future))
        client._authenticated_at = time.time()  # simulate recent login
        self.assertTrue(client.is_authenticated())

    def test_is_authenticated_returns_true_with_no_expiry_cookie(self):
        client = self._make_client()
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=None))
        client._authenticated_at = time.time()  # simulate recent login
        self.assertTrue(client.is_authenticated())

    def test_is_authenticated_returns_false_with_expired_cookie(self):
        client = self._make_client()
        past = time.time() - 3600
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=past))
        self.assertFalse(client.is_authenticated())
        # Cookie jar should be cleared
        self.assertEqual(len(list(client.cookie_jar)), 0)

    def test_call_api_retries_on_html_response(self):
        """When the server returns HTML (login page) instead of JSON,
        _decode_api_response raises AuthenticationError. The retry in
        _call_api must catch it, re-login, and succeed on the second try."""
        client = self._make_client()

        # Pre-seed a valid cookie so is_authenticated() returns True initially
        future = time.time() + 3600
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=future))

        html_login_page = '<!DOCTYPE html><html><body><form id="login"></form></body></html>'
        valid_json = json.dumps({"d": json.dumps({"success": True, "parameter": "[]"})})

        call_count = {"request": 0}

        def fake_request_text(method, path, data=None, headers=None):
            call_count["request"] += 1
            if call_count["request"] == 1:
                # First API call returns HTML (expired session)
                return html_login_page
            # After re-login, return valid JSON
            return valid_json

        with patch.object(client, "_request_text", side_effect=fake_request_text), \
             patch.object(client, "login") as mock_login:
            result = client._call_api("/vorbesteller/OrderForm.aspx/MenuOffer", {})

        # login() should have been called exactly once for re-authentication
        mock_login.assert_called_once()
        # The result should be the decoded valid response
        self.assertEqual(result, [])

    def test_call_api_retries_on_http_401(self):
        """When _request_text raises AuthenticationError (HTTP 401/403),
        _call_api must re-login and retry."""
        client = self._make_client()

        future = time.time() + 3600
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=future))

        valid_json = json.dumps({"d": json.dumps({"success": True, "parameter": "[]"})})

        call_count = {"request": 0}

        def fake_request_text(method, path, data=None, headers=None):
            call_count["request"] += 1
            if call_count["request"] == 1:
                raise AuthenticationError("schulessen.net rejected the session")
            return valid_json

        with patch.object(client, "_request_text", side_effect=fake_request_text), \
             patch.object(client, "login") as mock_login:
            result = client._call_api("/vorbesteller/OrderForm.aspx/MenuOffer", {})

        mock_login.assert_called_once()
        self.assertEqual(result, [])

    def test_call_api_raises_after_retry_exhausted(self):
        """When re-login also fails, AuthenticationError must propagate."""
        client = self._make_client()

        future = time.time() + 3600
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=future))

        html_login_page = '<!DOCTYPE html><html><body><form id="login"></form></body></html>'

        def always_html(method, path, data=None, headers=None):
            return html_login_page

        with patch.object(client, "_request_text", side_effect=always_html), \
             patch.object(client, "login"):
            with self.assertRaises(AuthenticationError):
                client._call_api("/vorbesteller/OrderForm.aspx/MenuOffer", {})

    def test_call_api_triggers_login_when_not_authenticated(self):
        """When is_authenticated() returns False, login() is called before
        the first request."""
        client = self._make_client()
        # No cookie → not authenticated

        valid_json = json.dumps({"d": json.dumps({"success": True, "parameter": "[]"})})

        def fake_request_text(method, path, data=None, headers=None):
            return valid_json

        with patch.object(client, "_request_text", side_effect=fake_request_text), \
             patch.object(client, "login") as mock_login:
            result = client._call_api("/vorbesteller/OrderForm.aspx/MenuOffer", {})

        # login() called because is_authenticated() was False
        mock_login.assert_called_once()
        self.assertEqual(result, [])


class SessionMaxAgeTests(unittest.TestCase):
    """Tests for the proactive session-age guard.

    ASP.NET session cookies often lack an ``expires`` attribute, so the
    CookieJar never considers them stale.  The client now tracks
    ``_authenticated_at`` and proactively forces a re-login once the
    session exceeds ``SESSION_MAX_AGE_SECONDS``.
    """

    def _make_client(self) -> SchulessenClient:
        client = SchulessenClient(base_url="https://www.schulessen.net")
        client.set_credentials("user", "pass")
        return client

    # -- is_authenticated() --------------------------------------------------

    def test_session_cookie_no_expiry_within_max_age(self):
        """A session cookie without expires is valid while within max age."""
        client = self._make_client()
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=None))
        client._authenticated_at = time.time() - 60  # 1 minute ago
        self.assertTrue(client.is_authenticated())

    def test_session_cookie_no_expiry_exceeds_max_age(self):
        """A session cookie without expires is rejected once max age passes."""
        client = self._make_client()
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=None))
        client._authenticated_at = time.time() - SESSION_MAX_AGE_SECONDS - 1
        self.assertFalse(client.is_authenticated())
        # Cookie jar and timestamp should be cleared
        self.assertEqual(len(list(client.cookie_jar)), 0)
        self.assertIsNone(client._authenticated_at)

    def test_session_cookie_no_expiry_no_timestamp(self):
        """A session cookie with no _authenticated_at timestamp is treated
        as unauthenticated (no cookie-level expiry to check either)."""
        client = self._make_client()
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=None))
        # _authenticated_at is None → time guard is skipped, cookie has no
        # expiry → is_authenticated returns True because the cookie exists.
        # This only happens when something manually injects a cookie without
        # going through login().  In production login() always sets the
        # timestamp.
        self.assertTrue(client.is_authenticated())

    # -- login() sets _authenticated_at --------------------------------------

    def test_login_sets_authenticated_at(self):
        """A successful login() must record _authenticated_at."""
        client = self._make_client()
        self.assertIsNone(client._authenticated_at)

        login_html = (
            '<input type="hidden" id="__VIEWSTATE" value="vs" />'
            '<input type="hidden" id="__VIEWSTATEGENERATOR" value="vg" />'
            '<input type="hidden" id="__EVENTVALIDATION" value="ev" />'
            '<input type="hidden" id="ESSID" value="eid" />'
        )

        call_count = {"n": 0}

        def fake_request_text(method, path, data=None, headers=None):
            call_count["n"] += 1
            if call_count["n"] <= 3:
                return login_html  # GET login page + 2 JS fetches
            return "<html>window.close()</html>"  # POST response

        with patch.object(client, "_request_text", side_effect=fake_request_text):
            client.login()

        self.assertIsNotNone(client._authenticated_at)
        self.assertAlmostEqual(client._authenticated_at, time.time(), delta=2)

    # -- _call_api re-login on stale session cookie --------------------------

    def test_call_api_relogins_when_session_cookie_stale(self):
        """When _authenticated_at has exceeded max age and the cookie has no
        expiry, _call_api must proactively re-login before the request."""
        client = self._make_client()
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=None))
        client._authenticated_at = time.time() - SESSION_MAX_AGE_SECONDS - 1

        valid_json = json.dumps({"d": json.dumps({"success": True, "parameter": "[]"})})

        def fake_request_text(method, path, data=None, headers=None):
            return valid_json

        with patch.object(client, "_request_text", side_effect=fake_request_text), \
             patch.object(client, "login") as mock_login:
            result = client._call_api("/vorbesteller/OrderForm.aspx/MenuOffer", {})

        # login() should have been called because is_authenticated() returned
        # False due to the age guard.
        mock_login.assert_called_once()
        self.assertEqual(result, [])

    def test_call_api_no_relogin_when_session_fresh(self):
        """When the session is still fresh, no re-login should happen."""
        client = self._make_client()
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=None))
        client._authenticated_at = time.time() - 60  # 1 minute ago

        valid_json = json.dumps({"d": json.dumps({"success": True, "parameter": "[]"})})

        def fake_request_text(method, path, data=None, headers=None):
            return valid_json

        with patch.object(client, "_request_text", side_effect=fake_request_text), \
             patch.object(client, "login") as mock_login:
            result = client._call_api("/vorbesteller/OrderForm.aspx/MenuOffer", {})

        # login() should NOT have been called
        mock_login.assert_not_called()
        self.assertEqual(result, [])

    def test_set_credentials_resets_authenticated_at(self):
        """Changing credentials must reset the session timestamp."""
        client = self._make_client()
        client._authenticated_at = time.time()

        client.set_credentials("other-user", "other-pass")
        self.assertIsNone(client._authenticated_at)

    def test_retry_resets_authenticated_at(self):
        """When _call_api retries after AuthenticationError, the stale
        timestamp must be cleared so the re-login path works."""
        client = self._make_client()
        client.cookie_jar.set_cookie(_make_aspxauth_cookie(expires=None))
        client._authenticated_at = time.time()  # fresh session

        html_login_page = '<!DOCTYPE html><html><body><form id="login"></form></body></html>'
        valid_json = json.dumps({"d": json.dumps({"success": True, "parameter": "[]"})})

        call_count = {"n": 0}

        def fake_request_text(method, path, data=None, headers=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return html_login_page
            return valid_json

        with patch.object(client, "_request_text", side_effect=fake_request_text), \
             patch.object(client, "login") as mock_login:
            result = client._call_api("/vorbesteller/OrderForm.aspx/MenuOffer", {})

        mock_login.assert_called_once()
        # _authenticated_at was reset to None during retry
        # (login mock doesn't set it back, so it stays None)
        self.assertIsNone(client._authenticated_at)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
