# Eurofurence Creator System

Web application for managing Eurofurence Video Creator applications.

## Development

The application is intended to run as a containerized service within the Eurofurence infrastructure.

Local development uses a VS Code Dev Container and a PostgreSQL development database provided through Docker Compose.

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

For local PostgreSQL development, the database URL should point to the PostgreSQL container exposed through Docker Desktop, for example:

```dotenv
DATABASE_URL="postgresql+psycopg://creators:creators@host.docker.internal:5432/creators"
```

### Start PostgreSQL

Start the local PostgreSQL database:

```bash
docker compose up -d db
```

Check that the container is running:

```bash
docker compose ps
```

To verify that PostgreSQL is accepting connections:

```bash
docker compose exec db pg_isready -U creators -d creators
```

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

### Stop the local database

When development is finished, stop the PostgreSQL container:

```bash
docker compose down
```

The PostgreSQL data volume is preserved.

To also remove the local database volume and all locally stored database data:

```bash
docker compose down -v
```

See `CONTRIBUTING.md` for contribution guidelines and `SECURITY.md` for reporting security issues.

## Maintainer

([@Neeklass](https://github.com/Neeklass))

## License

See `LICENSE`.
