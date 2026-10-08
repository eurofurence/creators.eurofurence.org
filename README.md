# Eurofurence Creator System

A server-rendered application for Eurofurence Video Creator applications,
review, creator profiles, helpers, badges and pickup. It also provides a public
creator gallery/API and administrative exports.

**Development milestone, not a production release.** Real EF OIDC is supported;
live Registration, trusted identity profile data and notification delivery still
need EF contracts and adapters. Local acceptance testing uses explicit manual
Registration records.

## Prerequisites and installation

- Python 3.14, Git, Docker with Compose (Docker Desktop on Windows).
- An EF development OIDC client and an approved callback URI. Obtain credentials
  through the maintainer/EF Identity team, never from repository history.
- Helm is optional for local development and required for chart verification.
- No Node.js or frontend build step. Runtime dependencies and their purposes are
  pinned in `requirements.txt`; test/lint tools are in `requirements-dev.txt`.

From a clone of this repository, create a virtual environment:

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

On Windows PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File scripts/dev-setup.ps1
```

The Windows setup securely prompts for missing OIDC credentials, preserves valid
existing `.env` values, starts local services without recreating existing
containers, applies migrations, selects/creates a local Event and runs safe
diagnostics. It does not fabricate Registration eligibility or grant roles.

Alternatively, use VS Code's **Dev Containers: Reopen in Container**. The Dev
Container installs dependencies and starts the supporting services; complete the
configuration and migrations below. Use `db` and `s3` as service hostnames inside
that container, and loopback addresses for host-side development.

## Configuration

For manual setup, copy `.env.example` to `.env` **only if `.env` does not already
exist**. Configure it privately; environment variables override this file.

| Setting | Local configuration |
| --- | --- |
| `ENVIRONMENT` | `development` |
| `SESSION_SECRET` | A unique random secret of at least 32 characters |
| `DATABASE_URL` | Compose's development database; use host `127.0.0.1:5432` on the host or `db:5432` in the Dev Container |
| `S3_ENDPOINT_URL` | `http://127.0.0.1:9090` on the host or `http://s3:9090` in the Dev Container |
| `S3_BUCKET`, `S3_REGION` | `creators`, `us-east-1` for the local mock |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Local mock values from `.env.example`; never reuse them in production |
| `OIDC_*` | Approved client ID/secret, issuer, discovery URL and exact registered redirect URI |
| `OIDC_REDIRECT_URI` | Normally `http://127.0.0.1:8000/auth/callback` for this host setup |
| `ACTIVE_EVENT_ID` | Existing local Event ID; no calendar-year fallback |
| `REGISTRATION_PROVIDER` | `manual_test` for explicit local fixtures; otherwise `unavailable` until a real adapter exists |
| `REGISTRATION_MANUAL_FILE` | Private ignored JSON path, for example `temp/manual-registration.json` |
| `NOTIFICATION_PROVIDER_FACTORY` | Leave unset locally; no real transport is bundled |

The current OIDC client requests only `openid`. Open the same origin as the
registered callback; mixing `localhost` and `127.0.0.1` breaks session/state
continuity. Do not change a valid existing client configuration to match an
example. Browser credentials, `.env` and manual identity fixtures must remain
untracked. `python -m app.dev.check` reports configuration status without values;
it does not prove that every external service is reachable.

### Services, migrations and Event

If the Docker network does not exist, create it once:

```sh
docker network create creators-dev
```

Then start services without replacing existing containers:

```sh
docker compose up -d --no-recreate db s3
docker compose exec -T db pg_isready -U creators -d creators
python -m alembic upgrade head
python -m app.dev.event --interactive
python -m app.dev.check
```

`app.dev.event` preserves an existing selected Event. New local Events use dates
relative to creation; the command records `ACTIVE_EVENT_ID` in `.env`. Use
`--select-id EVENT_ID` to select an existing Event without changing its data.
Do not run `--refresh` on a real or independently configured Event.

New Compose containers bind to loopback and persist PostgreSQL and S3 in named
volumes. **Older S3 containers may have no persistent volume.** `--no-recreate`
keeps them intact but does not apply changed ports, images or mounts. Before
applying Compose changes, inspect `docker compose ps` and the S3 container's
mounts; back up/transfer its objects or complete an approved domain reset first.
Never recreate an old unmounted S3 container while retaining its DB references.

### Manual Registration

Complete real OIDC login first. `/auth/me` shows the local user ID. In a private
terminal, `python -m app.dev.identity --user-id USER_ID` retrieves that user's
verified issuer/subject for the local fixture. Its output is personal data; do
not attach it to an issue or commit it.

Create the ignored JSON file with one entry per identity and Event. Replace the
placeholders with that verified identity and the actual Event ID/year:

```json
[
  {
    "issuer": "https://your-approved-issuer.example/",
    "subject": "verified-subject",
    "event_id": 1,
    "event_year": 2027,
    "status": "PAID",
    "reg_id": "LOCAL-TEST-1",
    "nickname": "Local test creator"
  }
]
```

`PAID` and `CHECKED_IN` are eligible; `INELIGIBLE`, `UNKNOWN` and `UNAVAILABLE`
exercise negative/error paths. Missing or duplicate bindings fail closed.
Fixtures reload on every check. The provider is rejected outside development/test.
Use separate real dev accounts and fixtures for Helper and role-isolation tests.

ADMIN and event-scoped BADGE_STAFF are explicitly granted by an operator after
login, using verified identity values and a reason:

```sh
python -m app.identity.roles grant-admin --issuer ISSUER --subject SUBJECT --reason "Initial administrator"
python -m app.identity.roles grant-badge-staff --issuer ISSUER --subject SUBJECT --event-id EVENT_ID --reason "Local pickup testing"
```

Corresponding commands are `revoke-admin` and `revoke-badge-staff`. OIDC login
never automatically grants a role.

## Start, use and stop

```sh
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open **http://127.0.0.1:8000/**. Login and CSRF-protected POST logout return to `/`.
`/health` checks the process; `/ready` checks DB connectivity only. `/docs` is the
OpenAPI reference.

- `/applications`: submission, current picture and workflow status.
- `/admin/applications`: ADMIN review. Review errors preserve entered notes.
- Creator profile links: public name, channels, pictures, invitations and participation.
- `/helpers`: the signed-in user's helper relationships.
- `/staff`: authorized Event selection, stored Reg-ID lookup and pickup.
- `/gallery/YEAR` and `/api/v1/events/YEAR/creators`: publicly visible creators.

The submitted PNG must be exactly 600×600 pixels and at most
`PROFILE_IMAGE_MAX_BYTES` (default 10 MiB). It is decoded/re-encoded and remains
the current picture through approval. Uploading a replacement is a separate
action. Complete the public creator name after approval before publication or
printing. Print checks still require real assets and current eligibility.

Stop Uvicorn with **Ctrl+C**. For normal pauses use:

```sh
docker compose stop
```

Do not use `down -v` to stop development. Legacy unmounted S3 containers require
the storage transition procedure before shutdown/recreation; do not assume their
contents survive. Keep a private DB-and-object backup for data you need to retain.

## Tests and checks

```sh
python -m pytest -ra
python -m ruff check .
python -m ruff format --check .
python -m alembic check
git diff --check
```

Tests use isolated data and simulated identity-provider responses, not EF
credentials. `alembic check` uses the configured database and is read-only;
upgrade that database normally if its migration head is behind.

For PostgreSQL coverage, use a **dedicated disposable test database**, never the
acceptance database. One local setup, with deliberately disposable credentials:

```sh
docker run --name creators-test-db -d -p 127.0.0.1:55439:5432 -e POSTGRES_USER=creator_test -e POSTGRES_PASSWORD=local_test_only -e POSTGRES_DB=creator_test postgres:16
docker exec creators-test-db pg_isready -U creator_test -d creator_test
```

If an appropriate container already uses that port, reuse it. Set the test URL:

```sh
export TEST_POSTGRES_URL='postgresql+psycopg://creator_test:local_test_only@127.0.0.1:55439/creator_test'
```

PowerShell uses `$env:TEST_POSTGRES_URL = 'postgresql+psycopg://creator_test:local_test_only@127.0.0.1:55439/creator_test'`.
Then run `python -m pytest -ra`. Fixtures create/drop random schemas and test
migrations, constraints and concurrency. Without this variable, PostgreSQL
cases are skipped; inspect the summary. SQLite variants of PostgreSQL-only
locking tests skip even in a full run.

Set `HELM_BINARY` to the installed Helm executable for the chart-rendering test.
CI uses Helm 4.3.0 and PostgreSQL 16; these are test targets, not an assumed EF
production database version. Windows-only tooling tests skip on other platforms.
Existing migration revisions are excluded from Ruff formatting checks.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Login unavailable or callback fails | Safe diagnostics, exact callback origin, approved credentials and issuer; never enable callback query logging |
| Eligibility unavailable | Active Event, fixture path, issuer/subject and Event ID/year; production has no manual fallback |
| Approved creator absent from gallery or print blocked | Public name, current valid image, primary channel, visibility, participation and retention; do not bypass print validation |
| Stored picture unavailable | S3 running, correct bucket/endpoint and retained object; an empty replacement container does not repair DB references |
| Reg-ID lookup empty | Search the stored attendee Reg-ID, not the separate badge number; check Event scope |
| Changes locked | Application state/window and badge-change deadline; print time is a separate setting |
| Notification pending | No transport is configured by default; the outbox is not proof of delivery |
| Dates expired after a pause | Inspect the Event before changing it; choose an explicit refresh, Event configuration or approved local reset |

## Deployment and maintenance

`docker build -t creators .` builds the non-root runtime image. The Helm chart
is [charts/creators](charts/creators); it does not provision PostgreSQL, S3 or
Identity.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
[SUPPORT.md](SUPPORT.md) and [LICENSE](LICENSE).
