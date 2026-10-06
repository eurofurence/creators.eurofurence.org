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

## Container build

```sh
docker build -t creators .
```

The image runs Uvicorn on port 8000. Supply environment-specific configuration
and apply migrations separately before using authentication.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines,
[SECURITY.md](SECURITY.md) for security reporting, and [LICENSE](LICENSE).
