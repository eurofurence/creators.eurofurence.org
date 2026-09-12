# Eurofurence Creator System

Web application for managing Eurofurence Video Creator applications.

## Development

The application is intended to run as a containerized service within the Eurofurence infrastructure.

Local development uses a VS Code Dev Container with PostgreSQL and an S3-compatible development storage provided through Docker Compose.

### Prerequisites

* Docker Desktop
* Visual Studio Code
* VS Code Dev Containers extension

### Set up the development environment

Clone the repository and open it in Visual Studio Code.

Then open the repository in the Dev Container:

1. Open the Command Palette (`Ctrl+Shift+P`).
2. Select `Dev Containers: Reopen in Container`.
3. Wait for the container setup to complete.

Python dependencies from `requirements.txt` are installed automatically when the Dev Container is created.

The PostgreSQL and S3 development services are started automatically when the Dev Container starts.

If dependencies need to be installed manually, run:

```bash
python -m pip install -r requirements.txt
```

### Configure the local environment

Create a local `.env` file based on `.env.example`.

```bash
cp .env.example .env
```

The `.env` file contains local configuration and secrets and must not be committed.

The default development configuration uses the Docker service names for PostgreSQL and S3:

```dotenv
DATABASE_URL="postgresql+psycopg://creators:creators@db:5432/creators"

S3_ENDPOINT_URL="http://s3:9090"
S3_BUCKET="creators"
S3_ACCESS_KEY_ID="test"
S3_SECRET_ACCESS_KEY="test"
S3_REGION="us-east-1"
```

### Local services

PostgreSQL and S3Mock are started automatically when the Dev Container starts.

Check that both services are running:

```bash
docker compose ps
```

They can also be started manually if necessary:

```bash
docker compose up -d
```

To verify that PostgreSQL is accepting connections:

```bash
docker compose exec db pg_isready -U creators -d creators
```

To verify the local S3 connection:

```bash
curl http://s3:9090
```

The response should contain the `creators` bucket.

### Database migrations

Apply available database migrations:

```bash
python -m alembic upgrade head
```

To check the currently applied migration:

```bash
python -m alembic current
```

### Start the application

Start the FastAPI development server:

```bash
python -m uvicorn app.main:app --reload
```

The application is then available at:

* `http://127.0.0.1:8000/health` — health check
* `http://127.0.0.1:8000/docs` — OpenAPI documentation

### Stop local services

When development is finished, stop the PostgreSQL and S3Mock containers:

```bash
docker compose down
```

The PostgreSQL data volume is preserved.

To also remove the local PostgreSQL volume and all locally stored database data:

```bash
docker compose down -v
```

See `CONTRIBUTING.md` for contribution guidelines and `SECURITY.md` for reporting security issues.

## Maintainer

[@Neeklass](https://github.com/Neeklass)

## License

See `LICENSE`.
