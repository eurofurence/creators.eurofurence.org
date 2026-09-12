from authlib.integrations.starlette_client import OAuth

from app.config import settings


oauth = OAuth()

eurofurence = oauth.register(
    name="eurofurence",
    client_id=settings.oidc_client_id,
    client_secret=(
        settings.oidc_client_secret.get_secret_value()
        if settings.oidc_client_secret is not None
        else None
    ),
    server_metadata_url=(
        f"{settings.oidc_issuer_url.rstrip('/')}/.well-known/openid-configuration"
        if settings.oidc_issuer_url is not None
        else None
    ),
    client_kwargs={
        "scope": "openid profile email",
        "code_challenge_method": "S256",
    },
)
