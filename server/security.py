"""Input validation shared by the API, the ingestion pipeline and the web shell.

Standard library only: this module is shipped inside the PyInstaller desktop
runtime and the Chaquopy APK, so adding a third-party sanitizer would require
synchronising three dependency manifests.
"""

import hmac
import ipaddress
import socket
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 2048
MAX_HTML_LENGTH = 500_000
MAX_BODY_BYTES = 16 * 1024 * 1024

_SPECIAL_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

_ALLOWED_TAGS = frozenset(
    {
        "a", "b", "blockquote", "br", "code", "dd", "del", "div", "dl", "dt",
        "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
        "hr", "i", "img", "li", "ol", "p", "pre", "s", "span", "strong",
        "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
    }
)
_VOID_TAGS = frozenset({"br", "hr", "img"})
_DROP_CONTENT_TAGS = frozenset(
    {
        "script", "style", "iframe", "object", "embed", "svg", "math",
        "template", "base", "link", "meta", "form", "input", "button",
        "textarea", "select", "option", "audio", "video", "source", "track",
        "noscript", "frame", "frameset", "applet", "portal",
    }
)
_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title"}),
}
_MAX_NESTING = 60


class UnsafeUrlError(ValueError):
    """A URL is not a public HTTP(S) address."""


def _hostname_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = hostname[1:-1] if hostname.startswith("[") and hostname.endswith("]") else hostname
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _assert_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, hostname: str
) -> None:
    if not address.is_global:
        raise UnsafeUrlError(f"host {hostname!r} resolves to a non-public address")


def validate_public_url(value: str, *, resolve_dns: bool = False) -> str:
    """Return a normalized URL, or raise ``UnsafeUrlError``.

    Literal private/loopback/link-local addresses are always rejected. When
    ``resolve_dns`` is set, every address a hostname resolves to is checked
    before the request is allowed to leave the process.
    """

    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_URL_LENGTH:
        raise UnsafeUrlError("URL must be between 1 and 2048 characters")
    try:
        parts = urlsplit(cleaned)
    except ValueError as exc:
        raise UnsafeUrlError(f"malformed URL: {exc}") from exc
    if parts.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise UnsafeUrlError("only http and https URLs are supported")
    if parts.username or parts.password:
        raise UnsafeUrlError("URL credentials are not allowed")
    hostname = parts.hostname
    if not hostname:
        raise UnsafeUrlError("URL must include a host")
    if hostname.lower() in _SPECIAL_HOSTS or hostname.lower().endswith(".localhost"):
        raise UnsafeUrlError("local hostnames are not allowed")

    literal = _hostname_ip(hostname)
    if literal is not None:
        _assert_public_address(literal, hostname)
    elif resolve_dns:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        try:
            resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeUrlError(f"could not resolve host {hostname!r}: {exc}") from exc
        addresses = {
            ipaddress.ip_address(entry[4][0]) for entry in resolved if entry[4]
        }
        if not addresses:
            raise UnsafeUrlError(f"host {hostname!r} did not resolve")
        for address in addresses:
            _assert_public_address(address, hostname)

    netloc = parts.netloc
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))


def validate_source_url(value: str) -> str:
    """Validate a URL supplied by a user without performing DNS resolution."""

    return validate_public_url(value)


def safe_http_url(value: object) -> str:
    """Return an http(s) URL, or an empty string for anything else."""

    if not isinstance(value, str):
        return ""
    try:
        return validate_public_url(value)
    except UnsafeUrlError:
        # Internal/private hosts are legitimate article links; only dangerous
        # schemes are stripped here. Private literal hosts are still allowed.
        try:
            parts = urlsplit(value.strip())
        except ValueError:
            return ""
        if parts.scheme.lower() in ALLOWED_URL_SCHEMES and parts.hostname:
            return value.strip()
        # Relative links are common inside articles. They carry no scheme or
        # host, and the client resolves them against the article URL.
        relative = value.strip()
        if not parts.scheme and not parts.netloc and relative.startswith(("/", "#", "./", "../")):
            return relative
        return ""


def sanitize_html(value: object) -> str | None:
    """Rebuild untrusted HTML from an allowlist of tags and attributes.

    Text is always escaped, event handlers and unknown attributes are dropped,
    and whole subtrees such as ``script``/``svg``/``math`` are discarded. Output
    is idempotent, so it is safe to apply again when reading older rows.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    parser = _Sanitizer()
    try:
        parser.feed(value[:MAX_HTML_LENGTH])
        parser.close()
    except (ValueError, AssertionError):
        return escape(value[:MAX_HTML_LENGTH], quote=True)
    return "".join(parser.output) or None


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self._open: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag in _DROP_CONTENT_TAGS:
                self._skip_depth += 1
            return
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag not in _ALLOWED_TAGS or len(self._open) >= _MAX_NESTING:
            return
        rendered = _render_attributes(tag, attrs)
        self.output.append(f"<{tag}{rendered}>")
        if tag not in _VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth or tag not in _ALLOWED_TAGS or tag in _DROP_CONTENT_TAGS:
            return
        self.output.append(f"<{tag}{_render_attributes(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in _DROP_CONTENT_TAGS:
                self._skip_depth -= 1
            return
        if tag not in _ALLOWED_TAGS or tag not in self._open:
            return
        index = len(self._open) - 1 - self._open[::-1].index(tag)
        for closing in reversed(self._open[index:]):
            self.output.append(f"</{closing}>")
        del self._open[index:]

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.output.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self._skip_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._skip_depth:
            self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return None

    def handle_decl(self, decl: str) -> None:
        return None

    def close(self) -> None:
        super().close()
        for tag in reversed(self._open):
            self.output.append(f"</{tag}>")
        self._open.clear()


def _render_attributes(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    allowed = _ALLOWED_ATTRIBUTES.get(tag, frozenset())
    rendered: list[str] = []
    for name, raw in attrs:
        if name.lower() not in allowed or raw is None:
            continue
        value = raw.strip()
        if name.lower() in {"href", "src"}:
            value = safe_http_url(value)
            if not value:
                continue
        rendered.append(f' {name.lower()}="{escape(value, quote=True)}"')
    return "".join(rendered)


def token_matches(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided, expected)


def is_loopback_host(host: str) -> bool:
    """Whether an HTTP ``Host`` header names this machine."""

    value = host.strip().lower()
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.rsplit(":", 1)[0]
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    address = _hostname_ip(value)
    return address is not None and address.is_loopback


def host_header_is_local(host: str) -> bool:
    """Public alias used by the web middleware."""

    return is_loopback_host(host)
