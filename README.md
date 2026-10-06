# Eurofurence Creator System

Web application for managing Eurofurence Video Creator applications.

## Development setup

Use Docker Desktop, Visual Studio Code, and the Dev Containers extension.
Open the repository and select **Dev Containers: Reopen in Container**.
The container installs `requirements-dev.txt` with pip and starts the local
PostgreSQL and S3-compatible services.

For development outside the container, create and activate a Python 3.14 virtual
environment, then install the development dependencies:

```sh
python -m pip install -r requirements-dev.txt
```

Runtime dependencies are in `requirements.txt`.

## Configuration

Copy `.env.example` to `.env` and configure it for your environment. Never commit
the `.env` file or credentials. Set `SESSION_SECRET` to a unique random value of
at least 32 characters.

Login requires `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ISSUER_URL`,
`OIDC_SERVER_METADATA_URL`, and `OIDC_REDIRECT_URI`. The redirect URI must exactly
match the registered callback. Open the application on the same origin.
The current login requests only `openid`.

Set `ACTIVE_EVENT_ID` to the local database ID of the application event.
Authenticated `GET /applications/eligibility` checks the current user for that
event. Missing event configuration or unavailable Registration verification returns
HTTP 503 with `UNAVAILABLE` and `retryable: true`. The Registration adapter is
currently unavailable until the EF contract and access are supplied.

Configure the active event's application opening/closing times and its separate
`badge_change_deadline_at` in the database using UTC timestamps. Existing events
have no change deadline after migration; approval changes fail closed until it is
configured. `BADGE_SEQUENCE_START` defaults to 1 and only affects creation of a
new event counter.

Interpret local event deadlines in `Europe/Berlin` (including summer-time offset)
and convert them to UTC before storing them. `badge_print_at` is the physical
print timestamp; it does not control editing. Set the event's `helper_limit` to
NULL for unlimited helpers or a nonnegative integer for a per-creator limit.

Set `DATABASE_URL` and the `S3_*` settings for your environment. Docker service
hostnames work inside the development network; host-side development must use
the published ports in `docker-compose.yml`.

The Dev Container creates the shared network automatically. To start services
manually, create `creators-dev` if it does not exist, then run:

```sh
docker compose up -d db s3
```

## Run

Apply migrations before logging in, then start the development server:

```sh
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --no-access-log
```

At the configured application origin, `/health` provides a health check, `/docs`
provides OpenAPI documentation, `/auth/login` starts login, and `/auth/me` returns
the authenticated local user ID. `POST /auth/logout` clears the local session.

`/applications` provides the creator dashboard and form. Administrators use
`/admin/applications`. Submission and approval require available Registration
verification. Trusted email is shown as unavailable until its integration is
configured. Review notifications are queued for the separate notification worker.

Approved creators manage profiles, pictures and invitations from the dashboard.
Helpers use `/helpers`. Invitation links require the helper's own login; their
secret stays in the URL fragment until the registration form is submitted.
Configure private S3 for pictures. `PROFILE_IMAGE_MAX_BYTES` defaults to 10485760
(10 MiB); `INVITATION_ATTEMPTS_PER_MINUTE` defaults to 10 per signed-in user.

Run image cleanup periodically in a separate process using the same DB/S3
configuration. It retries failed deletions, removes replaced/abandoned uploads,
and deletes images when their event reaches `data_delete_at`. A nonzero exit
status means deletions remain pending and the command should be retried:

```sh
python -m app.creators.images
```

Run this successfully before deleting event records. This command handles image
assets only; full event/person cleanup remains pending.

Staff use `/staff` to select an authorized event for Reg-ID lookup and per-badge
pickup. ADMIN can undo accidental pickup, manage banned channels, inspect failed
notifications, download profile PNGs and generate operational XLSX exports.
Print-ready exports require fresh Registration checks and complete profile assets.
Operations-only workbooks remain available for diagnosis and are explicitly not
print-ready. Both include private operational data and must be handled accordingly.

Run notification delivery in a separate process with the same database settings:

```sh
python -m app.notifications.worker
```

Set `NOTIFICATION_PROVIDER_FACTORY` to a trusted installed `module:factory`
implementing `app.notifications.client.NotificationProvider` after EF approves
notification access, registered Operational type keys and recipient mapping.
No live EF transport is bundled while that contract is unconfirmed. Unset
configuration explicitly reports provider unavailable; there is no SMTP fallback.
The worker processes at most 100 due items, retries up to five attempts with
backoff/Retry-After, and exposes failures at `/admin/notifications`. It exits nonzero
for unavailable configuration or terminal failed items. Schedule repeated runs to
process retries. Provider calls must be asynchronous and bounded; the worker uses
a 60-second timeout and a five-minute recovery lease. An interrupted delivery after
provider acceptance can be retried, so the approved adapter should use the stable
delivery identifier for provider deduplication if the real contract supports it.

An operator with database access explicitly grants or revokes ADMIN after the
target user has logged in. Use the verified issuer and subject, not a local
numeric user ID. No user is automatically promoted:

```sh
python -m app.identity.roles grant-admin --issuer "$OIDC_ISSUER_URL" --subject "TARGET_SUBJECT" --reason "Initial administrator"
python -m app.identity.roles revoke-admin --issuer "$OIDC_ISSUER_URL" --subject "TARGET_SUBJECT" --reason "Access removed"
python -m app.identity.roles grant-badge-staff --issuer "$OIDC_ISSUER_URL" --subject "TARGET_SUBJECT" --event-id 1 --reason "Pickup shift"
python -m app.identity.roles revoke-badge-staff --issuer "$OIDC_ISSUER_URL" --subject "TARGET_SUBJECT" --event-id 1 --reason "Shift ended"
```

To stop local services while preserving database data:

```sh
docker compose down
```

## Checks

Run the same checks as CI:

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

Tests use isolated databases and simulated identity-provider responses; they do
not require Eurofurence services or credentials. Existing migration revisions
are excluded from formatting checks.

Set `TEST_POSTGRES_URL` to a dedicated local PostgreSQL database to run the full
integration suite. CI uses PostgreSQL 16 as a test target; this does not prescribe
the production version. Tests create and remove a randomly named schema in that
database and verify migrations, constraints, concurrent approvals and rollback.
Without this variable, PostgreSQL tests are explicitly skipped.

## Container build

```sh
docker build -t creators .
```

The image runs Uvicorn on port 8000. Supply environment-specific configuration
and apply migrations separately before using authentication.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines,
[SECURITY.md](SECURITY.md) for security reporting, and [LICENSE](LICENSE).
