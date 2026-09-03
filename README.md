# selu-agent-schulessen

A school lunch agent for [Selu](https://github.com/selu-bot/selu). It reads
[schulessen.net](https://www.schulessen.net/Vorbesteller/Default.aspx), distinguishes
meal availability from order status, and places or cancels orders after approval.
German is the default language; English prompts and labels are included.

## Setup in Selu

Install the release's `agent.tar.gz` through the Selu marketplace or your host's
agent installation flow. Release metadata includes the archive URL, SHA-256 and
versioned GHCR capability image. For local development, build the image named in
`capabilities/schulessen/manifest.yaml`:

```sh
docker build -t selu-cap-schulessen:latest capabilities/schulessen/container
```

Configure **per-user credentials in Selu**, scoped to this capability:

| Name | Value |
| --- | --- |
| `USERNAME` | schulessen.net username or card number |
| `PASSWORD` | schulessen.net password |

The host supplies these in each gRPC request's `config_json`; simply setting them
as container environment variables does not configure the service. Credentials
and session cookies remain in process memory. Changing credentials clears the
previous session. Do not put passwords into prompts, source files or commits.

Keep the manifest's recommended policies: reads are `allow`; `place_order` and
`cancel_order` are `ask`. The host must enforce these permissions. The service
itself is a trusted internal capability, not an authenticated public API.

## Tools

Dates are real calendar dates in `YYYY-MM-DD` format. Defaults use
**Europe/Berlin**, from today through Friday (the same day on weekends). The
maximum read range is 93 inclusive days. For a single day, supply both endpoints.
Amounts are integer cents; `null` means unknown, never zero or false.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `get_menu` | Optional `from_date`, `to_date`, `include_inactive` (default false) | Menu, closure notices, deadlines and effective availability |
| `get_cart` | Optional `from_date`, `to_date` | Confirmed orders, pending changes, settled cancellations, unknown entries and account balance |
| `place_order` | Required `date`, `meal_id`; optional `quantity=1`, `outlet_slot_id=1`, `components=[]`, `allow_checkout_existing_cart=false` | Rechecks availability, stages the order, checks the cart, submits checkout, verifies the saved order |
| `cancel_order` | Required `date`, `meal_id`; optional `transaction_id` | Resolves an active transaction, checks cancellation permission, stages cancellation, submits checkout and verifies the result |

Unknown arguments and incorrect types are rejected. In particular, `"false"` is
not a boolean and `true` is not an integer ID. Quantities and IDs must be positive
integers. Obtain IDs and any required component/slot choices from the live site;
never guess them.

Example read arguments:

```json
{"from_date": "2026-09-03", "to_date": "2026-09-03"}
```

### Availability is not order status

A day exposes `is_delivery`, `is_closed`, `reason_closed`, `availability`, and
`can_order`. A meal exposes the raw `is_orderable` flag plus effective `can_order`.
Only **`can_order=true`** permits an offer to order. Effective meal availability is
one of `orderable`, `no_service`, `no_offer`, `unavailable`, `deadline_passed`,
`sold_out`, or `unknown`. Closure notices override any stored dish description.
Closed days suppress the underlying description and price in tool results.

Order and cancellation deadlines are ISO timestamps with Berlin offsets, derived
from the site's millisecond timestamps. A passed deadline blocks ordering even
if the site's raw `is_orderable` flag remains true. Missing critical availability
fields block writes rather than guessing. A served meal whose order deadline has
passed can still be described; that differs from a closed day with no meal.

Always use `get_cart` for the same dates before claiming a meal is or is not
ordered. A menu's absent `is_ordered` field is `null`. It is not evidence of an
empty cart. Enabling the website's own Bestellassistent does not establish a
particular order's status.

Cart statuses:

- `active`: saved/paid quantity agrees with requested quantity and no change is pending.
- `cancelled`: zero quantity is settled, with no pending refund/change.
- `pending`: an unsaved quantity, component change or nonzero payable amount.
- `pending_cancellation`: requested quantity is zero but the change remains pending.
- `unknown`: insufficient data to establish a settled state.

The result includes corresponding item lists and counts. `has_active_order` is
`null` when there are only pending/unknown entries; `status_known=false` means
those entries need review. Malformed or unrecognized responses fail explicitly
instead of returning a false “no orders” result. Balance comes only from an
account balance field, including the site's `bank_balance`, never a line amount.

## Ordering safety and operational limits

Checkout is a **shared-cart action**: the provider's `ShoppingCardPay` endpoint
accepts no transaction ID or date. Before writing, the client checks the same
window used by the website: Monday of the current week through 90 days later.
Mutation dates must fall within this window. Existing pending changes block
ordering unless the user explicitly approved all of them and
`allow_checkout_existing_cart=true` is supplied. Existing pending changes always
block cancellation; resolve them on the website first.

The client checks the staged cart before payment, rejects unrelated changes, and
verifies the final target order/cancellation. Writes are never automatically
retried, including after authentication failures. Only read calls get one
re-authentication attempt. If staging, payment or verification fails, the tool
reports an **uncertain result**: the order or cart may already have changed. Read
the cart and inspect the website before retrying. There is no automatic rollback.

The upstream API has no transactional checkout token or idempotency key. Checks
cannot guarantee coverage beyond the website's 90-day window or prevent another
browser/process from changing the cart between the last check and payment.
Operate a single capability instance per account and avoid concurrent manual
cart editing. Where that cannot be guaranteed, keep write tools disabled and use
the website for checkout. Custom meal components/slots still require explicit
user choices; unsupported or incomplete provider data blocks the operation.

Calls are serialized inside one process, including credential switching, with a
five-second queue wait. This lock does not coordinate multiple replicas. HTTP
requests time out after 20 seconds, response reads are capped at 2 MiB, JSON input
fields at 64 KiB each, and gRPC messages/concurrency are bounded. A tool may perform
several HTTP calls, so configure the host timeout to allow the entire workflow;
a lost response to a write must not trigger a retry.

The runtime serves the `selu.capability.v1.Capability` gRPC contract on port 50051
with `Invoke`, `StreamInvoke` and `Healthcheck`. Streaming produces one final
chunk. Healthcheck tests process liveness, not credential validity or upstream
availability. The container runs as UID/GID 1000 and supports a read-only root
filesystem with temporary `/tmp`. It includes a Docker healthcheck.

Do not expose port 50051 publicly: internal gRPC is plaintext and relies on the
Selu host's isolation and approval enforcement. Permit outbound HTTPS only to
`www.schulessen.net:443` as the manifest specifies. The client also rejects
foreign origins and redirects away from HTTPS schulessen.net. Logs contain tool
names and error classes, not credentials, raw payloads or upstream response bodies.

## Development and verification

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r capabilities/schulessen/container/requirements-build.txt
python -m unittest discover -s tests -v
python -m pip check
```

The tests generate protobuf modules in a temporary directory. They cover closure
rules, deadlines, uncertain order states, duplicate prevention, checkout and
cancellation failures, session expiry, input validation, secret-safe errors and
the real localhost gRPC contract. No test needs real account credentials or
calls schulessen.net. Without build dependencies the gRPC test module is skipped;
the full CI/release gate installs them and runs it.

To run the service directly, generate the protocol modules first:

```sh
python -m grpc_tools.protoc \
  --proto_path=capabilities/schulessen/container \
  --python_out=capabilities/schulessen/container \
  --grpc_python_out=capabilities/schulessen/container \
  capabilities/schulessen/container/capability.proto
python capabilities/schulessen/container/server.py
```

Runtime dependencies are pinned separately from build dependencies. The builder
generates protocol modules and runtime wheels; the final image has no
`grpcio-tools`. Update gRPC, protobuf and generated modules together and rerun the
full gate. The Python base image follows the `3.12-slim` tag; rebuild regularly for
OS updates and retain the deployed image digest for rollback.

## CI and releases

CI runs unit/gRPC tests on Python 3.12, checks dependency consistency, audits runtime
dependencies for known vulnerabilities, builds the
image, and smoke-tests it without network access, with a read-only filesystem and
dropped Linux capabilities. Tag releases must pass the same gate before
publishing multi-architecture images and the agent archive.

Release tags use `vMAJOR.MINOR.PATCH` (optional prerelease/build-style suffix).
The release workflow requires repository package/release permissions and the
existing `SELU_WEBHOOK_URL` / `SELU_WEBHOOK_TOKEN` secrets for marketplace
notification. It rewrites the packaged manifest to the versioned GHCR image and
publishes the archive's SHA-256.

Deploy the **image and agent archive together**: this fix changes both parsing
and prompts. A local source change alone does not update an installed Selu agent.
Retain the previous release archive/image to roll back. Verify menu and cart reads
for one closed day and one known ordered day after upgrading. Real order changes
require user approval and were not exercised against the live account here.

## Repository layout

- `agent.yaml`, `agent*.md`: identity, routing and language prompts; memory is disabled.
- `capabilities/schulessen/manifest.yaml`: tools, schemas, credentials and resource policy.
- `capabilities/schulessen/prompt*.md`: availability, order-status and approval rules.
- `capabilities/schulessen/container/`: standard-library client, gRPC service and image build.
- `i18n/`: user-facing labels and approval text.
- `scripts/diagnose.py`: credential-safe read-only diagnostic helper.
- `tests/`: offline regressions and localhost service tests.
