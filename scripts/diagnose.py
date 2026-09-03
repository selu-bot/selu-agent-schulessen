#!/usr/bin/env python3
"""Read-only diagnosis. Never invokes add, delete, pay, or any other write."""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "capabilities/schulessen/container")
)
from schulessen_client import SchulessenClient, SchulessenError, _ensure_date, _today


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=_today().isoformat(),
        help="YYYY-MM-DD; defaults to today in Europe/Berlin",
    )
    args = parser.parse_args()
    try:
        meal_date = _ensure_date(args.date)
        username = os.environ.get("SCHULESSEN_USERNAME") or getpass.getpass(
            "schulessen.net username (hidden): "
        )
        password = os.environ.get("SCHULESSEN_PASSWORD") or getpass.getpass(
            "schulessen.net password (hidden): "
        )
        client = SchulessenClient()
        client.set_credentials(username, password)
        menu = client.get_menu(meal_date, meal_date, include_inactive=True)
        cart = client.get_cart_for_range(meal_date, meal_date)
        # Intentionally omit names, dish text, balances, prices, transaction IDs,
        # cookies and credentials. No raw payload or files are written.
        report = {
            "date": meal_date,
            "timezone": "Europe/Berlin",
            "days": [],
            "cart": {
                k: cart[k]
                for k in (
                    "active_item_count",
                    "pending_item_count",
                    "cancelled_item_count",
                    "unknown_item_count",
                    "status_known",
                )
            },
        }
        for day in menu["days"]:
            entry = {
                k: day[k]
                for k in (
                    "date",
                    "is_delivery",
                    "is_closed",
                    "reason_closed",
                    "availability",
                    "can_order",
                )
            }
            entry["meals"] = [
                {
                    k: meal[k]
                    for k in (
                        "meal_id",
                        "is_active",
                        "is_orderable",
                        "can_order",
                        "availability",
                        "order_deadline",
                        "cancellation_deadline",
                        "is_ordered",
                    )
                }
                for meal in day["meals"]
            ]
            report["days"].append(entry)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except (SchulessenError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Diagnosis cancelled.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
