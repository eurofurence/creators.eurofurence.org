import os

# Set these before importing application settings; never use a developer's .env.
os.environ.update(
    ENVIRONMENT="development",
    SESSION_SECRET="test-session-secret-at-least-32-characters",
    DATABASE_URL="sqlite://",
    OIDC_CLIENT_ID="test-client",
    OIDC_CLIENT_SECRET="test-client-secret",
    OIDC_ISSUER_URL="https://identity.example/",
    OIDC_SERVER_METADATA_URL="https://identity.example/.well-known/openid-configuration",
    OIDC_REDIRECT_URI="http://127.0.0.1:8000/auth/callback",
)
