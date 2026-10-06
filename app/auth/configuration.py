"""Value-free OIDC configuration diagnostics shared by login and operators."""

from urllib.parse import urlsplit


def value_status(value, *, minimum=1):
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    if not value:
        return "missing"
    if (
        not isinstance(value, str)
        or len(value.strip()) < minimum
        or value.strip().upper().startswith("INSERT_")
        or any(character in value for character in ("\r", "\n", "\x00"))
    ):
        return "placeholder/invalid"
    return "configured"


def oidc_status(configuration):
    fields = (
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_issuer_url",
        "oidc_server_metadata_url",
        "oidc_redirect_uri",
    )
    result = {field: value_status(getattr(configuration, field)) for field in fields}
    for field in fields[2:]:
        if result[field] != "configured":
            continue
        try:
            url = urlsplit(getattr(configuration, field))
            local_http = (
                field == "oidc_redirect_uri"
                and configuration.environment == "development"
                and url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            if (
                not url.hostname
                or url.username
                or url.password
                or url.fragment
                or (field == "oidc_redirect_uri" and url.query)
                or (url.scheme != "https" and not local_http)
            ):
                result[field] = "placeholder/invalid"
        except ValueError:
            result[field] = "placeholder/invalid"
    return result
