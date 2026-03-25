# selu-agent-schulessen

School lunch agent for [Selu](https://github.com/selu-bot/selu).

This agent logs into `schulessen.net`, reads the lunch menu, shows existing
orders, and can place or cancel meal orders with explicit approval.

## Capability

- **schulessen**
  - `get_menu`
  - `get_cart`
  - `place_order`
  - `cancel_order`

## Credentials

- `USERNAME` (required, per user)
- `PASSWORD` (required, per user)

## Permission model

- `get_menu` and `get_cart` are read-only and default to `allow`.
- `place_order` and `cancel_order` change real orders and default to `ask`.
- Network access is limited to `www.schulessen.net:443`.
- Filesystem access is `temp` only.

## Notes

- The site is an older ASP.NET application and requires a bootstrap login flow
  before JSON endpoints work. The capability handles that automatically.
- Ordering is intentionally cautious: if the cart already contains unexpected
  pending items, the tool refuses to submit them unless explicitly allowed.
