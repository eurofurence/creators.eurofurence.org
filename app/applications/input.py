import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlsplit

PLATFORMS = (
    "Bluesky",
    "Facebook",
    "Instagram",
    "Mastodon",
    "Threads",
    "TikTok",
    "Twitch",
    "X",
    "YouTube",
)
PLATFORM_LABELS = {platform: platform for platform in PLATFORMS} | {"X": "X (Twitter)"}
CONTENT_TYPES = ("LIVESTREAM", "SHORTS", "VLOGS")
HOSTS = {
    "Bluesky": ("bsky.app",),
    "Facebook": ("facebook.com", "www.facebook.com"),
    "Instagram": ("instagram.com", "www.instagram.com"),
    "Threads": ("threads.net", "www.threads.net", "threads.com", "www.threads.com"),
    "TikTok": ("tiktok.com", "www.tiktok.com"),
    "Twitch": ("twitch.tv", "www.twitch.tv"),
    "X": ("x.com", "www.x.com", "twitter.com", "www.twitter.com"),
    "YouTube": ("youtube.com", "www.youtube.com"),
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
    if platform == "YouTube" and value.lower().startswith(
        tuple(host + "/" for host in HOSTS["YouTube"])
    ):
        value = "https://" + value
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
        if platform == "YouTube" and path.startswith("channel/"):
            channel_id = path.removeprefix("channel/")
            if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
                raise ValueError("Enter a YouTube /@handle or /channel/ channel URL.")
            # IDs are opaque and case-sensitive. Do not infer handle/ID equivalence.
            return ChannelInput(
                platform,
                original,
                "channel:" + channel_id,
                "https://www.youtube.com/channel/" + channel_id,
                primary,
            )
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
        if platform in ("Mastodon", "Threads", "TikTok", "YouTube"):
            if not path.startswith("@"):
                raise ValueError("Use an account profile URL containing /@handle.")
            path = path[1:]
        value = path
    else:
        value = value.removeprefix("@")
    if platform == "YouTube":
        value = unicodedata.normalize("NFC", unquote(value) if is_url else value)
        if not value or not all(
            c.isalnum() or c in "_.-·" or unicodedata.category(c).startswith("M")
            for c in value
        ):
            raise ValueError("Enter a YouTube handle or channel URL, not a video URL.")
        account = value.lower()
        canonical = "https://www.youtube.com/@" + quote(account, safe="_.-")
    elif platform == "Mastodon":
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


def parse_channels(form) -> tuple[ChannelInput, ...]:
    platforms, accounts = form.getlist("platform"), form.getlist("account")
    if len(platforms) != len(accounts):
        raise ValueError("Each channel needs a platform and account.")
    channels = []
    for i, (platform, account) in enumerate(zip(platforms, accounts)):
        try:
            channels.append(
                normalize_channel(platform, account, str(i) == form.get("primary"))
            )
        except ValueError as error:
            raise ValueError(f"Publication channel {i + 1}: {error}") from None
    return tuple(channels)


def video_values(form) -> list[str]:
    if "video_url" in form or "videos_present" in form:
        return form.getlist("video_url")
    # Accept forms opened before the repeated-field editor was deployed.
    return form.get("videos", "").splitlines()


class VideoInputError(ValueError):
    def __init__(self, errors):
        self.video_errors = errors
        super().__init__(
            " ".join(
                f"Convention video link {i + 1}: {text}" for i, text in errors.items()
            )
        )


def parse_videos(form) -> tuple[str, ...]:
    videos, errors = [], {}
    for index, value in enumerate(video_values(form)):
        if value.strip():
            try:
                videos.append(https_url(value))
            except ValueError as error:
                errors[index] = str(error)
    if errors:
        raise VideoInputError(errors)
    if len(videos) > 10:
        raise ValueError("Provide at most 10 convention video links.")
    return tuple(videos)


def parse_application(
    form, existing: ApplicationInput | None = None
) -> ApplicationInput:
    # Omission in a partial edit preserves a section. Explicit empty controls clear
    # optional data or trigger validation; unchecking every content type is invalid.
    channels = (
        existing.channels
        if existing
        and not any(
            key in form
            for key in ("platform", "account", "primary", "channels_present")
        )
        else parse_channels(form)
    )
    videos = (
        existing.videos
        if existing
        and not any(key in form for key in ("videos", "video_url", "videos_present"))
        else parse_videos(form)
    )
    result = ApplicationInput(
        existing.content_types
        if existing
        and "content_type" not in form
        and "content_types_present" not in form
        else tuple(form.getlist("content_type")),
        channels,
        videos,
    )
    result.validate()
    return result
