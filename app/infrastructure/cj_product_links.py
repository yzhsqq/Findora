"""CJ product-page identity and provenance, independent of API availability."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

PUBLIC_LINK_STATUSES = frozenset({"observed", "page_verified"})
_HOSTS = frozenset({"cjdropshipping.com", "www.cjdropshipping.com", "eur.cjdropshipping.com"})
_PID = re.compile(r"(?:\d+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", re.I)


def valid_product_url(value: object, pid: str) -> str | None:
    if not isinstance(value, str) or not _PID.fullmatch(pid):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
            return None
        if not re.fullmatch(r"/product/[a-z0-9._%-]+-p-" + re.escape(pid) + r"\.html", parsed.path, re.I):
            return None
    except ValueError:
        return None
    return value


def candidate_product_url(title: str, pid: str) -> str | None:
    """Candidate only: constructing a route does not verify a product page."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:180]
    return valid_product_url(f"https://cjdropshipping.com/product/{slug}-p-{pid}.html", pid) if slug else None


def product_link_fields(row) -> dict:
    # Older frozen snapshots and test fixtures need no schema migration to read.
    if "source_url_status" not in row.keys():
        return {}
    fields = {"source_url_status": row["source_url_status"],
              "source_url_checked_at": row["source_url_checked_at"]}
    unavailable = "detail_status" in row.keys() and str(row["detail_status"] or "").startswith("unavailable:")
    if row["source_url_status"] in PUBLIC_LINK_STATUSES and not unavailable:
        url = valid_product_url(row["source_url"], str(row["pid"]))
        if url:
            fields["source_url"] = url
    return fields
