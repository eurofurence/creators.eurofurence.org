# AGENTS.md

## Project Overview

This repository contains the Eurofurence Creator System.

The application manages the Video Creator Badge workflow and is intended to become the source of truth for Eurofurence creator data.

The target architecture, system boundaries, infrastructure, and architectural decisions are documented in:

`docs/docs/ARCHITECTURE.md`

Read `docs/docs/ARCHITECTURE.md` before making architectural, infrastructure, database, authentication, storage, or deployment changes.

Do not introduce changes that conflict with the documented architecture unless explicitly instructed.

---

## Technology Stack

The primary stack is:

* Python 3.14
* FastAPI
* SQLAlchemy
* Alembic
* PostgreSQL
* Jinja2
* HTMX
* Authlib
* psycopg
* pytest
* Ruff

Dependency management uses `uv` and a committed `uv.lock`.

HTMX must be vendored and served locally. Do not load HTMX from a CDN.

Do not introduce Node.js, npm, a frontend framework, or another major technology without an explicit requirement.

See `docs/docs/ARCHITECTURE.md` for architecture and infrastructure details.

---

## Development Principles

Keep the implementation simple, explicit, and maintainable.

* Prefer server-side rendering.
* Keep business logic on the server.
* Keep routes reasonably thin and templates focused on presentation.
* Use HTMX only where it meaningfully improves the user experience.
* Prefer existing project patterns.
* Avoid unnecessary abstractions, dependencies, services, and JavaScript.
* Do not introduce microservices.
* Do not perform unrelated or speculative refactoring.
* Make small, focused changes.
* Do not implement features that were not requested.

Do not silently resolve open requirements or architecture questions.

If a task requires an unresolved product, architecture, or infrastructure decision, surface the decision instead of inventing one.

---

## Coding Conventions

Write clear, conventional Python.

Prefer:

* type hints
* descriptive names
* small, focused functions
* explicit control flow
* standard FastAPI patterns
* standard SQLAlchemy 2.x patterns

Prefer readability over cleverness.

Use Ruff for formatting and linting.

Follow existing conventions when modifying existing code.

---

## Dependencies

Use `uv` for Python dependency management.

Do not manually edit `uv.lock`.

New dependencies must have a concrete purpose.

Prefer existing dependencies or the Python standard library where practical.

Security-sensitive standards should use established and maintained libraries rather than custom implementations.

Do not introduce Node.js or npm solely for frontend asset management.

---

## Testing

Use `pytest`.

New functionality should include appropriate tests.

Bug fixes should include regression tests when practical.

Tests must not depend on production infrastructure or production credentials.

Before completing implementation work, run when applicable:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Do not claim tests or checks passed unless they were actually executed successfully.

---

## Security

Security requirements are part of every implementation.

* Never commit or hard-code secrets.
* Never log credentials, tokens, passwords, or API keys.
* Treat user input and uploads as untrusted.
* Validate input server-side.
* Enforce authorization server-side.
* Clearly separate public and internal data.
* Follow least privilege.
* Do not expose production credentials to development tooling or coding agents.

Do not implement custom cryptography, OAuth 2.0, OpenID Connect, password hashing, or token formats when established implementations exist.

---

## Database

Use SQLAlchemy 2.x for database access and Alembic for schema migrations.

Schema changes must use migrations.

Do not manually modify the production database.

Do not assume a production PostgreSQL version that has not been confirmed.

Do not delete historical domain data merely because it is no longer publicly visible.

See `docs/docs/ARCHITECTURE.md` for database architecture.

---

## Authentication and Authorization

Authentication uses the Eurofurence Identity Provider through OpenID Connect / OAuth 2.0.

Use an established library such as Authlib.

Do not implement OIDC or OAuth manually.

Do not assume undocumented claims or Identity Provider behavior.

Authentication does not imply authorization.

Authorization must always be enforced server-side.

See `docs/docs/ARCHITECTURE.md` for authentication architecture and currently open questions.

---

## Storage

Production uploads use Eurofurence-managed S3-compatible object storage.

Treat uploads as untrusted input and validate them server-side.

Do not store uploaded binary files directly in PostgreSQL.

Do not assume production bucket names, credentials, endpoints, or policies.

See `docs/docs/ARCHITECTURE.md` for storage architecture.

---

## Deployment

Production deployment uses Eurofurence-managed infrastructure.

See `docs/docs/ARCHITECTURE.md` for the target development and production topology.

### Container Strategy

* Do not create or restructure container definitions unless required by the task.
* The Dev Container provides the reproducible development environment.
* The Production Container contains only application runtime requirements.
* Docker Compose provides supporting local services such as PostgreSQL.
* Production runs on Eurofurence Kubernetes and connects to centrally managed services.

Keep development tooling out of the production image unless required at runtime.

Do not assume direct access to production infrastructure.

---

## Documentation and Source of Truth

Keep documentation synchronized with meaningful changes.

The target architecture and architectural decisions are documented in `docs/docs/ARCHITECTURE.md`.

GitHub Issues in this repository are the primary source of truth for concrete implementation tasks, requirements, acceptance criteria, and known open questions related to that task.

Before implementing a task:

1. Read the relevant GitHub Issue and its current comments.
2. Read `AGENTS.md`.
3. Consult `docs/docs/ARCHITECTURE.md` for relevant architectural constraints.
4. Inspect the existing implementation before making changes.
5. Verify that the requested work is sufficiently specified.

Do not implement assumptions that are not supported by the Issue, project documentation, or existing confirmed behavior.

### Human in the Loop

Development is human-directed.

The human maintainer remains responsible for product decisions, architecture decisions, and resolving ambiguous requirements.

If an Issue contains ambiguity, conflicting information, missing requirements, or requires a consequential decision that is not already documented:

* do not silently choose an interpretation
* do not expand the scope of the Issue
* clearly identify the unresolved question
* ask the human maintainer for a decision before implementing the affected part

AI may suggest alternatives and explain trade-offs, but must not treat its own suggestion as an approved decision.

Prefer incremental changes that can be reviewed by the human maintainer before continuing with larger or difficult-to-reverse changes.

Do not close GitHub Issues or mark implementation work as complete on behalf of the human maintainer unless explicitly instructed.

### Documentation and Changelog

Use documentation according to its purpose:

* `docs/docs/ARCHITECTURE.md` describes the target architecture, system boundaries, and architectural decisions.
* GitHub Issues describe implementation tasks, requirements, acceptance criteria, and open questions.
* `docs/CHANGELOG.md` records completed, meaningful, release-relevant changes.

Update `docs/docs/ARCHITECTURE.md` when the architecture itself changes.

Maintain `docs/CHANGELOG.md` for changes that affect functionality, user-visible behavior, APIs, configuration, deployment behavior, security, or other release-relevant aspects of the system.

Do not use `docs/CHANGELOG.md` as a development diary. Do not add entries for trivial refactoring, formatting, comments, tests, or other internal changes without release relevance.

Reference the related GitHub Issue in changelog entries where appropriate.

Do not document proposals or assumptions as completed changes.

When requirements or architectural decisions are unclear, treat them as unresolved and ask the human maintainer rather than documenting or implementing them as established decisions.

---

## Git and Pull Requests

Keep commits and pull requests focused.

Use conventional commits.

Do not mix unrelated refactoring with feature work.

Before completing a change:

1. Review the diff.
2. Remove accidental or unrelated changes.
3. Run relevant tests.
4. Run Ruff.
5. Check for accidentally committed secrets or local configuration.
6. Update documentation when required.

AI-generated code follows the same review, testing, security, and quality requirements as manually written code.

---

## Important Constraints

* Do not invent missing requirements.
* Do not silently make product, architecture, or infrastructure decisions.
* Do not introduce unnecessary abstractions or runtime dependencies.
* Do not introduce a frontend framework without explicit approval.
* Do not introduce Node.js or npm solely for frontend asset management.
* HTMX must be vendored and must not be loaded from a CDN.
* Do not create microservices.
* Do not implement OAuth 2.0 or OIDC manually.
* Do not commit or expose secrets.
* Do not depend on production infrastructure for automated tests.
* Do not assume undocumented Eurofurence infrastructure behavior.
* Do not expose internal or personal data through public endpoints.
* Prefer small, focused implementations over speculative future-proofing.

If task instructions, requirements, existing code, or `docs/docs/ARCHITECTURE.md` conflict in a consequential way, identify the conflict instead of silently choosing an interpretation.
