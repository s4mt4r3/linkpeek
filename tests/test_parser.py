from app.parser import parse_metadata

BASE = "https://example.com/blog/post"


def test_open_graph_takes_priority():
    html = """
    <html><head>
      <title>HTML title</title>
      <meta name="description" content="html description">
      <meta name="twitter:title" content="Twitter title">
      <meta property="og:title" content="OG title">
      <meta property="og:description" content="OG description">
      <meta property="og:image" content="https://cdn.example.com/og.png">
      <meta name="twitter:image" content="https://cdn.example.com/tw.png">
      <meta property="og:site_name" content="Example">
    </head></html>
    """
    meta = parse_metadata(html, BASE)
    assert meta.title == "OG title"
    assert meta.description == "OG description"
    assert meta.image == "https://cdn.example.com/og.png"
    assert meta.site_name == "Example"


def test_twitter_card_fallback():
    html = """
    <head>
      <title>HTML title</title>
      <meta name="description" content="html description">
      <meta name="twitter:title" content="Twitter title">
      <meta name="twitter:description" content="Twitter description">
      <meta name="twitter:image" content="/img/card.png">
    </head>
    """
    meta = parse_metadata(html, BASE)
    assert meta.title == "Twitter title"
    assert meta.description == "Twitter description"
    assert meta.image == "https://example.com/img/card.png"


def test_html_title_and_description_fallback():
    html = "<head><title>  Plain\n  title </title><meta name='description' content='Plain description'></head>"
    meta = parse_metadata(html, BASE)
    assert meta.title == "Plain title"
    assert meta.description == "Plain description"
    assert meta.image is None


def test_missing_metadata_gives_nulls():
    meta = parse_metadata("<html><body><p>hi</p></body></html>", BASE)
    assert meta.model_dump() == {
        "title": None,
        "description": None,
        "image": None,
        "site_name": None,
        "favicon": None,
    }


def test_empty_og_content_falls_through():
    html = '<head><meta property="og:title" content="   "><title>Real title</title></head>'
    assert parse_metadata(html, BASE).title == "Real title"


def test_og_tags_written_with_name_attribute():
    html = '<head><meta name="og:title" content="Misattributed OG"></head>'
    assert parse_metadata(html, BASE).title == "Misattributed OG"


def test_relative_urls_resolved():
    html = """
    <head>
      <meta property="og:image" content="../images/hero.jpg">
      <link rel="shortcut icon" href="/favicon.png">
    </head>
    """
    meta = parse_metadata(html, BASE)
    assert meta.image == "https://example.com/images/hero.jpg"
    assert meta.favicon == "https://example.com/favicon.png"


def test_protocol_relative_and_base_href():
    html = """
    <head>
      <base href="https://static.example.org/assets/">
      <meta property="og:image" content="hero.jpg">
      <link rel="icon" href="//icons.example.net/i.ico">
    </head>
    """
    meta = parse_metadata(html, BASE)
    assert meta.image == "https://static.example.org/assets/hero.jpg"
    assert meta.favicon == "https://icons.example.net/i.ico"


def test_favicon_prefers_icon_over_apple_touch_icon():
    html = """
    <head>
      <link rel="apple-touch-icon" href="/apple.png">
      <link rel="icon" type="image/svg+xml" href="/icon.svg">
    </head>
    """
    assert parse_metadata(html, BASE).favicon == "https://example.com/icon.svg"


def test_non_http_image_dropped():
    html = '<head><meta property="og:image" content="javascript:alert(1)"></head>'
    assert parse_metadata(html, BASE).image is None


def test_long_title_truncated():
    html = f"<head><title>{'x' * 5000}</title></head>"
    assert len(parse_metadata(html, BASE).title) == 300
