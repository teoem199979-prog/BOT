"""Shared code identity policy for extraction, queues, browser and persistence."""
from urllib.parse import urlsplit


CASE_SENSITIVE_CODE_DOMAINS = frozenset({
    "getcodexx8bonus.pages.dev", "tangquaqq88.com", "hi88-freecode.pages.dev",
})
_ALIASES = {
    "xx8": "getcodexx8bonus.pages.dev", "qq88": "tangquaqq88.com",
    "hi88": "hi88-freecode.pages.dev",
}


def normalize_code_domain(domain: str) -> str:
    raw = str(domain or "").strip().lower()
    if not raw:
        return ""
    host = urlsplit(raw if "://" in raw else "//" + raw).hostname or ""
    host = host.removeprefix("www.").rstrip(".")
    return _ALIASES.get(host, host)


def normalize_code(code: str, domain: str = "", *, force_uppercase: bool = False) -> str:
    """Preserve sensitive codes; fold other domains to uppercase.

    Without domain context, preserve case to avoid merging distinct codes.
    force_uppercase=True also folds codes without domain context, but never
    overrides a sensitive site's policy. Character cleaning is separate.
    """
    value = str(code or "").strip()
    canonical = normalize_code_domain(domain)
    if canonical in CASE_SENSITIVE_CODE_DOMAINS:
        return value
    return value.upper() if canonical or force_uppercase else value
