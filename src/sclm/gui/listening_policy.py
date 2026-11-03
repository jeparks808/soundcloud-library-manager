"""HTTPS navigation rules for the official SoundCloud listening page."""
from urllib.parse import urlsplit

HOME = "https://soundcloud.com/"
IDENTITY_HOSTS = {"accounts.google.com", "appleid.apple.com", "facebook.com", "www.facebook.com", "m.facebook.com"}


def valid_url(value: str, *, identity: bool = False) -> bool:
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.username is not None or url.password is not None
                or url.port not in (None, 443) or "\\" in value
                or any(ord(c) <= 32 for c in value)):
            return False
        host = url.hostname or ""
        return (host == "soundcloud.com" or host.endswith(".soundcloud.com")
                or (identity and host in IDENTITY_HOSTS))
    except ValueError:
        return False
