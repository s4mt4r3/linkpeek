from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.models import Metadata

MAX_TITLE_LEN = 300
MAX_DESCRIPTION_LEN = 1000


def _clean(value: str | None, limit: int | None = None) -> str | None:
    if not value:
        return None
    value = " ".join(value.split())
    if not value:
        return None
    return value[:limit] if limit else value


def _absolute(value: str | None, base_url: str) -> str | None:
    value = _clean(value)
    if not value:
        return None
    resolved = urljoin(base_url, value)
    # Drops javascript:, data: and similar schemes a page might put in og:image.
    if urlsplit(resolved).scheme not in ("http", "https"):
        return None
    return resolved


def _meta_map(tree: HTMLParser) -> dict[str, str]:
    """First non-empty content per meta key, keyed by lowercased property/name.

    OG tags are specified with `property=`, Twitter with `name=`, but plenty of
    sites mix them up, so both attributes are read for every tag.
    """
    found: dict[str, str] = {}
    for node in tree.css("meta"):
        attrs = node.attributes
        content = attrs.get("content")
        if not content or not content.strip():
            continue
        for attr in ("property", "name"):
            key = attrs.get(attr)
            if key:
                found.setdefault(key.strip().lower(), content)
    return found


def _first(meta: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        if meta.get(key, "").strip():
            return meta[key]
    return None


def _favicon(tree: HTMLParser) -> str | None:
    fallback = None
    for node in tree.css("link[rel][href]"):
        rels = (node.attributes.get("rel") or "").lower().split()
        href = node.attributes.get("href")
        if not href or not href.strip():
            continue
        if "icon" in rels:
            return href
        if fallback is None and ("apple-touch-icon" in rels or "apple-touch-icon-precomposed" in rels):
            fallback = href
    return fallback


def parse_metadata(html: str, base_url: str) -> Metadata:
    tree = HTMLParser(html)
    meta = _meta_map(tree)

    base_node = tree.css_first("base[href]")
    if base_node is not None:
        base_url = urljoin(base_url, base_node.attributes.get("href") or "")

    title_node = tree.css_first("title")
    html_title = title_node.text() if title_node is not None else None

    return Metadata(
        title=_clean(_first(meta, "og:title", "twitter:title") or html_title, MAX_TITLE_LEN),
        description=_clean(
            _first(meta, "og:description", "twitter:description", "description"),
            MAX_DESCRIPTION_LEN,
        ),
        image=_absolute(
            _first(meta, "og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"),
            base_url,
        ),
        site_name=_clean(_first(meta, "og:site_name", "application-name"), MAX_TITLE_LEN),
        favicon=_absolute(_favicon(tree), base_url),
    )
