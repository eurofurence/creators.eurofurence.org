from urllib.parse import urlsplit

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth

from app.auth.configuration import oidc_status
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
    server_metadata_url=settings.oidc_server_metadata_url,
    client_kwargs={
        "scope": "openid",
        "code_challenge_method": "S256",
        "id_token_signed_response_alg": "RS256",
        "token_endpoint_auth_method": "client_secret_post",
    },
)


def require_configuration() -> None:
    problems = {
        key: status
        for key, status in oidc_status(settings).items()
        if status != "configured"
    }
    if problems:
        import logging

        for key, status in problems.items():
            logging.getLogger(__name__).warning(
                "OIDC configuration: %s is %s", key.upper(), status
            )
        raise ValueError("OIDC configuration unavailable")


async def require_provider_metadata() -> None:
    """Pin the trusted issuer and algorithm before Authlib uses discovery."""
    metadata = await eurofurence.load_server_metadata()
    if metadata.get("issuer") != settings.oidc_issuer_url:
        raise OAuthError(error="invalid_issuer")
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = metadata.get(field)
        if not isinstance(value, str):
            raise TypeError("Incomplete provider metadata")
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.fragment:
            raise ValueError("Invalid provider endpoint")
    algorithms = metadata.get("id_token_signing_alg_values_supported")
    if not isinstance(algorithms, list) or "RS256" not in algorithms:
        raise ValueError("Provider must support RS256")
    metadata["id_token_signing_alg_values_supported"] = ["RS256"]
