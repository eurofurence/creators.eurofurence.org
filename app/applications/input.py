import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

PLATFORMS = (
    "Bluesky",
    "Facebook",
    "Instagram",
    "Mastodon",
    "Threads",
    "TikTok",
    "Twitch",
    "X",
)
CONTENT_TYPES = ("LIVESTREAM", "SHORTS", "VLOGS")
HOSTS = {
    "Bluesky": ("bsky.app",),
    "Facebook": ("facebook.com", "www.facebook.com"),
    "Instagram": ("instagram.com", "www.instagram.com"),
    "Threads": ("threads.net", "www.threads.net", "threads.com", "www.threads.com"),
    "TikTok": ("tiktok.com", "www.tiktok.com"),
    "Twitch": ("twitch.tv", "www.twitch.tv"),
    "X": ("x.com", "www.x.com", "twitter.com", "www.twitter.com"),
}


def https_url(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 2048
        or any(c.isspace() or ord(c) < 32 for c in value)
        or "\\" in value
    ):
        raise ValueError("Enter a valid HTTPS URL (maximum 2048 characters).")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        parts.hostname.encode("idna")
    except ValueError, UnicodeError:
        raise ValueError("Enter an HTTPS URL without embedded credentials.") from None
    return value


@dataclass(frozen=True)
class ChannelInput:
    platform: str
    original_representation: str
    normalized_account: str
    canonical_url: str
    is_primary: bool


def normalize_channel(platform: str, original: str, primary: bool) -> ChannelInput:
    if platform not in PLATFORMS:
        raise ValueError("Choose a supported publication platform.")
    if len(original) > 2048:
        raise ValueError("Channel input is too long.")
    value = original.strip()
    host = ""
    is_url = "://" in value
    if is_url:
        parts = urlsplit(https_url(value))
        host = parts.hostname.lower()
        if parts.port not in (None, 443):
            raise ValueError("Publication URLs must use the standard HTTPS port.")
        if platform != "Mastodon" and host not in HOSTS[platform]:
            raise ValueError("The channel URL must belong to the selected platform.")
        path = parts.path.strip("/")
        if platform == "Facebook" and path == "profile.php":
            query = parse_qs(parts.query)
            if (
                len(query.get("id", [])) != 1
                or not query["id"][0].isascii()
                or not query["id"][0].isdigit()
            ):
                raise ValueError("Facebook profile URLs require a numeric id.")
            account = "id:" + query["id"][0]
            return ChannelInput(
                platform,
                original,
                account,
                "https://www.facebook.com/profile.php?id=" + query["id"][0],
                primary,
            )
        if platform == "Bluesky":
            if not path.startswith("profile/"):
                raise ValueError("Use a Bluesky profile URL or complete handle.")
            path = path.removeprefix("profile/")
        if platform in ("Mastodon", "Threads", "TikTok"):
            if not path.startswith("@"):
                raise ValueError("Use an account profile URL containing /@handle.")
            path = path[1:]
        value = path
    else:
        value = value.removeprefix("@")
    if platform == "Mastodon":
        if not is_url:
            raise ValueError("Mastodon requires a complete https://host/@account URL.")
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError("Enter a Mastodon account profile URL.")
        host = host.encode("idna").decode("ascii")
        account = value.lower() + "@" + host
        canonical = f"https://{host}/@{value.lower()}"
    else:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError(
                "Enter a channel handle or account profile URL, not a post URL."
            )
        account = value.lower()
        if platform == "Bluesky" and "." not in account:
            raise ValueError("Use the complete Bluesky handle, including its domain.")
        prefixes = {
            "Bluesky": "https://bsky.app/profile/",
            "Facebook": "https://www.facebook.com/",
            "Instagram": "https://www.instagram.com/",
            "Threads": "https://www.threads.com/@",
            "TikTok": "https://www.tiktok.com/@",
            "Twitch": "https://www.twitch.tv/",
            "X": "https://x.com/",
        }
        canonical = prefixes[platform] + account
    return ChannelInput(platform, original, account, canonical, primary)


@dataclass(frozen=True)
class ApplicationInput:
    content_types: tuple[str, ...]
    channels: tuple[ChannelInput, ...]
    videos: tuple[str, ...]

    def validate(self) -> None:
        if not self.content_types or not set(self.content_types) <= set(CONTENT_TYPES):
            raise ValueError("Choose at least one planned content type.")
        if sum(c.is_primary for c in self.channels) != 1:
            raise ValueError("Choose exactly one primary publication channel.")
        accounts = [(c.platform, c.normalized_account) for c in self.channels]
        if len(set(accounts)) != len(accounts):
            raise ValueError("Each publication account may only be listed once.")
        if len(self.videos) > 10:
            raise ValueError("Provide at most 10 convention video URLs.")
        for video in self.videos:
            https_url(video)


def parse_application(form) -> ApplicationInput:
    platforms, accounts = form.getlist("platform"), form.getlist("account")
    if len(platforms) != len(accounts):
        raise ValueError("Each channel needs a platform and account.")
    channels = tuple(
        normalize_channel(p, a, str(i) == form.get("primary"))
        for i, (p, a) in enumerate(zip(platforms, accounts))
        if a.strip()
    )
    result = ApplicationInput(
        tuple(form.getlist("content_type")),
        channels,
        tuple(https_url(v) for v in form.get("videos", "").splitlines() if v.strip()),
    )
    result.validate()
    return result
