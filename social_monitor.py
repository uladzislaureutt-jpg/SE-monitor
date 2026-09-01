from __future__ import annotations

import csv
import datetime as dt
import html
import json
import logging
import os
import re
import smtplib
import ssl
import statistics
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

LOG = logging.getLogger("social_monitor")
UTC = dt.timezone.utc
MONITOR_BUILD = "2026-08-31.social.74-result-event-integrity-1.0"
ARCHITECTURE_CORE_VERSION = "3.5"

ARTICLE_EXTENSIONS = (".html", ".htm", ".shtml", ".php")
BLOCKED_PATH_PARTS = (
    "/tag/", "/tags/", "/topic/", "/topics/", "/author/", "/authors/",
    "/category/", "/categories/", "/search", "/video/", "/videos/",
    "/photo/", "/photos/", "/podcast/", "/live/", "/about", "/contact",
    "/privacy", "/terms", "/login", "/subscribe",
    "/cdn-cgi/", "email-protection",
)


CONTENT_NOISE_SELECTORS = (
    "script, style, noscript, nav, footer, aside, form, iframe, "
    ".advertisement, .advert, .ads, [class*='advert'], [id*='advert'], "
    "[class*='banner'], [id*='banner'], "
    ".social-share, [class*='social'], [id*='social'], "
    ".related, [class*='related'], [id*='related'], "
    "[class*='recommend'], [id*='recommend'], "
    "[class*='popular'], [id*='popular'], "
    "[class*='most-read'], [id*='most-read'], "
    "[class*='latest'], [id*='latest'], "
    "[class*='sidebar'], [id*='sidebar'], "
    "[class*='teaser'], [id*='teaser'], "
    "[class*='promo'], [id*='promo'], "
    "[class*='widget'], [id*='widget'], "
    "[class*='newsletter'], [id*='newsletter'], "
    "[class*='comment'], [id*='comment'], "
    "[class*='breadcrumb'], [id*='breadcrumb'], "
    "[class*='share'], [id*='share']"
)

# Broad layout classes such as ``sidebar-right`` may be applied to body, main
# or article. Keep those structural ancestors while removing nested widgets.
CONTENT_NOISE_PROTECTED_TAGS = frozenset({"html", "body", "main", "article"})


def remove_content_noise(soup: BeautifulSoup) -> None:
    """Remove auxiliary blocks without deleting the article's root container."""
    for tag in soup.select(CONTENT_NOISE_SELECTORS):
        if str(getattr(tag, "name", "")).lower() in CONTENT_NOISE_PROTECTED_TAGS:
            continue
        tag.decompose()


ARTICLE_CONTENT_SELECTORS = (
    "[itemprop='articleBody']",
    "[data-testid*='article-body']",
    "[class*='article-body']",
    "[class*='article__body']",
    "[class*='article-content']",
    "[class*='article__content']",
    "[class*='story-body']",
    "[class*='story__body']",
    "[class*='post-content']",
    "[class*='entry-content']",
    "[class*='content-body']",
    "[class*='news-text']",
    "[class*='article-text']",
    "main article",
    "article",
    "main",
    "[role='main']",
)


# Curated direct-web profiles for sources that were separately diagnosed.
# They are dormant until a matching source is present in config/sources.csv.
# Keeping them in code lets us test the adapters before changing the live list.
STRATEGIC_SOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "onliner.by": {
        "protected": False,
        # Core 3.3 scans a bounded discovery tail.  Onlíner's homepage also
        # links to forum and marketplace controls which return HTTP 200 but do
        # not contain newsroom article bodies.  Reject those routes before
        # they can consume admission or degraded-recovery capacity.
        "blocked_path_patterns": (
            r"^/(?:viewforum|viewtopic|fleamarketposting)\.php(?:/|$)",
        ),
    },
    "slutsk-gorod.by": {
        "protected": False,
        # Classified advertisements are not editorial publications.  The
        # enlarged discovery tail must not turn them into extraction retries.
        "blocked_path_patterns": (
            r"^/obyavleniya-slutsk(?:/|$)",
        ),
    },
    "vgr.by": {
        "protected": False,
        "transport_order": ("requests", "amp"),
        "amp_suffix": "/amp/",
    },
    "vkurier.by": {
        "protected": True,
        "transport_order": ("requests", "chromium"),
        "prefer_largest_container": True,
    },
    "flagshtok.info": {
        "protected": False,
        # Флагшток отдаёт большие sitemap в порядке «новые сначала». Прямые
        # актуальные карты стабильнее автоматического перебора общих адресов
        # и не требуют увеличивать лимит источника.
        "sitemaps": (
            "https://flagshtok.info/sitemap-news.xml",
            "https://flagshtok.info/sitemap-part-2026.xml",
        ),
        "exact_discovery": True,
        "skip_homepage": False,
        "article_path_patterns": (
            r"^/(?:ru|by)/(?:naviny|regieny)/[^/]+\.html$",
            r"^/by/telegram/post-[a-z0-9-]+\.html$",
        ),
        "transport_order": ("requests",),
    },
    "zerkalo.io": {
        "protected": True,
        "transport_order": ("requests", "feed_metadata"),
        "feed_metadata_fallback": True,
        "feed_metadata_min_chars": 40,
    },
    "bobr.by": {
        "protected": False,
        # BOBR.by exposes directory, jobs, posters, comments, advertisements,
        # and photo facts alongside newsroom links on the homepage.  Only the
        # stable newsroom shape has a verified article body and should consume
        # the source admission budget.
        "article_path_patterns": (r"^/news/[^/]+/\d+/?$",),
        "article_path_allowlist_only": True,
    },
    # Dormant profiles for the regional candidates diagnosed in the separate
    # source-access workflow.  They do not add or enable sources by
    # themselves; they merely ensure that, once a source row is approved, the
    # production URL classifier accepts the site's actual article shape.
    "hoiniki.by": {
        # WordPress posts use ?p=<numeric id>; ?cat and ?page_id are archive
        # and service pages and deliberately remain unclassified.
        "article_query_patterns": (r"(?:^|&)p=\d+(?:&|$)",),
    },
    "klich.by": {
        "article_query_patterns": (r"(?:^|&)p=\d+(?:&|$)",),
    },
    "orshanka.by": {
        "article_query_patterns": (r"(?:^|&)p=\d+(?:&|$)",),
    },
    "pvestnik.by": {
        # This WordPress site exposes month/day archives under /YYYY/MM/DD/.
        # Its publication pages are either a numeric WordPress slug or a
        # transliterated multi-word slug directly below the root.
        "blocked_path_patterns": (
            r"^/20\d{2}/(?:0[1-9]|1[0-2])(?:/|$)",
            r"^/(?:author|category|tag|page)(?:/|$)",
            # Permanent navigation pages can have a long, article-like slug
            # and used to be selected before actual news from the homepage.
            r"^/(?:kak-podpisatsya-2|polatski-vesnik-2|redakciya|istoriya-gazety|"
            r"reklama|kontakty|dokumenty|ssylki)(?:/|$)",
        ),
        "article_path_patterns": (
            r"^/\d+(?:-\d+)?/?$",
            r"^/[a-z0-9]+(?:-[a-z0-9]+){2,}/?$",
        ),
        "article_path_allowlist_only": True,
    },
    "nashkraj.by": {
        # Real stories have three path segments: /news/<rubric>/<slug>/.
        # Rubric pages such as /news/obshchestvo/ are listings, not articles.
        "blocked_path_patterns": (r"^/news/[a-z0-9-]+/?$",),
        "article_path_patterns": (r"^/news/[a-z0-9-]+/[a-z0-9-]+/?$",),
        "article_path_allowlist_only": True,
    },
    "golk.by": {
        # Static tourist/reference routes have a full page of text but are not
        # newsroom publications.  Actual news use one root-level .html slug.
        "blocked_path_patterns": (
            r"^/(?:turizm-i-otdyx|podpiska|reklama|informaciya|vopros-otvet|"
            r"kontakty|o-nas)(?:/|$)",
        ),
        "article_path_patterns": (r"^/[^/]+\.html$",),
        "article_path_allowlist_only": True,
    },
    "ctv.by": {
        # /news/<rubric> is a section landing; an article has its own slug.
        "blocked_path_patterns": (r"^/news/[a-z0-9-]+/?$",),
        "article_path_patterns": (r"^/news/[a-z0-9-]+/[a-z0-9-]+/?$",),
        "article_path_allowlist_only": True,
    },
    "news.by": {
        # The browser-facing /news listing is a JavaScript shell.  Its
        # sitemap exposes the same canonical article shape, while channel
        # schedules and section landings must not consume an article probe.
        "blocked_path_patterns": (
            r"^/(?:televidenie|videogalereya|teleshou|programma-tv)(?:/|$)",
            r"^/news/[a-z0-9-]+/?$",
        ),
        "article_path_patterns": (r"^/news/[a-z0-9-]+/[a-z0-9-]+/?$",),
        "article_path_allowlist_only": True,
    },
    "nashaniva.com": {
        "protected": False,
        # The homepage exposes both the newsroom URL and a separate comments
        # route for the same story.  Comment pages have no article body and
        # must never consume the source admission or degraded-queue budget.
        # Locale prefixes cover the current /ru/ and /be_latn/ variants while
        # keeping the Belarusian canonical /<id> route unchanged.
        "blocked_path_patterns": (
            r"^/(?:[a-z]{2}(?:_[a-z]{4})?/)?\d+/comments(?:/|$)",
        ),
    },
    "charter97.org": {
        "protected": True,
        "feeds": (
            "https://charter97.org/ru/rss/society/",
            "https://charter97.org/ru/rss/economics/",
        ),
        "sitemaps": (),
        "listing_pages": (),
        "exact_discovery": True,
        "skip_homepage": True,
        "article_path_patterns": (
            r"^/ru/news/20\d{2}/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/\d+(?:/|$)",
        ),
        "transport_order": ("requests", "official_mirror"),
        "mirror_domains": ("charter97.link",),
        "extraction_order": (
            "source_specific", "embedded_json", "json_ld", "generic_html",
        ),
    },
    "pozirk.online": {
        "protected": True,
        "feeds": (),
        "sitemaps": (),
        "listing_pages": ("https://pozirk.online/ru/news/",),
        "exact_discovery": True,
        "skip_homepage": True,
        "article_path_patterns": (r"^/ru/news/\d+(?:/|$)",),
        "transport_order": ("requests",),
        "extraction_order": (
            "source_specific", "embedded_json", "json_ld", "generic_html",
        ),
    },
    "ru.belsat.eu": {
        "protected": True,
        "feeds": (
            "https://ru.belsat.eu/rss",
            "https://ru.belsat.eu/shared/feed/google_news.php",
        ),
        "sitemaps": ("https://ru.belsat.eu/sitemap-full_index.xml",),
        "listing_pages": (),
        "exact_discovery": True,
        "skip_homepage": True,
        "article_path_patterns": (r"^/\d{6,}/[^/]+(?:/|$)",),
        "transport_order": ("requests", "chromium"),
        "extraction_order": (
            "source_specific", "embedded_json", "json_ld", "generic_html",
        ),
        "embedded_json": True,
        "prefer_largest_container": True,
        "chromium_threshold": 250,
    },
    "belsat.eu": {
        "protected": True,
        "feeds": (
            "https://ru.belsat.eu/rss",
            "https://ru.belsat.eu/shared/feed/google_news.php",
        ),
        "sitemaps": ("https://ru.belsat.eu/sitemap-full_index.xml",),
        "listing_pages": (),
        "exact_discovery": True,
        "skip_homepage": True,
        "article_path_patterns": (r"^/\d{6,}/[^/]+(?:/|$)",),
        "transport_order": ("requests", "chromium"),
        "extraction_order": (
            "source_specific", "embedded_json", "json_ld", "generic_html",
        ),
        "embedded_json": True,
        "prefer_largest_container": True,
        "chromium_threshold": 250,
    },
    # Dormant strategic profiles. They do not enable a source; they only
    # centralize the access policy if/when the source is present in sources.csv.
    "reform.news": {
        "protected": True,
        "feeds": (),
        "sitemaps": (),
        "listing_pages": (),
        "exact_discovery": False,
        "skip_homepage": False,
        "article_path_patterns": (),
        "transport_order": ("requests", "chromium"),
        "extraction_order": (
            "source_specific", "embedded_json", "json_ld", "generic_html",
        ),
    },
    "gazetaby.com": {
        "protected": True,
        "feeds": (),
        "sitemaps": (),
        "listing_pages": (),
        "exact_discovery": False,
        "skip_homepage": False,
        "article_path_patterns": (),
        "transport_order": ("chromium", "requests"),
        "extraction_order": (
            "source_specific", "embedded_json", "json_ld", "generic_html",
        ),
    },
}

# Compatibility alias for older tests/integration code. New architecture should
# use source_profile_for_domain()/effective_source_profile().
SOURCE_ADAPTER_PROFILES = STRATEGIC_SOURCE_PROFILES



def normalized_domain(value: str) -> str:
    return normalize_space(value).lower().split(":")[0].removeprefix("www.")


def source_domain_key(source: "Source") -> str:
    return normalized_domain(source.domain)


def source_profile_for_domain(
    domain: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the centralized source profile without enabling the source.

    settings may later provide project-local overrides under ``source_profiles``.
    The current repository does not require such overrides, but supporting the
    shape now keeps endpoint/transport policy out of ad-hoc conditionals.
    """
    key = normalized_domain(domain)
    base_profile = dict(STRATEGIC_SOURCE_PROFILES.get(key, {}))
    overrides = {}
    if settings:
        raw = settings.get("source_profiles", {})
        if isinstance(raw, dict):
            candidate = raw.get(key, {})
            if isinstance(candidate, dict):
                overrides = candidate
    profile = {**base_profile, **overrides}
    profile.setdefault("protected", False)
    profile.setdefault("feeds", ())
    profile.setdefault("sitemaps", ())
    profile.setdefault("listing_pages", ())
    profile.setdefault("exact_discovery", False)
    profile.setdefault("skip_homepage", False)
    profile.setdefault("article_path_patterns", ())
    profile.setdefault("article_query_patterns", ())
    profile.setdefault("transport_order", ("requests",))
    profile.setdefault(
        "extraction_order",
        ("source_specific", "embedded_json", "json_ld", "generic_html"),
    )
    profile.setdefault("mirror_domains", ())
    profile.setdefault("embedded_json", False)
    profile.setdefault("prefer_largest_container", False)
    profile.setdefault("chromium_threshold", 250)
    profile.setdefault("amp_suffix", "")
    profile.setdefault("feed_metadata_fallback", False)
    profile.setdefault("feed_metadata_min_chars", 80)
    profile.setdefault("blocked_path_patterns", ())
    profile["domain"] = key
    # Lists are easier to serialize into coverage/debugging output.
    for field in (
        "feeds", "sitemaps", "listing_pages", "article_path_patterns",
        "article_query_patterns",
        "transport_order", "extraction_order", "mirror_domains",
        "blocked_path_patterns",
    ):
        value = profile.get(field, ())
        if isinstance(value, str):
            value = (value,)
        profile[field] = list(value)
    return profile


def effective_source_profile(
    source: "Source",
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the registry profile with explicit source-row endpoints."""
    profile = source_profile_for_domain(source.domain, settings)
    feeds = set(profile.get("feeds", [])) | set(configured_endpoints(source.feed_url))
    sitemaps = set(profile.get("sitemaps", [])) | set(configured_endpoints(source.sitemap_url))
    profile = dict(profile)
    profile["feeds"] = sorted(feeds)
    profile["sitemaps"] = sorted(sitemaps)
    profile["listing_pages"] = list(profile.get("listing_pages", []))
    # Telegram is a real transport, not an HTTP-requests profile.  Linked-site
    # adapters use Telegram for discovery/body metadata and preserve the original
    # publisher URL, so the declarative profile must match the actual path.
    if source.media_type == "telegram" or source.adapter in {"telegram", "telegram_linked_site"}:
        profile["transport_order"] = ["telegram_inline"]
    return profile


def source_adapter_profile(source: "Source") -> dict[str, Any]:
    # Backward-compatible name retained for tested update-24 adapters.
    return source_profile_for_domain(source.domain)


URL_CLASSES = {
    "article", "video", "gallery", "rubric", "archive",
    "service", "external", "unknown",
}


def classify_source_url(
    url: str,
    domain: str,
    profile: dict[str, Any] | None = None,
) -> str:
    """Classify a URL before extraction.

    Classification is deliberately structural. It does not make editorial
    relevance decisions and therefore cannot suppress a socially relevant story
    because of headline wording.
    """
    if not url:
        return "unknown"
    if not same_site(url, domain):
        return "external"

    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.lower()
    profile = profile or source_profile_for_domain(domain)

    for pattern in profile.get("blocked_path_patterns", []):
        if re.search(pattern, path, flags=re.IGNORECASE):
            return "service"

    for pattern in profile.get("article_path_patterns", []):
        if re.search(pattern, path, flags=re.IGNORECASE):
            return "article"

    query = parsed.query
    for pattern in profile.get("article_query_patterns", []):
        if re.search(pattern, query, flags=re.IGNORECASE):
            return "article"

    # Some mixed-purpose portals expose many URL types from the same homepage.
    # A diagnosed allowlist prevents service pages from falling through to the
    # deliberately broad generic article heuristic.  This remains dormant for
    # every source unless its profile opts in explicitly.
    if profile.get("article_path_allowlist_only"):
        return "service"

    if re.search(r"/(?:video|videos|tv|watch)(?:/|$)", path):
        return "video"
    if re.search(r"/(?:photo|photos|gallery|galleries|fotogalereya)(?:/|$)", path):
        return "gallery"
    if re.search(r"/(?:archive|archives|arhiv|arhive)(?:/|$)", path):
        return "archive"
    if re.search(r"/(?:tag|tags|topic|topics|category|categories|rubric|rubrics)(?:/|$)", path):
        return "rubric"
    if any(part in path for part in (
        "/search", "/author/", "/authors/", "/about", "/contact",
        "/privacy", "/terms", "/login", "/subscribe", "/cdn-cgi/",
        "email-protection",
    )):
        return "service"

    return "article" if is_probable_article_url(url, domain) else "unknown"


def is_source_article_url(url: str, source: "Source") -> bool:
    return classify_source_url(
        url,
        source.domain,
        source_adapter_profile(source),
    ) == "article"



# Selectors that must be read before broad global noise cleanup.  Pozirk's
# article wrapper includes the class "height-for-sidebar"; the generic
# [class*='sidebar'] noise rule would otherwise delete the article itself.
SOURCE_PRECLEAN_CONTENT_SELECTORS: dict[str, tuple[str, ...]] = {
    "pozirk.online": (".wrapperEditor", ".single-news__content"),
    "hrodna.life": (".post-content.entry-content",),
    "masheka.by": (".full-text",),
    "vgr.by": (
        ".edgtf-post-text-main", ".amp-wp-article-content",
        ".amp-wp-content",
    ),
    # Both WordPress themes expose a valid article body inside #main.site-main,
    # but its parent has a layout class containing "sidebar".  Pre-cleaning
    # the bounded main container avoids the global sidebar noise rule without
    # retaining the actual sidebar widget.
    "hoiniki.by": ("main#main.site-main", "#main.site-main", ".site-main"),
    "klich.by": ("main#main.site-main", "#main.site-main", ".site-main"),
    # A first-level theme wrapper may carry a sidebar layout class.  Read the
    # actual WordPress article body before global noise removal, just as for
    # the other affected district sites.
    "pvestnik.by": (
        "[itemprop='articleBody']", ".entry-content", ".post-content",
        ".td-post-content",
    ),
    # Result Event Integrity 1.14/1.15: the CONTENT_NOISE_PROTECTED_TAGS
    # root-cause fix (html/body/main/article never wholesale-removed by
    # noise selectors) already handles vkurier.by's real page structure
    # (<body class="...sidebar-right">), so this entry is not strictly load
    # -bearing for the live site today. Kept as defense-in-depth: it also
    # covers article markup shapes the root-cause fix alone does not (e.g.
    # paragraphs directly inside <article> with no nested .entry-content
    # wrapper, or content inside a bare <main> with no <article> at all) —
    # see test_result_event_integrity_23_report_23.py, which specifically
    # regression-tests both of those shapes.
    "vkurier.by": ("article", "main"),
}

SOURCE_CONTENT_SELECTORS: dict[str, tuple[str, ...]] = {
    "bgmedia.site": (
        ".article__content", ".article-content", ".news-text",
        ".post-content", "main article",
    ),
    "ex-press.live": (
        ".article-content", ".entry-content", ".post-content",
        ".news-text", "main article",
    ),
    "vkurier.by": (
        ".article-content", ".entry-content", ".post-content",
        ".td-post-content", "main article",
    ),
    "belnovosti.by": (
        "[itemprop='articleBody']", ".article-body", ".news-text",
        ".field-name-body", "main article",
    ),
    "virtualbrest.ru": (
        ".article-text", ".news-text", ".entry-content", "main article",
    ),
    "brestcity.com": (
        ".entry-content", ".post-content", ".td-post-content", "article",
    ),
    "smartpress.by": (
        ".article-content", ".news-text", ".post-content", "main article",
    ),
    "masheka.by": (
        ".full-text", ".article-text", ".news-text", ".content-text",
        "main article",
    ),
    "vgr.by": (
        ".edgtf-post-text-main", ".amp-wp-article-content",
        ".amp-wp-content", "main article",
    ),
    "zerkalo.io": (
        "[itemprop='articleBody']", "article[itemprop='articleBody']",
        "main article",
    ),
    "belsat.eu": (
        "[data-testid*='article']", ".article-content", ".article__content",
        ".post-content", ".entry-content", "main article",
    ),
    "ru.belsat.eu": (
        "[data-testid*='article']", ".article-content", ".article__content",
        ".post-content", ".entry-content", "main article",
    ),
    "reform.news": (
        ".entry-content", ".post-content", ".td-post-content",
        "[itemprop='articleBody']", "main article",
    ),
    "pozirk.online": (
        "[itemprop='articleBody']", ".article-content", ".news-content",
        ".post-content", "main article",
    ),
    "gazetaby.com": (
        ".post-content", ".entry-content", ".article-content",
        ".text", "main article",
    ),
    "charter97.org": (
        ".article_text", ".article-text", ".news_text", ".news-text",
        "[itemprop='articleBody']", "main article",
    ),
    "nashkraj.by": ("div.contentCol",),
    "hoiniki.by": (".entry-content", "main#main.site-main", ".site-main"),
    "klich.by": (".entry-content", "main#main.site-main", ".site-main"),
    "pvestnik.by": (
        "[itemprop='articleBody']", ".entry-content", ".post-content",
        ".td-post-content", "main article",
    ),
}

NOISE_TEXT_PREFIXES = (
    "читайте также", "также читайте", "рекомендуем", "популярное",
    "последние новости", "подробнее", "смотрите также",
    "read also", "also read", "related", "recommended", "most read",
    "latest news", "see also",
    "zobacz", "czytaj także", "sprawdź także",
    "читайте також", "також читайте", "рекомендуємо", "останні новини",
)


@dataclass(frozen=True)
class Source:
    enabled: bool
    country: str  # В этом проекте поле хранит регион Беларуси.
    country_code: str
    locality: str
    rank: int
    priority: str
    name: str
    media_type: str
    domain: str
    start_url: str
    language: str
    adapter: str = "standard"
    query_hint: str = ""
    collection_hint: str = ""
    access: str = ""
    complexity: str = ""
    feed_url: str = ""
    sitemap_url: str = ""
    telegram_url: str = ""


@dataclass
class Candidate:
    source: Source
    url: str
    title: str = ""
    summary: str = ""
    published_at: str = ""
    discovered_via: str = ""
    inline_text: str = ""
    title_generated: bool = False


@dataclass(frozen=True)
class SourceCollectionMetrics:
    feed_candidates: int = 0
    sitemap_candidates: int = 0
    listing_candidates: int = 0
    homepage_candidates: int = 0
    telegram_candidates: int = 0
    merged_candidates: int = 0
    selected_candidates: int = 0
    selected_feed: int = 0
    selected_sitemap: int = 0
    selected_listing: int = 0
    selected_homepage: int = 0
    selected_telegram: int = 0
    selected_fresh: int = 0
    selected_current: int = 0
    selected_soft: int = 0
    soft_tail_budget: int = 0
    clipped_soft: int = 0
    telegram_site_duplicates: int = 0
    source_limit: int = 0
    source_limit_hit: bool = False
    soft_limit_ceiling: int = 0
    selected_overflow: int = 0
    selected_protected_title: int = 0
    clipped_candidates: int = 0
    endpoint_total: int = 0
    endpoint_ok: int = 0
    endpoint_failed: int = 0
    endpoint_degraded: int = 0
    endpoint_circuit_skipped: int = 0
    endpoint_tail_probes: int = 0
    discovery_seconds: float = 0.0
    endpoint_discovery_seconds: float = 0.0
    endpoint_http_seconds: float = 0.0
    feed_limit_hit: bool = False
    sitemap_limit_hit: bool = False
    listing_limit_hit: bool = False
    homepage_limit_hit: bool = False
    telegram_limit_hit: bool = False
    clipped_fresh: int = 0
    clipped_unseen: int = 0
    clipped_undated: int = 0
    clipped_prefilter_strong: int = 0
    clipped_prefilter_possible: int = 0
    clipped_prefilter_needs_text: int = 0
    clipped_protected_title: int = 0
    clipped_feed: int = 0
    clipped_sitemap: int = 0
    clipped_listing: int = 0
    clipped_homepage: int = 0
    clipped_telegram: int = 0
    endpoint_observations: tuple["EndpointTelemetry", ...] = ()


@dataclass(frozen=True)
class MetadataPrefilterDecision:
    status: str
    reason: str = ""
    title_signal: bool = False


@dataclass(frozen=True)
class CandidateAdmissionDecision:
    status: str
    reason: str = ""
    effective_date: dt.datetime | None = None
    service_like: bool = False


@dataclass(frozen=True)
class CandidateRankingContract:
    """Normalized diagnostic contract used before candidate clipping."""

    candidate: Candidate
    canonical_url: str
    admission_status: str
    prefilter_status: str
    protected_title_admission: bool
    ranking_tier: str
    ranking_level: int
    source_priority: str
    effective_date: dt.datetime | None
    discovery_channels: tuple[str, ...]
    integrity_flags: tuple[str, ...]


@dataclass(frozen=True)
class EventFingerprint:
    region: str = ""
    locality: str = ""
    object_key: str = ""
    object_label: str = ""
    problem_key: str = ""
    problem_label: str = ""
    signature: str = ""


@dataclass
class CandidateProcessingTelemetry:
    url_class: str = "unknown"
    prefilter_status: str = "needs_text"
    transport: str = ""
    transport_status: str = ""
    extraction_strategy: str = ""
    text_length: int = 0
    html_length: int = 0
    metadata_only: bool = False
    extraction_failed: bool = False
    relevance_passed: bool = False
    publication_allowed: bool = False
    excerpt_built: bool = False
    recovery_retry: bool = False
    recovery_recovered: bool = False
    degraded_reason: str = ""
    rejection_reason: str = ""
    transport_circuit_skipped: bool = False
    event_region: str = ""
    event_locality: str = ""
    event_object: str = ""
    event_problem: str = ""
    event_signature: str = ""
    event_published_at: str = ""
    event_echo: bool = False
    event_echo_anchor: str = ""
    event_echo_sources: tuple[str, ...] = ()
    event_echo_priority: bool = False
    final_stage: str = "not_processed"
    processing_seconds: float = 0.0
    http_seconds: float = 0.0
    extraction_seconds: float = 0.0
    chromium_seconds: float = 0.0
    chromium_attempts: int = 0
    http_attempts: int = 0
    transport_status_code: int = 0
    transport_failure_class: str = ""
    http_observations: tuple["HttpObservation", ...] = ()


@dataclass
class SourceProcessingMetrics:
    processed: int = 0
    prefilter_strong: int = 0
    prefilter_possible: int = 0
    prefilter_needs_text: int = 0
    fetch_ok: int = 0
    fetch_failed: int = 0
    extraction_full: int = 0
    extraction_metadata_only: int = 0
    extraction_failed: int = 0
    relevance_passed: int = 0
    relevance_rejected: int = 0
    date_rejected: int = 0
    excerpt_empty: int = 0
    included: int = 0
    degraded_queued: int = 0
    recovery_retried: int = 0
    recovery_recovered: int = 0
    event_geo_resolved: int = 0
    event_signature_ready: int = 0
    event_echo_hits: int = 0
    event_echo_current: int = 0
    event_echo_state: int = 0
    event_echo_degraded_prioritized: int = 0
    event_regions: set[str] = field(default_factory=set)
    event_localities: set[str] = field(default_factory=set)
    transport_circuit_skipped: int = 0
    transport_requests: int = 0
    transport_official_mirror: int = 0
    transport_chromium: int = 0
    transport_telegram_inline: int = 0
    transport_amp: int = 0
    transport_feed_metadata: int = 0
    extraction_source_specific: int = 0
    extraction_embedded_json: int = 0
    extraction_json_ld: int = 0
    extraction_generic_html: int = 0
    extraction_metadata_description: int = 0
    extraction_feed_summary: int = 0
    processing_seconds: float = 0.0
    processing_max_seconds: float = 0.0
    http_seconds: float = 0.0
    extraction_seconds: float = 0.0
    chromium_seconds: float = 0.0
    chromium_attempts: int = 0
    http_attempts: int = 0

    def add(self, trace: CandidateProcessingTelemetry) -> None:
        self.processed += 1
        self.processing_seconds += max(0.0, trace.processing_seconds)
        self.processing_max_seconds = max(
            self.processing_max_seconds, max(0.0, trace.processing_seconds)
        )
        self.http_seconds += max(0.0, trace.http_seconds)
        self.extraction_seconds += max(0.0, trace.extraction_seconds)
        self.chromium_seconds += max(0.0, trace.chromium_seconds)
        self.chromium_attempts += max(0, trace.chromium_attempts)
        self.http_attempts += max(0, trace.http_attempts)
        status_field = {
            "strong": "prefilter_strong",
            "possible": "prefilter_possible",
            "needs_text": "prefilter_needs_text",
        }.get(trace.prefilter_status, "prefilter_needs_text")
        setattr(self, status_field, getattr(self, status_field) + 1)

        if trace.transport_status == "ok":
            self.fetch_ok += 1
        elif trace.transport_status == "failed":
            self.fetch_failed += 1

        if trace.metadata_only:
            self.extraction_metadata_only += 1
        elif trace.extraction_failed:
            self.extraction_failed += 1
        elif trace.text_length > 0:
            self.extraction_full += 1

        if trace.relevance_passed:
            self.relevance_passed += 1
        if trace.recovery_retry:
            self.recovery_retried += 1
        if trace.recovery_recovered:
            self.recovery_recovered += 1
        if trace.event_region or trace.event_locality:
            self.event_geo_resolved += 1
        if trace.event_signature:
            self.event_signature_ready += 1
        if trace.event_echo:
            self.event_echo_hits += 1
            if trace.event_echo_anchor == "current":
                self.event_echo_current += 1
            elif trace.event_echo_anchor == "state":
                self.event_echo_state += 1
        if trace.event_echo_priority:
            self.event_echo_degraded_prioritized += 1
        if trace.event_region:
            self.event_regions.add(trace.event_region)
        if trace.event_locality:
            self.event_localities.add(trace.event_locality)
        if trace.transport_circuit_skipped:
            self.transport_circuit_skipped += 1
        if trace.final_stage == "relevance_rejected":
            self.relevance_rejected += 1
        elif trace.final_stage == "date_rejected":
            self.date_rejected += 1
        elif trace.final_stage == "excerpt_empty":
            self.excerpt_empty += 1
        elif trace.final_stage == "included":
            self.included += 1
        elif trace.final_stage == "degraded_queued":
            self.degraded_queued += 1

        transport_field = {
            "requests": "transport_requests",
            "official_mirror": "transport_official_mirror",
            "chromium": "transport_chromium",
            "telegram_inline": "transport_telegram_inline",
            "amp": "transport_amp",
            "feed_metadata": "transport_feed_metadata",
        }.get(trace.transport)
        if transport_field:
            setattr(self, transport_field, getattr(self, transport_field) + 1)

        extraction_field = {
            "source_specific": "extraction_source_specific",
            "embedded_json": "extraction_embedded_json",
            "json_ld": "extraction_json_ld",
            "generic_html": "extraction_generic_html",
            "metadata_description": "extraction_metadata_description",
            "feed_summary": "extraction_feed_summary",
        }.get(trace.extraction_strategy)
        if extraction_field:
            setattr(self, extraction_field, getattr(self, extraction_field) + 1)


@dataclass
class ArticleExtraction:
    title: str
    text: str
    html_length: int = 0
    published_at: str = ""
    date_source: str = ""
    metadata_summary: str = ""
    extraction_strategy: str = ""
    transport: str = ""
    transport_status: str = ""
    transport_status_code: int = 0
    transport_failure_class: str = ""
    transport_circuit_skipped: bool = False
    failure_stage: str = ""
    http_seconds: float = 0.0
    extraction_seconds: float = 0.0
    chromium_seconds: float = 0.0
    chromium_attempts: int = 0
    http_attempts: int = 0
    http_observations: tuple["HttpObservation", ...] = ()

    def __iter__(self):
        # Сохраняет совместимость со старым кодом: title, text = extract_article(...)
        yield self.title
        yield self.text


@dataclass
class ArticleResult:
    source_name: str
    source_type: str
    country: str  # Регион.
    locality: str
    priority: str
    source_language: str
    title: str
    title_generated: bool
    url: str
    published_at: str
    category: str
    subcategory: str
    excerpt: str
    signal_type: str
    official_response: bool
    score: int
    matched_terms: str
    discovered_via: str
    text_length: int
    event_region: str = ""
    event_locality: str = ""
    event_object: str = ""
    event_problem: str = ""
    event_signature: str = ""
    event_echo: bool = False
    event_echo_anchor: str = ""
    event_echo_sources: str = ""
    related_coverage: tuple[tuple[str, str], ...] = ()


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def local_now(settings: dict[str, Any]) -> dt.datetime:
    timezone_name = str(settings.get("monitor", {}).get("timezone", "UTC"))
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        LOG.warning("Неизвестный часовой пояс %s; используется UTC.", timezone_name)
        timezone = UTC
    return utc_now().astimezone(timezone)


def parse_datetime(value: Any) -> dt.datetime | None:
    """Parse publication dates and normalize them to UTC.

    Belarusian regional sites frequently publish a local clock time without an
    explicit offset. Treating such values as UTC shifts them three hours into
    the future. Since this monitor contains only Belarusian sources, naive
    timestamps are interpreted as Europe/Minsk.
    """
    if not value:
        return None
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Minsk"))
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        return None


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_search_text(value: str) -> str:
    return normalize_space(value).casefold()


# Conservative Belarus event geography.  This is deliberately a compact,
# high-confidence gazetteer rather than a guesser: an unknown village simply
# remains unresolved instead of inheriting the newsroom's source locality.
EVENT_REGION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Брестская область", (
        r"брестск(?:ая|ой|ую|ою)\s+област", r"брэсцк(?:ая|ай|ую|аю)\s+вобласц",
    )),
    ("Витебская область", (
        r"витебск(?:ая|ой|ую|ою)\s+област", r"віцебск(?:ая|ай|ую|аю)\s+вобласц",
    )),
    ("Гомельская область", (
        r"гомельск(?:ая|ой|ую|ою)\s+област", r"гомельск(?:ая|ай|ую|аю)\s+вобласц",
    )),
    ("Гродненская область", (
        r"гродненск(?:ая|ой|ую|ою)\s+област", r"гродзенск(?:ая|ай|ую|аю)\s+вобласц",
    )),
    ("Минская область", (
        r"минск(?:ая|ой|ую|ою)\s+област", r"мінск(?:ая|ай|ую|аю)\s+вобласц",
    )),
    ("Могилевская область", (
        r"могил[её]вск(?:ая|ой|ую|ою)\s+област", r"магіл[ёе]ўск(?:ая|ай|ую|аю)\s+вобласц",
    )),
)


EVENT_LOCALITY_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Минск", "Минск", (
        r"(?<![а-яёіўa-z])минск(?:е|а|у|ом)?(?![а-яёіўa-z])",
        r"(?<![а-яёіўa-z])минск(?:их|ие|ий|ого|ому|ую)(?![а-яёіўa-z])",
        r"(?<![а-яёіўa-z])мінск(?:у|а|ам|е)?(?![а-яёіўa-z])",
        r"(?<![а-яёіўa-z])мінск(?:іх|ія|і|ага|аму|ую)(?![а-яёіўa-z])",
    )),
    ("Брест", "Брестская область", (r"(?<![а-яёіўa-z])брест(?:е|а|у|ом)?(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])брэст(?:е|а|у|ам)?(?![а-яёіўa-z])")),
    ("Барановичи", "Брестская область", (r"баранович(?:и|ах|ей|ам|ами)?", r"баранавіч(?:ы|ах|аў|ам|амі)?")),
    ("Пинск", "Брестская область", (r"(?<![а-яёіўa-z])пинск(?:е|а|у|ом)?(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])пінск(?:у|а|ам|е)?(?![а-яёіўa-z])")),
    ("Кобрин", "Брестская область", (r"кобрин(?:е|а|у|ом)?", r"кобрын(?:е|а|у|ам)?")),
    ("Береза", "Брестская область", (r"(?<![а-яёіўa-z])бер[её]з(?:а|е|ы|у|ой)(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])бяроз(?:а|е|ы|у|ай)(?![а-яёіўa-z])")),
    ("Ивацевичи", "Брестская область", (r"ивацевич(?:и|ах|ей|ам|ами)?", r"івацэвіч(?:ы|ах|аў|ам|амі)?")),
    ("Лунинец", "Брестская область", (r"лунинц(?:е|а|у|ом)|лунинец", r"лунінц(?:е|а|у|ам)|лунінец")),
    ("Пружаны", "Брестская область", (r"пружан(?:ы|ах|ам|ами)?",)),
    ("Столин", "Брестская область", (r"столин(?:е|а|у|ом)?", r"столін(?:е|а|у|ам)?")),

    ("Витебск", "Витебская область", (r"витебск(?:е|а|у|ом)?", r"віцебск(?:у|а|ам|е)?")),
    ("Орша", "Витебская область", (r"(?<![а-яёіўa-z])орш(?:а|е|и|у|ой)(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])орш(?:а|ы|у|ай)(?![а-яёіўa-z])")),
    ("Полоцк", "Витебская область", (r"полоцк(?:е|а|у|ом)?", r"полацк(?:у|а|ам|е)?")),
    ("Новополоцк", "Витебская область", (r"новополоцк(?:е|а|у|ом)?", r"наваполацк(?:у|а|ам|е)?")),
    ("Глубокое", "Витебская область", (r"глубок(?:ое|ом|ого|ому)", r"глыбок(?:ае|ім|ага|аму)")),
    ("Поставы", "Витебская область", (r"постав(?:ы|ах|ам|ами)?", r"пастав(?:ы|ах|ам|амі)?")),
    ("Лепель", "Витебская область", (r"лепел(?:ь|е|я|ю|ем)?",)),

    ("Гомель", "Гомельская область", (r"гомел(?:ь|е|я|ю|ем)?",)),
    ("Мозырь", "Гомельская область", (r"мозыр(?:ь|е|я|ю|ем)?", r"мазыр(?:ы|ыі|у|ом)?")),
    ("Жлобин", "Гомельская область", (r"жлобин(?:е|а|у|ом)?", r"жлобін(?:е|а|у|ам)?")),
    ("Речица", "Гомельская область", (r"речиц(?:а|е|ы|у|ей)", r"рэчыц(?:а|е|ы|у|ай)")),
    ("Светлогорск", "Гомельская область", (r"светлогорск(?:е|а|у|ом)?", r"светлагорск(?:у|а|ам|е)?")),
    ("Рогачев", "Гомельская область", (r"рогач[её]в(?:е|а|у|ом)?", r"рагач[оё]ў(?:е|а|у|ам)?")),
    ("Калинковичи", "Гомельская область", (r"калинкович(?:и|ах|ей|ам|ами)?", r"калінкавіч(?:ы|ах|аў|ам|амі)?")),
    ("Лельчицы", "Гомельская область", (r"лельчиц(?:ы|ах|ам|ами)?", r"лельчыц(?:ы|ах|ам|амі)?")),

    ("Гродно", "Гродненская область", (r"(?<![а-яёіўa-z])гродн(?:о|е|а|у)(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])гродн(?:а|е|у)(?![а-яёіўa-z])")),
    ("Лида", "Гродненская область", (r"(?<![а-яёіўa-z])лид(?:а|е|ы|у|ой)(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])лід(?:а|зе|ы|у|ай)(?![а-яёіўa-z])")),
    ("Слоним", "Гродненская область", (r"слоним(?:е|а|у|ом)?", r"слонім(?:е|а|у|ам)?")),
    ("Волковыск", "Гродненская область", (r"волковыск(?:е|а|у|ом)?", r"ваўкавыск(?:у|а|ам|е)?")),
    ("Новогрудок", "Гродненская область", (r"новогрудк(?:е|а|у|ом)|новогрудок", r"навагрудк(?:у|а|ам|е)|навагрудак")),
    ("Сморгонь", "Гродненская область", (r"сморгон(?:ь|и|я|ю|ью)?", r"смаргон(?:ь|і|ї|ю|ню)?")),

    ("Борисов", "Минская область", (r"борисов(?:е|а|у|ом)?", r"барысаў|барысав(?:е|а|у|ам)?")),
    ("Молодечно", "Минская область", (r"молодечн(?:о|е|а|у)", r"маладзечн(?:а|е|а|у)")),
    ("Солигорск", "Минская область", (r"солигорск(?:е|а|у|ом)?", r"салігорск(?:у|а|ам|е)?")),
    ("Слуцк", "Минская область", (r"слуцк(?:е|а|у|ом)?",)),
    ("Жодино", "Минская область", (r"жодин(?:о|е|а|у)", r"жодзін(?:а|е|а|у)")),
    ("Дзержинск", "Минская область", (r"дзержинск(?:е|а|у|ом)?", r"дзяржынск(?:у|а|ам|е)?")),
    ("Несвиж", "Минская область", (r"несвиж(?:е|а|у|ем)?", r"нясвіж(?:ы|а|у|ам)?")),
    ("Вилейка", "Минская область", (r"вилейк(?:а|е|и|у|ой)", r"вілейк(?:а|е|і|у|ай)")),
    ("Марьина Горка", "Минская область", (r"марьин(?:а|ой|у)\s+горк(?:а|е|и|у|ой)", r"мар'ін(?:а|ай|у)\s+горк(?:а|е|і|у|ай)")),
    ("Раков", "Минская область", (r"(?<![а-яёіўa-z])раков(?:е|а|у|ом)?(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])ракаў|ракав(?:е|а|у|ам)(?![а-яёіўa-z])")),

    ("Могилев", "Могилевская область", (r"могил[её]в(?:е|а|у|ом)?", r"магіл[ёе]ў|магіл[её]в(?:е|а|у|ам)?")),
    ("Бобруйск", "Могилевская область", (r"бобруйск(?:е|а|у|ом)?", r"бабруйск(?:у|а|ам|е)?")),
    ("Осиповичи", "Могилевская область", (r"осипович(?:и|ах|ей|ам|ами)?", r"асіповіч(?:ы|ах|аў|ам|амі)?")),
    ("Кричев", "Могилевская область", (r"крич[её]в(?:е|а|у|ом)?", r"крычаў|крычав(?:е|а|у|ам)?")),
    ("Горки", "Могилевская область", (r"(?<![а-яёіўa-z])горк(?:и|ах|ам|ами)(?![а-яёіўa-z])", r"(?<![а-яёіўa-z])горк(?:і|ах|ам|амі)(?![а-яёіўa-z])")),
    ("Шклов", "Могилевская область", (r"шклов(?:е|а|у|ом)?", r"шклоў|шклов(?:е|а|у|ам)?")),
)


EVENT_OBJECT_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # Must precede ``greenery``: in reports about rolling stock the word
    # «парк» denotes a fleet, not an urban park.
    ("rail_platform_wagons", "вагоны-платформы БЖД", (
        r"вагон[а-яёіў]*.{0,45}платформ",
        r"платформ[а-яёіў]*.{0,45}вагон",
        r"бжд.{0,80}(?:дефицит|нехват|вагон)",
    )),
    # A local school's published appearance restrictions may generate a
    # substantive public dispute.  It is kept as a named event so a short
    # Telegram retelling and a fuller article do not become duplicate cards.
    ("school_appearance_rules", "требования к внешнему виду школьников", (
        r"школ[а-яёіў]*.{0,90}(?:внешн[а-яёіў]*\s+вид|прическ|волос|макияж)",
        r"(?:внешн[а-яёіў]*\s+вид|прическ|волос|макияж).{0,90}"
        r"(?:ученик|школьник|школ)",
    )),
    ("food_product", "пищевая продукция", (
        r"пирож", r"десерт", r"кондитер", r"пищев.*продукц",
        r"харчов.*прадукц",
    )),
    ("communal_billing", "коммунальные начисления", (
        r"общежит[а-яёіў]*.*(?:вод|плат|жиров|счет|сч[её]т)",
        r"(?:утеч|протеч)[а-яёіў]*.*вод[а-яёіў]*",
        r"коммунальн[а-яёіў]*.*(?:начисл|списал|счет|сч[её]т|плат)",
    )),
    ("parking", "парковка/машино-места", (r"парков", r"паркінг", r"машино-мест", r"машынамесц")),
    ("road", "дорога/улица", (
        r"(?<![а-яёіўa-z0-9])дорог(?:а|и|у|е|ой|ою|ам|ами|ах)?(?![а-яёіўa-z0-9])",
        r"(?<![а-яёіўa-z0-9])дарог(?:а|і|у|е|ай|аю|ам|амі|ах)?(?![а-яёіўa-z0-9])",
        r"(?<![а-яёіўa-z0-9])улиц(?:а|ы|у|е|ей|ам|ами|ах)?(?![а-яёіўa-z0-9])",
        r"(?<![а-яёіўa-z0-9])вуліц(?:а|ы|у|е|ай|аю|ам|амі|ах)?(?![а-яёіўa-z0-9])",
        r"асфальт", r"тротуар", r"тратуар", r"разметк", r"размец",
    )),
    ("station_storage", "камеры хранения на вокзале", (r"камер.*хранен", r"камера.*хранен", r"ячейк.*хранен", r"ячэйк.*захоў")),
    # "остановк" was bare and matched "приостановка" (temporary suspension
    # of a licence/enterprise/operations — a different domain entirely).
    # Anchored to word-start; "прыпынк" (be) is a distinct root with no
    # equivalent collision so left as-is.
    ("public_transport", "общественный транспорт", (r"автобус", r"аўтобус", r"маршрут", r"(?<![а-яёіўa-z])остановк", r"прыпынк", r"вокзал", r"поезд", r"цягнік")),
    ("water_supply", "водоснабжение", (r"водоснаб", r"водопровод", r"питьев.*вод", r"пітн.*вад", r"горяч.*вод", r"гарач.*вад")),
    ("natural_water", "река/озеро/берег", (
        r"(?<![а-яёіўa-z])рек(?:а|е|и|у|ой|ах|ами)(?![а-яёіўa-z])",
        r"(?<![а-яёіўa-z])рак(?:а|і|у|ой|ах|амі)(?![а-яёіўa-z])",
        r"озер", r"возер", r"водо[её]м", r"вада[её]м", r"пруд",
        r"берег", r"бераг", r"днепр", r"дняпро", r"припят", r"прыпяц",
        r"неман", r"нёман", r"западн[а-яёіў]*\s+двин", r"заходн[а-яёіў]*\s+дзвін",
        r"сож", r"буг",
    )),
    # "санитар" was previously bare and matched any word with that root
    # (e.g. "санитарно-эпидемиологическая служба", "санитарные нормы"),
    # mistagging unrelated billing/procurement stories as waste. Narrowed
    # to the antisanitary-condition sense and explicit "sanitary condition"
    # phrasing, in both languages. See report-29: a laundry-pricing story
    # was mistagged "waste" purely because it mentioned a sanitary-
    # epidemiological inspection service.
    ("waste", "мусор/санитарное состояние", (
        r"мусор", r"смец", r"свалк", r"звалк",
        r"антисанитар", r"антысанітар",
        r"санитарн[а-яёіў]*\s+состояни", r"санітарн[а-яёіў]*\s+стан",
    )),
    # "трав(?!м)" (first patch, update75) excluded "травма" but missed the
    # more common false-friend shape: "трав" preceded by a prefix letter, as
    # in "отравились/отравление" (poisoned) and "затравили/натравить"
    # (hounded/incited) — confirmed live in run 30, where a food-poisoning
    # story ("400 человек отравились...") was mistagged event_object=
    # "greenery" purely because "трав" is a substring of "отравились". A
    # negative lookahead alone cannot catch this because the problem is on
    # the LEFT side of the match, not the right. Anchored with the same
    # word-start idiom already used for "парк" below, so it only fires when
    # "трав" begins a word (трава/травы/травяной/травинка), not when it's
    # glued onto a prefix (о-, за-, на-, вы-, из-...).
    #
    # "косил" needed the mirror-image fix: bare it matched "закосил" (draft-
    # dodging slang) and "перекосил-" (skewed/warped, unrelated). But unlike
    # "трав", it has legitimate PREFIXED forms that must keep matching —
    # "покосил/скосил/выкосил" are still perfective forms of mowing. So
    # instead of a blanket word-start anchor, only "по/вы/с" are allowed
    # immediately before "косил"; any other prefix (за-, пере-...) is
    # excluded via the same word-start idiom applied to the whole group.
    #
    # "дерев" was bare and matched "одеревенела" (went numb/stiff with
    # shock, a common figurative expression unrelated to trees).
    (
        "greenery",
        "озеленение/покос",
        (
            r"(?<![а-яёіўa-z])трав(?!м)[а-яёіў]*",
            r"покос",
            r"(?<![а-яёіўa-z])(?:по|вы|с)?косил",
            r"(?<![а-яёіўa-z])дерев",
            r"дрэў",
            r"(?<![а-яёіўa-z])парк(?:е|а|у|ом|и|ах)?(?![а-яёіўa-z])",
            r"сквер",
        ),
    ),
    ("lighting", "уличное освещение", (r"освещ", r"асвятл", r"фонар")),
    # "двор" was bare and matched "дворец/дворцовый" (palace) — thematically
    # a different kind of building. Excludes that continuation while still
    # matching "двор/двора/дворе/дворик/дворовый" etc.
    ("housing", "жилой дом/двор", (r"жкх", r"коммунальн.*(?:плат|услуг|служб)", r"камунальн.*(?:плац|паслуг|служб)", r"подъезд", r"пад'езд", r"подвал", r"падвал", r"лифт", r"ліфт", r"крыша", r"дах", r"двор(?!ец|ц)", r"двар")),
    ("healthcare", "медицина", (r"поликлин", r"паліклін", r"больниц", r"бальніц", r"врач", r"урач", r"медицин", r"медыцын")),
    ("retail", "магазин/торговля", (r"магазин", r"крама", r"торгов", r"гандл")),
    ("consumer_goods", "непродовольственные потребительские товары", (
        r"обув[а-яёіў]*", r"абут[а-яёіў]*", r"одежд[а-яёіў]*",
        r"вопратк[а-яёіў]*", r"потребительск[а-яёіў]*\s+товар",
    )),
    ("telecom", "связь/интернет", (r"интернет", r"інтэрнэт", r"мобильн.*связ", r"мабільн.*сувяз", r"телефонн.*связ", r"тэлефонн.*сувяз", r"iptv", r"телевид", r"тэлебач")),
    ("labor", "труд/оплата труда", (r"зарплат", r"заработн.*плат", r"работник", r"працаўнік", r"работодател", r"наймальнік", r"услови.*труд", r"ўмов.*прац")),
    ("education", "школа/детский сад", (r"школ", r"школ", r"детск.*сад", r"дзіцяч.*сад", r"садик")),
    ("animals", "содержание животных", (
        r"животн", r"жыв[её]л", r"собак", r"сабак", r"кошк", r"катоў",
        r"приют", r"прытул", r"пункт.*содерж",
        r"рыб[а-яёіў]*.*(?:ламп|аквари|вод)",
    )),
    ("memorial", "кладбище/мемориал", (r"кладбищ", r"могілк", r"мемориал", r"мемарыял", r"памятник", r"помнік")),
)


EVENT_PROBLEM_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("public_resonance", "общественный резонанс", (
        r"(?:активн[а-яёіў]*\s+)?обсуждени", r"возмущ", r"массов[а-яёіў]*\s+критик",
    )),
    ("contamination", "микробиологическое загрязнение", (
        r"кишечн[а-яёіў]*\s+палоч", r"стафилокок", r"s\.?\s*aureus",
        r"колиформ", r"микробиологическ[а-яёіў]*\s+наруш",
    )),
    ("billing_overcharge", "ошибочные начисления/возврат", (
        r"платил[а-яёіў]*.*за.*(?:утеч|протеч)",
        r"списал[а-яёіў]*.*на\s+жильц", r"лишн[а-яёіў]*\s+сумм[а-яёіў]*.*жиров",
        r"вернул[а-яёіў]*.*(?:рубл|жильц|проживающ)",
    )),
    ("bullying", "травля/насилие в образовательной среде", (
        r"буллинг", r"травл[а-яёіў]*", r"избивал[а-яёіў]*\s+толп",
        r"оскорбля[а-яёіў]*.*сверстник",
    )),
    ("hiring_shortage", "невозможность найма работников", (
        r"не\s+(?:может|могут).*найти.*(?:продав|работник|сотрудник)",
        r"ищем.*(?:человек|работник|продав).*никто\s+не\s+хочет",
        r"не\s+может\s+найти\s+продавц",
    )),
    ("animal_welfare", "ненадлежащие условия содержания животных", (
        r"неправильн[а-яёіў]*\s+услови[а-яёіў]*", r"перегрев[а-яёіў]*\s+от\s+ламп",
        r"нулев[а-яёіў]*\s+фильтрац", r"не\s+хватает\s+вод[а-яёіў]*",
    )),
    ("low_water", "критическое обмеление/низкий уровень воды", (
        r"критическ[а-яёіў]*\s+обмел", r"катастрофическ[а-яёіў]*\s+обмел",
        r"обмел[а-яёіў]*", r"маловод[а-яёіў]*",
        r"уровен[а-яёіў]*\s+вод[а-яёіў]*.*(?:низк|упал|снизил)",
        r"узровен[а-яёіў]*\s+вад[а-яёіў]*.*(?:нізк|упаў|зніз)",
    )),
    ("counterfeit", "контрафакт/отсутствие документов о безопасности", (
        r"контрафакт[а-яёіў]*", r"падробк[а-яёіў]*",
        r"без\s+документ[а-яёіў]*.*(?:качеств|безопасност)",
        r"не\s+было\s+документ[а-яёіў]*.*(?:качеств|безопасност)",
    )),
    ("outage", "отключение/перебои", (r"отключ", r"адключ", r"перебо", r"перабо", r"пропал[аио]?\s+(?:вода|свет|интернет|связ)", r"нет\s+(?:воды|света|интернета|связи)", r"няма\s+(?:вады|святла|інтэрнэту|сувязі)")),
    ("nonpayment", "невыплата/списание", (r"невыплат", r"не\s+выплат", r"не\s+заплат", r"не\s+выплац", r"списал", r"списан", r"навяз.*услуг", r"платн.*без\s+соглас")),
    ("queue_delay", "очередь/задержка", (
        r"(?<![а-яёіўa-z0-9])очеред(?:ь|и|ей|ью|ям|ями|ях)(?![а-яёіўa-z0-9])",
        r"(?<![а-яёіўa-z0-9])чарг(?:а|і|у|ой|ою|ам|амі|ах)(?![а-яёіўa-z0-9])",
        r"задерж", r"затрым", r"долго\s+ждат", r"доўга\s+чака",
    )),
    # "сток" was bare and matched "восток" (east), "исток" (origin/source,
    # incl. the common figurative "у истоков"), and "листок" — most
    # concerningly "больничный листок" (sick-leave note), a phrase very
    # likely to occur in exactly this monitor's domain. Anchored to
    # word-start, with an explicit allowance for the "водо-" compound
    # ("водосток" = downspout/gutter) since that IS still on-topic.
    (
        "pollution",
        "загрязнение",
        (
            r"загряз", r"забрудж",
            r"(?<![а-яёіўa-z])(?:водо)?сток",
            r"сцёк", r"нечистот", r"брудн.*вод",
        ),
    ),
    ("access_restriction", "ограничение доступа", (r"перекры.*доступ", r"закрыл.*доступ", r"запрет", r"забарон", r"не\s+пуска", r"захват.*берег", r"захап.*бераг", r"огородил.*берег", r"перакры.*доступ")),
    ("maintenance", "неудовлетворительное содержание", (r"не\s+кос", r"нескош", r"непокош", r"не\s+скош", r"не\s+убира", r"не\s+прыбіра", r"зарос", r"зарас", r"мусор", r"смец", r"не\s+ремонт", r"не\s+рамант")),
    ("damage", "повреждение/плохое состояние", (
        r"разбит", r"разбіт", r"разруш", r"разбур",
        r"(?<![а-яёіўa-z0-9])ям(?:а|ы|у|е|ой|ою|ам|ами|ах)?(?![а-яёіўa-z0-9])",
        r"выбоин", r"стерл.*размет",
        r"(?<![а-яёіўa-z0-9])ст[её]рт[а-яёіў]*\s+(?:дорожн[а-яёіў]*\s+)?разметк",
        r"сцерл.*размет",
        r"(?<![а-яёіўa-z0-9])сц[её]рт[а-яёіў]*\s+(?:дарожн[а-яёіў]*\s+)?(?:разметк|размец)",
        r"неисправ",
        r"няспраў", r"аварийн.*состоя", r"дрэнн.*стан",
    )),
    ("absence_shortage", "отсутствие/нехватка", (r"не\s+хватает", r"не\s+хапае", r"нехват", r"дефицит", r"адсутн", r"отсутств", r"закрыл[исая]*", r"закры[тл]", r"нет\s+(?:магазин|врач|автобус|освещ)", r"няма\s+(?:крам|ўрач|аўтобус|асвятл)")),
    ("safety", "опасность/безопасность", (r"опасн", r"небезпеч", r"небяспеч", r"угроз", r"пагроз", r"травм", r"траўм")),
    # "спек" was bare and matched "спекуляция/спекулянт" ("спек"+"ул...")
    # and "спектакль/спектр" ("спек"+"т..."), mistagging price-speculation
    # or cultural-event stories as heat-related work conditions. Negative
    # lookahead excludes those continuations while still matching
    # "спека/спекотный" (heat, a regional RU/BE term).
    ("work_conditions", "условия труда", (r"жар[аыу]", r"спек(?!ул|т)", r"температур", r"тэмператур", r"услови.*труд", r"ўмов.*прац")),
    ("service_quality", "качество услуги", (r"плох.*качеств", r"дрэнн.*якасц", r"некачествен", r"няякасн", r"плох.*связ", r"не\s+работает", r"не\s+працуе")),
)


# The queue noun has several common non-delay meanings: a construction phase,
# an idiom ("в первую очередь") and an administrative housing-waiting status.
# Remove only those contexts before scoring the Event Echo problem. Editorial
# relevance is deliberately untouched; this merely prevents false signatures.
EVENT_QUEUE_NON_DELAY_PATTERNS: tuple[str, ...] = (
    r"(?<![а-яёіўa-z0-9])в\s+(?:первую|последнюю|свою)\s+очередь(?![а-яёіўa-z0-9])",
    r"(?<![а-яёіўa-z0-9])у\s+(?:першую|апошнюю|сваю)\s+чаргу(?![а-яёіўa-z0-9])",
    r"(?<![а-яёіўa-z0-9])(?:первая|вторая|третья|четвертая|пятая|следующая)\s+очеред(?:ь|и)(?![а-яёіўa-z0-9])",
    r"(?<![а-яёіўa-z0-9])(?:першая|другая|трэцяя|чацв[её]ртая|пятая|наступная)\s+чарг(?:а|і)(?![а-яёіўa-z0-9])",
    r"(?<![а-яёіўa-z0-9])\d+\s+очеред(?:ь|и|ей)(?![а-яёіўa-z0-9])",
    r"(?<![а-яёіўa-z0-9])\d+\s+чарг(?:а|і|аў)(?![а-яёіўa-z0-9])",
    r"(?<![а-яёіўa-z0-9])очеред(?:ь|и)\s+(?:строительств|возвед|реконструкц|ремонт|ввода|реализац|объект)",
    r"(?<![а-яёіўa-z0-9])чарг(?:а|і)\s+(?:будаўніцтв|узвядзен|рэканструкц|рамонт|уводу|рэалізац|аб'ект)",
)

EVENT_DAMAGE_NON_PROBLEM_PATTERNS: tuple[str, ...] = (
    r"(?<![а-яёіўa-z0-9])разбит(?:а|о|ы)?\s+на\s+\d+\s+(?:очеред|этап|част)",
    r"(?<![а-яёіўa-z0-9])разбі(?:т|та|тае|тыя)\s+на\s+\d+\s+(?:чарг|этап|част)",
)

EVENT_HOUSING_QUEUE_STATUS_PATTERN = re.compile(
    r"(?:очередник|нуждающ.{0,35}(?:жиль|жилищ)|"
    r"(?:уч[её]т|очеред).{0,35}(?:жиль|жилищ)|"
    r"чаргавік|маюч.{0,35}патрэб.{0,35}жыл)",
    flags=re.IGNORECASE,
)
EVENT_QUEUE_DISTRESS_PATTERN = re.compile(
    r"(?:годами|месяцами|часами|долго|доўга|гадамі|месяцамі|гадзінамі|"
    r"не\s+могут\s+дожд|не\s+могуць\s+дачака|жал(?:об|у|уют)|скардз|"
    r"задерж|затрым|образовал.{0,25}очеред|скопил.{0,25}очеред)",
    flags=re.IGNORECASE,
)


def _event_problem_parts(
    parts: tuple[tuple[str, int], ...],
) -> tuple[tuple[str, int], ...]:
    cleaned: list[tuple[str, int]] = []
    for text, weight in parts:
        value = text
        for pattern in EVENT_DAMAGE_NON_PROBLEM_PATTERNS:
            value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
        for pattern in EVENT_QUEUE_NON_DELAY_PATTERNS:
            value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
        if (
            EVENT_HOUSING_QUEUE_STATUS_PATTERN.search(value)
            and not EVENT_QUEUE_DISTRESS_PATTERN.search(value)
        ):
            value = re.sub(
                r"(?<![а-яёіўa-z0-9])очеред(?:ь|и|ей|ью|ям|ями|ях)(?![а-яёіўa-z0-9])",
                " ",
                value,
                flags=re.IGNORECASE,
            )
        cleaned.append((value, weight))
    return tuple(cleaned)


def _pattern_score(parts: tuple[tuple[str, int], ...], patterns: tuple[str, ...]) -> int:
    score = 0
    for text, weight in parts:
        if not text:
            continue
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                score += weight
                break
    return score


def _best_pattern_label(
    parts: tuple[tuple[str, int], ...],
    definitions: tuple[tuple[str, str, tuple[str, ...]], ...],
    specificity: dict[str, int] | None = None,
) -> tuple[str, str]:
    scored: list[tuple[int, str, str]] = []
    specificity = specificity or {}
    for key, label, patterns in definitions:
        score = _pattern_score(parts, patterns)
        if score > 0:
            score += int(specificity.get(key, 0))
            scored.append((score, key, label))
    if not scored:
        return "", ""
    scored.sort(reverse=True)
    best_score = scored[0][0]
    if best_score < 3:
        return "", ""
    best = [item for item in scored if item[0] == best_score]
    # Ties are intentionally unresolved. Event Echo is a recovery hint, not a
    # classifier that is allowed to invent precision.
    if len(best) != 1:
        return "", ""
    return best[0][1], best[0][2]


def infer_event_geography(title: str, summary: str = "", body: str = "") -> tuple[str, str]:
    title_text = normalized_search_text(repair_mojibake(title))
    summary_text = normalized_search_text(repair_mojibake(summary))
    opening_text = normalized_search_text(repair_mojibake(body))[:1800]
    parts = ((title_text, 5), (summary_text, 3), (opening_text, 1))
    national_title = bool(
        re.search(r"(?<![а-яёіўa-z])беларус|(?<![а-яёіўa-z])белорус", title_text)
    )

    locality_scores: list[tuple[int, str, str]] = []
    for locality, region, patterns in EVENT_LOCALITY_PATTERNS:
        score = _pattern_score(parts, patterns)
        if score > 0:
            locality_scores.append((score, locality, region))

    explicit_regions: list[tuple[int, str]] = []
    for region, patterns in EVENT_REGION_PATTERNS:
        score = _pattern_score(parts, patterns)
        if score > 0:
            explicit_regions.append((score, region))

    explicit_region = ""
    if explicit_regions:
        explicit_regions.sort(reverse=True)
        top = explicit_regions[0][0]
        tied = {region for score, region in explicit_regions if score == top}
        if top >= 3 and len(tied) == 1:
            explicit_region = next(iter(tied))

    locality = ""
    locality_region = ""
    if locality_scores:
        locality_scores.sort(reverse=True)
        top = locality_scores[0][0]
        tied = [(loc, region) for score, loc, region in locality_scores if score == top]
        if top >= 3 and len(tied) == 1:
            locality, locality_region = tied[0]
        elif top >= 3:
            # Multiple equally strong places are not collapsed into one event.
            # A shared oblast may still be reported as coarse geography.
            tied_regions = {region for _loc, region in tied}
            if len(tied_regions) == 1 and not explicit_region:
                explicit_region = next(iter(tied_regions))

    if locality and national_title:
        title_has_locality = any(
            loc == locality and _pattern_score(((title_text, 5),), patterns) > 0
            for loc, _region, patterns in EVENT_LOCALITY_PATTERNS
        )
        if not title_has_locality:
            locality = ""
            locality_region = ""
    if locality and explicit_region and explicit_region != locality_region:
        # Conflicting explicit geography is more likely a comparative or
        # multi-place story. Keep only the coarse explicit region.
        return explicit_region, ""
    if locality:
        return locality_region, locality
    return explicit_region, ""


def infer_event_fingerprint(
    title: str,
    summary: str = "",
    body: str = "",
) -> EventFingerprint:
    region, locality = infer_event_geography(title, summary, body)
    title_text = normalized_search_text(repair_mojibake(title))
    summary_text = normalized_search_text(repair_mojibake(summary))
    opening_text = normalized_search_text(repair_mojibake(body))[:1800]
    parts = ((title_text, 5), (summary_text, 3), (opening_text, 1))
    object_key, object_label = _best_pattern_label(
        parts,
        EVENT_OBJECT_PATTERNS,
        {
            "station_storage": 2, "communal_billing": 2,
            "food_product": 2, "parking": 1, "water_supply": 1,
            "lighting": 1, "school_appearance_rules": 2,
        },
    )
    problem_key, problem_label = _best_pattern_label(
        _event_problem_parts(parts),
        EVENT_PROBLEM_PATTERNS,
        {
            "contamination": 5, "billing_overcharge": 3,
            "bullying": 3, "animal_welfare": 3, "hiring_shortage": 2,
            "public_resonance": 2,
        },
    )
    signature = ""
    event_scope = locality.casefold() if locality else (
        f"region:{region.casefold()}" if region else ""
    )
    if not event_scope and re.search(r"\b(?:в|по)\s+беларус[а-яёіў]*\b", " ".join((title_text, summary_text, opening_text))):
        event_scope = "беларусь"
    # A rare pair of microbiological findings is a stronger event anchor than
    # an omitted locality in a short Telegram rewrite.  Normalising this one
    # narrow family to a national scope allows the next-day short retelling to
    # connect to the fuller regional reports, while a single pathogen remains
    # locality-bound and cannot collapse unrelated recalls.
    combined_event_text = " ".join((title_text, summary_text, opening_text))
    # The shortage of BZD platform wagons is a nationwide transport-market
    # event even where a rewrite does not spell out a locality.  This narrow
    # normalisation is intentionally not applied to ordinary train stories.
    if (
        object_key == "rail_platform_wagons"
        and re.search(r"(?:\bбжд\b|белорусск[а-яёіў]*\s+железн[а-яёіў]*\s+дорог)", combined_event_text)
    ):
        event_scope = "беларусь"
    if (
        object_key == "food_product"
        and problem_key == "contamination"
        and re.search(r"кишечн[а-яёіўa-z0-9]*\s+палоч", combined_event_text)
        and re.search(r"стафилокок", combined_event_text)
    ):
        event_scope = "беларусь:кишечная-палочка+стафилококк"
    if event_scope and object_key and problem_key:
        signature = "|".join((event_scope, object_key, problem_key))
    return EventFingerprint(
        region=region,
        locality=locality,
        object_key=object_key,
        object_label=object_label,
        problem_key=problem_key,
        problem_label=problem_label,
        signature=signature,
    )


def event_times_within_window(
    left: str,
    right: str,
    window_hours: int = 48,
) -> bool:
    left_dt = parse_datetime(left)
    right_dt = parse_datetime(right)
    if left_dt is None or right_dt is None:
        return False
    return abs((left_dt - right_dt).total_seconds()) <= max(1, window_hours) * 3600


def apply_event_fingerprint(
    trace: CandidateProcessingTelemetry,
    fingerprint: EventFingerprint,
) -> None:
    if fingerprint.region:
        trace.event_region = fingerprint.region
    if fingerprint.locality:
        trace.event_locality = fingerprint.locality
    if fingerprint.object_label:
        trace.event_object = fingerprint.object_label
    if fingerprint.problem_label:
        trace.event_problem = fingerprint.problem_label
    if fingerprint.signature:
        trace.event_signature = fingerprint.signature



def mojibake_score(value: str) -> tuple[int, int, int]:
    """Lower is better: suspicious sequences, replacement chars, controls."""
    suspicious_tokens = (
        "Ã", "Â", "Ð", "Ñ", "â€", "â€™", "â€œ", "â€", "ðŸ", "¤",
    )
    suspicious = sum(value.count(token) for token in suspicious_tokens)
    replacements = value.count("\ufffd")
    controls = sum(
        1
        for character in value
        if ord(character) < 32 and character not in "\n\r\t"
    )
    readable = len(
        re.findall(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґЎў]", value)
    )
    return suspicious + replacements * 8 + controls * 4, replacements, -readable


def repair_mojibake(value: str) -> str:
    """Repair common UTF-8/Latin-1/CP1252 mojibake, including double encoding."""
    original = value or ""
    if not original:
        return original

    best = original
    best_score = mojibake_score(best)
    frontier = [original]
    visited = {original}

    for _round in range(2):
        next_frontier: list[str] = []
        for current in frontier:
            for encoding in ("latin-1", "cp1252"):
                try:
                    candidate = current.encode(encoding).decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if candidate in visited:
                    continue
                visited.add(candidate)
                next_frontier.append(candidate)
                candidate_score = mojibake_score(candidate)
                if candidate_score < best_score:
                    best = candidate
                    best_score = candidate_score
        frontier = next_frontier
        if not frontier:
            break

    if best.count("\ufffd") > original.count("\ufffd"):
        return original
    return best


def detect_language(text: str, fallback: str) -> str:
    """Определяет русский или белорусский язык без перевода текста."""
    cleaned = normalize_space(repair_mojibake(text)).casefold()
    fallback_code = (fallback or "ru").lower().strip()
    if not cleaned:
        return fallback_code
    if "ў" in cleaned:
        return "be"

    words = re.findall(r"[а-яёіў]+", cleaned)
    be_markers = {
        "гэта", "няма", "ёсць", "які", "якая", "якія", "што", "жыхары",
        "вуліца", "вуліцы", "горад", "раён", "дарога", "вада", "кошт",
        "паведамілі", "звярнуліся", "скардзяцца", "праблема", "працуе",
    }
    ru_markers = {
        "это", "нет", "есть", "который", "которая", "которые", "что",
        "жители", "улица", "улицы", "город", "район", "дорога", "вода",
        "цена", "сообщили", "обратились", "жалуются", "проблема", "работает",
    }
    be_score = sum(word in be_markers for word in words) + cleaned.count("ў") * 4
    ru_score = sum(word in ru_markers for word in words) + len(re.findall(r"[ыэёъ]", cleaned))
    if be_score >= ru_score + 2:
        return "be"
    if re.search(r"[а-яё]", cleaned):
        return "ru"
    return fallback_code if fallback_code in {"ru", "be"} else "ru"



def canonicalize_url(url: str) -> str:
    raw = html.unescape((url or "").strip())
    # Some archive pages expose a query separator as part of the path
    # (``index.html%3Fp=123``). Restore it before URL parsing so the report
    # contains a directly clickable publication link.
    if "?" not in raw and re.search(r"%3[fF]", raw):
        raw = re.sub(r"%3[fF]", "?", raw, count=1)
        raw = re.sub(r"%26", "&", raw, flags=re.IGNORECASE)
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return url.strip()

    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    filtered = [
        (key, value) for key, value in query
        if not key.lower().startswith("utm_")
        and key.lower() not in {
            "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
            "output", "share", "cmpid", "cid", "tg"
        }
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((
        parsed.scheme.lower() or "https",
        parsed.netloc.lower(),
        path,
        urllib.parse.urlencode(filtered),
        "",
    ))


def same_site(url: str, domain: str) -> bool:
    host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    domain = domain.lower().split(":")[0]
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host)


# Domains where a bare numeric route is a known duplicate/redirect risk
# against an already-admitted localized form (e.g. nashaniva.com serves
# canonical articles at /ru/<id> and /be_latn/<id>/, and its bare /<id>
# route was deliberately left "unknown" pending diagnosis of its
# extraction/duplication behavior — see
# test_architecture_core32a22_nasha_niva_comments_guard_covers_all_locales).
# The general numeric-id article rule below must not override that
# considered decision for these specific domains.
NUMERIC_ARTICLE_ID_EXCLUDED_DOMAINS: frozenset[str] = frozenset({"nashaniva.com"})


def is_probable_article_url(url: str, domain: str) -> bool:
    if not same_site(url, domain):
        return False
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.lower()
    segments = [segment for segment in path.split("/") if segment]
    # Result Event Integrity 1.9: some outlets (vkurier.by is the diagnosed
    # case: /238683, /238672, ...) publish articles directly under the
    # domain root as a bare numeric CMS id, shorter than the length-8 floor
    # below that was tuned for slug-style paths. This rule is deliberately
    # general — bounded to 5-9 digits, domain-agnostic — rather than a
    # per-domain special case, specifically so a *future* source using the
    # same short-numeric-id scheme is recognised immediately instead of
    # needing its own multi-round diagnosis (as vkurier.by did). A prior
    # revision of this fix narrowed it back down to a vkurier.by-only
    # hardcoded check, which would have silently reintroduced exactly that
    # problem for any of the newly diagnosed candidate sources sharing the
    # same URL shape.
    if (
        len(segments) == 1
        and segments[0].isdigit()
        and 5 <= len(segments[0]) <= 9
        and not any(part in path for part in BLOCKED_PATH_PARTS)
        and domain.lower().removeprefix("www.") not in NUMERIC_ARTICLE_ID_EXCLUDED_DOMAINS
    ):
        return True
    if len(path.strip("/")) < 8:
        return False
    if any(part in path for part in BLOCKED_PATH_PARTS):
        return False
    if re.search(r"\.(jpg|jpeg|png|gif|svg|webp|pdf|mp3|mp4|zip)$", path):
        return False
    if path.endswith(ARTICLE_EXTENSIONS):
        return True
    if len(segments) >= 2 or bool(re.search(r"/20\d{2}/", path)):
        return True
    # WordPress and several Belarusian media use a single long slug directly
    # under the domain. Short service pages remain excluded.
    if len(segments) == 1:
        slug = segments[0]
        return len(slug) >= 24 and slug.count("-") >= 2
    return False


def title_is_suspicious(value: str) -> bool:
    score, replacements, _readable = mojibake_score(value or "")
    return score > 0 or replacements > 0


@dataclass(frozen=True)
class HttpObservation:
    url: str
    status_code: int = 0
    attempts: int = 0
    outcome: str = "failed"
    failure_class: str = ""
    seconds: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class EndpointTelemetry:
    channel: str
    endpoint: str
    outcome: str = "failed"
    status_code: int = 0
    failure_class: str = ""
    attempts: int = 0
    seconds: float = 0.0
    candidates: int = 0
    probe_mode: str = "normal"
    detail: str = ""


def classify_http_failure(status_code: int) -> str:
    if status_code == 429:
        return "rate_limited"
    if status_code in {401, 403, 404, 410, 451}:
        return "permanent_http"
    if status_code >= 500:
        return "transient_http"
    if 400 <= status_code < 500:
        return "client_http"
    return "unknown"


def request_exception_detail(error: Exception) -> str:
    """Return a compact, actionable and secret-free transport diagnosis.

    ``requests`` nests the operating-system cause at the end of a long error
    chain.  Keeping its beginning hid the actual errno in run 27.  Prefer the
    final errno fragment; otherwise preserve the end of the message, where
    urllib3 normally places the root cause.
    """
    message = normalize_space(str(error))
    errno_matches = re.findall(r"\[Errno\s+[^\]]+\][^)]{0,140}", message)
    if errno_matches:
        return f"{type(error).__name__}: {errno_matches[-1]}"
    return f"{type(error).__name__}: {message[-300:]}"


class HttpClient:
    BROWSER_HEADER_DOMAINS = {
        "belsat.eu", "reform.news", "pozirk.online",
        "gazetaby.com", "charter97.org",
    }

    def __init__(self, settings: dict[str, Any]):
        monitor = settings["monitor"]
        self.timeout = int(monitor.get("request_timeout_seconds", 18))
        self.user_agent = str(monitor.get("user_agent", "Private Media Monitor/1.0"))
        self._observations: dict[str, HttpObservation] = {}
        self._observation_log: list[HttpObservation] = []
        self.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/rss+xml,application/atom+xml;q=0.8,*/*;q=0.5",
            "Accept-Language": "ru,be;q=0.9,en;q=0.5,*;q=0.3",
        }
        self.browser_headers = {
            **self.headers,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,be;q=0.8,en-US;q=0.6,en;q=0.5",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }

    def headers_for_url(self, url: str) -> dict[str, str]:
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
        if any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in self.BROWSER_HEADER_DOMAINS
        ):
            return self.browser_headers
        return self.headers

    def observation_for(self, url: str) -> HttpObservation | None:
        return self._observations.get(canonicalize_url(url))

    def observation_count(self) -> int:
        return len(self._observation_log)

    def observations_since(self, index: int = 0) -> tuple[HttpObservation, ...]:
        return tuple(self._observation_log[max(0, int(index)):])

    def http_seconds(self) -> float:
        return sum(item.seconds for item in self._observation_log)

    def http_attempts(self) -> int:
        return sum(item.attempts for item in self._observation_log)

    def _record_observation(
        self, url: str, status_code: int, attempts: int,
        outcome: str, failure_class: str = "", seconds: float = 0.0,
        detail: str = "",
    ) -> None:
        observation = HttpObservation(
            url=canonicalize_url(url),
            status_code=int(status_code or 0),
            attempts=attempts,
            outcome=outcome,
            failure_class=failure_class,
            seconds=max(0.0, float(seconds or 0.0)),
            detail=normalize_space(detail)[:360],
        )
        self._observations[canonicalize_url(url)] = observation
        self._observation_log.append(observation)

    def get(self, url: str, retries: int = 1) -> requests.Response | None:
        """GET with differentiated retry classes and observable outcome.

        Permanent 4xx failures are never retried.  Rate limits, transient 5xx
        responses and network errors may use the caller's retry budget with
        differentiated backoff.  The public return type remains unchanged.
        """
        started = time.perf_counter()
        last_error: Exception | None = None
        attempts = 0
        last_status = 0
        last_failure_class = ""
        max_attempts = max(1, int(retries) + 1)
        for attempt in range(max_attempts):
            attempts = attempt + 1
            try:
                response = requests.get(
                    url,
                    headers=self.headers_for_url(url),
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                last_status = int(response.status_code or 0)
                if response.status_code == 200:
                    self._record_observation(
                        url, 200, attempts, "ok",
                        seconds=time.perf_counter() - started,
                    )
                    return response

                last_failure_class = classify_http_failure(response.status_code)
                if last_failure_class in {"permanent_http", "client_http"}:
                    self._record_observation(
                        url, response.status_code, attempts, "failed",
                        last_failure_class,
                        time.perf_counter() - started,
                    )
                    return None
                if attempt < max_attempts - 1:
                    if last_failure_class == "rate_limited":
                        time.sleep(2.0 + attempt * 2.0)
                    elif last_failure_class == "transient_http":
                        time.sleep(1.0 + attempt * 1.5)
            except requests.RequestException as exc:
                last_error = exc
                last_failure_class = "network_error"
                if attempt < max_attempts - 1:
                    time.sleep(1.0 + attempt * 1.5)

        self._record_observation(
            url, last_status, attempts, "failed", last_failure_class or "unknown",
            time.perf_counter() - started,
            request_exception_detail(last_error) if last_error else "",
        )
        if last_error:
            LOG.debug("GET failed %s: %s", url, last_error)
        return None



class RecoveryController:
    """Persistent recovery state for discovery endpoints and transports.

    The controller mutates only the in-memory state object.  run_monitor() keeps
    the existing dry-run guarantee by saving state only when dry_run is false.
    """

    def __init__(
        self,
        state: dict[str, Any],
        settings: dict[str, Any],
        now: dt.datetime,
    ) -> None:
        self.now = now
        self.settings = settings
        self.lock = threading.Lock()
        root = state.setdefault("recovery", {})
        if not isinstance(root, dict):
            root = {}
            state["recovery"] = root
        root["version"] = max(2, int(root.get("version", 1) or 1))
        root.setdefault("endpoints", {})
        root.setdefault("transports", {})
        root.setdefault("degraded_queue", {})
        root.setdefault("event_seeds", [])
        self.root = root
        policy = settings.get("recovery", {}) or {}
        self.failure_threshold = max(2, int(policy.get("failure_threshold", 3)))
        self.tail_probe_hours = max(1, int(policy.get("tail_probe_hours", 6)))
        self.history_size = max(3, int(policy.get("history_size", 8)))
        self.drop_ratio = float(policy.get("candidate_drop_ratio", 0.20))
        self.drop_min_baseline = max(5, int(policy.get("candidate_drop_min_baseline", 10)))
        self.queue_max_attempts = max(1, int(policy.get("queue_max_attempts", 3)))
        self.queue_retain_days = max(2, int(policy.get("queue_retain_days", 7)))
        raw_schedule = policy.get("queue_retry_hours", [6, 24, 72])
        if not isinstance(raw_schedule, list) or not raw_schedule:
            raw_schedule = [6, 24, 72]
        self.queue_retry_hours = [max(1, int(value)) for value in raw_schedule]
        event_policy = settings.get("event_echo", {}) or {}
        self.event_echo_window_hours = max(
            6, int(event_policy.get("window_hours", 48))
        )
        self.event_echo_priority_retry_hours = max(
            1, int(event_policy.get("priority_retry_hours", 1))
        )
        self.event_seed_limit = max(
            50, int(event_policy.get("seed_limit", 500))
        )

    def _endpoint_key(self, source: Source, channel: str, endpoint: str) -> str:
        return f"{source.name}|{channel}|{canonicalize_url(endpoint)}"

    def _transport_key(self, source: Source, transport: str) -> str:
        return f"{source.name}|{normalized_domain(source.domain)}|{transport}"

    def _decision(self, entry: dict[str, Any] | None) -> str:
        if not entry or not entry.get("circuit_open"):
            return "normal"
        next_probe = parse_datetime(entry.get("next_probe_at"))
        if next_probe and self.now < next_probe:
            return "skip"
        return "tail_probe"

    def endpoint_decision(self, source: Source, channel: str, endpoint: str) -> str:
        with self.lock:
            entry = self.root["endpoints"].get(
                self._endpoint_key(source, channel, endpoint)
            )
            return self._decision(entry)

    def transport_decision(self, source: Source, transport: str) -> str:
        # Transport-level circuit breaking is intentionally limited to the
        # protected strategic class. Ordinary sources keep the historical
        # request path so the recovery layer cannot silently reduce coverage.
        if not effective_source_profile(source, self.settings).get("protected", False):
            return "normal"
        with self.lock:
            entry = self.root["transports"].get(
                self._transport_key(source, transport)
            )
            return self._decision(entry)

    def _history_baseline(self, entry: dict[str, Any]) -> float | None:
        values = [
            int(item.get("candidates", 0))
            for item in entry.get("history", [])
            if item.get("outcome") == "ok" and int(item.get("candidates", 0)) > 0
        ]
        if len(values) < 3:
            return None
        return float(statistics.median(values[-self.history_size:]))

    def record_endpoint(
        self,
        source: Source,
        channel: str,
        endpoint: str,
        observation: HttpObservation | None,
        candidates: int,
        probe_mode: str = "normal",
    ) -> str:
        key = self._endpoint_key(source, channel, endpoint)
        with self.lock:
            endpoints = self.root["endpoints"]
            entry = endpoints.setdefault(key, {
                "source": source.name,
                "channel": channel,
                "endpoint": canonicalize_url(endpoint),
                "history": [],
                "consecutive_failures": 0,
                "consecutive_degraded": 0,
                "circuit_open": False,
            })
            baseline = self._history_baseline(entry)
            network_ok = observation is None or observation.outcome == "ok"
            health = "ok"
            if not network_ok:
                health = "failed"
            elif (
                baseline is not None
                and baseline >= self.drop_min_baseline
                and candidates <= max(2, int(baseline * self.drop_ratio))
            ):
                health = "degraded"

            if health == "failed":
                entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
                entry["consecutive_degraded"] = 0
                if entry["consecutive_failures"] >= self.failure_threshold:
                    entry["circuit_open"] = True
                    entry["next_probe_at"] = (
                        self.now + dt.timedelta(hours=self.tail_probe_hours)
                    ).isoformat()
            else:
                entry["consecutive_failures"] = 0
                entry["circuit_open"] = False
                entry["next_probe_at"] = ""
                if health == "degraded":
                    entry["consecutive_degraded"] = int(entry.get("consecutive_degraded", 0)) + 1
                else:
                    entry["consecutive_degraded"] = 0
                    entry["last_success"] = self.now.isoformat()

            entry["last_checked"] = self.now.isoformat()
            entry["last_status"] = int(observation.status_code if observation else 0)
            entry["last_failure_class"] = observation.failure_class if observation else ""
            entry["last_candidates"] = int(candidates)
            entry["last_health"] = health
            entry["last_probe_mode"] = probe_mode
            history = entry.setdefault("history", [])
            history.append({
                "at": self.now.isoformat(),
                "outcome": "ok" if network_ok else "failed",
                "health": health,
                "status": int(observation.status_code if observation else 0),
                "candidates": int(candidates),
            })
            del history[:-self.history_size]
            return health

    def record_transport(
        self,
        source: Source,
        transport: str,
        ok: bool,
        status_code: int = 0,
        failure_class: str = "",
        probe_mode: str = "normal",
    ) -> None:
        key = self._transport_key(source, transport)
        with self.lock:
            transports = self.root["transports"]
            entry = transports.setdefault(key, {
                "source": source.name,
                "domain": normalized_domain(source.domain),
                "transport": transport,
                "history": [],
                "consecutive_failures": 0,
                "circuit_open": False,
            })
            if ok:
                entry["consecutive_failures"] = 0
                entry["circuit_open"] = False
                entry["next_probe_at"] = ""
                entry["last_success"] = self.now.isoformat()
            else:
                entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
                protected = effective_source_profile(source, self.settings).get("protected", False)
                if protected and entry["consecutive_failures"] >= self.failure_threshold:
                    entry["circuit_open"] = True
                    entry["next_probe_at"] = (
                        self.now + dt.timedelta(hours=self.tail_probe_hours)
                    ).isoformat()
            entry["last_checked"] = self.now.isoformat()
            entry["last_status"] = int(status_code or 0)
            entry["last_failure_class"] = failure_class
            entry["last_probe_mode"] = probe_mode
            history = entry.setdefault("history", [])
            history.append({
                "at": self.now.isoformat(),
                "outcome": "ok" if ok else "failed",
                "status": int(status_code or 0),
                "failure_class": failure_class,
            })
            del history[:-self.history_size]

    def queue_degraded(self, candidate: Candidate, trace: CandidateProcessingTelemetry) -> str:
        key = canonicalize_url(candidate.url)
        with self.lock:
            queue = self.root["degraded_queue"]
            record = queue.get(key, {})
            attempts = int(record.get("attempts", 0)) + 1
            first_queued = record.get("first_queued") or self.now.isoformat()
            status = "active"
            next_retry_at = ""
            if attempts >= self.queue_max_attempts:
                status = "exhausted"
            else:
                schedule_index = min(attempts - 1, len(self.queue_retry_hours) - 1)
                retry_hours = self.queue_retry_hours[schedule_index]
                if trace.event_echo_priority:
                    retry_hours = min(
                        retry_hours,
                        self.event_echo_priority_retry_hours,
                    )
                next_retry_at = (
                    self.now + dt.timedelta(hours=retry_hours)
                ).isoformat()
            queue[key] = {
                "url": key,
                "source": candidate.source.name,
                "title": candidate.title,
                "summary": candidate.summary,
                "published_at": candidate.published_at,
                "discovered_via": candidate.discovered_via,
                "inline_text": candidate.inline_text,
                "title_generated": bool(candidate.title_generated),
                "first_queued": first_queued,
                "last_attempt": self.now.isoformat(),
                "attempts": attempts,
                "status": status,
                "next_retry_at": next_retry_at,
                "reason": trace.degraded_reason,
                "transport": trace.transport,
                "extraction_strategy": trace.extraction_strategy,
                "event_region": trace.event_region,
                "event_locality": trace.event_locality,
                "event_object": trace.event_object,
                "event_problem": trace.event_problem,
                "event_signature": trace.event_signature,
                "event_published_at": trace.event_published_at,
                "event_echo": bool(trace.event_echo),
                "event_echo_anchor": trace.event_echo_anchor,
                "event_echo_sources": list(trace.event_echo_sources),
                "event_echo_priority": bool(trace.event_echo_priority),
            }
            return status

    def remove_from_queue(self, url: str) -> None:
        with self.lock:
            self.root["degraded_queue"].pop(canonicalize_url(url), None)

    def should_defer_url(self, url: str) -> bool:
        with self.lock:
            record = self.root["degraded_queue"].get(canonicalize_url(url))
            if not record:
                return False
            if record.get("status") == "exhausted":
                return True
            next_retry = parse_datetime(record.get("next_retry_at"))
            return bool(next_retry and self.now < next_retry)

    def due_candidates(self, sources: list[Source]) -> list[Candidate]:
        by_name = {source.name: source for source in sources}
        due: list[Candidate] = []
        with self.lock:
            records = list(self.root["degraded_queue"].values())
        for record in records:
            if record.get("status") != "active":
                continue
            next_retry = parse_datetime(record.get("next_retry_at"))
            if next_retry and self.now < next_retry:
                continue
            source = by_name.get(str(record.get("source", "")))
            if not source:
                continue
            due.append(Candidate(
                source=source,
                url=str(record.get("url", "")),
                title=str(record.get("title", "")),
                summary=str(record.get("summary", "")),
                published_at=str(record.get("published_at", "")),
                discovered_via=str(record.get("discovered_via", "recovery_queue")),
                inline_text=str(record.get("inline_text", "")),
                title_generated=bool(record.get("title_generated", False)),
            ))
        return due

    def queue_count_for_source(self, source_name: str) -> int:
        with self.lock:
            return sum(
                1 for item in self.root["degraded_queue"].values()
                if item.get("source") == source_name and item.get("status") == "active"
            )

    def open_circuits_for_source(self, source_name: str) -> int:
        with self.lock:
            return sum(
                1
                for bucket_name in ("endpoints", "transports")
                for item in self.root[bucket_name].values()
                if item.get("source") == source_name and item.get("circuit_open")
            )

    def active_queue_count(self) -> int:
        with self.lock:
            return sum(
                1 for item in self.root["degraded_queue"].values()
                if item.get("status") == "active"
            )

    def recent_event_seeds(self) -> list[dict[str, Any]]:
        cutoff = self.now - dt.timedelta(hours=self.event_echo_window_hours)
        with self.lock:
            records = list(self.root.get("event_seeds", []))
        result: list[dict[str, Any]] = []
        for record in records:
            published = parse_datetime(record.get("published_at"))
            if not published or published < cutoff:
                continue
            if not record.get("event_signature"):
                continue
            result.append(dict(record))
        return result

    def remember_event_seed(self, result: ArticleResult) -> None:
        if not result.event_signature or not result.published_at:
            return
        record = {
            "event_signature": result.event_signature,
            "event_region": result.event_region,
            "event_locality": result.event_locality,
            "event_object": result.event_object,
            "event_problem": result.event_problem,
            "source": result.source_name,
            "url": canonicalize_url(result.url),
            "published_at": result.published_at,
            "remembered_at": self.now.isoformat(),
        }
        with self.lock:
            seeds = self.root.setdefault("event_seeds", [])
            seeds[:] = [
                item for item in seeds
                if not (
                    item.get("source") == record["source"]
                    and canonicalize_url(str(item.get("url", ""))) == record["url"]
                )
            ]
            seeds.append(record)
            if len(seeds) > self.event_seed_limit:
                del seeds[:-self.event_seed_limit]

    def event_seed_count(self) -> int:
        return len(self.recent_event_seeds())

    def prune(self) -> None:
        cutoff = self.now - dt.timedelta(days=self.queue_retain_days)
        with self.lock:
            queue = self.root["degraded_queue"]
            stale = []
            for key, record in queue.items():
                first = parse_datetime(record.get("first_queued"))
                if first and first < cutoff:
                    stale.append(key)
            for key in stale:
                queue.pop(key, None)
            seed_cutoff = self.now - dt.timedelta(
                hours=max(self.event_echo_window_hours * 2, 72)
            )
            seeds = self.root.setdefault("event_seeds", [])
            kept = []
            for record in seeds:
                published = parse_datetime(record.get("published_at"))
                if published and published >= seed_cutoff:
                    kept.append(record)
            self.root["event_seeds"] = kept[-self.event_seed_limit:]



def _echo_sources_for_event(
    signature: str,
    published_at: str,
    source_name: str,
    current_results: list[ArticleResult],
    state_seeds: list[dict[str, Any]],
    window_hours: int,
) -> tuple[tuple[str, ...], str]:
    current_sources = sorted({
        item.source_name
        for item in current_results
        if item.event_signature == signature
        and item.source_name != source_name
        and event_times_within_window(
            published_at, item.published_at, window_hours
        )
    })
    if current_sources:
        return tuple(current_sources), "current"

    state_sources = sorted({
        str(item.get("source", ""))
        for item in state_seeds
        if item.get("event_signature") == signature
        and str(item.get("source", ""))
        and str(item.get("source", "")) != source_name
        and event_times_within_window(
            published_at,
            str(item.get("published_at", "")),
            window_hours,
        )
    })
    if state_sources:
        return tuple(state_sources), "state"
    return (), ""


def apply_event_echo(
    processing_outcomes: dict[
        str, tuple[Candidate, CandidateProcessingTelemetry]
    ],
    results: list[ArticleResult],
    recovery: RecoveryController | None,
    settings: dict[str, Any],
) -> None:
    """Mark cross-source event resonance without changing editorial inclusion.

    Event Echo is a recovery prioritizer only. A degraded candidate remains
    excluded from the report even when an included publication from another
    source describes the same locality + object + problem inside the time
    window.
    """
    policy = settings.get("event_echo", {}) or {}
    window_hours = max(
        6,
        int(
            policy.get(
                "window_hours",
                recovery.event_echo_window_hours if recovery else 48,
            )
        ),
    )
    state_seeds = recovery.recent_event_seeds() if recovery else []

    for result in results:
        if not result.event_signature or not result.published_at:
            continue
        sources, anchor = _echo_sources_for_event(
            result.event_signature,
            result.published_at,
            result.source_name,
            results,
            state_seeds,
            window_hours,
        )
        if sources:
            result.event_echo = True
            result.event_echo_anchor = anchor
            result.event_echo_sources = ", ".join(sources)
            outcome = processing_outcomes.get(canonicalize_url(result.url))
            if outcome:
                trace = outcome[1]
                trace.event_echo = True
                trace.event_echo_anchor = anchor
                trace.event_echo_sources = sources

    for key, (candidate, trace) in processing_outcomes.items():
        if trace.final_stage != "degraded_queued":
            continue
        if not trace.event_signature or not trace.event_published_at:
            continue
        sources, anchor = _echo_sources_for_event(
            trace.event_signature,
            trace.event_published_at,
            candidate.source.name,
            results,
            state_seeds,
            window_hours,
        )
        if not sources:
            continue
        trace.event_echo = True
        trace.event_echo_anchor = anchor
        trace.event_echo_sources = sources
        trace.event_echo_priority = True


def load_settings(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    if not isinstance(settings, dict):
        raise ValueError("config/settings.yaml должен содержать YAML-объект.")
    return settings


def load_sources(path: Path) -> list[Source]:
    result: list[Source] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            enabled = str(raw.get("enabled", "true")).strip().lower() in {
                "1", "true", "yes", "on"
            }
            if not enabled:
                continue
            result.append(Source(
                enabled=enabled,
                country=raw.get("country", raw.get("region", "Беларусь")).strip(),
                country_code=raw.get("country_code", raw.get("region_code", "BY")).strip(),
                locality=raw.get("locality", "").strip(),
                rank=int(raw.get("rank", 1)),
                priority=raw.get("priority", "B").strip().upper(),
                name=raw["name"].strip(),
                media_type=raw.get("source_type", raw.get("media_type", "website")).strip().lower(),
                domain=raw["domain"].strip().lower(),
                start_url=raw["start_url"].strip(),
                language=raw.get("language", "ru").strip().lower(),
                adapter=raw.get("adapter", "standard").strip().lower(),
                query_hint=raw.get("query_hint", "").strip(),
                collection_hint=raw.get("collection_hint", "").strip(),
                access=raw.get("access", "").strip(),
                complexity=raw.get("complexity", "").strip(),
                feed_url=raw.get("feed_url", "").strip(),
                sitemap_url=raw.get("sitemap_url", "").strip(),
                telegram_url=str(raw.get("telegram_url") or "").strip(),
            ))
    return result


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        LOG.warning("Не удалось прочитать %s; создается новый файл.", path)
        return default


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


def endpoint_is_fresh(cache_entry: dict[str, Any], cache_days: int) -> bool:
    checked = parse_datetime(cache_entry.get("checked_at"))
    if not checked:
        return False
    return utc_now() - checked < dt.timedelta(days=cache_days)


def absolute_url(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href.strip())



def configured_endpoints(value: str) -> list[str]:
    """В sources.csv несколько адресов можно разделять символом |."""
    return [
        item.strip()
        for item in re.split(r"[|;]", value or "")
        if item.strip()
    ]


def discover_endpoints(
    source: Source,
    settings: dict[str, Any],
    cache: dict[str, Any],
    client: HttpClient,
) -> dict[str, Any]:
    cache_days = int(settings["discovery"].get("cache_days", 14))
    cached = cache.get(source.domain, {})
    profile = effective_source_profile(source, settings)

    # Explicit source rows remain authoritative, while a diagnosed profile can
    # supply exact publisher endpoints before the source is activated.
    configured_feeds = set(configured_endpoints(source.feed_url))
    configured_sitemaps = set(configured_endpoints(source.sitemap_url))
    profile_feeds = set(profile.get("feeds", ()))
    profile_sitemaps = set(profile.get("sitemaps", ()))
    profile_listings = list(profile.get("listing_pages", ()))

    if profile.get("exact_discovery"):
        return {
            "feeds": sorted(configured_feeds | profile_feeds)[:4],
            "sitemaps": sorted(configured_sitemaps | profile_sitemaps)[:8],
            "listing_pages": profile_listings[:4],
            "skip_homepage": bool(profile.get("skip_homepage", False)),
            "curated_profile": True,
        }

    cached_has_endpoints = bool(
        cached.get("feeds") or cached.get("sitemaps")
    )
    effective_cache_days = cache_days if cached_has_endpoints else min(cache_days, 1)

    if endpoint_is_fresh(cached, effective_cache_days):
        return {
            "feeds": sorted(
                configured_feeds | profile_feeds | set(cached.get("feeds", []))
            )[:4],
            "sitemaps": sorted(
                configured_sitemaps | profile_sitemaps | set(cached.get("sitemaps", []))
            )[:8],
            "listing_pages": profile_listings[:4],
            "skip_homepage": bool(profile.get("skip_homepage", False)),
            "curated_profile": bool(profile),
        }

    feeds: set[str] = set(configured_feeds | profile_feeds)
    sitemaps: set[str] = set(configured_sitemaps | profile_sitemaps)

    home = client.get(source.start_url)
    if home:
        soup = BeautifulSoup(home.content, "html.parser")
        for link in soup.select('link[rel~="alternate"][href]'):
            mime = (link.get("type") or "").lower()
            href = link.get("href", "")
            # OEmbed and API XML are not news RSS/Atom feeds.
            if "oembed" in href.lower() or "wp-json" in href.lower():
                continue
            if "rss" in mime or "atom" in mime or "xml" in mime:
                feeds.add(absolute_url(home.url, href))

    root = f"https://{source.domain}"
    robots = client.get(root + "/robots.txt")
    if robots:
        for line in robots.content.decode(
            robots.encoding or "utf-8",
            errors="replace",
        ).splitlines():
            if line.lower().startswith("sitemap:"):
                candidate = line.split(":", 1)[1].strip()
                if candidate:
                    sitemaps.add(candidate)

    for path in settings["discovery"].get("common_sitemap_paths", []):
        if len(sitemaps) >= 4:
            break
        url = root + path
        response = client.get(url)
        if response and (
            "xml" in response.headers.get("content-type", "").lower()
            or response.content.lstrip().startswith(b"<?xml")
            or b"<urlset" in response.content[:500].lower()
            or b"<sitemapindex" in response.content[:500].lower()
        ):
            sitemaps.add(response.url)

    if not feeds:
        for path in settings["discovery"].get("common_feed_paths", []):
            url = root + path
            response = client.get(url)
            if not response:
                continue
            if parse_feed_document(response.content):
                feeds.add(response.url)
                if len(feeds) >= 2:
                    break

    result: dict[str, Any] = {
        "feeds": sorted(feeds)[:4],
        "sitemaps": sorted(sitemaps)[:8],
        "listing_pages": profile_listings[:4],
        "skip_homepage": bool(profile.get("skip_homepage", False)),
        "curated_profile": bool(profile),
    }
    # Preserve the existing cache shape: only discovered feed/sitemap endpoints
    # are persisted. Listing profiles stay code-defined and are read-only.
    cache[source.domain] = {
        "feeds": result["feeds"],
        "sitemaps": result["sitemaps"],
        "checked_at": utc_now().isoformat(),
    }
    return result


def first_child_text(element: ET.Element, names: set[str]) -> str:
    for child in element.iter():
        if local_name(child.tag) in names and child.text:
            return normalize_space(repair_mojibake(child.text))
    return ""


def parse_feed_document(xml_text: str | bytes) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries: list[dict[str, str]] = []
    root_name = local_name(root.tag)

    if root_name == "rss":
        channel = next((child for child in root if local_name(child.tag) == "channel"), root)
        items = [child for child in channel if local_name(child.tag) == "item"]
        for item in items:
            entries.append({
                "title": first_child_text(item, {"title"}),
                "link": first_child_text(item, {"link", "guid"}),
                "summary": first_child_text(item, {"description", "summary", "content", "encoded"}),
                "published": first_child_text(item, {"pubdate", "published", "updated", "date"}),
            })
        return entries

    atom_entries = [child for child in root if local_name(child.tag) == "entry"]
    for entry in atom_entries:
        link = ""
        for child in entry:
            if local_name(child.tag) == "link":
                href = child.attrib.get("href", "").strip()
                rel = child.attrib.get("rel", "alternate").strip().lower()
                if href and rel in {"alternate", ""}:
                    link = href
                    break
                if href and not link:
                    link = href
        entries.append({
            "title": first_child_text(entry, {"title"}),
            "link": link or first_child_text(entry, {"link", "id"}),
            "summary": first_child_text(entry, {"summary", "content", "description"}),
            "published": first_child_text(entry, {"published", "updated", "date"}),
        })
    return entries


def candidate_from_feed_entry(
    source: Source,
    entry: dict[str, str],
    feed_url: str,
) -> Candidate | None:
    link = normalize_space(entry.get("link", ""))
    if link and not urllib.parse.urlsplit(link).scheme:
        link = absolute_url(feed_url, link)
    if not link or not is_source_article_url(link, source):
        return None
    published_value = entry.get("published", "")
    published = parse_datetime(published_value)
    return Candidate(
        source=source,
        url=canonicalize_url(link),
        title=normalize_space(repair_mojibake(entry.get("title", ""))),
        summary=normalize_space(
            BeautifulSoup(repair_mojibake(entry.get("summary", "")), "html.parser").get_text(" ")
        ),
        published_at=published.isoformat() if published else "",
        discovered_via=f"feed:{feed_url}",
    )


def collect_from_feed(
    source: Source,
    feed_url: str,
    client: HttpClient,
    cutoff: dt.datetime,
    limit: int,
) -> list[Candidate]:
    response = client.get(feed_url)
    if not response:
        return []
    entries = parse_feed_document(response.content)
    candidates: list[Candidate] = []
    for entry in entries[: limit * 2]:
        candidate = candidate_from_feed_entry(source, entry, feed_url)
        if not candidate:
            continue
        published = parse_datetime(candidate.published_at)
        if published and published < cutoff:
            continue
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap_document(xml_text: str | bytes) -> tuple[str, list[dict[str, str]]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "invalid", []
    kind = local_name(root.tag)
    records: list[dict[str, str]] = []
    for child in root:
        if local_name(child.tag) not in {"url", "sitemap"}:
            continue
        record: dict[str, str] = {}
        # Google News fields live one level deeper inside <news:news>.
        # Iterating recursively preserves the direct loc/lastmod fields and
        # also captures publication_date/title for admission and cutoff logic.
        for item in child.iter():
            if item is child:
                continue
            name = local_name(item.tag)
            if name in {"loc", "lastmod", "publication_date", "title"} and item.text:
                record[name] = normalize_space(repair_mojibake(item.text))
        if record.get("loc"):
            records.append(record)
    return kind, records


def collect_from_sitemap(
    source: Source,
    sitemap_url: str,
    client: HttpClient,
    cutoff: dt.datetime,
    limit: int,
    max_children: int,
) -> list[Candidate]:
    response = client.get(sitemap_url)
    if not response:
        return []
    kind, records = parse_sitemap_document(response.content)
    if kind == "sitemapindex":
        preferred = sorted(
            records,
            key=lambda x: (
                0 if "news" in x.get("loc", "").lower() else 1,
                0 if any(token in x.get("loc", "").lower()
                         for token in ("2026", "2025", "current", "latest")) else 1,
            ),
        )
        merged: list[Candidate] = []
        for record in preferred[:max_children]:
            merged.extend(collect_from_sitemap(
                source, record["loc"], client, cutoff,
                max(5, limit - len(merged)), 0
            ))
            if len(merged) >= limit:
                break
        return merged[:limit]

    # Sitemap order is not standardized: some publishers append new entries,
    # while others (including flagshtok.info) put them first. Inspect a bounded
    # head and tail, then sort dated entries explicitly newest-first.
    scan_size = max(limit * 8, 1000)
    if len(records) > scan_size:
        scan_records = [*records[:scan_size], *records[-scan_size:]]
    else:
        scan_records = list(records)
    unique_scan_records: list[dict[str, str]] = []
    seen_scan_urls: set[str] = set()
    for record in scan_records:
        record_url = canonicalize_url(record.get("loc", ""))
        if not record_url or record_url in seen_scan_urls:
            continue
        seen_scan_urls.add(record_url)
        unique_scan_records.append(record)
    dated_records: list[tuple[dt.datetime, dict[str, str]]] = []
    undated_records: list[dict[str, str]] = []
    for record in unique_scan_records:
        published = parse_datetime(
            record.get("lastmod") or record.get("publication_date")
        )
        if published:
            dated_records.append((published, record))
        else:
            undated_records.append(record)
    ordered_records = [
        record for _published, record in sorted(
            dated_records, key=lambda item: item[0], reverse=True
        )
    ] + undated_records

    candidates: list[Candidate] = []
    for record in ordered_records:
        url = canonicalize_url(record.get("loc", ""))
        if not url or not is_source_article_url(url, source):
            continue
        published = parse_datetime(record.get("lastmod") or record.get("publication_date"))
        if published and published < cutoff:
            continue
        candidates.append(Candidate(
            source=source,
            url=url,
            title=record.get("title", ""),
            published_at=published.isoformat() if published else "",
            discovered_via=f"sitemap:{sitemap_url}",
        ))
        if len(candidates) >= limit:
            break
    return candidates


def collect_from_listing_page(
    source: Source,
    listing_url: str,
    client: HttpClient,
    limit: int,
) -> list[Candidate]:
    response = client.get(listing_url)
    if not response:
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    candidates: list[Candidate] = []
    seen: set[str] = set()

    # Prefer links from news-like containers, then fall back to all anchors.
    links = soup.select(
        "article a[href], main a[href], "
        "h1 a[href], h2 a[href], h3 a[href], a[href]"
    )

    for link in links:
        href = normalize_space(link.get("href", ""))
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        url = canonicalize_url(absolute_url(response.url, href))
        if not url or url in seen:
            continue
        if not is_source_article_url(url, source):
            continue

        title = normalize_space(
            link.get_text(" ")
            or link.get("aria-label", "")
            or link.get("title", "")
        )
        if not title:
            image = link.select_one("img[alt]")
            if image:
                title = normalize_space(image.get("alt", ""))

        if len(title) < 16:
            parent_heading = link.find_parent(["h1", "h2", "h3", "h4"])
            if parent_heading:
                title = normalize_space(parent_heading.get_text(" "))

        if len(title) < 16:
            continue

        seen.add(url)
        via = (
            "homepage"
            if canonicalize_url(listing_url) == canonicalize_url(source.start_url)
            else f"listing:{listing_url}"
        )
        candidates.append(Candidate(
            source=source,
            url=url,
            title=title,
            discovered_via=via,
        ))
        if len(candidates) >= limit:
            break

    return candidates


def collect_from_homepage(
    source: Source,
    client: HttpClient,
    limit: int,
) -> list[Candidate]:
    return collect_from_listing_page(source, source.start_url, client, limit)


def telegram_username(url: str) -> str:
    return urllib.parse.urlsplit(url).path.strip("/").split("/")[0]


TELEGRAM_SERVICE_TITLE_WORDS = {
    "быстро", "срочно", "важно", "молния", "экстренно",
    "фото", "видео", "подробности", "обновлено", "live",
}


def telegram_line_is_service_only(value: str) -> bool:
    normalized = normalize_space(value).strip("#*_—–:;,.!¡?¿()[]{}")
    normalized = re.sub(r"[^0-9A-Za-zА-Яа-яЁёІіЎў]+", " ", normalized)
    words = [item.casefold() for item in normalized.split() if item]
    if not words:
        return True
    return len(words) <= 3 and all(word in TELEGRAM_SERVICE_TITLE_WORDS for word in words)


def telegram_title_from_text(text: str) -> str:
    raw = repair_mojibake(text or "").strip()
    if not raw:
        return "Telegram-публикация"
    lines = [normalize_space(line) for line in raw.splitlines() if normalize_space(line)]
    meaningful = [line for line in lines if not telegram_line_is_service_only(line)]
    candidate = meaningful[0] if meaningful else (lines[0] if lines else normalize_space(raw))
    first_sentence = split_sentences(candidate)
    if first_sentence:
        candidate = first_sentence[0]
    if len(candidate) > 180:
        candidate = candidate[:177].rsplit(" ", 1)[0].rstrip(" ,;:—–") + "…"
    return candidate or "Telegram-публикация"


def telegram_linked_article_url(
    wrap: Any, source: Source, fallback_url: str
) -> str:
    if source.adapter != "telegram_linked_site":
        return fallback_url
    expected = source.domain.lower().removeprefix("www.")
    for link in wrap.select("a[href]"):
        href = normalize_space(link.get("href", ""))
        if not href.startswith(("http://", "https://")):
            continue
        hostname = (urllib.parse.urlsplit(href).hostname or "").lower().removeprefix("www.")
        if hostname == expected or hostname.endswith("." + expected):
            cleaned = canonicalize_url(href)
            # Apply the same source profile used by feed/sitemap/homepage
            # discovery.  This keeps official Telegram inline text working,
            # but prevents a linked service URL (for example, a comments page)
            # from bypassing a domain guard.
            if is_source_article_url(cleaned, source):
                return cleaned
    return fallback_url


def collect_from_telegram(
    source: Source,
    client: HttpClient,
    cutoff: dt.datetime,
    limit: int,
) -> list[Candidate]:
    username = telegram_username(source.start_url)
    if not username:
        return []
    preview_url = f"https://t.me/s/{username}"
    response = client.get(preview_url)
    if not response:
        return []
    soup = BeautifulSoup(response.content, "html.parser")
    candidates: list[Candidate] = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        message = wrap.select_one(".tgme_widget_message")
        data_post = normalize_space(message.get("data-post", "") if message else "")
        if not data_post:
            continue
        text_node = wrap.select_one(".tgme_widget_message_text")
        if not text_node:
            continue
        raw_text = text_node.get_text("\n", strip=True)
        text = normalize_space(repair_mojibake(raw_text))
        if len(text) < 25:
            continue
        time_node = wrap.select_one("time[datetime]")
        published = parse_datetime(time_node.get("datetime") if time_node else "")
        if published and published < cutoff:
            continue
        telegram_post_url = canonicalize_url(f"https://t.me/{data_post}")
        candidates.append(Candidate(
            source=source,
            url=telegram_linked_article_url(wrap, source, telegram_post_url),
            title=telegram_title_from_text(raw_text),
            summary="",
            published_at=published.isoformat() if published else "",
            discovered_via=f"telegram:{preview_url}",
            inline_text=text,
            title_generated=True,
        ))
        if len(candidates) >= limit:
            break
    return candidates


def collect_from_telegram_fallback(
    source: Source,
    telegram_url: str,
    client: HttpClient,
    cutoff: dt.datetime,
    limit: int,
) -> list[Candidate]:
    """Collect an official Telegram fallback without changing source identity.

    The temporary linked-site adapter prefers the publisher's original article
    URL when the Telegram post contains one.  The returned candidate is then
    restored to the configured website source, while retaining Telegram's
    inline text so a blocked article page does not create an extraction loss.
    """
    fallback_source = replace(
        source,
        media_type="telegram",
        start_url=telegram_url,
        adapter="telegram_linked_site",
        feed_url="",
        sitemap_url="",
        telegram_url="",
    )
    return [
        replace(candidate, source=source)
        for candidate in collect_from_telegram(
            fallback_source, client, cutoff, limit
        )
    ]

def candidate_discovery_channels(candidate: Candidate) -> set[str]:
    channels: set[str] = set()
    for item in (candidate.discovered_via or "").split(" | "):
        value = item.strip().lower()
        if not value:
            continue
        if value == "homepage" or value.startswith("listing:"):
            channels.add("homepage")
        elif value.startswith("feed:"):
            channels.add("feed")
        elif value.startswith("sitemap:"):
            channels.add("sitemap")
        elif value.startswith("telegram:"):
            channels.add("telegram")
    return channels


def candidate_discovery_stages(candidate: Candidate) -> set[str]:
    """Exact discovery provenance for coverage; unlike legacy balancing, listing
    and homepage remain separate."""
    channels: set[str] = set()
    for item in (candidate.discovered_via or "").split(" | "):
        value = item.strip().lower()
        if not value:
            continue
        if value == "homepage":
            channels.add("homepage")
        elif value.startswith("listing:"):
            channels.add("listing")
        elif value.startswith("feed:"):
            channels.add("feed")
        elif value.startswith("sitemap:"):
            channels.add("sitemap")
        elif value.startswith("telegram:"):
            channels.add("telegram")
    return channels


def candidate_quality(candidate: Candidate) -> int:
    return (
        int(bool(candidate.title)) * 4
        + int(bool(candidate.summary)) * 3
        + int(bool(candidate.published_at)) * 3
        + int(bool(candidate.inline_text)) * 4
        + min(len(candidate.title or "") // 60, 2)
    )


_CANDIDATE_TITLE_STOPWORDS = {
    "а", "без", "бы", "был", "была", "были", "было", "в", "во", "для",
    "до", "его", "ее", "её", "и", "из", "или", "их", "к", "как", "на",
    "не", "но", "о", "об", "от", "по", "после", "при", "с", "со", "у",
    "что", "это", "этот", "эта", "эти", "ў", "і", "з", "на", "не", "па",
    "пра", "пасля", "для", "як", "што", "гэты", "гэта", "гэтыя",
}


def candidate_effective_date(candidate: Candidate) -> dt.datetime | None:
    """Best pre-fetch publication date without letting lastmod revive archives."""
    candidate_date = parse_datetime(candidate.published_at)
    url_date = extract_date_from_url(candidate.url)
    if url_date and candidate_date and url_date.date() < candidate_date.date():
        return url_date
    return candidate_date or url_date


def candidate_service_like(candidate: Candidate) -> bool:
    """Conservative soft penalty for routes that resemble controls, not stories."""
    parsed = urllib.parse.urlsplit(candidate.url)
    source_domain = normalized_domain(candidate.source.domain)
    host = normalized_domain(parsed.hostname or "")
    if source_domain == "onliner.by":
        # Onlíner's homepage mixes newsroom stories with marketplace pages.
        # These routes remain discoverable as a soft recovery tail, but must
        # not compete with dated/feed-backed editorial publications.
        if host in {
            "ab.onliner.by",
            "baraholka.onliner.by",
            "catalog.onliner.by",
            "forum.onliner.by",
            "go.onliner.by",
            "mb.onliner.by",
        }:
            return True
        if re.match(r"^/go(?:/|$)", parsed.path, flags=re.IGNORECASE):
            return True
        if re.match(
            r"^/(?:viewforum|viewtopic|fleamarketposting)\.php(?:/|$)",
            parsed.path,
            flags=re.IGNORECASE,
        ):
            return True
    if (
        source_domain == "slutsk-gorod.by"
        and re.match(
            r"^/obyavleniya-slutsk(?:/|$)",
            parsed.path,
            flags=re.IGNORECASE,
        )
    ):
        return True
    segments = [item.casefold() for item in parsed.path.split("/") if item]
    terminal = segments[-1] if segments else ""
    if terminal in {
        "comment", "comments", "lastcomments", "login", "logout", "register",
        "registration", "subscribe", "feedback", "contacts", "contact",
    }:
        return True
    if any(key.casefold() in {"replytocom", "comment-page"}
           for key, _value in urllib.parse.parse_qsl(parsed.query)):
        return True
    generic_title = normalize_space(candidate.title).casefold().strip(" .:—–-")
    return generic_title in {
        "комментарии", "каментары", "comments", "читать далее",
        "чытаць далей", "подробнее", "more",
    }


def candidate_admission_decision(
    candidate: Candidate,
    cutoff: dt.datetime | None = None,
) -> CandidateAdmissionDecision:
    channels = candidate_discovery_channels(candidate)
    effective_date = candidate_effective_date(candidate)
    service_like = candidate_service_like(candidate)
    if service_like:
        return CandidateAdmissionDecision(
            status="soft",
            reason="service_like_url",
            effective_date=effective_date,
            service_like=True,
        )
    if cutoff and effective_date and effective_date < cutoff:
        return CandidateAdmissionDecision(
            status="soft",
            reason="known_stale_url",
            effective_date=effective_date,
        )
    if effective_date or ("telegram" in channels and bool(candidate.inline_text)):
        return CandidateAdmissionDecision(
            status="fresh",
            reason="dated_or_inline_fresh_channel",
            effective_date=effective_date,
        )
    if channels - {"sitemap"}:
        return CandidateAdmissionDecision(
            status="current",
            reason="undated_current_channel",
        )
    return CandidateAdmissionDecision(
        status="soft",
        reason="undated_sitemap_only" if channels == {"sitemap"} else "unknown_route",
    )


def _candidate_title_tokens(candidate: Candidate) -> set[str]:
    return {
        token
        for token in re.findall(
            r"[0-9a-zа-яёіў]+",
            normalize_space(repair_mojibake(candidate.title)).casefold(),
        )
        if len(token) >= 3 and token not in _CANDIDATE_TITLE_STOPWORDS
    }


def candidates_are_same_telegram_story(
    left: Candidate,
    right: Candidate,
) -> bool:
    """Conservatively link an unlinked Telegram post to its publisher page."""
    if (
        left.source.name != right.source.name
        or normalized_domain(left.source.domain) != normalized_domain(right.source.domain)
    ):
        return False

    left_host = normalized_domain(urllib.parse.urlsplit(left.url).hostname or "")
    right_host = normalized_domain(urllib.parse.urlsplit(right.url).hostname or "")
    if left_host == "t.me" and same_site(right.url, right.source.domain):
        telegram_item, site_item = left, right
    elif right_host == "t.me" and same_site(left.url, left.source.domain):
        telegram_item, site_item = right, left
    else:
        return False
    if "telegram" not in candidate_discovery_channels(telegram_item):
        return False

    telegram_date = candidate_effective_date(telegram_item)
    site_date = candidate_effective_date(site_item)
    if not telegram_date or not site_date:
        return False
    if abs((telegram_date - site_date).total_seconds()) > 36 * 3600:
        return False

    left_tokens = _candidate_title_tokens(telegram_item)
    right_tokens = _candidate_title_tokens(site_item)
    smaller = min(len(left_tokens), len(right_tokens))
    if smaller < 5:
        return False
    shared = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    containment = shared / smaller if smaller else 0.0
    jaccard = shared / union if union else 0.0
    return shared >= 5 and containment >= 0.85 and jaccard >= 0.55


def _candidate_url_preference(candidate: Candidate) -> tuple[int, str]:
    host = normalized_domain(urllib.parse.urlsplit(candidate.url).hostname or "")
    source_domain = normalized_domain(candidate.source.domain)
    if source_domain != "t.me" and host != "t.me" and same_site(
        candidate.url, candidate.source.domain
    ):
        return (0, canonicalize_url(candidate.url))
    if host == "t.me":
        return (2, canonicalize_url(candidate.url))
    return (1, canonicalize_url(candidate.url))


def merge_candidate_records(current: Candidate, new: Candidate) -> Candidate:
    best, other = (
        (new, current)
        if candidate_quality(new) > candidate_quality(current)
        else (current, new)
    )
    provenance = sorted({
        item.strip()
        for value in (current.discovered_via, new.discovered_via)
        for item in (value or "").split(" | ")
        if item.strip()
    })
    title_source = max(
        (current, new),
        key=lambda item: (
            int(bool(item.title)),
            int(not item.title_generated),
            len(item.title or ""),
        ),
    )
    summary_source = max(
        (current, new),
        key=lambda item: len(item.summary or ""),
    )
    inline_source = max(
        (current, new),
        key=lambda item: len(item.inline_text or ""),
    )
    published_source = max(
        (current, new),
        key=lambda item: (
            int(bool(item.published_at)),
            parse_datetime(item.published_at).timestamp()
            if parse_datetime(item.published_at)
            else 0.0,
        ),
    )
    url_source = min((current, new), key=_candidate_url_preference)
    return Candidate(
        source=best.source,
        url=canonicalize_url(url_source.url or best.url or other.url),
        title=title_source.title,
        summary=summary_source.summary,
        published_at=published_source.published_at,
        discovered_via=" | ".join(provenance),
        inline_text=inline_source.inline_text,
        title_generated=title_source.title_generated,
    )


def deduplicate_candidates_with_stats(
    candidates: Iterable[Candidate],
) -> tuple[list[Candidate], int]:
    chosen: dict[str, Candidate] = {}
    telegram_site_duplicates = 0
    for candidate in candidates:
        key = canonicalize_url(candidate.url)
        if not key:
            continue
        normalized = Candidate(
            source=candidate.source,
            url=key,
            title=candidate.title,
            summary=candidate.summary,
            published_at=candidate.published_at,
            discovered_via=candidate.discovered_via,
            inline_text=candidate.inline_text,
            title_generated=candidate.title_generated,
        )
        current = chosen.get(key)
        if current is not None:
            current_channels = candidate_discovery_channels(current)
            new_channels = candidate_discovery_channels(normalized)
            if (
                ("telegram" in current_channels) != ("telegram" in new_channels)
                and bool((current_channels | new_channels) - {"telegram"})
            ):
                telegram_site_duplicates += 1
        chosen[key] = (
            normalized
            if current is None
            else merge_candidate_records(current, normalized)
        )
    merged = list(chosen.values())
    removed: set[int] = set()
    for index, item in enumerate(merged):
        if index in removed:
            continue
        host = normalized_domain(urllib.parse.urlsplit(item.url).hostname or "")
        if host != "t.me" or "telegram" not in candidate_discovery_channels(item):
            continue
        matches = [
            other_index
            for other_index, other in enumerate(merged)
            if other_index != index
            and other_index not in removed
            and candidates_are_same_telegram_story(item, other)
        ]
        # Ambiguity is deliberately preserved. Exact linked URLs were already
        # merged above; the title/date heuristic acts only on one clear match.
        if len(matches) != 1:
            continue
        other_index = matches[0]
        merged[other_index] = merge_candidate_records(merged[other_index], item)
        removed.add(index)
        telegram_site_duplicates += 1
    return (
        [item for index, item in enumerate(merged) if index not in removed],
        telegram_site_duplicates,
    )


def deduplicate_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    merged, _telegram_site_duplicates = deduplicate_candidates_with_stats(candidates)
    return merged


def candidate_admission_sort_key(
    candidate: Candidate,
    cutoff: dt.datetime | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    decision = candidate_admission_decision(candidate, cutoff)
    published = decision.effective_date
    timestamp = published.timestamp() if published else 0.0
    channels = candidate_discovery_channels(candidate)
    channel_rank = (
        0 if len(channels) >= 2
        else 1 if "telegram" in channels
        else 2 if "feed" in channels
        else 3 if "homepage" in channels
        else 4 if "sitemap" in channels
        else 5
    )
    prefilter_rank = 1
    # The thematic prefilter is only a tie-breaker inside the soft tail.
    # Computing it for fresh/current candidates cannot change their order and
    # made the global admission sort needlessly expensive.
    if settings is not None and decision.status == "soft":
        prefilter_rank = {
            "strong": 0, "possible": 1, "needs_text": 2,
        }.get(metadata_prefilter(candidate, settings).status, 2)
    soft_prefilter_rank = prefilter_rank if decision.status == "soft" else 0
    return (
        {"fresh": 0, "current": 1, "soft": 2}.get(decision.status, 3),
        int(decision.service_like),
        soft_prefilter_rank,
        -timestamp,
        channel_rank,
        -len(channels),
        -candidate_quality(candidate),
        candidate.url,
    )


_CORE33_RANKING_LEVELS = {
    "protected_title": 0,
    "fresh_strong": 1,
    "current_strong": 2,
    "fresh_possible": 3,
    "current_possible": 4,
    "fresh_needs_text": 5,
    "current_needs_text": 6,
    "soft_strong": 7,
    "soft_possible": 8,
    "soft_needs_text": 9,
}

_CORE33_TERM_PATTERN_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}


def _core33_term_pattern(terms: Iterable[str]) -> re.Pattern[str]:
    key = tuple(str(term) for term in terms if term)
    cached = _CORE33_TERM_PATTERN_CACHE.get(key)
    if cached is not None:
        return cached
    parts: list[str] = []
    for raw in key:
        needle = normalized_search_text(raw)
        if not needle:
            continue
        if needle.startswith("re:"):
            parts.append(f"(?:{needle[3:]})")
        elif needle.startswith("="):
            exact = re.escape(needle[1:])
            parts.append(
                rf"(?<![а-яёіўa-z0-9]){exact}(?![а-яёіўa-z0-9])"
            )
        elif " " in needle or "-" in needle:
            parts.append(re.escape(needle))
        else:
            parts.append(
                rf"(?<![а-яёіўa-z0-9]){re.escape(needle)}[а-яёіўa-z0-9]*"
            )
    try:
        pattern = re.compile("|".join(parts) if parts else r"(?!x)x")
    except re.error:
        # Optional project terms must never stop a monitoring run.
        pattern = re.compile(r"(?!x)x")
    _CORE33_TERM_PATTERN_CACHE[key] = pattern
    return pattern


def _core33_ranking_patterns(
    settings: dict[str, Any],
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Compile the two immutable ranking dictionaries once per ranking pass."""
    topic = settings.get("topic", {})
    category_terms = tuple(
        str(term)
        for values in topic.get("categories", {}).values()
        for term in values
        if term
    )
    problem_terms = tuple(
        str(term)
        for key in (
            "explicit_complaint_terms", "negative_condition_terms",
            "institutional_finding_terms", "persistence_terms",
        )
        for term in topic.get(key, [])
        if term
    )
    return (
        _core33_term_pattern(category_terms),
        _core33_term_pattern(problem_terms),
    )


def candidate_ranking_prefilter(
    candidate: Candidate,
    settings: dict[str, Any],
    patterns: tuple[re.Pattern[str], re.Pattern[str]] | None = None,
) -> MetadataPrefilterDecision:
    """Fast metadata signal used only to rank, never to reject, a URL."""
    title = normalized_search_text(repair_mojibake(candidate.title))
    summary = normalized_search_text(repair_mojibake(candidate.summary))
    combined = normalize_space(f"{title} {summary}")
    category_pattern, problem_pattern = (
        patterns if patterns is not None
        else _core33_ranking_patterns(settings)
    )
    has_topic = bool(category_pattern.search(combined))
    has_problem = bool(problem_pattern.search(combined))
    title_signal = bool(
        category_pattern.search(title) and problem_pattern.search(title)
    )
    if has_topic and has_problem:
        return MetadataPrefilterDecision(
            "strong", "topic and problem signal already present in metadata",
            title_signal,
        )
    if has_topic or has_problem:
        return MetadataPrefilterDecision(
            "possible", "partial metadata signal; body still required",
            title_signal,
        )
    return MetadataPrefilterDecision(
        "needs_text", "metadata is inconclusive; body remains eligible", False
    )


def candidate_integrity_flags(
    candidate: Candidate,
    admission: CandidateAdmissionDecision | None = None,
) -> tuple[str, ...]:
    """Diagnostic-only completeness flags; they never exclude a candidate."""
    admission = admission or candidate_admission_decision(candidate)
    channels = candidate_discovery_channels(candidate)
    flags: list[str] = []
    if not normalize_space(candidate.title):
        flags.append("missing_title")
    if candidate.title_generated:
        flags.append("generated_title")
    if not admission.effective_date:
        flags.append("undated")
    if len(channels) <= 1:
        flags.append("single_channel")
    if not normalize_space(candidate.summary) and not candidate.inline_text:
        flags.append("missing_summary")
    if admission.service_like:
        flags.append("service_like")
    if admission.status == "soft":
        flags.append("soft_admission")
    return tuple(flags)


def candidate_ranking_contract(
    candidate: Candidate,
    cutoff: dt.datetime | None,
    settings: dict[str, Any],
    patterns: tuple[re.Pattern[str], re.Pattern[str]] | None = None,
) -> CandidateRankingContract:
    admission = candidate_admission_decision(candidate, cutoff)
    prefilter = candidate_ranking_prefilter(candidate, settings, patterns)
    protected_title = bool(
        prefilter.title_signal
        and admission.status in {"fresh", "current"}
        and not admission.service_like
    )
    if protected_title:
        tier = "protected_title"
    else:
        admission_lane = (
            admission.status
            if admission.status in {"fresh", "current", "soft"}
            else "soft"
        )
        prefilter_lane = (
            prefilter.status
            if prefilter.status in {"strong", "possible", "needs_text"}
            else "needs_text"
        )
        tier = f"{admission_lane}_{prefilter_lane}"
    return CandidateRankingContract(
        candidate=candidate,
        canonical_url=canonicalize_url(candidate.url),
        admission_status=admission.status,
        prefilter_status=prefilter.status,
        protected_title_admission=protected_title,
        ranking_tier=tier,
        ranking_level=_CORE33_RANKING_LEVELS.get(tier, 99),
        source_priority=candidate.source.priority,
        effective_date=admission.effective_date,
        discovery_channels=tuple(sorted(candidate_discovery_channels(candidate))),
        integrity_flags=candidate_integrity_flags(candidate, admission),
    )


def candidate_ranking_contract_sort_key(
    contract: CandidateRankingContract,
) -> tuple[Any, ...]:
    timestamp = (
        contract.effective_date.timestamp() if contract.effective_date else 0.0
    )
    channels = set(contract.discovery_channels)
    channel_rank = (
        0 if len(channels) >= 2
        else 1 if "telegram" in channels
        else 2 if "feed" in channels
        else 3 if "homepage" in channels
        else 4 if "sitemap" in channels
        else 5
    )
    return (
        contract.ranking_level,
        priority_value(contract.source_priority),
        len(contract.integrity_flags),
        -timestamp,
        channel_rank,
        -len(channels),
        -candidate_quality(contract.candidate),
        contract.candidate.source.country,
        contract.candidate.source.rank,
        contract.canonical_url,
    )


def rank_candidates_core33(
    candidates: Iterable[Candidate],
    cutoff: dt.datetime | None,
    settings: dict[str, Any],
) -> tuple[list[Candidate], list[CandidateRankingContract]]:
    """Rank the complete deduplicated set without excluding any contract."""
    merged = deduplicate_candidates(candidates)
    patterns = _core33_ranking_patterns(settings)
    contracts = [
        candidate_ranking_contract(candidate, cutoff, settings, patterns)
        for candidate in merged
    ]
    contracts.sort(key=candidate_ranking_contract_sort_key)
    return [contract.candidate for contract in contracts], contracts


def candidate_ranking_tier_counts(
    contracts: Iterable[CandidateRankingContract],
) -> dict[str, int]:
    counts = {tier: 0 for tier in _CORE33_RANKING_LEVELS}
    for contract in contracts:
        counts[contract.ranking_tier] = counts.get(contract.ranking_tier, 0) + 1
    return counts


def candidate_collection_sort_key(candidate: Candidate) -> tuple[Any, ...]:
    return candidate_admission_sort_key(candidate)


def trim_channel_candidates(
    candidates: Iterable[Candidate],
    limit: int,
    cutoff: dt.datetime | None = None,
    settings: dict[str, Any] | None = None,
) -> list[Candidate]:
    if settings is not None:
        ordered, _contracts = rank_candidates_core33(candidates, cutoff, settings)
    else:
        ordered = deduplicate_candidates(candidates)
        ordered.sort(key=candidate_collection_sort_key)
    return ordered[:max(0, limit)]


def _normalized_channel_targets(
    candidates: list[Candidate],
    limit: int,
    reserves: dict[str, int],
) -> dict[str, int]:
    active = {
        channel
        for channel, reserve in reserves.items()
        if reserve > 0 and any(
            channel in candidate_discovery_channels(item) for item in candidates
        )
    }
    if not active or limit <= 0:
        return {}
    weights = {channel: max(1, reserves[channel]) for channel in active}
    total = sum(weights.values())
    if total <= limit:
        return weights

    exact = {channel: weights[channel] * limit / total for channel in active}
    targets = {channel: int(exact[channel]) for channel in active}
    if limit >= len(active):
        for channel in active:
            targets[channel] = max(1, targets[channel])
    order = {"telegram": 0, "feed": 1, "homepage": 2, "sitemap": 3}
    while sum(targets.values()) > limit:
        channel = max(
            (item for item in active if targets[item] > 1),
            key=lambda item: (targets[item] - exact[item], targets[item]),
        )
        targets[channel] -= 1
    while sum(targets.values()) < limit:
        channel = max(
            active,
            key=lambda item: (
                exact[item] - targets[item],
                -order.get(item, 9),
            ),
        )
        targets[channel] += 1
    return targets


def _select_candidate_lane(
    candidates: list[Candidate],
    limit: int,
    reserves: dict[str, int],
    cutoff: dt.datetime | None,
    settings: dict[str, Any] | None,
) -> list[Candidate]:
    if settings is not None:
        ordered, _contracts = rank_candidates_core33(
            candidates, cutoff, settings
        )
    else:
        ordered = sorted(
            deduplicate_candidates(candidates),
            key=lambda item: candidate_admission_sort_key(item, cutoff, settings),
        )
    targets = _normalized_channel_targets(ordered, limit, reserves)
    selected: list[Candidate] = []
    selected_urls: set[str] = set()
    channel_order = ("telegram", "feed", "homepage", "sitemap")
    for channel in channel_order:
        target = targets.get(channel, 0)
        current = sum(
            channel in candidate_discovery_channels(item) for item in selected
        )
        needed = max(0, target - current)
        for item in ordered:
            if len(selected) >= limit or needed <= 0:
                break
            key = canonicalize_url(item.url)
            if key in selected_urls:
                continue
            if channel not in candidate_discovery_channels(item):
                continue
            selected.append(item)
            selected_urls.add(key)
            needed -= 1
    for item in ordered:
        if len(selected) >= limit:
            break
        key = canonicalize_url(item.url)
        if key in selected_urls:
            continue
        selected.append(item)
        selected_urls.add(key)
    return selected


def soft_admission_tail_budget(
    candidates: Iterable[Candidate],
    limit: int,
    sitemap_reserve: int,
    cutoff: dt.datetime | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    merged = deduplicate_candidates(candidates)
    soft = [
        item for item in merged
        if candidate_admission_decision(item, cutoff).status == "soft"
    ]
    if not soft:
        return 0
    reliable_non_sitemap = sum(
        candidate_admission_decision(item, cutoff).status != "soft"
        and bool(candidate_discovery_channels(item) - {"sitemap"})
        for item in merged
    )
    cap_threshold = max(3, min(10, max(1, limit // 6)))
    if reliable_non_sitemap >= cap_threshold:
        # The tail ceiling used to be the raw (global, source-agnostic)
        # sitemap_reserve. It is now scaled per source via
        # soft_admission_budget_ceiling(); sitemap_reserve is kept as a
        # fallback only for callers that do not pass settings.
        soft_ceiling = (
            soft_admission_budget_ceiling(limit, settings)
            if settings is not None
            else max(0, sitemap_reserve)
        )
        remaining_after_trusted = max(0, limit - min(limit, len(merged) - len(soft)))
        return min(
            soft_ceiling,
            len(soft),
            remaining_after_trusted,
        )
    non_soft = len(merged) - len(soft)
    return min(len(soft), max(0, limit - min(limit, non_soft)))


def select_balanced_source_candidates(
    candidates: Iterable[Candidate],
    limit: int,
    feed_reserve: int = 20,
    homepage_reserve: int = 10,
    sitemap_reserve: int = 5,
    telegram_reserve: int | None = None,
    cutoff: dt.datetime | None = None,
    settings: dict[str, Any] | None = None,
    soft_overflow_limit: int = 0,
) -> list[Candidate]:
    """Apply a base budget, then a bounded headline-protected overflow."""
    if limit <= 0:
        return []

    merged = deduplicate_candidates(candidates)
    non_soft = [
        item for item in merged
        if candidate_admission_decision(item, cutoff).status != "soft"
    ]
    soft = [
        item for item in merged
        if candidate_admission_decision(item, cutoff).status == "soft"
    ]
    soft_budget = soft_admission_tail_budget(
        merged, limit, sitemap_reserve, cutoff, settings=settings
    )
    trusted_limit = max(0, limit - soft_budget)
    reserves = {
        "telegram": feed_reserve if telegram_reserve is None else telegram_reserve,
        "feed": feed_reserve,
        "homepage": homepage_reserve,
        "sitemap": sitemap_reserve,
    }
    selected = _select_candidate_lane(
        non_soft, trusted_limit, reserves, cutoff, settings
    )
    if settings is not None:
        soft_ordered, _soft_contracts = rank_candidates_core33(
            soft, cutoff, settings
        )
    else:
        soft_ordered = sorted(
            soft,
            key=lambda item: candidate_admission_sort_key(item, cutoff, settings),
        )
    # Service-like routes (ad redirects, marketplace/forum controls, etc.)
    # stay counted as "soft" for budget accounting, but must never be spent
    # from the tail budget themselves: they should not fill the extra
    # headroom that soft_admission_budget_ceiling() now allocates for
    # genuinely ambiguous (mostly undated_sitemap_only) candidates. Any
    # leftover soft budget is simply left unused rather than reaching into
    # known-junk routes.
    soft_admissible = [
        item for item in soft_ordered if not candidate_service_like(item)
    ]
    selected.extend(soft_admissible[:soft_budget])
    selected = deduplicate_candidates(selected)[:limit]

    # The configured source limit is a soft operating budget.  A fresh/current
    # candidate whose headline already contains both the monitored topic and a
    # concrete problem signal may overflow it, within a bounded ceiling.  Soft,
    # stale and service-like URLs are never eligible for this overflow.
    if settings is not None and soft_overflow_limit > 0:
        ordered, contracts = rank_candidates_core33(merged, cutoff, settings)
        protected_urls = {
            contract.canonical_url
            for contract in contracts
            if contract.protected_title_admission
        }
        selected_urls = {canonicalize_url(item.url) for item in selected}
        ceiling = limit + max(0, soft_overflow_limit)
        for item in ordered:
            if len(selected) >= ceiling:
                break
            key = canonicalize_url(item.url)
            if key in selected_urls or key not in protected_urls:
                continue
            selected.append(item)
            selected_urls.add(key)

    if settings is not None:
        selected, _selected_contracts = rank_candidates_core33(
            selected, cutoff, settings
        )
    else:
        selected.sort(
            key=lambda item: candidate_admission_sort_key(item, cutoff, settings)
        )
    return selected


def source_candidate_limit(source: Source, settings: dict[str, Any]) -> int:
    monitor = settings.get("monitor", {})
    default_limit = int(monitor.get("per_source_candidate_limit", 35))
    overrides = monitor.get("source_candidate_limits", {}) or {}
    for key in (source.name, source.domain, source.domain.removeprefix("www.")):
        value = overrides.get(key)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                LOG.warning("Некорректный лимит источника %s: %r", key, value)
    return max(1, default_limit)


def source_soft_overflow_limit(
    source: Source,
    settings: dict[str, Any],
) -> int:
    """Bounded Core 3.3 headroom above the unchanged configured base limit."""
    monitor = settings.get("monitor", {})
    base_limit = source_candidate_limit(source, settings)
    try:
        ratio = float(monitor.get("core33_soft_limit_ratio", 0.25))
    except (TypeError, ValueError):
        ratio = 0.25
    ratio = max(0.0, min(0.50, ratio))
    try:
        floor = max(0, int(monitor.get("core33_soft_limit_floor", 3)))
    except (TypeError, ValueError):
        floor = 3
    try:
        cap = max(0, int(monitor.get("core33_soft_limit_cap", 15)))
    except (TypeError, ValueError):
        cap = 15
    proportional = int(base_limit * ratio)
    if proportional < base_limit * ratio:
        proportional += 1
    return min(cap, max(floor, proportional)) if cap else 0


def soft_admission_budget_ceiling(
    limit: int,
    settings: dict[str, Any] | None,
) -> int:
    """Per-source ceiling for "soft"-status (mostly undated_sitemap_only)
    candidate admission, used by soft_admission_tail_budget() when the
    source already has a reliable non-sitemap flow covering its base
    budget (see reliable_non_sitemap there).

    Previously this ceiling was the single global
    discovery.sitemap_candidate_reserve value (default 5), applied
    identically to every source regardless of its own configured
    candidate limit. That silently capped high-volume, high-priority
    sources at just 5 admitted undated candidates per run even when
    their sitemap discovery produced dozens more, while smaller sources
    with the same fixed cap were barely affected. This scales the
    ceiling with the source's own per_source_candidate_limit /
    source_candidate_limits value, the same ratio/floor/cap pattern
    already used by source_soft_overflow_limit().
    """
    monitor = (settings or {}).get("monitor", {})
    try:
        ratio = float(monitor.get("soft_admission_ratio", 0.2))
    except (TypeError, ValueError):
        ratio = 0.2
    ratio = max(0.0, min(0.50, ratio))
    try:
        floor = max(0, int(monitor.get("soft_admission_floor", 5)))
    except (TypeError, ValueError):
        floor = 5
    try:
        cap = max(0, int(monitor.get("soft_admission_cap", 20)))
    except (TypeError, ValueError):
        cap = 20
    proportional = int(limit * ratio)
    if proportional < limit * ratio:
        proportional += 1
    return min(cap, max(floor, proportional)) if cap else 0


def channel_soft_scan_limit(limit: int) -> int:
    """Inspect a small tail before applying the per-channel base budget."""
    base = max(1, int(limit))
    return base + min(50, max(20, (base + 3) // 4))


def candidate_processing_capacity(
    sources: list[Source], settings: dict[str, Any]
) -> int:
    monitor = settings.get("monitor", {})
    configured = int(monitor.get("max_candidates_per_run", 4000))
    planned_count = max(0, int(monitor.get("planned_source_reserve", 0)))
    planned_limit = max(1, int(
        monitor.get(
            "planned_source_candidate_limit",
            monitor.get("per_source_candidate_limit", 35),
        )
    ))
    required = sum(
        source_candidate_limit(item, settings)
        + source_soft_overflow_limit(item, settings)
        for item in sources
    )
    planned_overflow = min(15, max(3, (planned_limit + 3) // 4))
    required += planned_count * (planned_limit + planned_overflow)
    rounded_required = ((required + 499) // 500) * 500
    return max(configured, rounded_required)


def collect_source_candidates(
    source: Source,
    settings: dict[str, Any],
    discovery_cache: dict[str, Any],
    cutoff: dt.datetime,
    recovery: RecoveryController | None = None,
    seen_urls: set[str] | None = None,
) -> tuple[list[Candidate], str | None, SourceCollectionMetrics]:
    discovery_started = time.perf_counter()
    client = HttpClient(settings)
    seen_urls = seen_urls or set()

    def client_observation_count() -> int:
        method = getattr(client, "observation_count", None)
        return int(method()) if callable(method) else 0

    def client_observations_since(index: int) -> tuple[HttpObservation, ...]:
        method = getattr(client, "observations_since", None)
        return tuple(method(index)) if callable(method) else ()

    def client_http_seconds() -> float:
        method = getattr(client, "http_seconds", None)
        return float(method()) if callable(method) else 0.0
    limit = source_candidate_limit(source, settings)
    source_overflow = source_soft_overflow_limit(source, settings)
    source_ceiling = limit + source_overflow
    discovery = settings.get("discovery", {})
    max_sitemaps = int(discovery.get("max_sitemaps_per_source", 5))
    channel_limit = max(
        limit,
        int(discovery.get("per_channel_candidate_limit", 100)),
    )
    channel_scan_ceiling = channel_soft_scan_limit(channel_limit)
    feed_reserve = int(discovery.get("feed_candidate_reserve", 20))
    homepage_reserve = int(discovery.get("homepage_candidate_reserve", 10))
    sitemap_reserve = int(discovery.get("sitemap_candidate_reserve", 5))
    telegram_reserve = int(discovery.get("telegram_candidate_reserve", feed_reserve))
    endpoint_stats = {
        "total": 0, "ok": 0, "failed": 0, "degraded": 0,
        "circuit_skipped": 0, "tail_probes": 0,
    }
    endpoint_observations: list[EndpointTelemetry] = []
    endpoint_discovery_seconds = 0.0

    def collect_endpoint(
        channel: str,
        endpoint: str,
        producer: Any,
    ) -> list[Candidate]:
        endpoint_stats["total"] += 1
        probe_mode = "normal"
        if recovery is not None:
            probe_mode = recovery.endpoint_decision(source, channel, endpoint)
            if probe_mode == "skip":
                endpoint_stats["circuit_skipped"] += 1
                endpoint_observations.append(EndpointTelemetry(
                    channel=channel,
                    endpoint=canonicalize_url(endpoint),
                    outcome="circuit_skipped",
                    failure_class="circuit_open",
                    probe_mode=probe_mode,
                ))
                return []
            if probe_mode == "tail_probe":
                endpoint_stats["tail_probes"] += 1
        observation_index = client_observation_count()
        endpoint_started = time.perf_counter()
        items = producer()
        endpoint_seconds = time.perf_counter() - endpoint_started
        new_observations = client_observations_since(observation_index)
        observation = None
        observer = getattr(client, "observation_for", None)
        if callable(observer):
            observation = observer(endpoint)
        if recovery is not None:
            health = recovery.record_endpoint(
                source, channel, endpoint, observation, len(items), probe_mode
            )
        else:
            health = "failed" if observation and observation.outcome != "ok" else "ok"
        endpoint_stats[health if health in endpoint_stats else "ok"] += 1
        if new_observations:
            endpoint_key = canonicalize_url(endpoint)
            for item in new_observations:
                endpoint_observations.append(EndpointTelemetry(
                    channel=channel,
                    endpoint=item.url,
                    outcome=item.outcome,
                    status_code=item.status_code,
                    failure_class=item.failure_class,
                    attempts=item.attempts,
                    seconds=item.seconds,
                    candidates=len(items) if item.url == endpoint_key else 0,
                    probe_mode=probe_mode,
                    detail=item.detail,
                ))
        else:
            endpoint_observations.append(EndpointTelemetry(
                channel=channel,
                endpoint=canonicalize_url(endpoint),
                outcome="ok" if health != "failed" else "failed",
                seconds=endpoint_seconds,
                candidates=len(items),
                probe_mode=probe_mode,
            ))
        return items

    try:
        if source.media_type == "telegram" or source.adapter == "telegram":
            username = telegram_username(source.start_url)
            preview_url = f"https://t.me/s/{username}" if username else source.start_url
            discovered = collect_endpoint(
                "telegram", preview_url,
                lambda: collect_from_telegram(
                    source, client, cutoff, source_ceiling
                ),
            )
            selected = select_balanced_source_candidates(
                discovered,
                limit,
                feed_reserve=feed_reserve,
                homepage_reserve=0,
                sitemap_reserve=0,
                telegram_reserve=telegram_reserve,
                cutoff=cutoff,
                settings=settings,
                soft_overflow_limit=source_overflow,
            )
            selected_contracts = [
                candidate_ranking_contract(item, cutoff, settings)
                for item in selected
            ]
            metrics = SourceCollectionMetrics(
                telegram_candidates=len(discovered),
                merged_candidates=len(discovered),
                selected_candidates=len(selected),
                selected_telegram=len(selected),
                selected_fresh=sum(
                    candidate_admission_decision(item, cutoff).status == "fresh"
                    for item in selected
                ),
                selected_current=sum(
                    candidate_admission_decision(item, cutoff).status == "current"
                    for item in selected
                ),
                selected_soft=sum(
                    candidate_admission_decision(item, cutoff).status == "soft"
                    for item in selected
                ),
                source_limit=limit,
                source_limit_hit=len(discovered) > limit,
                soft_limit_ceiling=source_ceiling,
                selected_overflow=max(0, len(selected) - limit),
                selected_protected_title=sum(
                    item.protected_title_admission for item in selected_contracts
                ),
                clipped_candidates=max(0, len(discovered) - len(selected)),
                endpoint_total=endpoint_stats["total"],
                endpoint_ok=endpoint_stats["ok"],
                endpoint_failed=endpoint_stats["failed"],
                endpoint_degraded=endpoint_stats["degraded"],
                endpoint_circuit_skipped=endpoint_stats["circuit_skipped"],
                endpoint_tail_probes=endpoint_stats["tail_probes"],
                discovery_seconds=time.perf_counter() - discovery_started,
                endpoint_http_seconds=client_http_seconds(),
                telegram_limit_hit=len(discovered) >= source_ceiling,
                endpoint_observations=tuple(endpoint_observations),
            )
            return selected, None, metrics

        endpoint_discovery_index = client_observation_count()
        endpoint_discovery_started = time.perf_counter()
        endpoints = discover_endpoints(source, settings, discovery_cache, client)
        endpoint_discovery_seconds = time.perf_counter() - endpoint_discovery_started
        for item in client_observations_since(endpoint_discovery_index):
            endpoint_observations.append(EndpointTelemetry(
                channel="endpoint_discovery",
                endpoint=item.url,
                outcome=item.outcome,
                status_code=item.status_code,
                failure_class=item.failure_class,
                attempts=item.attempts,
                seconds=item.seconds,
                detail=item.detail,
            ))

        feed_candidates: list[Candidate] = []
        for feed_url in endpoints["feeds"]:
            feed_candidates.extend(collect_endpoint(
                "feed", feed_url,
                lambda feed_url=feed_url: collect_from_feed(
                    source, feed_url, client, cutoff, channel_scan_ceiling
                ),
            ))
        feed_candidates = trim_channel_candidates(
            feed_candidates, channel_limit, cutoff, settings
        )

        sitemap_candidates: list[Candidate] = []
        for sitemap_url in endpoints["sitemaps"][:max_sitemaps]:
            sitemap_candidates.extend(collect_endpoint(
                "sitemap", sitemap_url,
                lambda sitemap_url=sitemap_url: collect_from_sitemap(
                    source, sitemap_url, client, cutoff,
                    channel_scan_ceiling, max_sitemaps
                ),
            ))
        sitemap_candidates = trim_channel_candidates(
            sitemap_candidates, channel_limit, cutoff, settings
        )

        listing_candidates: list[Candidate] = []
        for listing_url in endpoints.get("listing_pages", []):
            listing_candidates.extend(collect_endpoint(
                "listing", listing_url,
                lambda listing_url=listing_url: collect_from_listing_page(
                    source, listing_url, client, channel_scan_ceiling
                ),
            ))
        listing_candidates = trim_channel_candidates(
            listing_candidates, channel_limit, cutoff, settings
        )

        homepage_candidates: list[Candidate] = []
        if not endpoints.get("skip_homepage", False):
            homepage_candidates.extend(collect_endpoint(
                "homepage", source.start_url,
                lambda: collect_from_homepage(
                    source, client, channel_scan_ceiling
                ),
            ))
        homepage_candidates = trim_channel_candidates(
            homepage_candidates, channel_limit, cutoff, settings
        )

        telegram_candidates: list[Candidate] = []
        for telegram_url in configured_endpoints(source.telegram_url):
            username = telegram_username(telegram_url)
            preview_url = (
                f"https://t.me/s/{username}" if username else telegram_url
            )
            telegram_candidates.extend(collect_endpoint(
                "telegram", preview_url,
                lambda telegram_url=telegram_url: collect_from_telegram_fallback(
                    source, telegram_url, client, cutoff, channel_scan_ceiling
                ),
            ))
        telegram_candidates = trim_channel_candidates(
            telegram_candidates, channel_limit, cutoff, settings
        )

        merged, telegram_site_duplicates = deduplicate_candidates_with_stats([
            *feed_candidates,
            *sitemap_candidates,
            *listing_candidates,
            *homepage_candidates,
            *telegram_candidates,
        ])
        selected = select_balanced_source_candidates(
            merged,
            limit,
            feed_reserve=feed_reserve,
            homepage_reserve=homepage_reserve,
            sitemap_reserve=sitemap_reserve,
            telegram_reserve=telegram_reserve,
            cutoff=cutoff,
            settings=settings,
            soft_overflow_limit=source_overflow,
        )
        selected_keys = {canonicalize_url(item.url) for item in selected}
        clipped = [
            item for item in merged
            if canonicalize_url(item.url) not in selected_keys
        ]
        clipped_prefilters = [metadata_prefilter(item, settings) for item in clipped]
        clipped_dates = [
            parse_datetime(item.published_at) or extract_date_from_url(item.url)
            for item in clipped
        ]
        clipped_stage_sets = [candidate_discovery_stages(item) for item in clipped]
        selected_stage_sets = [candidate_discovery_stages(item) for item in selected]
        selected_admission = [
            candidate_admission_decision(item, cutoff) for item in selected
        ]
        clipped_admission = [
            candidate_admission_decision(item, cutoff) for item in clipped
        ]
        selected_contracts = [
            candidate_ranking_contract(item, cutoff, settings)
            for item in selected
        ]
        clipped_contracts = [
            candidate_ranking_contract(item, cutoff, settings)
            for item in clipped
        ]
        metrics = SourceCollectionMetrics(
            feed_candidates=len(feed_candidates),
            sitemap_candidates=len(sitemap_candidates),
            listing_candidates=len(listing_candidates),
            homepage_candidates=len(homepage_candidates),
            telegram_candidates=len(telegram_candidates),
            merged_candidates=len(merged),
            selected_candidates=len(selected),
            selected_feed=sum("feed" in channels for channels in selected_stage_sets),
            selected_sitemap=sum("sitemap" in channels for channels in selected_stage_sets),
            selected_listing=sum("listing" in channels for channels in selected_stage_sets),
            selected_homepage=sum("homepage" in channels for channels in selected_stage_sets),
            selected_telegram=sum("telegram" in channels for channels in selected_stage_sets),
            selected_fresh=sum(
                item.status == "fresh" for item in selected_admission
            ),
            selected_current=sum(
                item.status == "current" for item in selected_admission
            ),
            selected_soft=sum(
                item.status == "soft" for item in selected_admission
            ),
            soft_tail_budget=soft_admission_tail_budget(
                merged, limit, sitemap_reserve, cutoff, settings=settings
            ),
            clipped_soft=sum(
                item.status == "soft" for item in clipped_admission
            ),
            telegram_site_duplicates=telegram_site_duplicates,
            source_limit=limit,
            source_limit_hit=len(merged) > limit,
            soft_limit_ceiling=source_ceiling,
            selected_overflow=max(0, len(selected) - limit),
            selected_protected_title=sum(
                item.protected_title_admission for item in selected_contracts
            ),
            clipped_candidates=max(0, len(merged) - len(selected)),
            endpoint_total=endpoint_stats["total"],
            endpoint_ok=endpoint_stats["ok"],
            endpoint_failed=endpoint_stats["failed"],
            endpoint_degraded=endpoint_stats["degraded"],
            endpoint_circuit_skipped=endpoint_stats["circuit_skipped"],
            endpoint_tail_probes=endpoint_stats["tail_probes"],
            discovery_seconds=time.perf_counter() - discovery_started,
            endpoint_discovery_seconds=endpoint_discovery_seconds,
            endpoint_http_seconds=client_http_seconds(),
            feed_limit_hit=len(feed_candidates) >= channel_limit,
            sitemap_limit_hit=len(sitemap_candidates) >= channel_limit,
            listing_limit_hit=len(listing_candidates) >= channel_limit,
            homepage_limit_hit=len(homepage_candidates) >= channel_limit,
            telegram_limit_hit=len(telegram_candidates) >= channel_limit,
            clipped_fresh=sum(
                bool(value and value >= cutoff) for value in clipped_dates
            ),
            clipped_unseen=sum(
                canonicalize_url(item.url) not in seen_urls for item in clipped
            ),
            clipped_undated=sum(value is None for value in clipped_dates),
            clipped_prefilter_strong=sum(
                item.status == "strong" for item in clipped_prefilters
            ),
            clipped_prefilter_possible=sum(
                item.status == "possible" for item in clipped_prefilters
            ),
            clipped_prefilter_needs_text=sum(
                item.status == "needs_text" for item in clipped_prefilters
            ),
            clipped_protected_title=sum(
                item.protected_title_admission for item in clipped_contracts
            ),
            clipped_feed=sum("feed" in stages for stages in clipped_stage_sets),
            clipped_sitemap=sum("sitemap" in stages for stages in clipped_stage_sets),
            clipped_listing=sum("listing" in stages for stages in clipped_stage_sets),
            clipped_homepage=sum("homepage" in stages for stages in clipped_stage_sets),
            clipped_telegram=sum("telegram" in stages for stages in clipped_stage_sets),
            endpoint_observations=tuple(endpoint_observations),
        )
        return selected, None, metrics
    except Exception as exc:
        return [], f"{source.name}: {type(exc).__name__}: {exc}", SourceCollectionMetrics(
            source_limit=limit,
            soft_limit_ceiling=source_ceiling,
            endpoint_total=endpoint_stats["total"],
            endpoint_ok=endpoint_stats["ok"],
            endpoint_failed=max(1, endpoint_stats["failed"]),
            endpoint_degraded=endpoint_stats["degraded"],
            endpoint_circuit_skipped=endpoint_stats["circuit_skipped"],
            endpoint_tail_probes=endpoint_stats["tail_probes"],
            discovery_seconds=time.perf_counter() - discovery_started,
            endpoint_discovery_seconds=endpoint_discovery_seconds,
            endpoint_http_seconds=client_http_seconds(),
            endpoint_observations=tuple(endpoint_observations),
        )


def iter_json_ld_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                yield from iter_json_ld_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_json_ld_objects(nested)


def extract_date_from_url(url: str) -> dt.datetime | None:
    path = urllib.parse.urlsplit(url).path
    patterns = (
        r"(?<!\d)(20\d{2})[/-](0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])(?!\d)",
        r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)",
        # A number of Belarusian district sites use a DDMMYYYY directory as
        # the publication date, e.g. /28082026/<slug>/.  Restrict the match
        # to exactly eight digits so arbitrary numeric article IDs stay dates
        # only when their calendar components are valid.
        r"(?<!\d)(0?[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])(20\d{2})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, path)
        if not match:
            continue
        try:
            groups = match.groups()
            if len(groups) == 3 and groups[0].startswith("20"):
                year, month, day = groups
            else:
                day, month, year = groups
            return dt.datetime(int(year), int(month), int(day), tzinfo=UTC)
        except ValueError:
            continue
    return None


def extract_json_ld_data(
    soup: BeautifulSoup,
) -> tuple[str, str, dt.datetime | None]:
    best_title = ""
    best_body = ""
    published: dt.datetime | None = None

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        for obj in iter_json_ld_objects(payload):
            raw_type = obj.get("@type", "")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            type_names = {str(item).casefold() for item in types if item}
            article_like = (
                any(
                    "article" in item or "posting" in item
                    for item in type_names
                )
                or isinstance(obj.get("articleBody"), str)
            )
            if not article_like:
                continue

            body = obj.get("articleBody")
            if isinstance(body, str):
                cleaned_body = normalize_space(repair_mojibake(body))
                if len(cleaned_body) > len(best_body):
                    best_body = cleaned_body

            headline = obj.get("headline") or obj.get("name")
            if isinstance(headline, str) and len(headline.strip()) > len(best_title):
                best_title = normalize_space(repair_mojibake(headline))

            if published is None:
                for key in ("datePublished", "dateCreated", "uploadDate"):
                    parsed = parse_datetime(obj.get(key))
                    if parsed:
                        published = parsed
                        break

    return best_title, best_body, published


def embedded_text_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    decoded = html.unescape(value)
    if "<" in decoded and ">" in decoded:
        decoded = BeautifulSoup(decoded, "html.parser").get_text(" ")
    return normalize_space(repair_mojibake(decoded))


def extract_embedded_json_data(
    soup: BeautifulSoup,
) -> tuple[str, str, dt.datetime | None]:
    """Extract article payloads embedded by Next/CMS pages.

    This is intentionally used only by explicitly profiled sources. Generic
    pages can contain enormous navigation/state JSON, so enabling it globally
    would create false article bodies.
    """
    best_title = ""
    best_body = ""
    published: dt.datetime | None = None
    best_score = -1
    body_keys = (
        "articleBody", "articleContent", "fullText", "full_text",
        "body", "content", "text",
    )
    title_keys = ("headline", "title", "name")
    date_keys = (
        "datePublished", "publishedAt", "published_at", "published",
        "publishDate", "dateCreated", "date",
    )

    for script in soup.select('script[type="application/json"], script#__NEXT_DATA__'):
        raw = script.string or script.get_text()
        if not raw or len(raw) < 20:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        for obj in iter_json_ld_objects(payload):
            body = ""
            body_key = ""
            for key in body_keys:
                text = embedded_text_value(obj.get(key))
                if len(text) > len(body):
                    body = text
                    body_key = key
            if len(body) < 180:
                continue

            title = ""
            for key in title_keys:
                text = embedded_text_value(obj.get(key))
                if len(text) > len(title) and len(text) <= 500:
                    title = text

            item_published: dt.datetime | None = None
            for key in date_keys:
                item_published = parse_datetime(obj.get(key))
                if item_published:
                    break

            explicit_body = body_key in {
                "articleBody", "articleContent", "fullText", "full_text"
            }
            # Generic content/text/body keys are accepted only when the same
            # object also looks article-like through a headline or date.
            if not explicit_body and not (title or item_published):
                continue

            score = len(body) + (500 if explicit_body else 0) + (120 if title else 0)
            if score > best_score:
                best_score = score
                best_body = body
                best_title = title
                published = item_published

    return best_title, best_body, published


def extract_meta_published_at(soup: BeautifulSoup) -> dt.datetime | None:
    selectors = (
        'meta[property="article:published_time"]',
        'meta[property="og:published_time"]',
        'meta[name="article:published_time"]',
        'meta[name="datePublished"]',
        'meta[name="date"]',
        'meta[name="pubdate"]',
        'meta[name="publish-date"]',
        'meta[name="publication_date"]',
        'meta[name="dc.date"]',
        'meta[name="dcterms.date"]',
        '[itemprop="datePublished"][content]',
        'time[itemprop="datePublished"][datetime]',
        'article time[datetime]',
        'main time[datetime]',
    )
    for selector in selectors:
        element = soup.select_one(selector)
        if not element:
            continue
        value = (
            element.get("content")
            or element.get("datetime")
            or element.get_text(" ")
        )
        parsed = parse_datetime(value)
        if parsed:
            return parsed
    return None


def title_is_technical(value: str) -> bool:
    folded = normalized_search_text(value)
    if not folded:
        return True
    generic = {
        "news", "latest news", "home", "homepage", "новости",
        "последние новости", "главная", "главная страница",
    }
    if folded in generic:
        return True
    return bool(
        re.fullmatch(
            r"(главная страница|home|homepage|news|новости)\s*[—|:-].*",
            folded,
        )
    )


def paragraph_is_noise(paragraph: Any, value: str) -> bool:
    folded = normalized_search_text(value)
    if any(folded.startswith(prefix) for prefix in NOISE_TEXT_PREFIXES):
        return True

    anchor_text = normalize_space(
        " ".join(anchor.get_text(" ") for anchor in paragraph.select("a"))
    )
    if anchor_text and len(anchor_text) / max(1, len(value)) > 0.72:
        return True
    return False


def extract_paragraphs(container: Any) -> list[str]:
    paragraphs: list[str] = []
    seen: set[str] = set()
    for paragraph in container.select("p"):
        value = normalize_space(repair_mojibake(paragraph.get_text(" ")))
        if len(value) < 30 or value in seen:
            continue
        if paragraph_is_noise(paragraph, value):
            continue
        seen.add(value)
        paragraphs.append(value)
    return paragraphs



def extract_preclean_source_text(soup: BeautifulSoup, source: Source) -> str:
    """Extract source-specific article containers before global noise removal."""
    selectors = SOURCE_PRECLEAN_CONTENT_SELECTORS.get(source_domain_key(source), ())
    if not selectors:
        return ""

    for selector in selectors:
        for container in soup.select(selector):
            clone = BeautifulSoup(str(container), "html.parser")
            # Do not apply CONTENT_NOISE_SELECTORS here: Pozirk's article wrapper
            # itself contains "sidebar" in a layout class. Remove only bounded
            # nested service blocks that cannot be the article body.
            for tag in clone.select(
                "script, style, noscript, nav, footer, aside, form, iframe, "
                ".sidebar, .advertisement, .advert, .ads, "
                "[class*='related'], [class*='recommend'], "
                "[class*='share'], [class*='comment'], "
                "[class*='breadcrumb'], [class*='newsletter']"
            ):
                tag.decompose()

            paragraphs = extract_paragraphs(clone)
            text = normalize_space(" ".join(paragraphs))
            if len(text) >= 120:
                return text

            fallback = normalize_space(repair_mojibake(clone.get_text(" ")))
            fallback_minimum = (
                120 if source_domain_key(source) == "masheka.by" else 300
            )
            if len(fallback) >= fallback_minimum:
                return fallback
    return ""


def extract_source_specific_article_text(
    soup: BeautifulSoup,
    source: Source,
) -> str:
    """Run curated per-source selectors before generic JSON/DOM fallbacks."""
    preclean = extract_preclean_source_text(soup, source)
    if preclean:
        return clean_article_text(preclean)

    selectors = SOURCE_CONTENT_SELECTORS.get(source_domain_key(source), ())
    if not selectors:
        return ""

    clone = BeautifulSoup(str(soup), "html.parser")
    remove_content_noise(clone)

    checked_nodes: set[int] = set()
    for selector in selectors:
        for container in clone.select(selector):
            identity = id(container)
            if identity in checked_nodes:
                continue
            checked_nodes.add(identity)
            paragraphs = extract_paragraphs(container)
            text = normalize_space(" ".join(paragraphs))
            if len(text) >= 100 and (
                len(paragraphs) >= 2
                or len(text) >= 300
                or len(text) >= 120
            ):
                return clean_article_text(text)
            fallback = normalize_space(repair_mojibake(container.get_text(" ")))
            if len(fallback) >= 300:
                return clean_article_text(fallback)
    return ""


def extract_scored_article_text(soup: BeautifulSoup, source: Source) -> str:
    """Choose the strongest article container for profiled JS-heavy sources."""
    remove_content_noise(soup)

    selectors = SOURCE_CONTENT_SELECTORS.get(source.domain, ()) + ARTICLE_CONTENT_SELECTORS
    selector_text = ", ".join(dict.fromkeys([
        *selectors,
        "div[class*='article']", "div[class*='content']",
        "div[class*='post']", "div[class*='news']", "div[class*='story']",
        "div[class*='detail']", "div[id*='article']", "div[id*='content']",
    ]))
    pool: list[tuple[float, str]] = []
    checked_nodes: set[int] = set()
    for container in soup.select(selector_text)[:180]:
        identity = id(container)
        if identity in checked_nodes:
            continue
        checked_nodes.add(identity)
        paragraphs = extract_paragraphs(container)
        text = normalize_space(" ".join(paragraphs))
        if len(text) < 100:
            continue
        raw_text = normalize_space(container.get_text(" "))
        link_text = normalize_space(" ".join(a.get_text(" ") for a in container.select("a")))
        link_ratio = len(link_text) / max(1, len(raw_text))
        score = len(text) + len(paragraphs) * 110 - link_ratio * 1500
        if len(text) >= 250 or len(paragraphs) >= 2:
            pool.append((score, text))
    return max(pool, key=lambda item: item[0])[1] if pool else ""


def extract_main_article_text(soup: BeautifulSoup, source: Source) -> str:
    remove_content_noise(soup)

    checked_nodes: set[int] = set()
    selectors = SOURCE_CONTENT_SELECTORS.get(source.domain, ()) + ARTICLE_CONTENT_SELECTORS
    for selector in selectors:
        for container in soup.select(selector):
            identity = id(container)
            if identity in checked_nodes:
                continue
            checked_nodes.add(identity)

            paragraphs = extract_paragraphs(container)
            text = normalize_space(" ".join(paragraphs))
            if len(text) >= 100 and (
                len(paragraphs) >= 2
                or len(text) >= 300
                or (
                    selector not in {"main", "[role='main']"}
                    and len(paragraphs) == 1
                    and len(text) >= 120
                )
            ):
                return text

            if selector not in {"main", "[role='main']"}:
                fallback = normalize_space(repair_mojibake(container.get_text(" ")))
                if len(fallback) >= 300:
                    return fallback

    # Для прошедших диагностику адаптеров разрешён ограниченный поиск лучшего
    # контейнера. Боковые блоки предварительно удалены, а высокий процент ссылок
    # резко снижает оценку контейнера.
    if source.adapter in {"robust_article", "numeric_articles", "belsat_article", "protected_article"}:
        pool: list[tuple[float, str]] = []
        for container in soup.select(
            "main, article, div[class*='article'], div[class*='content'], "
            "div[class*='post'], div[class*='news'], div[class*='story'], "
            "div[class*='detail'], div[id*='article'], div[id*='content']"
        )[:140]:
            paragraphs = extract_paragraphs(container)
            text = normalize_space(" ".join(paragraphs))
            if len(text) < 250:
                continue
            raw_text = normalize_space(container.get_text(" "))
            link_text = normalize_space(" ".join(a.get_text(" ") for a in container.select("a")))
            link_ratio = len(link_text) / max(1, len(raw_text))
            score = len(text) + len(paragraphs) * 110 - link_ratio * 1500
            if score > 250:
                pool.append((score, text))
        if pool:
            return max(pool, key=lambda item: item[0])[1]

    # Нет fallback на все <p> страницы: он втягивает рекомендации и карточки.
    return ""


def clean_article_text(value: str) -> str:
    """Remove CMS markers that can leak into JSON-LD articleBody."""
    cleaned = normalize_space(repair_mojibake(value))

    # Aktuality and some other CMS templates may glue the technical
    # bannerBox token directly to the first real word.
    cleaned = re.sub(
        r"^\s*bannerbox(?=[^\W\d_])",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Plain-text JSON-LD can contain inline "see also" cards. Remove the
    # labelled recommendation sentence without touching normal article text.
    recommendation_labels = (
        "ZOBACZ",
        "CZYTAJ TAKŻE",
        "SPRAWDŹ TAKŻE",
        "SEE ALSO",
        "READ ALSO",
        "ЧИТАЙТЕ ТАКЖЕ",
        "ЧИТАЙТЕ ТАКОЖ",
        "ЧЫТАЙЦЕ ТАКСАМА",
        "ЧИТАЙТЕ ЕЩЁ",
        "СМОТРИТЕ ТАКЖЕ",
    )
    label_pattern = "|".join(re.escape(item) for item in recommendation_labels)
    cleaned = re.sub(
        rf"(?:(?<=^)|(?<=[.!?…。！？])\s+)"
        rf"(?:{label_pattern})\s*:\s*[^.!?…。！？]{{1,260}}[.!?…。！？]",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return normalize_space(cleaned)

def extract_article_from_html(
    candidate: Candidate,
    html_content: str | bytes,
) -> ArticleExtraction:
    soup = BeautifulSoup(html_content, "html.parser")
    candidate_title = normalize_space(repair_mojibake(candidate.title))
    profile = source_adapter_profile(candidate.source)

    # Architecture Core 1 extraction cascade:
    # source-specific -> embedded JSON -> JSON-LD -> generic HTML.
    # Belsat keeps its tested "prefer largest reliable container" policy so the
    # refactor cannot replace a fuller body with a shorter teaser.
    source_body = extract_source_specific_article_text(soup, candidate.source)
    json_title, json_body, json_published = extract_json_ld_data(soup)
    embedded_title = ""
    embedded_body = ""
    embedded_published: dt.datetime | None = None
    if profile.get("embedded_json"):
        embedded_title, embedded_body, embedded_published = extract_embedded_json_data(soup)

    page_title = ""
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        page_title = normalize_space(repair_mojibake(og["content"]))
    if not page_title:
        headline = soup.select_one("h1")
        if headline:
            page_title = normalize_space(repair_mojibake(headline.get_text(" ")))
    if not page_title and embedded_title:
        page_title = embedded_title
    if not page_title and json_title:
        page_title = json_title
    if not page_title and soup.title:
        page_title = normalize_space(repair_mojibake(soup.title.get_text(" ")))

    title = candidate_title
    if page_title and not title_is_technical(page_title):
        title = page_title
    elif not title and embedded_title:
        title = embedded_title
    elif not title and json_title:
        title = json_title

    generic_body = ""
    strategy = ""
    text = ""

    if profile.get("prefer_largest_container"):
        generic_body = extract_scored_article_text(
            BeautifulSoup(html_content, "html.parser"),
            candidate.source,
        )
        body_options = [
            ("source_specific", clean_article_text(source_body)),
            ("embedded_json", clean_article_text(embedded_body)),
            ("json_ld", clean_article_text(json_body)),
            ("generic_html", clean_article_text(generic_body)),
        ]
        body_options = [(name, value) for name, value in body_options if value]
        if body_options:
            strategy, text = max(body_options, key=lambda item: len(item[1]))
    else:
        if source_body:
            strategy, text = "source_specific", clean_article_text(source_body)
        elif embedded_body:
            strategy, text = "embedded_json", clean_article_text(embedded_body)
        elif json_body:
            strategy, text = "json_ld", clean_article_text(json_body)
        else:
            generic_body = extract_main_article_text(
                BeautifulSoup(html_content, "html.parser"),
                candidate.source,
            )
            if generic_body:
                strategy, text = "generic_html", clean_article_text(generic_body)

    descriptions: list[str] = []
    for selector in (
        'meta[property="og:description"]',
        'meta[name="description"]',
        'meta[name="twitter:description"]',
    ):
        node = soup.select_one(selector)
        if node and node.get("content"):
            descriptions.append(
                normalize_space(repair_mojibake(node.get("content", "")))
            )
    if candidate.summary:
        descriptions.append(normalize_space(repair_mojibake(candidate.summary)))
    metadata_summary = max(descriptions, key=len, default="")

    # Preserve the current update-24 behavior: protected/Belsat sources may use
    # a sufficiently informative official description when the body is thin.
    # The strategy is now explicit in telemetry so a later Degraded Mode can
    # stop auto-promotion without mixing that policy change into this refactor.
    if candidate.source.adapter in {"belsat_article", "protected_article"}:
        if len(text) < 180 and len(metadata_summary) >= 80:
            text = clean_article_text(metadata_summary)
            strategy = "metadata_description"

    published = embedded_published or json_published
    date_source = (
        "embedded-json" if embedded_published
        else "json-ld" if json_published
        else ""
    )
    if published is None:
        published = extract_meta_published_at(soup)
        if published:
            date_source = "meta"

    return ArticleExtraction(
        title=title,
        text=text,
        # Debug must retain the amount of HTML that production received.
        # This distinguishes a blank/blocked response from selector failure.
        html_length=len(html_content),
        published_at=published.isoformat() if published else "",
        date_source=date_source,
        metadata_summary=metadata_summary,
        extraction_strategy=strategy or "empty",
        failure_stage="" if text else "extraction_empty",
    )



def source_mirror_urls(candidate: Candidate) -> list[str]:
    """Build diagnosed mirror URLs while keeping the canonical report URL."""
    profile = source_adapter_profile(candidate.source)
    mirrors = profile.get("mirror_domains", ())
    parsed = urllib.parse.urlsplit(candidate.url)
    urls: list[str] = []
    for mirror in mirrors:
        host = normalize_space(str(mirror)).strip("/")
        if not host:
            continue
        urls.append(urllib.parse.urlunsplit(
            (parsed.scheme or "https", host, parsed.path, parsed.query, "")
        ))
    return urls


def source_amp_urls(
    candidate: Candidate,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    """Build a diagnosed publisher AMP URL without changing the report URL."""
    profile = profile or source_adapter_profile(candidate.source)
    suffix = normalize_space(str(profile.get("amp_suffix", "")))
    if not suffix:
        return []
    parsed = urllib.parse.urlsplit(candidate.url)
    path = parsed.path or "/"
    normalized_suffix = "/" + suffix.strip("/") + "/"
    if path.rstrip("/").lower().endswith(normalized_suffix.rstrip("/").lower()):
        return []
    amp_path = path.rstrip("/") + normalized_suffix
    return [urllib.parse.urlunsplit((
        parsed.scheme or "https",
        parsed.netloc,
        amp_path,
        parsed.query,
        "",
    ))]


def feed_metadata_extraction(
    candidate: Candidate,
    profile: dict[str, Any],
) -> ArticleExtraction | None:
    """Use an official RSS lead only for an explicitly diagnosed source.

    This is intentionally not a generic metadata mode.  A profile must opt in,
    the candidate must actually come from a feed, and the publisher-supplied
    lead must be long enough to pass ordinary relevance and excerpt logic.
    """
    if not profile.get("feed_metadata_fallback"):
        return None
    if "feed:" not in (candidate.discovered_via or "").lower():
        return None
    summary = clean_article_text(candidate.summary)
    minimum = max(40, int(profile.get("feed_metadata_min_chars", 80)))
    if len(summary) < minimum:
        return None
    return ArticleExtraction(
        title=repair_mojibake(candidate.title),
        text=summary,
        published_at=candidate.published_at,
        date_source="feed",
        metadata_summary=summary,
        extraction_strategy="feed_summary",
        transport="feed_metadata",
        transport_status="ok",
        transport_status_code=200,
    )


def render_belsat_article_html(
    url: str,
    settings: dict[str, Any],
) -> str:
    """Render one Belsat article in Chromium only when static HTML is too thin.

    Playwright is imported lazily so non-Belsat sources keep
    exactly the same dependencies until Belsat is explicitly activated.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError:
        LOG.warning(
            "Белсат: нужен Playwright для JS-render fallback; "
            "статический результат оставлен без изменений."
        )
        return ""

    request_timeout = int(settings.get("monitor", {}).get("request_timeout_seconds", 18))
    navigation_timeout_ms = max(10_000, min(30_000, request_timeout * 1000 + 8_000))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(locale="ru-RU")
                page = context.new_page()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms,
                )
                if response is not None and response.status >= 400:
                    return ""
                try:
                    page.wait_for_load_state("networkidle", timeout=8_000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1_500)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        LOG.warning("Белсат: JS-render fallback не сработал для %s: %s", url, exc)
        return ""


def attach_access_cost(
    extracted: ArticleExtraction,
    client: HttpClient,
    extraction_seconds: float,
    chromium_seconds: float,
    chromium_attempts: int,
) -> ArticleExtraction:
    """Attach passive timing data without changing extraction decisions."""
    http_seconds_method = getattr(client, "http_seconds", None)
    http_attempts_method = getattr(client, "http_attempts", None)
    observations_method = getattr(client, "observations_since", None)
    extracted.http_seconds = (
        float(http_seconds_method()) if callable(http_seconds_method) else 0.0
    )
    extracted.extraction_seconds = max(0.0, extraction_seconds)
    extracted.chromium_seconds = max(0.0, chromium_seconds)
    extracted.chromium_attempts = max(0, chromium_attempts)
    extracted.http_attempts = (
        int(http_attempts_method()) if callable(http_attempts_method) else 0
    )
    extracted.http_observations = (
        tuple(observations_method(0)) if callable(observations_method) else ()
    )
    return extracted


def extract_article(
    candidate: Candidate,
    settings: dict[str, Any],
    recovery: RecoveryController | None = None,
) -> ArticleExtraction:
    if candidate.inline_text:
        return ArticleExtraction(
            title=repair_mojibake(candidate.title),
            text=repair_mojibake(candidate.inline_text),
            published_at=candidate.published_at,
            date_source="telegram",
            extraction_strategy="inline_text",
            transport="telegram_inline",
            transport_status="ok",
            transport_status_code=200,
        )

    client = HttpClient(settings)
    profile = effective_source_profile(candidate.source, settings)
    response = None
    transport = ""
    circuit_skipped = False
    last_observation: HttpObservation | None = None
    extraction_seconds = 0.0
    chromium_seconds = 0.0
    chromium_attempts = 0

    transport_order = profile.get("transport_order", ["requests"])

    def request_amp_fallback() -> tuple[Any | None, HttpObservation | None, bool]:
        skipped = False
        observation: HttpObservation | None = None
        decision = (
            recovery.transport_decision(candidate.source, "amp")
            if recovery else "normal"
        )
        if decision == "skip":
            return None, None, True
        for amp_url in source_amp_urls(candidate, profile):
            amp_response = client.get(amp_url, retries=0)
            observation = client.observation_for(amp_url)
            if recovery:
                recovery.record_transport(
                    candidate.source,
                    "amp",
                    bool(amp_response),
                    observation.status_code if observation else 0,
                    observation.failure_class if observation else "",
                    decision,
                )
            if amp_response:
                return amp_response, observation, skipped
        return None, observation, skipped

    if "requests" in transport_order:
        decision = recovery.transport_decision(candidate.source, "requests") if recovery else "normal"
        if decision == "skip":
            circuit_skipped = True
        else:
            response = client.get(candidate.url, retries=1)
            last_observation = client.observation_for(candidate.url)
            if recovery:
                recovery.record_transport(
                    candidate.source,
                    "requests",
                    bool(response),
                    last_observation.status_code if last_observation else 0,
                    last_observation.failure_class if last_observation else "",
                    decision,
                )
            if response:
                transport = "requests"

    if not response and "official_mirror" in transport_order:
        decision = recovery.transport_decision(candidate.source, "official_mirror") if recovery else "normal"
        if decision == "skip":
            circuit_skipped = True
        else:
            for mirror_url in source_mirror_urls(candidate):
                response = client.get(mirror_url, retries=0)
                last_observation = client.observation_for(mirror_url)
                if recovery:
                    recovery.record_transport(
                        candidate.source,
                        "official_mirror",
                        bool(response),
                        last_observation.status_code if last_observation else 0,
                        last_observation.failure_class if last_observation else "",
                        decision,
                    )
                if response:
                    transport = "official_mirror"
                    break

    if not response and "amp" in transport_order:
        amp_response, amp_observation, amp_skipped = request_amp_fallback()
        circuit_skipped = circuit_skipped or amp_skipped
        if amp_observation:
            last_observation = amp_observation
        if amp_response:
            response = amp_response
            transport = "amp"

    # For Belsat, Chromium is also a recovery transport when the static HTTP
    # request itself failed/circuit-opened, not only when static HTML is thin.
    if (
        not response
        and "chromium" in transport_order
    ):
        decision = recovery.transport_decision(candidate.source, "chromium") if recovery else "normal"
        if decision == "skip":
            circuit_skipped = True
        else:
            chromium_attempts += 1
            chromium_started = time.perf_counter()
            rendered_html = render_belsat_article_html(candidate.url, settings)
            chromium_seconds += time.perf_counter() - chromium_started
            if recovery:
                recovery.record_transport(
                    candidate.source, "chromium", bool(rendered_html),
                    200 if rendered_html else 0,
                    "" if rendered_html else "render_failed", decision,
                )
            if rendered_html:
                extraction_started = time.perf_counter()
                rendered = extract_article_from_html(candidate, rendered_html)
                extraction_seconds += time.perf_counter() - extraction_started
                rendered.transport = "chromium"
                rendered.transport_status = "ok"
                rendered.transport_status_code = 200
                rendered.transport_circuit_skipped = circuit_skipped
                return attach_access_cost(
                    rendered, client, extraction_seconds,
                    chromium_seconds, chromium_attempts,
                )

    if not response:
        feed_fallback = feed_metadata_extraction(candidate, profile)
        if feed_fallback:
            feed_fallback.transport_circuit_skipped = circuit_skipped
            return attach_access_cost(
                feed_fallback, client, extraction_seconds,
                chromium_seconds, chromium_attempts,
            )
        failed = ArticleExtraction(
            title=repair_mojibake(candidate.title),
            text="",
            metadata_summary=normalize_space(repair_mojibake(candidate.summary)),
            extraction_strategy="empty",
            transport=transport or (
                transport_order[0] if transport_order else "requests"
            ),
            transport_status="failed",
            transport_status_code=last_observation.status_code if last_observation else 0,
            transport_failure_class=last_observation.failure_class if last_observation else (
                "circuit_open" if circuit_skipped else "unknown"
            ),
            transport_circuit_skipped=circuit_skipped,
            failure_stage="transport_failed",
        )
        return attach_access_cost(
            failed, client, extraction_seconds,
            chromium_seconds, chromium_attempts,
        )

    extraction_started = time.perf_counter()
    extracted = extract_article_from_html(candidate, response.content)
    extraction_seconds += time.perf_counter() - extraction_started
    extracted.transport = transport or "requests"
    extracted.transport_status = "ok"
    extracted.transport_status_code = 200
    extracted.transport_circuit_skipped = circuit_skipped

    if (
        not extracted.text
        and transport != "amp"
        and "amp" in transport_order
    ):
        amp_response, amp_observation, amp_skipped = request_amp_fallback()
        extracted.transport_circuit_skipped = (
            extracted.transport_circuit_skipped or amp_skipped
        )
        if amp_response:
            extraction_started = time.perf_counter()
            amp_extracted = extract_article_from_html(
                candidate, amp_response.content
            )
            extraction_seconds += time.perf_counter() - extraction_started
            if amp_extracted.text:
                amp_extracted.transport = "amp"
                amp_extracted.transport_status = "ok"
                amp_extracted.transport_status_code = 200
                amp_extracted.transport_circuit_skipped = (
                    extracted.transport_circuit_skipped
                )
                return attach_access_cost(
                    amp_extracted, client, extraction_seconds,
                    chromium_seconds, chromium_attempts,
                )

    if not extracted.text:
        feed_fallback = feed_metadata_extraction(candidate, profile)
        if feed_fallback:
            feed_fallback.transport_circuit_skipped = (
                extracted.transport_circuit_skipped
            )
            return attach_access_cost(
                feed_fallback, client, extraction_seconds,
                chromium_seconds, chromium_attempts,
            )

    chromium_threshold = int(profile.get("chromium_threshold", 250))
    if (
        "chromium" in transport_order
        and len(extracted.text) < chromium_threshold
    ):
        decision = recovery.transport_decision(candidate.source, "chromium") if recovery else "normal"
        if decision == "skip":
            extracted.transport_circuit_skipped = True
        else:
            chromium_attempts += 1
            chromium_started = time.perf_counter()
            rendered_html = render_belsat_article_html(candidate.url, settings)
            chromium_seconds += time.perf_counter() - chromium_started
            if recovery:
                recovery.record_transport(
                    candidate.source, "chromium", bool(rendered_html),
                    200 if rendered_html else 0,
                    "" if rendered_html else "render_failed", decision,
                )
            if rendered_html:
                extraction_started = time.perf_counter()
                rendered = extract_article_from_html(candidate, rendered_html)
                extraction_seconds += time.perf_counter() - extraction_started
                if len(rendered.text) > len(extracted.text):
                    rendered.transport = "chromium"
                    rendered.transport_status = "ok"
                    rendered.transport_status_code = 200
                    rendered.transport_circuit_skipped = extracted.transport_circuit_skipped
                    return attach_access_cost(
                        rendered, client, extraction_seconds,
                        chromium_seconds, chromium_attempts,
                    )

    return attach_access_cost(
        extracted, client, extraction_seconds,
        chromium_seconds, chromium_attempts,
    )



def contains_any(text: str, terms: Iterable[str]) -> bool:
    return bool(find_terms(text, terms))


def economic_direction_signal(text: str) -> bool | None:
    """False = clearly favourable economic outcome; True = adverse outcome."""
    value = normalized_search_text(text)
    if not value:
        return None
    if re.search(r"(?:реальн[а-яёіў]*\s+доход[а-яёіў]*.*сниз|не\s+хвата[а-яёіў]*\s+денег|безработ(?:иц|н)[а-яёіў]*.*(?:вырос|увелич)|тариф[а-яёіў]*.*вырос|цен[а-яёіў]*\s+вырос.*сильнее|цен[а-яёіў]*.*(?:выше|сильнее).*зарплат)", value):
        return True
    if re.search(r"(?:покупательн[а-яёіў]*\s+способност[а-яёіў]*.*(?:выше|увелич|вырос)|рост\s+цен.*(?:ниже|меньше).*рост[а-яёіў]*\s+зарплат|реальн[а-яёіў]*\s+доход[а-яёіў]*.*(?:подскоч|вырос)|безработиц[а-яёіў]*.*(?:рекордн[а-яёіў]*\s+минимум|сниз))", value):
        return False
    return None


def has_unreversed_negative_outcome(text: str) -> bool:
    return economic_direction_signal(text) is True


def term_present(text: str, term: str) -> bool:
    folded = normalized_search_text(text)
    needle = normalized_search_text(term)
    if not folded or not needle:
        return False
    if needle.startswith("re:"):
        try:
            return bool(re.search(needle[3:], folded, flags=re.IGNORECASE))
        except re.error:
            return False
    if needle.startswith("="):
        exact = needle[1:]
        return bool(re.search(
            rf"(?<![а-яёіўa-z0-9]){re.escape(exact)}(?![а-яёіўa-z0-9])",
            folded,
        ))
    if " " in needle or "-" in needle:
        return needle in folded
    # Однословные элементы словарей — это, как правило, основы слов.
    # Они должны начинаться с границы слова: «жалу» → «жалуются», но
    # «яма» не должна срабатывать внутри слова «прямая».
    return bool(re.search(
        rf"(?<![а-яёіўa-z0-9]){re.escape(needle)}[а-яёіўa-z0-9]*",
        folded,
    ))


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    return [term for term in terms if term and term_present(text, str(term))]


TERM_DISPLAY_LABELS = {
    "re:(?<![а-яёіўa-z0-9])дорог(?:а|и|у|е|ам|ами|ах)(?![а-яёіўa-z0-9])": "дорога",
    "re:(?<![а-яёіўa-z0-9])очеред(?:ь|и|ей|ью|ям|ями|ях)(?![а-яёіўa-z0-9])": "очередь",
    "re:(?<![а-яёіўa-z0-9])чарг(?:а|і|ой|у|амі|ах)(?![а-яёіўa-z0-9])": "чарга",
    "re:(?<![а-яёіўa-z0-9])фонар(?:ь|я|ю|ём|ем|и|ей|ям|ями|ях)(?![а-яёіўa-z0-9])": "фонарь",
}


def display_term(term: str) -> str:
    """Return a readable report label instead of an internal regex."""
    return TERM_DISPLAY_LABELS.get(term, term[1:] if term.startswith("=") else term)


def split_sentences(text: str) -> list[str]:
    raw = repair_mojibake(text or "").replace("\r", "\n")
    if not raw.strip():
        return []
    parts = re.split(r"(?<=[.!?…。！？])\s+|\n+", raw)
    return [normalize_space(part) for part in parts if len(normalize_space(part)) >= 8]


def category_hits(text: str, categories: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        category: hits
        for category, terms in categories.items()
        if (hits := find_terms(text, terms))
    }


def sentence_has_denial(text: str, settings: dict[str, Any]) -> bool:
    if contains_any(text, settings["topic"].get("denial_patterns", [])):
        return True
    folded = normalized_search_text(text)
    # A complaint stem followed by an explicit negation must not become a
    # positive public signal (for example, "жаловаться не собираемся").
    return bool(re.search(
        r"(?:жалова[а-яёіўa-z0-9]*\s+не\s+(?:собира|буд|стал)|"
        r"никто\s+не\s+жалова|без\s+жалоб)",
        folded,
    ))


@dataclass(frozen=True)
class RelevanceDecision:
    relevant: bool
    category: str = ""
    subcategory: str = ""
    signal_type: str = ""
    score: int = 0
    official_response: bool = False
    title_signal: bool = False
    evidence_indices: tuple[int, ...] = ()
    matched_terms: tuple[str, ...] = ()
    reason: str = ""


RELEVANCE_PRECISION_TERMS: dict[str, tuple[str, ...]] = {
    # Editorial genres that repeatedly produced false positives in the
    # 10--12 August baseline.  These are intentionally genre markers rather
    # than source or section bans: the same sections also publish valid
    # complaints and findings.
    "sports": (
        "футбольн", "хоккейн", "матч", "чемпионат", "турнир",
        "проигрывает", "полтайма", "сборной", "мирового спорта",
        "спартыўн", "матч", "чэмпіянат", "турнір",
    ),
    "hobby_entertainment": (
        "для прикола", "собирает 50 000 монет", "собирает 50 тысяч монет",
        "собирает 500 рублей монетами", "парад планет",
        "астрономическ событи", "космическое шоу", "слоны устроили",
        "редкой лягушки", "битва помидорами",
    ),
    "commercial_explainer": (
        "где купить", "кредитный калькулятор", "калькулятор кредита",
        "калькулятор",
        "считаем, сколько придется платить по кредиту",
        "считаем, сколько придётся платить по кредиту",
        "цифровые меню-борды", "меню на экране в общепите",
        "обзор belgee", "история яркого belgee",
    ),
    "listing_nouns": (
        "дом", "квартир", "ресторан", "бар", "бизнес", "недвижимост",
        "коттедж", "усадьб", "дач",
    ),
    "listing_actions": (
        "продают", "продается", "продаётся", "выставили на продажу",
        "купить", "скупают", "прадаюць", "прадаецца", "купіць",
    ),
    "health_advice": (
        "железо у вегетарианцев", "продукт для поднятия гемоглобина",
        "продукты для поднятия гемоглобина", "как поднять гемоглобин",
        "продукты для гемоглобина", "гемоглобин", "правил питания",
        "правилах питания",
    ),
    "historical": (
        "дневник подпольщ", "история могилева", "история могилёва",
        "часть i", "часть ii", "часть iii", "1941 год",
        "однажды в могилеве", "однажды в могилёве", "из былого",
    ),
    "neutral_infrastructure": (
        "завершают строительство", "движение откроют",
        "принимаются дополнительные меры", "планируют повысить надёжность",
        "планируют повысить надежность", "повышению надежности",
        "повышению надёжности", "расширят просеки возле лэп",
        "аварийных отключений станет меньше",
    ),
    "planned_service_notice": (
        "могут возникнуть проблемы с банковскими картами",
        "возможны перебои с банковскими картами",
        "плановые технические работы", "регламентные работы",
        "предупредили о повышенном уровне шума",
    ),
    "routine_monitoring": (
        "проходит массовая проверка", "проводят массовую проверку",
        "проверяют на цены, качество и документы",
        "проверили станции техосмотра",
    ),
    "external_trade_balance": (
        "дефицит внешней торговли", "дефицит торговли промышленной",
        "дефицит экспорта", "внешнеторговый дефицит",
    ),
    "financial_market": (
        "валютный рынок", "курс доллара", "курс евро", "курс рубля",
        "валютный прогноз", "инвестиционный прогноз",
    ),
    "neutral_education_advice": (
        "пора собирать чемоданы в общежитие", "приемные кампании закрыты",
        "приёмные кампании закрыты", "как подготовиться к общежитию",
    ),
    "opinion_debate": (
        "нормально ли объявлять сборы", "два противоположных мнения",
        "благотворительность или рынок человеческих потребностей",
    ),
    "recreation_advice": (
        "перед поездкой на белорусские мальдивы", "меловые карьеры",
    ),
    "generic_greeting": (
        "доброе утро", "добры ранак", "добры дзень",
    ),
    "neutral_household_advice": (
        "сэкономить на коммуналке", "сэкономить на коммунальных",
        "как уменьшить коммунальные платежи", "в машине пахнет сыростью",
        "виноват кондиционер",
    ),
    "foreign_military_crime": (
        "российского бизнесмена", "расійскага бізнэсмена",
        "некачественной тушенки", "няякаснай тушонкі", "тушенки на сво",
        "тушонки для армии", "тушонкі для арміі",
    ),
    "personal_health_incident": (
        "попал в реанимацию после курения вейпа",
        "отравился после курения вейпа", "отравление вейпом",
        "сделали новую операцию на сердце", "сделали операцию на сердце",
        "выписан домой после улучшения состояния",
    ),
    "neighbour_dispute": (
        "соседская собака лает", "собака лает ночами",
        "куда жаловаться на соседскую собаку",
    ),
    "neutral_reporting_channel": (
        "смогут сообщать о проблемах онлайн",
        "куда обращаться молодым специалистам",
    ),
    "routine_enforcement": (
        "лишила минчанина статуса водителя", "после работы спецкомплекса",
        "гаи выявила нарушителя", "гаи задержала нарушителя",
    ),
    "routine_rescue": (
        "спасатели ликвидировали", "ликвидировали почти 60 гнезд",
        "шершни атаковали",
    ),
    "single_incident": (
        "подрались в очереди", "подрались под офисом", "драка в очереди",
        "произошла потасовка", "водительская принципиальность",
        "а вы бы тоже не пустили", "ее сбил", "её сбил",
    ),
    "human_interest_profile": (
        "жизнь глазами", "о первых рабочих буднях",
        "рассказал выпускник медколледжа",
    ),
    "neutral_benefit_explainer": (
        "как использовать семейный капитал",
        "напомнили, как многодетные семьи могут оплатить обучение",
        "оплатить обучение в вузе или колледже с помощью семейного капитала",
    ),
    "multi_story_digest": (
        "відэанавіны:", "видеоновости:", "відэанавіны /", "видеоновости /",
    ),
    "neutral_positive_feature": (
        "своими силами обновили памятное место",
        "прыйшлі на дапамогу хлебаробам", "пришли на помощь хлеборобам",
    ),
    "obituary": (
        "умер ", "умерла ", "скончался", "скончалась", "памёр ",
        "памерла ",
    ),
    "foreign_non_belarus": (
        "румыни", "чили", "чилийск", "в москве", "в россии",
    ),
    "actual_findings": (
        "выявили", "выявлено", "обнаружили", "обнаружено", "изъяли",
        "установили нарушение", "нашли просроч", "оштрафовал",
        "закрыли магазин", "вынесли предписание",
    ),
}


# Result Integrity 1.3 is a single genre/evidence gate.  It handles broad
# editorial forms before the older narrow precision rules, so new false
# positives do not require unrelated checks to be scattered through the
# relevance function.  These patterns describe genres, not source sections.
RESULT_INTEGRITY_GENRE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    # Core 3.6 treats editorial intent as a first-class signal.  These broad
    # forms repeatedly carry social vocabulary without reporting a concrete
    # public problem.  They remain source-agnostic and evidence-aware.
    "medical_or_psychology_advice": tuple(re.compile(value) for value in (
        r"(?:врач|невролог|офтальмолог|психолог|психотерапевт)[а-яёіў]*.*"
        r"(?:рассказал|назвал|объяснил|совет|симптом|распознат|почему)",
        r"(?:как\s+распознат|симптом[а-яёіў]*.*нельзя\s+игнорироват|"
        r"почему\s+нам.*интересн[а-яёіў]*\s+чуж[а-яёіў]*.*(?:жизн|конфликт))",
        r"головн[а-яёіў]*\s+бол[а-яёіў]*.*(?:магнитн|геомагнитн)[а-яёіў]*\s+бур",
    )),
    # Consumer/lifestyle explainers can contain vocabulary such as
    # «дефицит», «школа» or «работа», but do not document a public failure.
    "lifestyle_advice_or_ranking": tuple(re.compile(value) for value in (
        r"(?:что\s+давать|что\s+положить).*перекус[а-яёіў]*.*школ",
        r"скрыт[а-яёіў]*\s+симптом[а-яёіў]*\s+дефицит[а-яёіў]*\s+магни",
        r"жир\s+при\s+похудени[а-яёіў]*.*(?:мышц|куда\s+он\s+уход)",
        r"топ[- ]?\d+\s+ваканси[а-яёіў]*",
    )),
    "educational_simulation": tuple(re.compile(value) for value in (
        r"(?:учат|обучают).{0,55}(?:работать|пользоваться).{0,70}"
        r"(?:умн[а-яёіў]*\s+)?сч[её]тчик",
        r"учебн[а-яёіў]*\s+(?:стенд|центр|пространств).{0,100}"
        r"(?:моделир|имитац).{0,70}(?:нештатн|аварийн)",
    )),
    "positive_local_update": tuple(re.compile(value) for value in (
        r"когда\s+щомысл[а-яёіў]*\s+примет.*дожинк",
        r"(?:готов[а-яёіў]*|подготовк[а-яёіў]*).*дожинк",
        r"в\s+ходе\s+строительств[а-яёіў]*.*(?:вл[- ]?330|березовск[а-яёіў]*\s+грэс)",
    )),
    "positive_public_infrastructure_opening": tuple(re.compile(value) for value in (
        r"(?:открыва[а-яёіў]*|откро[а-яёіў]*|начнет\s+функциониров)[а-яёіў]*"
        r".{0,90}(?:подземк[а-яёіў]*|подземн[а-яёіў]*\s+переход)",
        # Belarusian: "адкрывае/адкрые/пачне функцыянаваць" + "падземка/
        # падземны пераход" — different roots from the Russian forms above.
        r"(?:адкрыва[а-яёіў]*|адкрые[а-яёіў]*|пачне\s+функцыянава[а-яёіў]*)"
        r".{0,90}(?:падземк[а-яёіў]*|падземн[а-яёіў]*\s+перахо[а-яёіў]*)",
    )),
    "aggregate_credit_debt_statistic": tuple(re.compile(value) for value in (
        r"просроченн[а-яёіў]*\s+задолженност[а-яёіў]*.{0,90}"
        r"(?:госсектор|государственн[а-яёіў]*\s+сектор|кредит)",
        r"(?:госсектор|государственн[а-яёіў]*\s+сектор).{0,90}"
        r"просроченн[а-яёіў]*\s+задолженност[а-яёіў]*",
        # Belarusian-language mirrors use different roots, not just different
        # endings ("пратэрмінаваны" != "просроченный", "запазычанасць" !=
        # "задолженность", "дзяржсектар" != "госсектор"), so the [а-яёіў]
        # suffix class above cannot bridge them. See report-29 leak: the
        # same Pozirk story passed as /be/news/201827 while /ru/news/201825
        # was correctly rejected.
        r"пратэрмінаван[а-яёіў]*\s+запазычанасц[а-яёіў]*.{0,90}"
        r"(?:дзяржсектар[а-яёіў]*|дзяржаўн[а-яёіў]*\s+сектар[а-яёіў]*|крэдыт[а-яёіў]*)",
        r"(?:дзяржсектар[а-яёіў]*|дзяржаўн[а-яёіў]*\s+сектар[а-яёіў]*).{0,90}"
        r"пратэрмінаван[а-яёіў]*\s+запазычанасц[а-яёіў]*",
    )),
    "personal_foreign_profile": tuple(re.compile(value) for value in (
        r"сам[а-яёіў]*\s+красив[а-яёіў]*\s+стран[а-яёіў]*\s+в\s+мире.*где\s+я\s+побывал",
        r"(?:отпуск|отдых).*чили.*(?:it|айти|работ)",
    )),
    "benefit_or_application_explainer": tuple(re.compile(value) for value in (
        r"^(?:когда\s+и\s+куда|куда\s+и\s+когда)\s+обращат[а-яёіў]*\s+за\s+пособ",
        r"^кто\s+.*(?:может|сможет|имеет\s+право).*(?:улучшит[а-яёіў]*\s+жиль|"
        r"пособ|льгот|господдерж)",
        r"(?:кому\s+положен|как\s+получит|порядок\s+(?:назначени|обращени)).*"
        r"(?:пособ|льгот|господдерж)",
    )),
    "scheduled_service_notice": tuple(re.compile(value) for value in (
        r"^на\s+каких\s+(?:улиц|адрес)[а-яёіў]*.*не\s+будет\s+"
        r"(?:электричеств|электроэнерги|вод[а-яёіў]*|свет[а-яёіў]*)",
        r"(?:планов[а-яёіў]*|регламентн[а-яёіў]*).*"
        r"(?:отключ|приостанов|не\s+будет).*(?:электр|вод|газ|тепл)",
        r"(?:электроснабж|водоснабж|газоснабж)[а-яёіў]*\s+будет\s+отсутствоват.*"
        r"(?:работ[а-яёіў]*\s+на|ремонт)",
        r"без\s+(?:вод[а-яёіў]*|свет[а-яёіў]*|электричеств[а-яёіў]*)\s+"
        r"на\s+время\s+(?:планов[а-яёіў]*\s+)?работ",
        r"(?:предупреждени|предупредил)[а-яёіў]*.*(?:отключ|без\s+вод[а-яёіў]*)",
    )),
    "positive_medical_achievement": tuple(re.compile(value) for value in (
        r"впервые\s+(?:в\s+беларус[а-яёіў]*\s+)?(?:имплантировал|установил|провел)[а-яёіў]*.*"
        r"(?:клапан|имплант|операц)",
        r"(?:новейш|уникальн|перв[а-яёіў]*\s+подобн)[а-яёіў]*.*"
        r"(?:сердечн[а-яёіў]*\s+клапан|операц|методик)",
    )),
    "neutral_service_launch": tuple(re.compile(value) for value in (
        r"(?:с\s+\d{1,2}\s+[а-яёіў]+\s+\d{4}\s+года\s+)?"
        r"(?:в\s+беларус[а-яёіў]*\s+)?заработает\s+сервис.*(?:поиск|найти).*репетитор",
        r"запуска[а-яёіў]*\s+(?:проект|сервис).*"
        r"(?:репетитор|логопед|музыкальн[а-яёіў]*\s+руководител)",
    )),
    "neutral_regulatory_explainer": tuple(re.compile(value) for value in (
        r"^кого\s+теперь\s+будут\s+направлят[а-яёіў]*\s+в\s+соцучреждени[а-яёіў]*.*"
        r"нов[а-яёіў]*\s+правил",
        r"вступа[а-яёіў]*\s+в\s+силу\s+изменени[а-яёіў]*.*"
        r"(?:социальн[а-яёіў]*\s+пансионат|центр[а-яёіў]*\s+социальн[а-яёіў]*\s+реабилитац)",
        r"(?:начал[а-яёіў]*\s+действоват|действуют).*нов[а-яёіў]*\s+правил[а-яёіў]*.*"
        r"(?:направлени[а-яёіў]*\s+в\s+соцучреждени|социальн[а-яёіў]*\s+пансионат)",
    )),
    "routine_status_explainer": tuple(re.compile(value) for value in (
        r"не\s+работаете\s+после\s+увольнени[а-яёіў]*.*(?:баз[а-яёіў]*\s+)?тунеядц",
        r"как\s+изменит[а-яёіў]*\s+коммуналк[а-яёіў]*.*сдач[а-яёіў]*\s+квартир[а-яёіў]*.*тунеядц",
    )),
    "career_explainer": tuple(re.compile(value) for value in (
        r"^кто\s+такие\s+программист[а-яёіў]*\s+1с\s+и\s+почему\s+они\s+нужны",
        r"^професси[а-яёіў]*\s+программист[а-яёіў]*\s+1с.*(?:чем\s+занима|почему\s+нуж)",
    )),
    "foreign_residency_facilitation": tuple(re.compile(value) for value in (
        r"кипр[а-яёіў]*.*(?:разрешил|начал\s+выдават)[а-яёіў]*.*"
        r"вид[а-яёіў]*\s+на\s+жительств[а-яёіў]*.*просроченн[а-яёіў]*\s+паспорт",
        r"вид[а-яёіў]*\s+на\s+жительств[а-яёіў]*.*кипр[а-яёіў]*.*"
        r"просроченн[а-яёіў]*\s+паспорт",
    )),
    "general_policy_statement": tuple(re.compile(value) for value in (
        r"борьб[а-яёіў]*\s+с\s+некачественн[а-яёіў]*\s+импорт[а-яёіў]*.*"
        r"будет\s+только\s+ужесточат",
        r"(?:правительств|премьер)[а-яёіў]*.*(?:будет|намерен)[а-яёіў]*.*"
        r"ужесточ[а-яёіў]*\s+(?:контрол|борьб)[а-яёіў]*.*импорт",
    )),
    "routine_law_enforcement_procedure": tuple(re.compile(value) for value in (
        r"следч[а-яёіў]*\s+комитет[а-яёіў]*\s+начал[а-яёіў]*\s+спецпроизводств",
        r"следч[а-яёіў]*\s+камітэт[а-яёіў]*\s+пачаў\s+спецвытворчасц",
    )),
    "positive_income_comparison": tuple(re.compile(value) for value in (
        r"рост\s+(?:потребительск[а-яёіў]*\s+)?цен.*(?:ниже|меньше).*рост[а-яёіў]*\s+зарплат",
        r"покупательн[а-яёіў]*\s+способност[а-яёіў]*.*(?:увеличил|вырос|стала\s+выше)",
        r"зарплат[а-яёіў]*\s+(?:росл|вырос)[а-яёіў]*\s+(?:быстрее|выше).*инфляц",
    )),
    "cultural_or_migration_commentary": tuple(re.compile(value) for value in (
        r"как\s+себя\s+вести\s+в\s+беларус[а-яёіў]*",
        r"памятк[а-яёіў]*.*(?:переезжающ|переехал)[а-яёіў]*.*беларус",
        r"почему\s+белорус[а-яёіў]*\s+не\s+люб[а-яёіў]*.*(?:турист|россиян)",
        r"почему\s+беларус[а-яёіў]*\s+не\s+люб[а-яёіў]*.*(?:турыст|расіян)",
    )),
    "resolved_minor_emergency": tuple(re.compile(value) for value in (
        r"(?:самостоятельно|самастойна)\s+ликвидировал[а-яёіў]*.*"
        r"(?:тлени|загорани|пожар)",
        r"пострадавш[а-яёіў]*\s+нет.*(?:риск|угроз)[а-яёіў]*\s+"
        r"(?:загорани|пожар)[а-яёіў]*\s+(?:нет|отсутств)",
    )),
    "protocol_or_personnel": tuple(re.compile(value) for value in (
        r"(?:аккредитовал|назначил|представил)[а-яёіў]*\s+нов[а-яёіў]*\s+"
        r"(?:военн[а-яёіў]*\s+)?(?:атташе|посл[а-яёіў]*|руководител|директор)",
        r"(?:обсудил|обсудили|рассмотрел|рассмотрели)[а-яёіў]*\s+вопрос[а-яёіў]*.*"
        r"(?:с\s+работник|на\s+встрече|в\s+ходе\s+встречи)",
        r"(?:состоял[а-яёіў]*\s+встреч|провел[а-яёіў]*\s+встреч).*"
        r"(?:обсудил|обсуждал|вопрос[а-яёіў]*)",
        r"(?:направил|накірава)[а-яёіў]*.*(?:обсудит|абмяркоўва)[а-яёіў]*.*"
        r"(?:сотрудничеств|супрац)[а-яёіў]*",
    )),
    "preventive_service_expansion": tuple(re.compile(value) for value in (
        r"смогут\s+проверит[а-яёіў]*\s+риск[а-яёіў]*.*(?:диспансеризац|скрининг)",
        r"во\s+время\s+(?:планов[а-яёіў]*\s+)?(?:диспансеризац|скрининг).*"
        r"(?:провер|обследован|выяв)",
        r"(?:нов[а-яёіў]*|расширен[а-яёіў]*).*"
        r"(?:диспансеризац|скрининг|профилактическ[а-яёіў]*\s+обследован)",
    )),
    "private_document_story": tuple(re.compile(value) for value in (
        r"(?:выбросил|потерял|уничтожил)[а-яёіў]*\s+"
        r"(?:свидетельств|паспорт|документ)[а-яёіў]*.*"
        r"(?:гражданств|восстанов|не\s+хватает)",
        r"(?:польск|иностранн)[а-яёіў]*\s+гражданств[а-яёіў]*.*"
        r"(?:свидетельств|паспорт|документ)",
    )),
    "editorial_meta_or_denial": tuple(re.compile(value) for value in (
        r"журналист[а-яёіў]*\s+привыкл[а-яёіў]*.*(?:редакц|обраща|письм)",
        r"(?:в\s+письм|материал|истори)[а-яёіў]*.*"
        r"нет\s+ни\s+одн[а-яёіў]*\s+жалоб",
    )),
    "aesthetic_or_symbolic_opinion": tuple(re.compile(value) for value in (
        r"(?:так\s+много|засиль[а-яёіў]*|велик[а-яёіў]*\s+кольк[а-яёіў]*)\s+"
        r"(?:льв|скульптур|символ|выяв[а-яёіў]*)",
        r"так\s+шмат\s+(?:львоў|скульптур|сімвал|выяв[а-яёіў]*)",
        r"(?:раздража|не\s+нравит)[а-яёіў]*.*"
        r"(?:символик|изображени|скульптур|оформлени)",
    )),
    "instructional": tuple(re.compile(value) for value in (
        r"^(?:как(?:\s+правильно)?|почему|что\s+такое|можно\s+ли|"
        r"обязательно\s+ли|сколько\s+стоит|к[оі]лькі\s+каштуе|"
        r"шесть\s+способов)(?:\s|$)",
        r"^где(?:\s|$).*(?:поесть|купить|заказать)",
        r"^не\s+спешите(?:\s|$).*(?:поликлиник|больниц|врач)",
    )),
    "routine_announcement": tuple(re.compile(value) for value in (
        r"министерств[а-яёіў]*\s+образован[а-яёіў]*.*запуска[а-яёіў]*\s+эксперимент",
        r"(?:запрет|запреты)\s+на\s+посещен[а-яёіў]*\s+лес[а-яёіў]*",
        r"ситуац[а-яёіў]*\s+с\s+лес[а-яёіў]*.*запрет",
        r"стало\s+известно.*сколько\s+денег\s+выделено",
        r"готовит[а-яёіў]*\s+к\s+[«\"]?дажынк",
        r"точн[а-яёіў]*\s+дат[а-яёіў]*\s+открыти[а-яёіў]*",
        r"ограничени[а-яёіў]*\s+на\s+посещен[а-яёіў]*\s+лес",
        r"планов[а-яёіў]*\s+заседани[а-яёіў]*\s+комисси[а-яёіў]*\s+"
        r"по\s+противодействи[а-яёіў]*\s+коррупци",
        r"рассказал[а-яёіў]*.*как.*расселя[а-яёіў]*\s+студент",
        r"студент[а-яёіў]*\s+ждут\s+в\s+общежити",
        r"платн[а-яёіў]*\s+парков[а-яёіў]*.*"
        r"(?:расшир|увелич|появ|планиру|довест).*машино-мест",
        r"(?:увелич|довест)[а-яёіў]*.*(?:количеств|числ)[а-яёіў]*.*"
        r"машино-мест",
    )),
    "routine_transport_or_construction": tuple(re.compile(value) for value in (
        r"(?:на\s+выходн[а-яёіў]*|до\s+\d{1,2}[:.]\d{2}).*"
        r"(?:закрыл|перекрыл)[а-яёіў]*\s+движени",
        r"(?:закрыл|перекрыл)[а-яёіў]*\s+(?:движени|развязк|дорог)[а-яёіў]*.*"
        r"(?:на\s+ремонт|из-за\s+ремонт)",
        r"(?:нов[а-яёіў]*\s+асфальт|светофор[а-яёіў]*).*"
        r"(?:закрыл|ремонт)[а-яёіў]*",
        r"(?:строят|строится|возводят|вядзецца\s+будаўніцтв)[а-яёіў]*.*"
        r"(?:путь|дорог|магистрал|объект)[а-яёіў]*.*(?:соединит|свяжет|злучыць)",
    )),
    "positive_feature_or_profile": tuple(re.compile(value) for value in (
        r"дал[а-яёіў]*\s+вторую\s+жизн[а-яёіў]*.*библиотек",
        r"появил[а-яёіў]*.*(?:для\s+обмена|буккроссинг|книг[а-яёіў]*)",
        r"(?:приносит|прын[оё]с)[а-яёіў]*\s+на\s+урок[а-яёіў]*.*"
        r"(?:экспонат|животн|зме[яі])",
        r"(?:путь|шлях)\s+к\s+(?:собственн[а-яёіў]*\s+)?бренд[а-яёіў]*",
        r"(?:купил|приобрел|набы)[а-яёіў]*.*(?:хутор|усадьб)[а-яёіў]*.*"
        r"(?:вернул|восстановил|аднав)[а-яёіў]*.*(?:жизн|да\s+жыцц)",
        r"(?:пробн[а-яёіў]*\s+урожай|завезл[а-яёіў]*.*(?:капуст|брокколи))",
    )),
    "event_or_figurative_collision": tuple(re.compile(value) for value in (
        r"очеред[а-яёіў]*.*(?:мероприяти|вечер[а-яёіў]*\s+в\s+посольств|каденци)",
        r"на\s+очереди.*(?:подключени|размещени|оформлени)",
        r"(?:в\s+лихи[а-яёіў]*\s+девяност|в\s+90[-–—]?[а-яёіў]*).*дефицит",
        r"реб[её]нок\s+жалует[а-яёіў]*.*не\s+понимает.*(?:урок|школ)",
    )),
    "routine_enforcement_incident": tuple(re.compile(value) for value in (
        r"водител[а-яёіў]*.*(?:поплатил|лишил)[а-яёіў]*.*(?:прав|рубл|штраф)",
        r"водител[а-яёіў]*.*(?:остал[а-яёіў]*\s+без|лиш[её]н[а-яёіў]*)\s+прав.*"
        r"(?:обгон|маневр|пдд)",
        r"(?:опасн[а-яёіў]*\s+)?(?:обгон|маневр)[а-яёіў]*.*"
        r"(?:лиш[её]н[а-яёіў]*\s+прав|штраф)",
        r"(?:привлечен|привлечены|прыцягнут)[а-яёіў]*\s+к\s+ответственност[а-яёіў]*.*"
        r"(?:маневр|пдд|аварийн[а-яёіў]*\s+обстановк)",
        r"(?:снесл|задел)[а-яёіў]*\s+зеркал[а-яёіў]*.*(?:гаи|штраф)",
        r"вызвал[а-яёіў]*\s+гаи.*получил[а-яёіў]*\s+штраф",
        r"водител[а-яёіў]*\s+спорят.*справедливост[а-яёіў]*\s+наказан",
    )),
    "single_incident": tuple(re.compile(value) for value in (
        r"пассажир\s+получил[а-яёіў]*\s+травм[а-яёіў]*\s+позвоночник[а-яёіў]*.*"
        r"суд\s+взыскал",
        r"получил[а-яёіў]*\s+травм[а-яёіў]*.*(?:фестивал|шоу|трюк)",
        r"создал[а-яёіў]*.*аварийн[а-яёіў]*.*проучит",
        r"(?:обнаружил[а-яёіў]*|нашли)\s+тел[а-яёіў]*.*(?:пропавш|альпинист)",
        r"спасател[а-яёіў]*.*(?:собак|кот[а-яёіў]*|животн[а-яёіў]*)",
        r"авиабомб[а-яёіў]*.*втор[а-яёіў]*\s+миров[а-яёіў]*",
        r"самолет[а-яёіў]*.*экстренн[а-яёіў]*\s+сел[а-яёіў]*.*пассажир",
        r"(?:пассажир|человек)[а-яёіў]*.*(?:умер|скончал)[а-яёіў]*.*"
        r"(?:самолет|борт|рейс)",
    )),
    "private_lifestyle": tuple(re.compile(value) for value in (
        r"соседк[а-яёіў]*.*бикини",
        r"терапевт.*мужчин[а-яёіў]*",
        r"у\s+меня\s+инфаркт.*закат",
        r"чем\s+(?:его|ее|её)\s+удивил[а-яёіў]*\s+наш[а-яёіў]*\s+стран",
        r"(?:какие|где|когда)\s+гриб[а-яёіў]*.*(?:собира|растут|найти)",
        r"(?:тих[а-яёіў]*\s+охот|грибник[а-яёіў]*).*"
        r"(?:совет|ориентир|урожай|сбор)",
        r"очеред[а-яёіў]*\s+на\s+(?:\d+|один|два|две|три|четыре)\s+час[а-яёіў]*.*"
        r"(?:яблок|урожай).*переработ",
        r"сам[а-яёіў]*\s+утеплил[а-яёіў]*.*(?:двухэтажн[а-яёіў]*\s+)?дом.*сэконом",
        r"чем\s+опасн[а-яёіў]*\s+болотн[а-яёіў]*\s+фотосесси",
    )),
    "financial_ticker": tuple(re.compile(value) for value in (
        r"курсы?\s+валют",
        r"доллар\s+и\s+евро.*(?:подорожал|подешевел)",
    )),
    "foreign_non_domestic": tuple(re.compile(value) for value in (
        r"в\s+нидерландах",
        r"на\s+(?:обмелевш[а-яёіў]*\s+)?эльбе",
        r"в\s+горах\s+кыргызстан[а-яёіў]*",
        r"в\s+германи[а-яёіў]*.*работающ[а-яёіў]*\s+беларус",
        r"(?:после|за)\s+границ[а-яёіў]*\s+с\s+(?:рф|росси[а-яёіў]*)",
        r"дефицит\s+топлив[а-яёіў]*.*(?:в\s+рф|в\s+росси[а-яёіў]*)",
    )),
    "promotional_or_corporate": tuple(re.compile(value) for value in (
        r"радиация\s+под\s+контролем",
        r"из\s+города\s+в\s+деревн[а-яёіў]*.*сем",
        r"героин[а-яёіў]*\s+книг[а-яёіў]*.*жкх",
        r"раскрыт\s+потенциал.*бпла",
        r"завод.*уйдет\s+на\s+ремонт",
        r"чистой\s+воды\s+рекламн[а-яёіў]*\s+мероприяти",
    )),
    "advisory_explainer": tuple(re.compile(value) for value in (
        r"^(?:кому|как|можно\s+ли|могут\s+ли|будут\s+ли|что\s+смогут|"
        r"обязательно\s+ли)(?:\s|$)",
        r"(?:разбираем|разобрались|рекомендац[а-яёіў]*|"
        r"как\s+подобрать|как\s+оформить|что\s+нужно\s+знать)",
    )),
    "reporting_channel": tuple(re.compile(value) for value in (
        r"горяч[а-яёіў]*\s+лини[а-яёіў]*",
        r"куда\s+(?:можно\s+)?(?:обратиться|пожаловаться|сообщить)",
    )),
    "preventive_program": tuple(re.compile(value) for value in (
        r"будут\s+проводить.*скрининг",
        r"нов[а-яёіў]*\s+(?:скрининг|диспансеризац|профилактическ[а-яёіў]*\s+план)",
    )),
    "travel_or_profile": tuple(re.compile(value) for value in (
        r"(?:открыли|отправил[а-яёіў]*|поехал[а-яёіў]*)\s+для\s+путешеств",
        r"(?:мотопутешеств|путеводител|туристическ[а-яёіў]*\s+маршрут)",
        r"бывш[а-яёіў]*\s+(?:председател|директор|руководител)",
        r"(?:считаю|лічу),?\s+что\s+правильн[а-яёіў]*\s+путь",
    )),
    "private_or_viral_incident": tuple(re.compile(value) for value in (
        r"видео\s+завирусил",
        r"перепутал[а-яёіў]*.*(?:переход|дорог|знак)",
        r"(?:сдавал|арендовал)[а-яёіў]*.*чуж[а-яёіў]*.*(?:квартир|жиль)",
        r"(?:арендатор|жильц)[а-яёіў]*.*чем.*закончил",
        r"чем\s+(?:вс[её]|усе)\s+закончил",
        r"как\s+отреагировал[а-яёіў]*\s+(?:общество|соцсет|пользовател)",
        r"подростк[а-яёіў]*\s+устроил[а-яёіў]*.*(?:вечерин|посиделк)",
        r"(?:показа[ўл]|паказа[ўл]|снял)[а-яёіў]*,?\s+(?:как|як)\s+.*"
        r"(?:припарковал|прыпаркава)[а-яёіў]*",
        r"(?:автомобил|аўтамабіл)[а-яёіў]*\s+на\s+(?:российск|расейск)[а-яёіў]*\s+"
        r"(?:номер|нумар)[а-яёіў]*.*(?:парк|пдд|даі|гаи)",
        r"продал[а-яёіў]*.*просроченн[а-яёіў]*\s+на\s+сем[ья]\s+лет.*киндер",
    )),
    "private_tenancy_dispute": tuple(re.compile(value) for value in (
        r"хозя(?:йк|ин)[а-яёіў]*.*(?:не\s+хотел[а-яёіў]*\s+возвращ|залог)",
        r"арендатор[а-яёіў]*.*(?:залог|хозя(?:йк|ин)|пош[её]л[а-яёіў]*\s+в\s+суд)",
        r"(?:залог|депозит)[а-яёіў]*.*(?:аренд|квартиросъ[её]м|найм[а-яёіў]*\s+жиль)",
    )),
    "interpersonal_service_incident": tuple(re.compile(value) for value in (
        r"^пассажир\s+пожаловал[а-яёіў]*.*(?:оскорб|обругал|нецензур)",
        r"водител[а-яёіў]*.*(?:оскорбил|обругал)[а-яёіў]*\s+пассажир",
        r"единичн[а-яёіў]*\s+конфликт[а-яёіў]*.*(?:водител|кондуктор|кассир)",
    )),
    "global_or_foreign_corporate": tuple(re.compile(value) for value in (
        r"^сми:\s*",
        r"^у\s+банк[а-яёіў]*\s+.+(?:дыр[ауы]|баланс|невыплат|дефицит|убыт)",
        r"^миров[а-яёіў]*\s+(?:здравоохран|рынок|эконом|отрасл)",
        r"(?:глобальн[а-яёіў]*|во\s+всем\s+мире).*(?:нехват|дефицит|кризис)",
        r"(?:регулятор|управлен[а-яёіў]*\s+по\s+регулирован[а-яёіў]*\s+рынк)[а-яёіў]*.*"
        r"кита[яй][а-яёіў]*.*(?:отзывн[а-яёіў]*\s+кампан|отзыва[а-яёіў]*\s+автомобил)",
        r"^мир\s+.*(?:на\s+порог[а-яёіў]*|ждет|ожидает)[а-яёіў]*.*"
        r"(?:кризис|дефицит|нехват)",
    )),
    "multi_story_digest": tuple(re.compile(value) for value in (
        r"^(?:что\s+)?сегодня\s+по\s+новостям",
        r"^(?:главн[а-яёіў]*|важн[а-яёіў]*)\s+новост[а-яёіў]*\s+(?:дня|вечера|утра|"
        r"понедельник[а-яёіў]*|вторник[а-яёіў]*|сред[а-яёіў]*|четверг[а-яёіў]*|"
        r"пятниц[а-яёіў]*|суббот[а-яёіў]*|воскресень[а-яёіў]*)",
        r"вечерн[а-яёіў]*\s+выпуск",
        r"^какие\s+новост[а-яёіў]*\s+прин[её]с\s+сегодняшн[а-яёіў]*\s+день",
    )),
    "hypothetical_derivative": tuple(re.compile(value) for value in (
        r"а\s+что,?\s+если\s+бы",
        r"представил[а-яёіў]*,?\s+как\s+бы",
        r"попросил[а-яёіў]*\s+нейросет",
    )),
    "historical_retrospective": tuple(re.compile(value) for value in (
        r"забыт[а-яёіў]*\s+(?:кладбищ|усадьб|мест)",
        r"(?:истори[а-яёіў]*|тайн[а-яёіў]*)\s+(?:стар[а-яёіў]*|забыт[а-яёіў]*)",
    )),
    "cosmetic_or_elective_service": tuple(re.compile(value) for value in (
        r"пересадк[а-яёіў]*\s+волос",
        r"косметолог[а-яёіў]*\s+(?:совет|процедур|услуг)",
        r"эстетическ[а-яёіў]*\s+(?:медицин|процедур)",
    )),
}


# Core 3.5.1 normalizes several forms of evidence that ordinary keyword
# adjacency cannot express reliably.  Every profile requires both a public
# context and a concrete failure/remedy marker inside the bounded lead.
BOUND_PUBLIC_ISSUE_PATTERNS: dict[
    str,
    tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]],
] = {
    "consumer_redress": (
        tuple(re.compile(value) for value in (
            r"магазин[а-яёіў]*", r"продав[а-яёіў]*", r"покупател[а-яёіў]*",
            r"потребител[а-яёіў]*", r"товар[а-яёіў]*", r"обув[а-яёіў]*",
        )),
        tuple(re.compile(value) for value in (
            r"отказал[а-яёіў]*.*(?:вернут|возврат|принять|заявлен|претензи)",
            r"не\s+(?:хотел[а-яёіў]*|стал[а-яёіў]*).*возвращ[а-яёіў]*\s+деньг",
            r"отказ[а-яёіў]*\s+в\s+(?:возврат|прием|приёме)",
            r"не\s+выдал[а-яёіў]*.*(?:бланк|заявлен)",
        )),
    ),
    "municipal_fixture_failure": (
        tuple(re.compile(value) for value in (
            r"мусорн[а-яёіў]*\s+(?:контейнер|бак)",
            r"контейнерн[а-яёіў]*\s+площадк", r"бак[а-яёіў]*\s+для\s+отход",
        )),
        tuple(re.compile(value) for value in (
            r"без\s+крышк", r"крышк[а-яёіў]*\s+(?:нет|няма)",
            r"неисправн[а-яёіў]*", r"невозможн[а-яёіў]*\s+закры",
            r"разнос[а-яёіў]*\s+(?:отход|мусор)",
        )),
    ),
    "consultation_accountability": (
        tuple(re.compile(value) for value in (
            r"общественн[а-яёіў]*\s+обсужден", r"публичн[а-яёіў]*\s+слушан",
            r"грамадск[а-яёіў]*\s+абмеркаван",
        )),
        tuple(re.compile(value) for value in (
            r"не\s+(?:уведомил|опубликовал|рассказал)[а-яёіў]*.*(?:итог|результат)",
            r"итог[а-яёіў]*.*не\s+(?:опубликован|известен|сообщен)",
            r"обсужден[а-яёіў]*\s+закончил[а-яёіў]*.*решени[а-яёіў]*\s+не\s+принят",
            r"ответ[а-яёіў]*\s+(?:получил[а-яёіў]*\s+)?расплывчат",
            r"массов[а-яёіў]*\s+критик[а-яёіў]*\s+жител",
        )),
    ),
    "financial_service_access": (
        tuple(re.compile(value) for value in (
            r"(?:payoneer|банковск[а-яёіў]*\s+счет|банкаўск[а-яёіў]*\s+рахунак|"
            r"вывод[а-яёіў]*\s+средств|вывад[а-яёіў]*\s+сродк)",
        )),
        tuple(re.compile(value) for value in (
            r"(?:закрыл|закрыва|не\s+поддержива|не\s+падтрымліва)[а-яёіў]*\s+"
            r"(?:счет|рахунак|вывод|вывад)",
            r"ограничени[а-яёіў]*\s+на\s+(?:вывод|операци|счет)",
            r"(?:белорус|беларус)[а-яёіў]*\s+(?:пользовател|граждан|клиент)[а-яёіў]*.*"
            r"(?:ограничени|не\s+поддержива|закрыл)",
        )),
    ),
    # Result Integrity 1.6.1 / Recall Safety.  These profiles restore narrow
    # public-interest cases that can be missed when the category term and the
    # concrete problem are worded differently.  Each profile deliberately
    # requires both a domain context and bounded evidence; it is not a broad
    # keyword whitelist.
    "domestic_product_safety_enforcement": (
        tuple(re.compile(value) for value in (
            r"(?:продукт|товар|лапш|какао|шоколад|кондитер|напитк|пищев)",
            r"(?:госстандарт|санитарн[а-яёіў]*\s+служб|торгов[а-яёіў]*\s+сет)",
        )),
        tuple(re.compile(value) for value in (
            r"в\s+беларус[а-яёіў]*.*запретил[а-яёіў]*.*(?:продав|ввоз|реализац)",
            r"(?:госстандарт|санитарн[а-яёіў]*\s+служб)[а-яёіў]*.*"
            r"(?:запретил|изъял|приостановил)[а-яёіў]*",
            r"(?:опасн[а-яёіў]*\s+красител|не\s+соответств[а-яёіў]*\s+требован|"
            r"небезопасн[а-яёіў]*\s+продукт).*"
            r"(?:запретил|изъял|не\s+допустил)[а-яёіў]*",
        )),
    ),
    "labour_rights_enforcement": (
        tuple(re.compile(value) for value in (
            r"(?:работник|сотрудник|работодател|начальник|руководител|трудов[а-яёіў]*\s+прав)",
        )),
        tuple(re.compile(value) for value in (
            r"(?:незаконн|необоснованн|самовольн)[а-яёіў]*.*"
            r"(?:штраф|удержан|приказ|увольнен)",
            r"(?:придумал|подделал|издал)[а-яёіў]*.*(?:приказ|штраф)[а-яёіў]*.*работник",
            r"(?:не\s+выплатил|задолжал|удержал)[а-яёіў]*.*(?:зарплат|заработн[а-яёіў]*\s+плат)",
            r"(?:не\s+выплатил|не\s+выплачивал|не\s+получил)[а-яёіў]*.*"
            r"(?:окончательн[а-яёіў]*\s+расчет|расчет[а-яёіў]*.*увольнен)",
            r"(?:окончательн[а-яёіў]*\s+расчет|расчет[а-яёіў]*.*увольнен).*"
            r"(?:не\s+выплатил|не\s+выплачивал|не\s+получил)[а-яёіў]*",
            r"(?:инспекц[а-яёіў]*\s+труд|прокуратур)[а-яёіў]*.*"
            r"(?:восстановил|отменил|выявил)[а-яёіў]*.*(?:прав|штраф|приказ)",
        )),
    ),
    "public_transport_capacity_failure": (
        tuple(re.compile(value) for value in (
            r"(?:автобус|маршрут|общественн[а-яёіў]*\s+транспорт|пассажир)",
        )),
        tuple(re.compile(value) for value in (
            r"вместо\s+(?:больш|обычн)[а-яёіў]*\s+автобус[а-яёіў]*.*"
            r"(?:маленьк|мал[а-яёіў]*\s+автобус|микроавтобус|"
            r"меньш[а-яёіў]*\s+вместимост)",
            r"(?:подал|прислал|приехал|пустил)[а-яёіў]*.*"
            r"автобус[а-яёіў]*\s+(?:мал[а-яёіў]*|меньш[а-яёіў]*\s+вместимост)",
            r"автобус[а-яёіў]*\s+(?:мал[а-яёіў]*|меньш[а-яёіў]*\s+вместимост).*"
            r"вместо\s+(?:больш|обычн)[а-яёіў]*",
            r"(?:не\s+хватил|нет|не\s+было)[а-яёіў]*\s+(?:мест|вместимост)",
            r"(?:переполнен|битком|давк)[а-яёіў]*.*(?:автобус|маршрут)",
            r"пассажир[а-яёіў]*.*(?:жалуют|возмущ)[а-яёіў]*.*"
            r"(?:автобус|маршрут|мест)",
        )),
    ),
    "real_income_pressure": (
        tuple(re.compile(value) for value in (
            r"(?:зарплат|заработк|доход|пенси)[а-яёіў]*",
        )),
        tuple(re.compile(value) for value in (
            r"(?:инфляц|рост[а-яёіў]*\s+цен|подорожан)[а-яёіў]*.*"
            r"(?:обогнал|опередил|быстрее|выше).*"
            r"(?:зарплат|доход|пенси)",
            r"(?:зарплат|доход|пенси)[а-яёіў]*.*"
            r"(?:не\s+успева|отста[её]т|ниже).*"
            r"(?:инфляц|цен|подорожан)",
            r"(?:реальн[а-яёіў]*\s+(?:доход|зарплат)|покупательн[а-яёіў]*\s+способност)[а-яёіў]*.*"
            r"(?:снизил|упал|сократил|хуже)",
            r"сравнил[а-яёіў]*\s+рост\s+зарплат[а-яёіў]*\s+и\s+инфляц",
        )),
    ),
    "employment_contraction": (
        tuple(re.compile(value) for value in (
            r"(?:занят[а-яёіў]*\s+в\s+экономик|занятост|рабоч[а-яёіў]*\s+мест)",
        )),
        tuple(re.compile(value) for value in (
            r"(?:количеств|числ)[а-яёіў]*\s+занят[а-яёіў]*.*"
            r"(?:снизил|сократил|уменьшил)",
            r"(?:занятост|рабоч[а-яёіў]*\s+мест)[а-яёіў]*.*"
            r"(?:снизил|сократил|стало\s+меньше)",
        )),
    ),
    "inactive_population_statistic": (
        tuple(re.compile(value) for value in (
            r"(?:нетунеядц|не\s+занят[а-яёіў]*\s+в\s+экономик)",
        )),
        tuple(re.compile(value) for value in (
            r"(?:сколько|числ|количеств|свеж[а-яёіў]*\s+данн|белстат)",
            r"(?:полной|повышенн)[а-яёіў]*\s+стоимост[а-яёіў]*.*(?:жкх|коммунальн[а-яёіў]*\s+услуг)",
        )),
    ),
    "telecom_service_complaint": (
        tuple(re.compile(value) for value in (
            r"(?:мобильн[а-яёіў]*\s+(?:оператор|интернет|связ)|оператор[а-яёіў]*\s+связ|"
            r"интернет-провайдер|трафик)",
        )),
        tuple(re.compile(value) for value in (
            r"(?:клиент|абонент|пользовател|белорус)[а-яёіў]*.*"
            r"(?:жалует|возмущ|оспарива)[а-яёіў]*",
            r"(?:неверн|ошибочн|необоснованн)[а-яёіў]*.*"
            r"(?:списал|начисл|трафик|плат[а-яёіў]*)",
            r"(?:оператор|провайдер)[а-яёіў]*.*(?:не\s+решил|игнорир|отказал)[а-яёіў]*.*"
            r"(?:проблем|жалоб|претензи)",
        )),
    ),
    # A concrete queue complaint about access to outpatient care is a
    # public-health signal even when the article starts with a long service
    # explanation and the body-level category evidence arrives after the
    # bounded lead. Keep this deliberately narrow: both a care setting and
    # either a resident complaint or an abnormally long queue are required.
    "outpatient_access_complaint": (
        tuple(re.compile(value) for value in (
            r"(?:поликлиник|больниц|амбулатор|гинеколог|врач|медкомисси)",
        )),
        tuple(re.compile(value) for value in (
            r"(?:жител|пациент|граждан)[а-яёіў]*.{0,80}(?:пожаловал|жалоб|не\s+мож(?:ет|ут)\s+попасть)",
            r"(?:длинн|многочасов|огромн)[а-яёіў]*\s+очеред",
        )),
    ),
}


def bound_public_issue_profiles(text: str) -> set[str]:
    """Return evidence profiles fully supported inside one bounded lead."""
    folded = normalized_search_text(text)
    return {
        name
        for name, (context_patterns, evidence_patterns) in BOUND_PUBLIC_ISSUE_PATTERNS.items()
        if any(pattern.search(folded) for pattern in context_patterns)
        and any(pattern.search(folded) for pattern in evidence_patterns)
    }


def result_integrity_genre_rejection(
    title: str,
    lead: str,
    *,
    title_explicit: bool = False,
    resident_explicit: bool = False,
    lead_findings: bool = False,
    persistence: bool = False,
    special_public_interest: bool = False,
    regulatory_public_interest: bool = False,
    critical_public_interest: bool = False,
    lead_bound_evidence: bool = True,
    domestic_scope: bool = True,
) -> str:
    """Return a stable genre-level rejection reason or an empty string.

    The gate is deliberately evidence-aware: an explainer or announcement is
    retained when its title/lead also contains a concrete resident complaint,
    an institutional finding or an actual service failure.
    """
    folded_title = normalized_search_text(title)
    folded_lead = normalized_search_text(lead)
    if economic_direction_signal(" ".join((title, lead))) is False:
        return "Result Integrity: положительная динамика доходов без подтверждённой проблемы"
    concrete_title_problem = bool(re.search(
        r"(?:жалоб|возмущ|не\s+работ|не\s+могут|игнорир|разбит|"
        r"луж[а-яёіў]*|гряз|мусор|очеред|задолж|просроч|тарак|"
        r"кишечн[а-яёіў]*\s+палоч|не\s+найти|обмел|отключ|перебо)"
        r"[а-яёіўa-z0-9]*",
        folded_title,
    ))
    if re.search(
        r"(?:без|нет|не\s+будет)\s+(?:очеред|проблем|жалоб|перебо)",
        folded_title,
    ):
        concrete_title_problem = False
    direct_public_evidence = bool(
        title_explicit or resident_explicit or concrete_title_problem
    )
    def matches(group: str, *, include_lead: bool = False) -> bool:
        patterns = RESULT_INTEGRITY_GENRE_PATTERNS[group]
        return any(pattern.search(folded_title) for pattern in patterns) or bool(
            include_lead
            and any(pattern.search(folded_lead) for pattern in patterns)
        )

    instructional_public_evidence = bool(
        direct_public_evidence
        or lead_findings
        or persistence
        or special_public_interest
    )
    incident_systemic_evidence = bool(
        concrete_title_problem
        and (title_explicit or lead_findings or persistence)
    )

    actual_public_evidence = bool(
        resident_explicit or lead_findings or persistence or special_public_interest
    )
    resident_or_systemic_evidence = bool(resident_explicit or persistence)
    bound_regulatory_discussion = bool(
        regulatory_public_interest
        and re.search(
            r"(?:обсужд[а-яёіў]*.*(?:социальн[а-яёіў]*\s+сет|соцсет)|"
            r"обращени[а-яёіў]*\s+граждан|жалоб[а-яёіў]*)",
            folded_lead,
        )
    )
    # Core 3.6 Editorial Intent Gate.  Generic references to residents,
    # services or a word such as «отсутствует» do not turn an advice column,
    # timetable, protocol event or private anecdote into a public problem.
    # Each neutral intent has an explicit evidence override, so a real
    # complaint/finding in the bounded lead is still retained.
    verified_problem_evidence = bool(
        title_explicit
        or lead_findings
        or persistence
        or concrete_title_problem
        or special_public_interest
    )
    institutional_problem_evidence = bool(
        title_explicit
        or resident_explicit
        or lead_findings
        or persistence
        or special_public_interest
    )

    if not lead_bound_evidence and not special_public_interest:
        return "Evidence Binding: тема и проблема связаны только глубоко в тексте"
    if matches("multi_story_digest"):
        return "Result Integrity: многосюжетная сводка без самостоятельного события"
    if matches("lifestyle_advice_or_ranking") and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: бытовой совет или рейтинг без подтверждённой общественной проблемы"
    if matches("educational_simulation", include_lead=True) and not (
        resident_explicit or persistence or special_public_interest
    ):
        return "Editorial Intent: учебная симуляция или инструкция без реального сбоя услуги"
    if matches("positive_local_update", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: позитивное инфраструктурное обновление без проблемы"
    if matches("positive_public_infrastructure_opening", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: открытие инфраструктурного объекта без жалобы или нарушения"
    if matches("aggregate_credit_debt_statistic", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Result Integrity: агрегированная кредитная статистика без влияния на жителей"
    if matches("personal_foreign_profile", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: личный зарубежный профиль без общественной проблемы"
    if matches("hypothetical_derivative") and not actual_public_evidence:
        return "Result Integrity: гипотетическая или сгенерированная иллюстрация события"
    explicit_domestic_market_link = bool(re.search(
        r"(?:в|на)\s+беларус[а-яёіў]*.*(?:рынок|продаж|ввоз|эксплуатац|"
        r"зарегистрирован|поставк|дилер|владельц)",
        folded_lead,
    ))
    if matches("global_or_foreign_corporate", include_lead=True) and not (
        explicit_domestic_market_link
        and domestic_scope
        and (resident_or_systemic_evidence or regulatory_public_interest)
    ):
        return "Result Integrity: зарубежный или глобальный корпоративный сюжет"
    if matches("historical_retrospective") and not actual_public_evidence:
        return "Result Integrity: историческая ретроспектива без текущей проблемы"
    if matches("medical_or_psychology_advice") and not (
        title_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: медицинский или психологический совет без общественной проблемы"
    if matches("benefit_or_application_explainer") and not (
        verified_problem_evidence
    ):
        return "Editorial Intent: порядок получения услуги или пособия без жалобы"
    if matches("scheduled_service_notice") and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: плановое сервисное уведомление"
    if matches("positive_medical_achievement", include_lead=True) and not (
        resident_explicit or persistence or special_public_interest
    ):
        return "Editorial Intent: медицинское достижение без проблемы доступности"
    if matches("neutral_service_launch", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: запуск сервиса без подтверждённой проблемы"
    if matches("neutral_regulatory_explainer", include_lead=True) and not (
        resident_explicit or persistence or bound_regulatory_discussion
    ):
        return "Editorial Intent: нейтральное разъяснение новых правил"
    if matches("routine_status_explainer", include_lead=True) and not (
        resident_explicit or lead_findings or persistence
    ):
        return "Editorial Intent: справочное разъяснение статуса без подтверждённой проблемы"
    if matches("career_explainer", include_lead=True) and not (
        resident_explicit or lead_findings or persistence
    ):
        return "Editorial Intent: справочный материал о профессии"
    if matches("foreign_residency_facilitation", include_lead=True) and not (
        lead_findings or persistence or bound_regulatory_discussion
    ):
        return "Result Integrity: зарубежное административное послабление без проблемы в Беларуси"
    if matches("general_policy_statement", include_lead=True) and not (
        resident_explicit or lead_findings or persistence
    ):
        return "Editorial Intent: общее политико-экономическое заявление без установленного нарушения"
    if matches("routine_law_enforcement_procedure", include_lead=True) and not (
        resident_explicit or persistence or critical_public_interest
    ):
        return "Result Integrity: обычное правоохранительное сообщение без социальной проблемы"
    if matches("positive_income_comparison", include_lead=True) and economic_direction_signal(" ".join((title, lead))) is not True and not (
        resident_explicit or lead_findings or persistence
    ):
        return "Result Integrity: положительная динамика доходов без подтверждённой проблемы"
    if matches("cultural_or_migration_commentary", include_lead=True) and not (
        lead_findings or persistence or critical_public_interest
    ):
        return "Editorial Intent: культурно-миграционный комментарий без общественной проблемы"
    if matches("resolved_minor_emergency", include_lead=True) and not (
        resident_explicit or persistence or special_public_interest
    ):
        return "Result Integrity: локальное происшествие без пострадавших и ущерба"
    if matches("protocol_or_personnel") and not (
        institutional_problem_evidence
    ):
        return "Editorial Intent: протокольное или кадровое сообщение"
    if matches("preventive_service_expansion") and not (
        title_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Editorial Intent: профилактическая программа без жалобы на доступность"
    if matches("private_document_story") and not (
        title_explicit or lead_findings or regulatory_public_interest
    ):
        return "Editorial Intent: частная история с документами без системного нарушения"
    if matches("editorial_meta_or_denial") and not (
        lead_findings or special_public_interest
    ):
        return "Editorial Intent: редакционная метаистория без самостоятельной проблемы"
    if matches("aesthetic_or_symbolic_opinion") and not (
        lead_findings or persistence or concrete_title_problem
    ):
        return "Editorial Intent: эстетическая дискуссия без инфраструктурной проблемы"
    if matches("travel_or_profile") and not resident_explicit:
        return "Result Integrity: туристический или портретный материал"
    if matches("private_or_viral_incident") and not incident_systemic_evidence:
        return "Result Integrity: частный или вирусный инцидент без системной проблемы"
    if matches("private_tenancy_dispute") and not (
        persistence or regulatory_public_interest
    ):
        return "Result Integrity: частный спор найма жилья без системной проблемы"
    if matches("interpersonal_service_incident") and not (
        persistence or regulatory_public_interest
    ):
        return "Result Integrity: единичный межличностный конфликт при оказании услуги"
    if matches("reporting_channel") and not actual_public_evidence:
        return "Result Integrity: канал обращений без подтверждённой проблемы"
    if matches("preventive_program") and not (
        title_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Result Integrity: профилактическая программа без жалобы на доступность"
    if matches("cosmetic_or_elective_service", include_lead=True) and not actual_public_evidence:
        return "Result Integrity: элективная или косметическая услуга без общественной проблемы"
    if matches("advisory_explainer") and not (
        resident_explicit
        or lead_findings
        or persistence
        or concrete_title_problem
        or bound_regulatory_discussion
        or critical_public_interest
    ):
        return "Result Integrity: справочный материал без доказанной общественной проблемы"

    if matches("instructional") and not instructional_public_evidence:
        return "Result Integrity: справочный или рекомендательный жанр без общественной проблемы"
    if matches("routine_announcement") and not direct_public_evidence:
        return "Result Integrity: нейтральное административное сообщение"
    if matches("routine_transport_or_construction", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Result Integrity: плановое транспортное или строительное сообщение"
    if matches("positive_feature_or_profile", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or critical_public_interest
    ):
        return "Result Integrity: позитивный репортаж или портрет без общественной проблемы"
    if matches("event_or_figurative_collision", include_lead=True) and not (
        resident_explicit or lead_findings or persistence or special_public_interest
    ):
        return "Evidence Binding: проблемное слово относится к событию, идиоме или примеру"
    if matches("routine_enforcement_incident", include_lead=True):
        return "Result Integrity: обычное сообщение о дорожном правонарушении"
    if matches("single_incident") and not incident_systemic_evidence:
        return "Result Integrity: единичное происшествие без системной социальной проблемы"
    if matches("private_lifestyle") and not lead_findings:
        return "Result Integrity: частный бытовой или lifestyle-сюжет"
    if matches("financial_ticker") and not direct_public_evidence:
        return "Result Integrity: биржевой или валютный бюллетень без жалобы жителей"
    if matches("foreign_non_domestic") and not resident_explicit:
        return "Result Integrity: зарубежный сюжет без воздействия на жителей Беларуси"
    if matches("promotional_or_corporate") and not direct_public_evidence:
        return "Result Integrity: имиджевый или корпоративный материал"
    return ""


def precision_terms(
    topic: dict[str, Any],
    key: str,
) -> tuple[str, ...]:
    configured = topic.get(f"relevance_precision_{key}_terms", [])
    if isinstance(configured, str):
        configured = [configured]
    return tuple(unique_values([
        *RELEVANCE_PRECISION_TERMS.get(key, ()),
        *(configured if isinstance(configured, list) else []),
    ]))


def evaluate_relevance(
    title: str,
    summary: str,
    article_text: str,
    source: Source,
    settings: dict[str, Any],
) -> RelevanceDecision:
    topic = settings["topic"]
    categories: dict[str, list[str]] = topic.get("categories", {})
    body_sentences = split_sentences(article_text or summary)
    max_sentences = int(topic.get("max_analysis_sentences", 30))
    body = body_sentences[:max_sentences]
    opening = " ".join([title, *body[:8]])
    preliminary_context = " ".join([title, *body[:10]])
    bounded_issue_text = " ".join([title, *body[:6]])
    bounded_issue_profiles = bound_public_issue_profiles(bounded_issue_text)
    preliminary_belarus_hits = find_terms(
        preliminary_context, topic.get("belarus_context_terms", [])
    )
    preliminary_recruitment_hits = find_terms(
        preliminary_context, topic.get("illegal_recruitment_terms", [])
    )
    domestic_illegal_recruitment = bool(
        preliminary_recruitment_hits and preliminary_belarus_hits
    )
    preliminary_business_loss_hits = find_terms(
        preliminary_context, topic.get("domestic_business_loss_terms", [])
    )
    domestic_business_loss_signal = bool(
        preliminary_business_loss_hits and preliminary_belarus_hits
    )

    politics_hits = find_terms(opening, topic.get("politics_exclusions", []))
    politics_lead = " ".join([title, *body[:2]])
    politics_service_exception = bool(
        category_hits(politics_lead, categories)
        and find_terms(
            politics_lead,
            topic.get("explicit_complaint_terms", []),
        )
    )
    if politics_hits and not (
        domestic_illegal_recruitment
        or domestic_business_loss_signal
        or politics_service_exception
        or "consultation_accountability" in bounded_issue_profiles
        or "financial_service_access" in bounded_issue_profiles
    ):
        return RelevanceDecision(False, reason=f"политическая тема: {politics_hits[0]}")

    emergency_event_terms = topic.get("emergency_event_terms", [])
    emergency_issue_terms = [
        *topic.get("emergency_complaint_terms", []),
        *topic.get("emergency_service_failure_terms", []),
        *topic.get("emergency_self_help_terms", []),
        *topic.get("emergency_unresolved_terms", []),
    ]
    emergency_event_hits = find_terms(
        " ".join([title, *body]), emergency_event_terms
    )

    title_categories = category_hits(title, categories)
    title_explicit = [] if sentence_has_denial(title, settings) else find_terms(
        title, topic.get("explicit_complaint_terms", [])
    )
    title_negative = [] if sentence_has_denial(title, settings) else find_terms(
        title, topic.get("negative_condition_terms", [])
    )
    title_findings = find_terms(title, topic.get("institutional_finding_terms", []))
    title_emergency = find_terms(title, emergency_issue_terms)
    title_signal = bool(
        title_categories
        and (title_explicit or title_negative or title_findings or title_emergency)
    )

    details: list[dict[str, Any]] = []
    for index, sentence in enumerate(body):
        hypothetical = contains_any(sentence, topic.get("hypothetical_terms", []))
        details.append({
            "index": index,
            "sentence": sentence,
            "categories": category_hits(sentence, categories),
            "explicit": [] if sentence_has_denial(sentence, settings) else find_terms(
                sentence, topic.get("explicit_complaint_terms", [])
            ),
            "negative": [] if sentence_has_denial(sentence, settings) or hypothetical else find_terms(
                sentence, topic.get("negative_condition_terms", [])
            ),
            "findings": [] if hypothetical else find_terms(
                sentence, topic.get("institutional_finding_terms", [])
            ),
            "residents": find_terms(sentence, topic.get("resident_terms", [])),
            "response": find_terms(sentence, topic.get("official_response_terms", [])),
            "persistent": find_terms(sentence, topic.get("persistence_terms", [])),
            "emergency": [] if hypothetical else find_terms(
                sentence, emergency_issue_terms
            ),
        })

    same_sentence: list[int] = []
    adjacent: list[int] = []
    category_weights: dict[str, int] = {name: 0 for name in categories}
    matched_terms: list[str] = []

    for detail in details:
        has_problem = bool(
            detail["explicit"]
            or detail["negative"]
            or detail["findings"]
            or detail["emergency"]
        )
        if detail["categories"] and has_problem:
            same_sentence.append(detail["index"])
            for category, hits in detail["categories"].items():
                category_weights[category] += 5
                matched_terms.extend(hits)
            matched_terms.extend(
                detail["explicit"]
                + detail["negative"]
                + detail["findings"]
                + detail["emergency"]
            )

    for index, detail in enumerate(details):
        if not detail["categories"]:
            continue
        for neighbour_index in (index - 1, index + 1):
            if neighbour_index < 0 or neighbour_index >= len(details):
                continue
            neighbour = details[neighbour_index]
            if (
                neighbour["explicit"]
                or neighbour["negative"]
                or neighbour["findings"]
                or neighbour["emergency"]
            ):
                adjacent.extend([index, neighbour_index])
                for category, hits in detail["categories"].items():
                    category_weights[category] += 2
                    matched_terms.extend(hits)

    if title_signal:
        for category, hits in title_categories.items():
            category_weights[category] += 7
            matched_terms.extend(hits)
        matched_terms.extend(
            title_explicit + title_negative + title_findings + title_emergency
        )

    explicit_hits = title_explicit + [hit for d in details for hit in d["explicit"]]
    negative_hits = title_negative + [hit for d in details for hit in d["negative"]]
    finding_hits = title_findings + [hit for d in details for hit in d["findings"]]
    response_indices = [d["index"] for d in details if d["response"]]
    persistence_hits = [hit for d in details for hit in d["persistent"]]
    emergency_issue_hits = title_emergency + [
        hit for d in details for hit in d["emergency"]
    ]
    resident_explicit_signal = any(
        detail["residents"] and detail["explicit"] for detail in details
    )

    preliminary_full_context = " ".join([title, *body])
    preliminary_foreign_hits = find_terms(
        preliminary_full_context, topic.get("foreign_context_terms", [])
    )
    regulatory_discussion_hits = find_terms(
        preliminary_full_context,
        topic.get("domestic_regulatory_discussion_terms", []),
    )
    public_reaction_hits = find_terms(
        preliminary_full_context, topic.get("public_reaction_terms", [])
    )
    belarus_regulatory_hits = find_terms(
        preliminary_full_context,
        topic.get("belarus_regulatory_context_terms", []),
    )
    domestic_regulatory_discussion = bool(
        regulatory_discussion_hits
        and public_reaction_hits
        and (
            belarus_regulatory_hits
            or (preliminary_belarus_hits and not preliminary_foreign_hits)
        )
    )
    public_consultation_hits = find_terms(
        preliminary_full_context, topic.get("public_consultation_terms", [])
    )
    public_consultation_problem_hits = find_terms(
        preliminary_full_context,
        topic.get("public_consultation_problem_terms", []),
    )
    domestic_public_consultation_problem = bool(
        public_consultation_hits
        and public_consultation_problem_hits
        and (
            preliminary_belarus_hits
            or not preliminary_foreign_hits
        )
    )
    domestic_public_consultation_problem = bool(
        domestic_public_consultation_problem
        or "consultation_accountability" in bounded_issue_profiles
    )
    cross_border_rights_hits = find_terms(
        preliminary_full_context,
        topic.get("cross_border_document_rights_terms", []),
    )
    cross_border_rights_signal = bool(
        cross_border_rights_hits
        and (explicit_hits or negative_hits or finding_hits)
    )
    building_envelope_hits = find_terms(
        preliminary_full_context,
        topic.get("building_envelope_context_terms", []),
    )
    building_envelope_problem_hits = find_terms(
        preliminary_full_context,
        topic.get("building_envelope_problem_terms", []),
    )
    building_envelope_signal = bool(
        building_envelope_hits and building_envelope_problem_hits
    )
    hotel_service_hits = find_terms(
        preliminary_full_context,
        topic.get("hotel_service_context_terms", []),
    )
    hotel_service_problem_hits = find_terms(
        preliminary_full_context,
        topic.get("hotel_service_problem_terms", []),
    )
    hotel_service_signal = bool(
        hotel_service_hits
        and hotel_service_problem_hits
        and (explicit_hits or negative_hits or finding_hits)
    )

    special_evidence_indices: list[int] = []
    recruitment_category = "Общественная безопасность и противоправные практики"
    regulatory_category = "Законы, права и общественное регулирование"
    business_loss_category = "Цены, торговля и дефицит"

    if domestic_business_loss_signal and business_loss_category in category_weights:
        category_weights[business_loss_category] += 16
        matched_terms.extend(preliminary_business_loss_hits)
        for index, sentence in enumerate(body):
            if find_terms(sentence, topic.get("domestic_business_loss_terms", [])):
                special_evidence_indices.append(index)

    if domestic_illegal_recruitment and recruitment_category in category_weights:
        category_weights[recruitment_category] += 16
        matched_terms.extend(preliminary_recruitment_hits)
        for index, sentence in enumerate(body):
            if find_terms(sentence, topic.get("illegal_recruitment_terms", [])):
                special_evidence_indices.append(index)

    if domestic_regulatory_discussion and regulatory_category in category_weights:
        category_weights[regulatory_category] += 16
        matched_terms.extend(regulatory_discussion_hits + public_reaction_hits)
        for index, sentence in enumerate(body):
            if (
                find_terms(sentence, topic.get("domestic_regulatory_discussion_terms", []))
                or find_terms(sentence, topic.get("public_reaction_terms", []))
            ):
                special_evidence_indices.append(index)

    if domestic_public_consultation_problem and regulatory_category in category_weights:
        category_weights[regulatory_category] += 18
        matched_terms.extend(
            public_consultation_hits + public_consultation_problem_hits
        )
        for index, sentence in enumerate(body):
            if (
                find_terms(sentence, topic.get("public_consultation_terms", []))
                or find_terms(
                    sentence, topic.get("public_consultation_problem_terms", [])
                )
            ):
                special_evidence_indices.append(index)

    if cross_border_rights_signal and regulatory_category in category_weights:
        category_weights[regulatory_category] += 14
        matched_terms.extend(cross_border_rights_hits)

    if building_envelope_signal and "ЖКХ и состояние жилья" in category_weights:
        category_weights["ЖКХ и состояние жилья"] += 14
        matched_terms.extend(
            building_envelope_hits + building_envelope_problem_hits
        )

    if hotel_service_signal and "Качество товаров и услуг" in category_weights:
        category_weights["Качество товаров и услуг"] += 14
        matched_terms.extend(hotel_service_hits)

    product_contamination_hits = find_terms(
        preliminary_full_context,
        (
            "re:кишечн[а-яёіўa-z0-9]*\\s+палоч[а-яёіўa-z0-9]*",
            "сальмонел", "листер", "опасн продукт",
            "небезопасн продукт", "микробиологическ загрязнен",
        ),
    )
    normalized_product_context = normalized_search_text(preliminary_full_context)
    product_contamination_signal = bool(
        product_contamination_hits
        and (
            finding_hits
            or re.search(
                r"\b(?:нашл|выяв|обнаруж|признан|оказал|запрет)[а-яёіўa-z0-9]*",
                normalized_product_context,
            )
            or (
                re.search(r"кишечн[а-яёіўa-z0-9]*\s+палоч", normalized_product_context)
                and re.search(r"стафилокок", normalized_product_context)
            )
        )
    )
    if product_contamination_signal:
        category_weights["Качество товаров и услуг"] += 20
        matched_terms.extend(product_contamination_hits)
        for index, sentence in enumerate(body):
            if find_terms(sentence, product_contamination_hits):
                special_evidence_indices.append(index)

    counterfeit_product_hits = find_terms(
        preliminary_full_context,
        (
            "контрафакт", "поддельн товар", "падроблен тавар",
            "re:без\\s+документ[а-яёіўa-z0-9]*.*(?:качеств|безопасност)",
            "re:не\\s+было\\s+документ[а-яёіўa-z0-9]*.*(?:качеств|безопасност)",
        ),
    )
    counterfeit_product_signal = bool(
        counterfeit_product_hits
        and re.search(
            r"(?:контрафакт|поддельн|падроблен|изъял|оштрафовал|"
            r"без\s+документ|не\s+было\s+документ)",
            normalized_search_text(preliminary_full_context),
        )
    )
    if counterfeit_product_signal:
        category_weights["Качество товаров и услуг"] += 20
        matched_terms.extend(counterfeit_product_hits)
        for index, sentence in enumerate(body):
            if find_terms(sentence, counterfeit_product_hits):
                special_evidence_indices.append(index)

    # Жалоба на ненадлежащие условия содержания животных может не содержать
    # традиционных словарных пар социальной темы и проблемы. Признаём узкую
    # подтверждённую связку самостоятельным общественным сигналом до раннего
    # контроля целостности результата.
    animal_welfare_category = "Защита животных и условия содержания"
    animal_welfare_context = normalized_search_text(preliminary_full_context)
    animal_welfare_signal = bool(
        re.search(
            r"(?:жив[а-яёіў]*\s+рыб|животн|приют|пункт[а-яёіў]*\s+содерж|"
            r"рыб[а-яёіў]*.*(?:кафе|декор|ламп|аквари)).*"
            r"(?:неправильн[а-яёіў]*\s+услов|перегрев|нулев[а-яёіў]*\s+фильтрац|"
            r"отсутств[а-яёіў]*\s+фильтрац|не\s+хватает\s+вод|погиба)",
            animal_welfare_context,
        )
    )
    if animal_welfare_signal and animal_welfare_category in category_weights:
        category_weights[animal_welfare_category] += 22
        matched_terms.append("ненадлежащие условия содержания животных")

    education_category = "Образование и дети"
    bullying_signal = bool(
        re.search(r"(?:школ|гимназ|ученик|школьник)", animal_welfare_context)
        and re.search(
            r"(?:буллинг|травл|избивал|избили|бил[аи]?\s+толп)",
            animal_welfare_context,
        )
        and re.search(r"(?:жалоб|пожаловал|разбирательств|провер)", animal_welfare_context)
    )
    if bullying_signal and education_category in category_weights:
        category_weights[education_category] += 22
        matched_terms.append("буллинг или насилие в учреждении образования")

    bound_profile_categories = {
        "consumer_redress": "Качество товаров и услуг",
        "municipal_fixture_failure": "Дороги и благоустройство",
        "consultation_accountability": regulatory_category,
        "financial_service_access": regulatory_category,
        "domestic_product_safety_enforcement": "Качество товаров и услуг",
        "labour_rights_enforcement": "Работа, зарплаты и доходы",
        "public_transport_capacity_failure": "Общественный транспорт",
        "real_income_pressure": "Работа, зарплаты и доходы",
        "employment_contraction": "Работа, зарплаты и доходы",
        "inactive_population_statistic": "Работа, зарплаты и доходы",
        "telecom_service_complaint": "Связь, интернет и телевидение",
        "outpatient_access_complaint": "Здравоохранение",
    }
    bound_profile_labels = {
        "consumer_redress": "подтверждённый спор о защите прав потребителя",
        "municipal_fixture_failure": "неисправный коммунальный объект",
        "consultation_accountability": "неопубликованные итоги общественного обсуждения",
        "financial_service_access": "ограничение доступа белорусов к финансовой услуге",
        "domestic_product_safety_enforcement": "запрет небезопасного товара в Беларуси",
        "labour_rights_enforcement": "подтверждённое нарушение трудовых прав",
        "public_transport_capacity_failure": "нехватка вместимости общественного транспорта",
        "real_income_pressure": "снижение реальных доходов относительно цен",
        "employment_contraction": "сокращение занятости или рабочих мест",
        "inactive_population_statistic": "статистика граждан, не занятых в экономике",
        "telecom_service_complaint": "подтверждённая жалоба на услугу связи",
        "outpatient_access_complaint": "подтверждённая жалоба на доступ к амбулаторной помощи",
    }
    for profile in sorted(bounded_issue_profiles):
        category = bound_profile_categories.get(profile)
        if category in category_weights:
            category_weights[category] += 18
            matched_terms.append(bound_profile_labels[profile])
        evidence_patterns = BOUND_PUBLIC_ISSUE_PATTERNS[profile][1]
        for index, sentence in enumerate(body[:6]):
            folded_sentence = normalized_search_text(sentence)
            if any(pattern.search(folded_sentence) for pattern in evidence_patterns):
                special_evidence_indices.append(index)

    special_public_interest_signal = bool(
        domestic_illegal_recruitment
        or domestic_regulatory_discussion
        or domestic_public_consultation_problem
        or cross_border_rights_signal
        or building_envelope_signal
        or hotel_service_signal
        or domestic_business_loss_signal
        or product_contamination_signal
        or counterfeit_product_signal
        or animal_welfare_signal
        or bullying_signal
        or bounded_issue_profiles
    )

    if not any(category_weights.values()):
        return RelevanceDecision(False, reason="нет связки социальной темы и проблемы")
    if not (title_signal or same_sentence or adjacent or special_public_interest_signal):
        return RelevanceDecision(False, reason="жалоба и тема не находятся рядом")

    # География: белорусские источники считаются базовым контекстом, но явная
    # иностранная география без белорусского контекста исключает материал.
    geo_text = " ".join([title, *body[:10]])
    belarus_hits = find_terms(geo_text, topic.get("belarus_context_terms", []))
    foreign_hits = find_terms(geo_text, topic.get("foreign_context_terms", []))
    foreign_title_hits = find_terms(
        title, topic.get("foreign_context_terms", [])
    )
    belarus_title_hits = find_terms(
        title, topic.get("belarus_context_terms", [])
    )
    foreign_residence_hits = find_terms(
        geo_text, topic.get("foreign_residence_terms", [])
    )
    locality_hits = find_terms(geo_text, [source.locality, source.country])

    # Упоминание «беларус/белоруска» само по себе не делает материал
    # внутренним: истории об эмигрантах и жизни за рубежом не относятся к
    # жалобам жителей Беларуси.
    if foreign_hits and foreign_residence_hits:
        return RelevanceDecision(
            False, reason=f"иностранная тема: {foreign_residence_hits[0]}"
        )
    # Если иностранная география вынесена прямо в заголовок, материал
    # исключается без явного воздействия на жителей/потребителей Беларуси.
    # Это отсекает перепечатки о коммунальных и природных проблемах других
    # стран, даже когда в шаблоне страницы случайно встречается слово
    # «Беларусь» или название региона источника.
    if (
        foreign_title_hits
        and not belarus_title_hits
        and not (
            cross_border_rights_signal
            or resident_explicit_signal
            or domestic_regulatory_discussion
        )
    ):
        return RelevanceDecision(
            False, reason=f"иностранная тема в заголовке: {foreign_title_hits[0]}"
        )
    if foreign_hits and not belarus_hits and not locality_hits:
        return RelevanceDecision(False, reason=f"иностранная тема: {foreign_hits[0]}")

    full_context = " ".join([title, *body])
    # Криминальные и происшественные маркеры могут находиться не в лиде,
    # а ниже по тексту (например, в интервью с милицией). Поэтому
    # проверяем весь анализируемый фрагмент, а не только первые предложения.
    crime_hits = find_terms(full_context, topic.get("crime_exclusions", []))
    incident_hits = find_terms(full_context, topic.get("incident_rescue_exclusions", []))
    incident_exception_hits = find_terms(
        full_context, topic.get("incident_relevance_exception_terms", [])
    )
    routine_enforcement_hits = find_terms(
        opening, topic.get("routine_enforcement_terms", [])
    )
    leisure_hits = find_terms(opening, topic.get("other_exclusions", []))
    trade_hits = find_terms(full_context, topic.get("business_trade_exclusions", []))
    resident_hits = [hit for d in details for hit in d["residents"]]
    domestic_consumer_hits = find_terms(
        full_context, topic.get("domestic_consumer_context_terms", [])
    )
    strong_explicit = bool(explicit_hits)

    # Neutral explainers, advice, routine maintenance notices and forecasts
    # are not complaints merely because they contain words such as
    # «отсутствие», «пыль», «вода» or «подтопление».
    neutral_title_hits = find_terms(
        title, topic.get("neutral_information_title_terms", [])
    )
    support_campaign_hits = find_terms(
        opening, topic.get("neutral_support_campaign_terms", [])
    )
    support_campaign_exception_hits = find_terms(
        full_context, topic.get("neutral_support_campaign_exception_terms", [])
    )
    planned_hits = find_terms(
        full_context, topic.get("planned_maintenance_terms", [])
    )
    forecast_hits = find_terms(
        opening, topic.get("forecast_exclusion_terms", [])
    )
    actual_damage_hits = find_terms(
        full_context, topic.get("actual_damage_terms", [])
    )
    macro_hits = find_terms(
        full_context, topic.get("macroeconomic_exclusion_terms", [])
    )
    financial_forecast_hits = find_terms(
        opening, topic.get("financial_forecast_exclusion_terms", [])
    )
    research_hits = find_terms(
        full_context, topic.get("research_exclusion_terms", [])
    )
    research_exception_hits = find_terms(
        full_context, topic.get("research_relevance_exception_terms", [])
    )
    profile_hits = find_terms(
        title, topic.get("profile_feature_exclusion_terms", [])
    )
    service_launch_hits = find_terms(
        opening, topic.get("neutral_service_launch_terms", [])
    )
    personnel_hits = find_terms(
        opening, topic.get("personnel_appointment_exclusion_terms", [])
    )
    tourism_hits = find_terms(
        opening, topic.get("tourism_exclusion_terms", [])
    )
    foreign_travelogue_hits = find_terms(
        full_context, topic.get("foreign_travelogue_exclusion_terms", [])
    )
    traffic_incident_hits = find_terms(
        full_context, topic.get("traffic_incident_exclusion_terms", [])
    )
    regulatory_reform_hits = find_terms(
        opening, topic.get("regulatory_reform_exclusion_terms", [])
    )
    foreign_tourism_bridge_hits = find_terms(
        full_context, topic.get("foreign_tourism_bridge_terms", [])
    )
    health_advice_hits = find_terms(
        opening, topic.get("health_advice_exclusion_terms", [])
    )
    positive_infrastructure_hits = find_terms(
        opening, topic.get("positive_infrastructure_exclusion_terms", [])
    )
    property_sale_hits = find_terms(
        opening, topic.get("property_sale_exclusion_terms", [])
    )
    neutral_restoration_hits = find_terms(
        opening, topic.get("neutral_restoration_terms", [])
    )
    no_actual_problem_hits = find_terms(
        full_context, topic.get("no_actual_problem_terms", [])
    )
    neutral_pension_explainer_hits = find_terms(
        opening, topic.get("neutral_pension_explainer_terms", [])
    )
    neutral_construction_progress_hits = find_terms(
        opening, topic.get("neutral_construction_progress_terms", [])
    )
    neutral_event_announcement_hits = find_terms(
        opening, topic.get("neutral_event_announcement_terms", [])
    )
    neutral_hotline_announcement_hits = find_terms(
        opening, topic.get("neutral_hotline_announcement_terms", [])
    )
    neutral_market_overview_hits = find_terms(
        opening, topic.get("neutral_market_overview_terms", [])
    )
    neutral_gardening_hits = find_terms(
        opening, topic.get("neutral_gardening_terms", [])
    )
    construction_problem_exception_hits = find_terms(
        full_context, topic.get("construction_problem_exception_terms", [])
    )
    historical_retrospective_hits = find_terms(
        title, topic.get("historical_retrospective_title_terms", [])
    )
    neutral_monitoring_announcement_hits = find_terms(
        full_context, topic.get("neutral_monitoring_announcement_terms", [])
    )
    private_legal_dispute_hits = find_terms(
        full_context, topic.get("private_legal_dispute_terms", [])
    )
    resident_explicit_signal = any(
        detail["residents"] and detail["explicit"] for detail in details
    )
    lead_context = " ".join([title, *body[:3]])
    lead_public_signal_hits = find_terms(
        lead_context, topic.get("neutral_information_exception_terms", [])
    )
    lead_finding_hits = find_terms(
        lead_context, topic.get("institutional_finding_terms", [])
    )
    lead_persistence_hits = find_terms(
        lead_context, topic.get("persistence_terms", [])
    )
    current_public_signal = bool(
        title_explicit
        or lead_public_signal_hits
        or lead_finding_hits
        or lead_persistence_hits
        or title_emergency
    )

    # Result Integrity 1.1 / Relevance Precision.  The baseline of 10--12
    # August showed that generic words deep in an article ("дефицит",
    # "качество", "очередь", "улица") can turn an unrelated editorial genre
    # into a false social signal.  Exclude the genre only when the title/lead
    # does not contain a real complaint, finding or domestic consumer impact.
    precision_lead = " ".join([title, *body[:5]])
    precision_title = normalize_space(title)
    precision_actual_findings = find_terms(
        precision_lead, precision_terms(topic, "actual_findings")
    )
    precision_public_exception = bool(
        resident_explicit_signal
        or title_explicit
        or precision_actual_findings
        or emergency_issue_hits
        or domestic_consumer_hits
    )
    evidence_indices = set(same_sentence + adjacent + special_evidence_indices)
    lead_bound_evidence = bool(
        title_signal
        or special_public_interest_signal
        or any(index <= 5 for index in evidence_indices)
    )
    precision_region, precision_locality = infer_event_geography(
        precision_title,
        precision_lead,
    )
    precision_domestic_scope = bool(
        preliminary_belarus_hits or precision_region or precision_locality
    )

    genre_rejection = result_integrity_genre_rejection(
        precision_title,
        precision_lead,
        title_explicit=bool(title_explicit),
        resident_explicit=resident_explicit_signal,
        lead_findings=bool(precision_actual_findings),
        persistence=bool(persistence_hits),
        special_public_interest=special_public_interest_signal,
        regulatory_public_interest=bool(
            domestic_regulatory_discussion
            or domestic_public_consultation_problem
        ),
        critical_public_interest=bool(
            domestic_illegal_recruitment
            or domestic_business_loss_signal
            or product_contamination_signal
            or counterfeit_product_signal
            or building_envelope_signal
            or hotel_service_signal
        ),
        lead_bound_evidence=lead_bound_evidence,
        domestic_scope=precision_domestic_scope,
    )
    if genre_rejection:
        return RelevanceDecision(False, reason=genre_rejection)

    sports_story_hits = find_terms(
        precision_title, precision_terms(topic, "sports")
    )
    if sports_story_hits and not (
        resident_explicit_signal and domestic_consumer_hits
    ):
        return RelevanceDecision(
            False, reason=f"спортивный сюжет без социальной проблемы: {sports_story_hits[0]}"
        )

    hobby_story_hits = find_terms(
        precision_title, precision_terms(topic, "hobby_entertainment")
    )
    if hobby_story_hits:
        return RelevanceDecision(
            False,
            reason=f"развлекательный или досуговый сюжет: {hobby_story_hits[0]}",
        )

    commercial_explainer_hits = find_terms(
        precision_title, precision_terms(topic, "commercial_explainer")
    )
    if commercial_explainer_hits and not (
        resident_explicit_signal or precision_actual_findings
    ):
        return RelevanceDecision(
            False,
            reason=f"коммерческий или калькуляторный материал: {commercial_explainer_hits[0]}",
        )

    listing_action_hits = find_terms(
        precision_title, precision_terms(topic, "listing_actions")
    )
    listing_noun_hits = find_terms(
        precision_title, precision_terms(topic, "listing_nouns")
    )
    if listing_action_hits and listing_noun_hits and not (
        resident_explicit_signal or precision_actual_findings
    ):
        return RelevanceDecision(
            False,
            reason=f"объявление или обзор продажи имущества: {listing_action_hits[0]}",
        )

    precision_health_advice_hits = find_terms(
        precision_title, precision_terms(topic, "health_advice")
    )
    if precision_health_advice_hits and not (
        resident_explicit_signal or precision_actual_findings or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"медицинский или пищевой совет: {precision_health_advice_hits[0]}",
        )

    precision_historical_hits = find_terms(
        precision_title, precision_terms(topic, "historical")
    )
    if precision_historical_hits:
        return RelevanceDecision(
            False, reason=f"историческая публикация: {precision_historical_hits[0]}"
        )

    neutral_infrastructure_hits = find_terms(
        precision_title, precision_terms(topic, "neutral_infrastructure")
    )
    if neutral_infrastructure_hits and not (
        resident_explicit_signal or precision_actual_findings or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"нейтральная инфраструктурная или административная мера: {neutral_infrastructure_hits[0]}",
        )

    planned_service_notice_hits = find_terms(
        precision_title, precision_terms(topic, "planned_service_notice")
    )
    if planned_service_notice_hits and not (
        resident_explicit_signal or actual_damage_hits or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"плановое предупреждение без фактического сбоя: {planned_service_notice_hits[0]}",
        )

    routine_monitoring_hits = find_terms(
        precision_title, precision_terms(topic, "routine_monitoring")
    )
    precision_full_findings = find_terms(
        full_context, precision_terms(topic, "actual_findings")
    )
    if routine_monitoring_hits and not (
        resident_explicit_signal or precision_full_findings or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"проверка или мониторинг без выявленной проблемы: {routine_monitoring_hits[0]}",
        )

    external_trade_balance_hits = find_terms(
        full_context, precision_terms(topic, "external_trade_balance")
    )
    if external_trade_balance_hits and not (
        resident_explicit_signal or domestic_consumer_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"макроэкономический торговый баланс без жалобы жителей: {external_trade_balance_hits[0]}",
        )

    financial_market_hits = find_terms(
        precision_title, precision_terms(topic, "financial_market")
    )
    if financial_market_hits and not resident_explicit_signal:
        return RelevanceDecision(
            False,
            reason=f"финансовая или валютная аналитика без жалобы жителей: {financial_market_hits[0]}",
        )

    neutral_education_advice_hits = find_terms(
        precision_title, precision_terms(topic, "neutral_education_advice")
    )
    if neutral_education_advice_hits and not precision_public_exception:
        return RelevanceDecision(
            False,
            reason=f"нейтральный образовательный анонс: {neutral_education_advice_hits[0]}",
        )

    generic_greeting_hits = find_terms(
        precision_title, precision_terms(topic, "generic_greeting")
    )
    if generic_greeting_hits:
        return RelevanceDecision(
            False, reason=f"служебное приветствие без заголовка: {generic_greeting_hits[0]}"
        )

    opinion_debate_hits = find_terms(
        precision_title, precision_terms(topic, "opinion_debate")
    )
    if opinion_debate_hits and not (
        precision_actual_findings or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"публицистическая дискуссия без социальной жалобы: {opinion_debate_hits[0]}",
        )

    recreation_advice_hits = find_terms(
        precision_title, precision_terms(topic, "recreation_advice")
    )
    if recreation_advice_hits and not (
        resident_explicit_signal or precision_actual_findings or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"туристический или рекреационный совет: {recreation_advice_hits[0]}",
        )

    neutral_household_advice_hits = find_terms(
        precision_title, precision_terms(topic, "neutral_household_advice")
    )
    if neutral_household_advice_hits and not precision_actual_findings:
        return RelevanceDecision(
            False,
            reason=f"бытовая инструкция без жалобы: {neutral_household_advice_hits[0]}",
        )

    foreign_military_crime_hits = find_terms(
        precision_lead, precision_terms(topic, "foreign_military_crime")
    )
    if len(foreign_military_crime_hits) >= 2:
        return RelevanceDecision(
            False,
            reason="зарубежный криминально-военный сюжет без проблемы для жителей Беларуси",
        )

    personal_health_incident_hits = find_terms(
        precision_title, precision_terms(topic, "personal_health_incident")
    )
    if personal_health_incident_hits and not (
        title_explicit or resident_explicit_signal or domestic_consumer_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"единичный медицинский инцидент без проблемы здравоохранения: {personal_health_incident_hits[0]}",
        )

    human_interest_profile_hits = find_terms(
        precision_title, precision_terms(topic, "human_interest_profile")
    )
    if human_interest_profile_hits and not title_explicit:
        return RelevanceDecision(
            False,
            reason=f"персональный портрет без социальной проблемы: {human_interest_profile_hits[0]}",
        )

    neutral_benefit_explainer_hits = find_terms(
        precision_title, precision_terms(topic, "neutral_benefit_explainer")
    )
    if neutral_benefit_explainer_hits and not (
        title_explicit
        or resident_explicit_signal
        or precision_actual_findings
        or domestic_regulatory_discussion
    ):
        return RelevanceDecision(
            False,
            reason=f"нейтральная инструкция по льготе или выплате: {neutral_benefit_explainer_hits[0]}",
        )

    multi_story_digest_hits = find_terms(
        precision_title, precision_terms(topic, "multi_story_digest")
    )
    if multi_story_digest_hits:
        return RelevanceDecision(
            False,
            reason=f"многосюжетный видеодайджест без одного события: {multi_story_digest_hits[0]}",
        )

    neighbour_dispute_hits = find_terms(
        precision_title, precision_terms(topic, "neighbour_dispute")
    )
    if neighbour_dispute_hits and not precision_actual_findings:
        return RelevanceDecision(
            False,
            reason=f"частный соседский спор: {neighbour_dispute_hits[0]}",
        )

    neutral_reporting_channel_hits = find_terms(
        precision_title, precision_terms(topic, "neutral_reporting_channel")
    )
    if neutral_reporting_channel_hits and not precision_actual_findings:
        return RelevanceDecision(
            False,
            reason=f"анонс канала обращений без подтвержденной проблемы: {neutral_reporting_channel_hits[0]}",
        )

    precision_routine_enforcement_hits = find_terms(
        precision_title, precision_terms(topic, "routine_enforcement")
    )
    if precision_routine_enforcement_hits and not precision_public_exception:
        return RelevanceDecision(
            False,
            reason=f"обычное правоохранительное сообщение: {precision_routine_enforcement_hits[0]}",
        )

    precision_routine_rescue_hits = find_terms(
        precision_title, precision_terms(topic, "routine_rescue")
    )
    if precision_routine_rescue_hits and not (
        incident_exception_hits or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"обычное спасательное сообщение: {precision_routine_rescue_hits[0]}",
        )

    precision_single_incident_hits = find_terms(
        precision_title, precision_terms(topic, "single_incident")
    )
    if precision_single_incident_hits and not (
        resident_explicit_signal or incident_exception_hits or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"единичный происшественный сюжет: {precision_single_incident_hits[0]}",
        )

    neutral_positive_hits = find_terms(
        precision_title, precision_terms(topic, "neutral_positive_feature")
    )
    if neutral_positive_hits and not precision_public_exception:
        return RelevanceDecision(
            False,
            reason=f"позитивный репортаж без социальной проблемы: {neutral_positive_hits[0]}",
        )

    obituary_hits = find_terms(
        precision_title, precision_terms(topic, "obituary")
    )
    if obituary_hits:
        return RelevanceDecision(False, reason=f"некролог: {obituary_hits[0]}")

    precision_foreign_hits = find_terms(
        precision_title, precision_terms(topic, "foreign_non_belarus")
    )
    if precision_foreign_hits and not (
        belarus_title_hits or resident_explicit_signal or domestic_consumer_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"иностранный сюжет без воздействия на жителей Беларуси: {precision_foreign_hits[0]}",
        )

    # Медицинские советы и профилактические интервью не являются жалобами
    # на доступность или качество здравоохранения. Сохраняем их только при
    # прямой жалобе в заголовке, официальном нарушении или длительной проблеме.
    if health_advice_hits and not (
        current_public_signal
        or finding_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"медицинский совет или профилактический материал: {health_advice_hits[0]}",
        )

    # Позитивные репортажи о строящихся мостах, дорогах и иных объектах
    # не являются социально-экономической проблемой без жалобы на сроки,
    # качество, доступность или иной конкретный недостаток.
    if positive_infrastructure_hits and not (
        current_public_signal
        or finding_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"нейтральный инфраструктурный проект: {positive_infrastructure_hits[0]}",
        )

    # Продажа имущества, аукционы и поиск нового владельца — экономические
    # новости, но не жалобы жителей сами по себе.
    if property_sale_hits and not (
        current_public_signal
        or finding_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"нейтральная продажа имущества или торги: {property_sale_hits[0]}",
        )

    # Сообщение о восстановлении штатной работы без фактического сбоя,
    # ущерба или жалоб не является социальной проблемой. Отрицательные слова
    # внутри формул «перебоев не было» и «не связано с аварией» не должны
    # создавать ложный сигнал.
    if (
        neutral_restoration_hits
        and no_actual_problem_hits
        and not (
            resident_explicit_signal
            or persistence_hits
            or actual_damage_hits
            or emergency_issue_hits
        )
    ):
        return RelevanceDecision(
            False,
            reason=(
                "штатное восстановление без фактической проблемы: "
                f"{neutral_restoration_hits[0]}"
            ),
        )

    # Справка о пенсионном возрасте и требуемом стаже остается нейтральным
    # разъяснением, пока в материале нет реальной общественной дискуссии,
    # жалоб граждан или выявленного нарушения их прав.
    if neutral_pension_explainer_hits and not (
        domestic_regulatory_discussion
        or resident_explicit_signal
        or persistence_hits
        or emergency_issue_hits
        or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=(
                "нейтральное пенсионное разъяснение: "
                f"{neutral_pension_explainer_hits[0]}"
            ),
        )

    # Обычный отчет о ходе строительства не является проблемным материалом.
    # Сохраняем сообщения о срыве сроков, дефектах, опасности, жалобах и
    # иных конкретных негативных последствиях.
    if neutral_construction_progress_hits and not (
        current_public_signal
        or finding_hits
        or persistence_hits
        or actual_damage_hits
        or construction_problem_exception_hits
    ):
        return RelevanceDecision(
            False,
            reason=(
                "нейтральный отчет о ходе строительства: "
                f"{neutral_construction_progress_hits[0]}"
            ),
        )

    # Анонс форума, конференции или просветительского мероприятия
    # не является социальной проблемой только из-за слов «дефицит»,
    # «здоровье» или «анализы» в программе.
    if neutral_event_announcement_hits and not (
        resident_explicit_signal
        or lead_finding_hits
        or lead_persistence_hits
        or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=(
                "нейтральный анонс мероприятия: "
                f"{neutral_event_announcement_hits[0]}"
            ),
        )

    # Анонс горячей линии с перечнем тем для возможных обращений не
    # подтверждает, что перечисленные нарушения уже выявлены.
    if neutral_hotline_announcement_hits and not (
        title_explicit
        or resident_explicit_signal
        or lead_finding_hits
        or lead_persistence_hits
        or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=(
                "анонс горячей линии без подтвержденной проблемы: "
                f"{neutral_hotline_announcement_hits[0]}"
            ),
        )

    # Обычный обзор цен и ассортимента на рынке не является жалобой без
    # подтвержденного дефицита, нарушения или претензий покупателей.
    if neutral_market_overview_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=(
                "нейтральный обзор цен или ассортимента: "
                f"{neutral_market_overview_hits[0]}"
            ),
        )

    # Садовые инструкции не должны попадать в ЖКХ по фразам «без воды»,
    # «дефицит» и названиям удобрений.
    if neutral_gardening_hits and not (
        resident_explicit_signal
        or finding_hits
        or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=(
                "садовый или огородный совет: "
                f"{neutral_gardening_hits[0]}"
            ),
        )

    # Исторические ретроспективы не являются текущей социальной проблемой
    # только из-за слов «дефицит», «цены» или «нехватка» в описании прошлого.
    if historical_retrospective_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"историческая ретроспектива: {historical_retrospective_hits[0]}",
        )

    # Анонс будущего мониторинга или инструкция, куда сообщать о возможной
    # нехватке, не подтверждает, что дефицит уже существует.
    if neutral_monitoring_announcement_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=(
                "анонс мониторинга без подтвержденной проблемы: "
                f"{neutral_monitoring_announcement_hits[0]}"
            ),
        )

    # Единичные семейные споры о дарении/наследовании имущества не являются
    # системной социально-экономической проблемой без более широкого сигнала.
    if private_legal_dispute_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or domestic_regulatory_discussion
        or domestic_public_consultation_problem
    ):
        return RelevanceDecision(
            False,
            reason=f"частный имущественный спор: {private_legal_dispute_hits[0]}",
        )

    if forecast_hits and not (strong_explicit or finding_hits or actual_damage_hits):
        return RelevanceDecision(False, reason=f"прогноз без фактической социальной проблемы: {forecast_hits[0]}")

    # Рыночные прогнозы, курсы валют и инвестиционные сценарии не являются
    # жалобами жителей. Оставляем материал только при прямой потребительской
    # жалобе, официально установленном нарушении или конкретном ущербе.
    if financial_forecast_hits and not (
        resident_explicit_signal or finding_hits or actual_damage_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"финансовый или рыночный прогноз: {financial_forecast_hits[0]}",
        )

    # Научно-популярные и сравнительные исследования здоровья/образа жизни
    # не должны проходить по словам «дефицит», «жители» или «проблема».
    # Исключение — конкретный внутренний сбой общественной услуги либо
    # официально выявленное нарушение в Беларуси.
    if research_hits and not (
        research_exception_hits
        or resident_explicit_signal
        or finding_hits
        or persistence_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"обзор исследования без конкретной жалобы жителей: {research_hits[0]}",
        )

    # Портреты руководителей, интервью и репортажи «кто управляет» могут
    # содержать общие фразы «мы иногда жалуемся» и «важно качество».
    # Без прямой жалобы жителей, устойчивой проблемы или проверки это не
    # самостоятельный социально-экономический сигнал.
    if profile_hits and not (
        title_signal
        or resident_explicit_signal
        or finding_hits
        or persistence_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"портретный или имиджевый материал: {profile_hits[0]}",
        )

    # Кадровые назначения и представление новых руководителей не являются
    # социально-экономической проблемой без самостоятельной жалобы или
    # официально выявленного нарушения работы учреждения.
    if personnel_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"кадровое назначение без социальной проблемы: {personnel_hits[0]}",
        )

    # Запуск нового сервиса, маршрута или услуги — нейтральная новость.
    # Она остаётся только при прямой жалобе пользователей либо установленном
    # нарушении качества/доступности.
    if service_launch_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"нейтральный запуск сервиса или услуги: {service_launch_hits[0]}",
        )

    # Законопроекты и анонсы новых правил не должны проходить только потому,
    # что в обосновании упомянуты общие случаи некачественной работы.
    if regulatory_reform_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or emergency_issue_hits
        or domestic_regulatory_discussion
    ):
        return RelevanceDecision(
            False,
            reason=f"регуляторная инициатива без конкретной жалобы: {regulatory_reform_hits[0]}",
        )

    # Зарубежный путевой очерк не становится социальной проблемой из-за
    # случайных слов «проблемы», «дорога», «озеро» или «обмелевшее».
    # Исключение допускается только для самостоятельной претензии белорусского
    # потребителя к оплаченной услуге или нарушения его прав.
    if foreign_hits and foreign_travelogue_hits and not domestic_consumer_hits:
        return RelevanceDecision(
            False,
            reason=f"туристическая тема: зарубежный путевой очерк — {foreign_travelogue_hits[0]}",
        )

    # Туристические рейтинги, направления отдыха и происшествия за рубежом,
    # связанные с Беларусью лишь упоминанием белорусских туристов, исключаются.
    if tourism_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or domestic_consumer_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"туристическая или рекреационная тема: {tourism_hits[0]}",
        )
    if foreign_hits and foreign_tourism_bridge_hits and not (
        resident_explicit_signal
        or finding_hits
        or persistence_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"зарубежное событие с формальной связью с Беларусью: {foreign_tourism_bridge_hits[0]}",
        )

    # Единичные дорожные нарушения и опасные манёвры — происшественная
    # хроника, а не жалоба на общественную услугу или инфраструктуру.
    if traffic_incident_hits and not (
        resident_explicit_signal
        or persistence_hits
        or emergency_issue_hits
        or incident_exception_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"единичный дорожный инцидент: {traffic_incident_hits[0]}",
        )

    # Сама по себе стихия не является социально-экономической жалобой.
    # Оставляем только сообщения о бездействии/задержке служб, длительном
    # отсутствии базовых услуг, жалобах жителей или вынужденной самоорганизации.
    if emergency_event_hits and not emergency_issue_hits:
        return RelevanceDecision(
            False,
            reason=(
                "последствия стихии без жалобы на работу служб: "
                f"{emergency_event_hits[0]}"
            ),
        )

    if planned_hits and not (
        current_public_signal
        or finding_hits
        or emergency_issue_hits
    ):
        return RelevanceDecision(False, reason=f"плановое сервисное сообщение: {planned_hits[0]}")

    # Заголовки формата «советы/рекомендации/как сделать» исключаем,
    # даже если внутри встречается общая фраза вроде «проблемы с сердцем».
    # Оставляем только материалы, где есть самостоятельный общественный сигнал:
    # жители/пользователи, повторные обращения, жалоба прямо в заголовке
    # либо официально выявленные нарушения.
    # Слова «люди/жители» в справочном материале сами по себе не превращают
    # его в жалобу. Для заголовков формата «как сделать/как получить/советы»
    # требуем прямое обращение или жалобу, затянувшуюся проблему, официально
    # выявленное нарушение либо специальный сигнал по чрезвычайной ситуации.
    neutral_exception_hits = find_terms(
        full_context, topic.get("neutral_information_exception_terms", [])
    )
    advisory_public_signal = bool(
        neutral_exception_hits
        or finding_hits
        or emergency_issue_hits
    )
    if neutral_title_hits and not advisory_public_signal:
        return RelevanceDecision(False, reason=f"справочный или рекомендательный материал: {neutral_title_hits[0]}")

    # Благотворительные кампании, сборы и акции поддержки сами по себе
    # не являются жалобами жителей. Сохраняем их только когда материал
    # одновременно описывает конкретную претензию к службам/властям,
    # официально выявленное нарушение или затянувшуюся нерешённую проблему.
    if support_campaign_hits and not (
        support_campaign_exception_hits
        or finding_hits
        or emergency_issue_hits
        or persistence_hits
    ):
        return RelevanceDecision(
            False,
            reason=f"нейтральная благотворительная или социальная кампания: {support_campaign_hits[0]}",
        )

    if macro_hits and not (
        strong_explicit
        or resident_hits
        or domestic_consumer_hits
        or bounded_issue_profiles
        & {"real_income_pressure", "employment_contraction"}
    ):
        return RelevanceDecision(False, reason=f"макроэкономическая тема без жалобы жителей: {macro_hits[0]}")

    # Обычные рейды, профилактические акции и усиление контроля ГАИ/милиции
    # не являются жалобами на доступность государственной услуги.
    if routine_enforcement_hits and not (strong_explicit or finding_hits or persistence_hits):
        return RelevanceDecision(
            False,
            reason=f"служебное профилактическое сообщение: {routine_enforcement_hits[0]}",
        )

    # Поиск пропавших, спасательные операции и единичные несчастные случаи
    # не относятся к социально-экономическим жалобам. Исключение — когда
    # публикация прямо описывает отказ или недоступность общественной службы.
    if incident_hits and not (incident_exception_hits or emergency_issue_hits):
        return RelevanceDecision(
            False,
            reason=f"поисково-спасательная/происшественная тема: {incident_hits[0]}",
        )

    # Export/import certification disputes are not resident complaints unless
    # the article also describes a domestic retail or consumer problem.
    if trade_hits and not (
        domestic_consumer_hits
        or resident_hits
        or "domestic_product_safety_enforcement" in bounded_issue_profiles
    ):
        return RelevanceDecision(False, reason=f"внешнеторговая/корпоративная тема: {trade_hits[0]}")

    if leisure_hits and not strong_explicit:
        return RelevanceDecision(False, reason=f"нерелевантный формат: {leisure_hits[0]}")

    # A generic salary/product word inside a criminal report is insufficient.
    # Keep only criminal cases that explicitly concern a social-rights
    # violation such as wage arrears or unsafe public services.
    crime_exception_hits = find_terms(
        full_context, topic.get("crime_relevance_exception_terms", [])
    )
    if crime_hits and not (
        crime_exception_hits
        or domestic_illegal_recruitment
        or "labour_rights_enforcement" in bounded_issue_profiles
    ):
        return RelevanceDecision(False, reason=f"криминальная/происшественная тема: {crime_hits[0]}")

    # Плесень, сырость, затопление и испорченное имущество внутри дома
    # относятся к состоянию жилья, а не к качеству товара.
    housing_category = "ЖКХ и состояние жилья"
    quality_category = "Качество товаров и услуг"
    housing_context_hits = find_terms(
        full_context, topic.get("housing_context_terms", [])
    )
    product_context_hits = find_terms(
        full_context, topic.get("product_quality_context_terms", [])
    )
    housing_problem_hits = find_terms(
        full_context, topic.get("housing_problem_terms", [])
    )
    if (
        housing_category in category_weights
        and housing_context_hits
        and housing_problem_hits
    ):
        category_weights[housing_category] += 10
        if quality_category in category_weights and not product_context_hits:
            category_weights[quality_category] = max(
                0, category_weights[quality_category] - 5
            )

    # Жалобы на мобильную связь, интернет и телевидение должны
    # классифицироваться в профильную категорию, даже если в тексте есть
    # общие слова «качество», «сервис» или «платная услуга».
    telecom_category = "Связь, интернет и телевидение"
    telecom_context_hits = find_terms(
        full_context, categories.get(telecom_category, [])
    )
    if telecom_category in category_weights and telecom_context_hits:
        category_weights[telecom_category] += 12
        if quality_category in category_weights:
            category_weights[quality_category] = max(
                0, category_weights[quality_category] - 4
            )

    # Жалобы на бесплатную стажировку, отказ в трудоустройстве и условия
    # на рабочем месте относятся к труду, даже если событие произошло
    # в магазине или связано с платной бытовой услугой.
    work_category = "Работа, зарплаты и доходы"
    price_category = "Цены, торговля и дефицит"
    employment_context_hits = find_terms(
        full_context, topic.get("employment_context_terms", [])
    )
    if work_category in category_weights and employment_context_hits:
        category_weights[work_category] += 14
        if quality_category in category_weights:
            category_weights[quality_category] = max(
                0, category_weights[quality_category] - 5
            )
        if price_category in category_weights:
            category_weights[price_category] = max(
                0, category_weights[price_category] - 3
            )

    # Ошибочные начисления жильцам за утечку в общежитии — коммунальная
    # проблема, даже если в тексте упоминаются виновные работники КУП.
    communal_overcharge_text = normalized_search_text(full_context)
    communal_overcharge_signal = bool(
        re.search(r"(?:общежит|жильц|жировк)", communal_overcharge_text)
        and re.search(r"(?:утеч|протеч)", communal_overcharge_text)
        and re.search(
            r"(?:платил|списал|начисл|вернул|лишн[а-яёіў]*\s+сумм)",
            communal_overcharge_text,
        )
    )
    if housing_category in category_weights and communal_overcharge_signal:
        category_weights[housing_category] += 22
        if work_category in category_weights:
            category_weights[work_category] = max(
                0, category_weights[work_category] - 12
            )
        if price_category in category_weights:
            category_weights[price_category] = max(
                0, category_weights[price_category] - 5
            )

    # Спор о доступе коммунальников в квартиру при капремонте относится
    # к ЖКХ, даже если в тексте часто встречается слово «работники».
    jkh_access_dispute_hits = find_terms(
        full_context, topic.get("jkh_access_dispute_terms", [])
    )
    if (
        housing_category in category_weights
        and jkh_access_dispute_hits
    ):
        category_weights[housing_category] += 18
        matched_terms.extend(jkh_access_dispute_hits)
        if work_category in category_weights:
            category_weights[work_category] = max(
                0, category_weights[work_category] - 10
            )
        if regulatory_category in category_weights:
            category_weights[regulatory_category] = max(
                0, category_weights[regulatory_category] - 3
            )

    # Жалобы на состояние служебного жилья или жилья для работников —
    # прежде всего проблема жилищных условий, а не оплаты труда.
    provided_housing_context_hits = find_terms(
        full_context, topic.get("provided_housing_context_terms", [])
    )
    provided_housing_problem_hits = find_terms(
        full_context, topic.get("provided_housing_problem_terms", [])
    )
    if (
        housing_category in category_weights
        and provided_housing_context_hits
        and provided_housing_problem_hits
    ):
        category_weights[housing_category] += 18
        matched_terms.extend(
            provided_housing_context_hits + provided_housing_problem_hits
        )
        if work_category in category_weights:
            category_weights[work_category] = max(
                0, category_weights[work_category] - 10
            )
        if quality_category in category_weights:
            category_weights[quality_category] = max(
                0, category_weights[quality_category] - 4
            )

    # Ржавая, мутная или пахнущая вода из крана — проблема коммунальной
    # услуги, а не обычного товара. Усиливаем категорию ЖКХ при явном
    # контексте водоснабжения.
    utility_water_hits = find_terms(
        full_context, topic.get("utility_water_context_terms", [])
    )
    utility_water_quality_hits = find_terms(
        full_context, topic.get("utility_water_quality_terms", [])
    )
    if (
        housing_category in category_weights
        and utility_water_hits
        and utility_water_quality_hits
    ):
        category_weights[housing_category] += 13
        if quality_category in category_weights:
            category_weights[quality_category] = max(
                0, category_weights[quality_category] - 5
            )

    # Отсутствие урн, скамеек и иной инфраструктуры в парках, на аллеях
    # и набережных относится к благоустройству, даже если в тексте много
    # слов о мусоре и санитарном состоянии.
    beautification_category = "Дороги и благоустройство"
    ecology_category = "Экология и санитарные проблемы"
    urban_amenity_hits = find_terms(
        full_context, topic.get("urban_amenity_context_terms", [])
    )
    if beautification_category in category_weights and urban_amenity_hits:
        category_weights[beautification_category] += 10
        if ecology_category in category_weights:
            category_weights[ecology_category] = max(
                0, category_weights[ecology_category] - 4
            )
        if housing_category in category_weights:
            category_weights[housing_category] = max(
                0, category_weights[housing_category] - 3
            )

    # Вырубка деревьев, озеленение улиц и компенсационные посадки —
    # вопросы городской среды и благоустройства. Случайное упоминание
    # гостиницы рядом с местом вырубки не превращает публикацию в жалобу
    # на гостиничную услугу.
    urban_greenery_hits = find_terms(
        full_context, topic.get("urban_greenery_context_terms", [])
    )
    if (
        beautification_category in category_weights
        and urban_greenery_hits
        and (
            explicit_hits
            or public_reaction_hits
            or negative_hits
            or finding_hits
        )
    ):
        category_weights[beautification_category] += 18
        matched_terms.extend(urban_greenery_hits)
        if quality_category in category_weights:
            category_weights[quality_category] = max(
                0, category_weights[quality_category] - 10
            )
        if ecology_category in category_weights:
            category_weights[ecology_category] = max(
                0, category_weights[ecology_category] - 2
            )

    # Паспортные требования для детей, гражданство и условия пересечения
    # границы — вопрос правового регулирования, а не общественного транспорта.
    transport_category = "Общественный транспорт"
    administrative_category = "Государственные и административные услуги"
    if (
        regulatory_category in category_weights
        and cross_border_rights_hits
        and (explicit_hits or negative_hits or finding_hits)
    ):
        category_weights[regulatory_category] += 17
        matched_terms.extend(cross_border_rights_hits)
        if transport_category in category_weights:
            category_weights[transport_category] = max(
                0, category_weights[transport_category] - 8
            )
        if administrative_category in category_weights:
            category_weights[administrative_category] = max(
                0, category_weights[administrative_category] - 3
            )

    # Осыпание штукатурки, дефекты фасада и проблемы после капремонта
    # относятся к состоянию жилого дома, а не к благоустройству улицы.
    construction_category = "Строительство и новостройки"
    if (
        housing_category in category_weights
        and building_envelope_hits
        and building_envelope_problem_hits
    ):
        category_weights[housing_category] += 17
        matched_terms.extend(
            building_envelope_hits + building_envelope_problem_hits
        )
        if beautification_category in category_weights:
            category_weights[beautification_category] = max(
                0, category_weights[beautification_category] - 7
            )
        if construction_category in category_weights:
            category_weights[construction_category] = max(
                0, category_weights[construction_category] - 4
            )

    # Result Integrity 1.7: предмет жалобы важнее буквального имени жилого
    # комплекса. «Северный Берег» — название новостройки, а не природный
    # берег; претензии дольщиков к балконам относятся к строительству.
    normalized_category_context = normalized_search_text(full_context)
    development_complaint_signal = bool(
        re.search(r"(?:дольщик|застройщик|жил[а-яёіў]*\s+комплекс|новострой)", normalized_category_context)
        and re.search(r"(?:балкон|фасад|проект|строят\s+другое|продают\s+одно)", normalized_category_context)
        and re.search(r"(?:жалоб|пожаловал|претензи|не\s+соответств)", normalized_category_context)
    )
    if development_complaint_signal and construction_category in category_weights:
        category_weights[construction_category] += 24
        if "Земля, водоёмы и доступ к природе" in category_weights:
            category_weights["Земля, водоёмы и доступ к природе"] = max(
                0, category_weights["Земля, водоёмы и доступ к природе"] - 16
            )

    # Очередь, невозможность записаться или пройти осмотр у гинеколога —
    # доступность здравоохранения, даже если в статье обсуждается график работы.
    healthcare_category = "Здравоохранение"
    healthcare_access_signal = bool(
        re.search(r"(?:гинеколог|поликлиник|амбулатори|врач|медицинск[а-яёіў]*\s+осмотр)", normalized_category_context)
        and re.search(r"(?:очеред|не\s+попаст|невозможн[а-яёіў]*\s+записат|как\s+пройти|нет\s+талон)", normalized_category_context)
    )
    if healthcare_access_signal and healthcare_category in category_weights:
        category_weights[healthcare_category] += 24
        if work_category in category_weights:
            category_weights[work_category] = max(0, category_weights[work_category] - 12)

    # Заваленная мусором контейнерная площадка относится к благоустройству /
    # коммунальному содержанию территории, а не к трудовым отношениям.
    waste_site_signal = bool(
        re.search(r"контейнерн[а-яёіў]*\s+площадк", normalized_category_context)
        and re.search(r"(?:завален|переполнен|не\s+вывоз|куч[а-яёіў]*\s+мусор|мусор)", normalized_category_context)
        and re.search(r"(?:жалоб|пожаловал|обращен|что\s+ответил)", normalized_category_context)
    )
    if waste_site_signal and beautification_category in category_weights:
        category_weights[beautification_category] += 24
        if work_category in category_weights:
            category_weights[work_category] = max(0, category_weights[work_category] - 14)

    # Свежая статистика базы незанятых относится к занятости и доходам, а
    # упоминание повышенной коммунальной оплаты описывает последствие статуса.
    inactive_population_signal = bool(
        re.search(r"(?:нетунеядц|не\s+занят[а-яёіў]*\s+в\s+экономик)", normalized_category_context)
        and re.search(r"(?:сколько|числ|количеств|свеж[а-яёіў]*\s+данн|белстат)", normalized_category_context)
    )
    if inactive_population_signal and work_category in category_weights:
        category_weights[work_category] += 22
        if housing_category in category_weights:
            category_weights[housing_category] = max(0, category_weights[housing_category] - 10)

    # Постановление МАРТ о нормативе отечественного ассортимента — изменение
    # регулирования торговли; критика качества остаётся содержанием, но не
    # должна перетягивать карточку в категорию единичного качества товара.
    retail_regulation_signal = bool(
        re.search(r"(?:март|министерств[а-яёіў]*\s+антимонопольн)[а-яёіў]*", normalized_category_context)
        and re.search(r"(?:постановлени|норматив)[а-яёіў]*.*(?:ассортимент|белорусск[а-яёіў]*\s+товар)", normalized_category_context)
    )
    if retail_regulation_signal and regulatory_category in category_weights:
        category_weights[regulatory_category] += 22
        if quality_category in category_weights:
            category_weights[quality_category] = max(0, category_weights[quality_category] - 8)

    # Комплекс коммунальных вопросов на выездном приёме (отопление, вода,
    # содержание территории) относится к ЖКХ, а не к качеству товара.
    communal_resident_issue_signal = bool(
        re.search(r"(?:отоплен|водоснабж|жкх|контейнерн[а-яёіў]*\s+площадк)", normalized_category_context)
        and re.search(r"(?:прием\s+граждан|приём\s+граждан|жалоб|обращен|жител)", normalized_category_context)
    )
    if communal_resident_issue_signal and housing_category in category_weights:
        category_weights[housing_category] += 22
        if quality_category in category_weights:
            category_weights[quality_category] = max(0, category_weights[quality_category] - 10)

    # Жалобы гостей на состояние гостиницы описывают качество коммерческой
    # услуги, а не состояние жилого фонда.
    if (
        quality_category in category_weights
        and hotel_service_signal
    ):
        category_weights[quality_category] += 16
        matched_terms.extend(
            hotel_service_hits + hotel_service_problem_hits
        )
        if housing_category in category_weights:
            category_weights[housing_category] = max(
                0, category_weights[housing_category] - 8
            )

    # Проблемное общественное обсуждение белорусского проекта (например,
    # отсутствие материалов) относится к общественному регулированию.
    if domestic_public_consultation_problem and regulatory_category in category_weights:
        category_weights[regulatory_category] += 8
        for competing_category in (
            "Здравоохранение",
            "Строительство и новостройки",
            "ЖКХ и состояние жилья",
        ):
            if competing_category in category_weights:
                category_weights[competing_category] = max(
                    0, category_weights[competing_category] - 5
                )

    # Контекстные слова «магазин/продукт» не должны перетягивать материал
    # о браке и ненадлежащем качестве в ценовую категорию.
    quality_evidence = " ".join([title, *body])
    if quality_category in category_weights and contains_any(
        quality_evidence,
        [
            "качеств", "якас", "некачествен", "няякасн", "брак",
            "просрочен", "сапсаван",
            "re:кишечн[а-яёіўa-z0-9]*\\s+палоч[а-яёіўa-z0-9]*",
            "сальмонел",
            "листер", "опасн продукт", "небезопасн продукт",
        ],
    ):
        category_weights[quality_category] += 12

    # Жалобы на мобильную связь, интернет и телевидение относятся к
    # самостоятельному блоку услуг связи. Слова «качество» и «сервис»
    # не должны перетягивать такие публикации в общую категорию товаров.
    telecom_category = "Связь, интернет и телевидение"
    if (
        telecom_category in category_weights
        and category_weights[telecom_category] > 0
    ):
        category_weights[telecom_category] += 10
        if quality_category in category_weights:
            category_weights[quality_category] = max(
                0, category_weights[quality_category] - 4
            )

    # Незаконное перекрытие прохода к берегу, заборы до воды и самовольное
    # занятие прибрежной полосы относятся к доступу к природным территориям,
    # а не к экологическому состоянию воды.
    land_water_category = "Земля, водоёмы и доступ к природе"
    shore_access_hits = find_terms(
        full_context, topic.get("shore_access_context_terms", [])
    )
    if (
        land_water_category in category_weights
        and shore_access_hits
        and (explicit_hits or negative_hits or finding_hits or persistence_hits)
    ):
        category_weights[land_water_category] += 20
        matched_terms.extend(shore_access_hits)
        if ecology_category in category_weights:
            category_weights[ecology_category] = max(
                0, category_weights[ecology_category] - 9
            )
        if beautification_category in category_weights:
            category_weights[beautification_category] = max(
                0, category_weights[beautification_category] - 3
            )

    # Загрязнение, сбросы, гибель рыбы, обмеление и цветение воды относятся
    # к экологии. Само упоминание реки или озера не должно перетягивать сюда
    # публикацию о незаконном перекрытии берега.
    natural_waterbody_hits = find_terms(
        full_context, topic.get("natural_waterbody_context_terms", [])
    )
    natural_water_problem_hits = find_terms(
        full_context, topic.get("natural_water_environment_problem_terms", [])
    )
    if (
        ecology_category in category_weights
        and natural_waterbody_hits
        and natural_water_problem_hits
        and not utility_water_hits
        and not shore_access_hits
    ):
        category_weights[ecology_category] += 16
        matched_terms.extend(natural_waterbody_hits + natural_water_problem_hits)
        if housing_category in category_weights:
            category_weights[housing_category] = max(
                0, category_weights[housing_category] - 6
            )
        if land_water_category in category_weights:
            category_weights[land_water_category] = max(
                0, category_weights[land_water_category] - 4
            )

    # Публичные жалобы на ненадлежащие условия содержания животных —
    # самостоятельная социальная тема. Слова «вода» и «фильтрация» в таком
    # материале не должны ошибочно превращать его в сюжет о ЖКХ.
    if animal_welfare_category in category_weights and animal_welfare_signal:
        if housing_category in category_weights:
            category_weights[housing_category] = max(
                0, category_weights[housing_category] - 10
            )

    category_match_counts: dict[str, int] = {name: 0 for name in categories}
    for category, hits in title_categories.items():
        category_match_counts[category] += len(set(hits))
    for detail in details:
        for category, hits in detail["categories"].items():
            category_match_counts[category] += len(set(hits))

    primary_category = max(
        category_weights,
        key=lambda name: (category_weights[name], category_match_counts[name]),
    )
    primary_hits: list[str] = []
    if primary_category in title_categories:
        primary_hits.extend(title_categories[primary_category])
    for detail in details:
        primary_hits.extend(detail["categories"].get(primary_category, []))
    primary_hits = [display_term(term) for term in unique_values(primary_hits)]

    score = 0
    if title_signal:
        score += 5
    score += min(8, len(set(same_sentence)) * 4)
    if adjacent:
        score += 2
    if explicit_hits:
        score += 2
    if finding_hits:
        score += 2
    if persistence_hits:
        score += 1
    if belarus_hits or locality_hits or source.country != "Беларусь":
        score += 1
    if special_public_interest_signal:
        score += 6
    if score < int(topic.get("minimum_score", 5)):
        return RelevanceDecision(False, reason=f"недостаточный балл: {score}")

    if domestic_illegal_recruitment:
        signal_type = "общественно значимое противоправное действие"
    elif domestic_regulatory_discussion:
        signal_type = "общественная реакция на закон или проект правил"
    elif domestic_public_consultation_problem:
        signal_type = "проблема общественного обсуждения проекта"
    elif explicit_hits:
        signal_type = "жалоба жителей или пользователей"
    elif finding_hits:
        signal_type = "критический материал или выявленные нарушения"
    else:
        signal_type = "описание конкретной социально-экономической проблемы"

    evidence = sorted(set(same_sentence + adjacent + special_evidence_indices))
    return RelevanceDecision(
        relevant=True,
        category=primary_category,
        subcategory=", ".join(primary_hits[:3]),
        signal_type=signal_type,
        score=score,
        official_response=bool(response_indices),
        title_signal=title_signal,
        evidence_indices=tuple(evidence),
        matched_terms=tuple(display_term(term) for term in unique_values(matched_terms)[:12]),
        reason="",
    )


def sentence_excerpt_score(
    sentence: str,
    decision: RelevanceDecision,
    settings: dict[str, Any],
) -> int:
    topic = settings["topic"]
    score = 0
    primary_terms = topic.get("categories", {}).get(decision.category, [])
    if contains_any(sentence, primary_terms):
        score += 5
    if contains_any(sentence, topic.get("explicit_complaint_terms", [])):
        score += 5
    if not sentence_has_denial(sentence, settings) and contains_any(
        sentence, topic.get("negative_condition_terms", [])
    ):
        score += 4
    if contains_any(sentence, topic.get("institutional_finding_terms", [])):
        score += 4
    if contains_any(sentence, topic.get("resident_terms", [])):
        score += 2
    if contains_any(sentence, topic.get("official_response_terms", [])):
        score += 4
    if contains_any(sentence, topic.get("persistence_terms", [])):
        score += 1
    return score


def compress_sentence_exact(sentence: str, focus_terms: list[str], max_chars: int) -> str:
    sentence = normalize_space(sentence)
    if len(sentence) <= max_chars:
        return sentence
    clauses = [
        part.strip()
        for part in re.split(r"(?<=[,;:—–])\s+", sentence)
        if part.strip()
    ]
    focus_indices = [
        index for index, clause in enumerate(clauses)
        if contains_any(clause, focus_terms)
    ]
    if focus_indices:
        selected = focus_indices[0]
        fragment = clauses[selected]
        for neighbour in (selected + 1, selected - 1):
            if 0 <= neighbour < len(clauses):
                proposal = (
                    f"{fragment} {clauses[neighbour]}"
                    if neighbour > selected else f"{clauses[neighbour]} {fragment}"
                )
                if len(proposal) <= max_chars - 8:
                    fragment = proposal
        prefix = "[…] " if selected > 0 else ""
        suffix = " […]" if selected < len(clauses) - 1 else ""
        return (prefix + fragment + suffix).rstrip()
    clipped = sentence[: max(20, max_chars - 4)].rsplit(" ", 1)[0].rstrip(" ,;:—–")
    return clipped + " […]"


def exact_excerpt(
    article_text: str,
    summary: str,
    decision: RelevanceDecision,
    settings: dict[str, Any],
) -> str:
    sentences = split_sentences(article_text or summary)
    if not sentences:
        return ""
    max_sentences = int(settings["report"].get("max_excerpt_sentences", 7))
    max_chars = int(settings["report"].get("max_excerpt_characters", 2400))

    chosen: set[int] = set(decision.evidence_indices)
    # Добавляем соседний контекст вокруг основных доказательств.
    for index in list(chosen):
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < len(sentences):
                chosen.add(neighbour)

    scored = sorted(
        (
            (sentence_excerpt_score(sentence, decision, settings), index)
            for index, sentence in enumerate(sentences)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    for score, index in scored:
        if score <= 0 or len(chosen) >= max_sentences:
            break
        chosen.add(index)

    if not chosen:
        chosen.add(0)
    ordered = sorted(chosen)[:max_sentences]

    focus_terms = list(decision.matched_terms)
    selected_sentences: list[tuple[int, str]] = []
    used_chars = 0
    for index in ordered:
        sentence = sentences[index]
        remaining = max_chars - used_chars
        if remaining < 80 and selected_sentences:
            continue
        if len(sentence) > remaining:
            if not selected_sentences:
                sentence = compress_sentence_exact(sentence, focus_terms, max_chars)
            else:
                continue
        selected_sentences.append((index, sentence))
        used_chars += len(sentence) + 5
        if len(selected_sentences) >= max_sentences:
            break

    if not selected_sentences:
        return ""
    pieces: list[str] = []
    previous: int | None = None
    if selected_sentences[0][0] > 0:
        pieces.append("[…]")
    for index, sentence in selected_sentences:
        if previous is not None and index > previous + 1:
            pieces.append("[…]")
        pieces.append(sentence)
        previous = index
    if selected_sentences[-1][0] < len(sentences) - 1:
        pieces.append("[…]")
    return normalize_space(" ".join(pieces)).rstrip()
def resolve_publication_datetime(
    candidate: Candidate,
    extracted_published_at: str = "",
) -> tuple[dt.datetime | None, str]:
    page_date = parse_datetime(extracted_published_at)
    url_date = extract_date_from_url(candidate.url)
    candidate_date = parse_datetime(candidate.published_at)

    # Дата в URL используется как предохранитель против старых архивов.
    # Если она совпадает по дню с более точной датой страницы/RSS, сохраняем
    # точное время. Если URL указывает на более ранний день, считаем его
    # исходной датой публикации и не позволяем sitemap lastmod «омолодить» статью.
    if url_date:
        precise = page_date or candidate_date
        if precise and precise.date() == url_date.date():
            return precise, "page" if page_date else "candidate"
        if precise and url_date.date() < precise.date():
            return url_date, "url"
        if page_date:
            return page_date, "page"
        if candidate_date:
            return candidate_date, "candidate"
        return url_date, "url"

    if page_date:
        return page_date, "page"
    if candidate_date:
        return candidate_date, "candidate"
    return None, ""


def publication_is_allowed(
    candidate: Candidate,
    resolved_date: dt.datetime | None,
    cutoff: dt.datetime | None,
    title_signal: bool,
    allow_undated_sitemap: bool = False,
) -> bool:
    if cutoff and resolved_date and resolved_date < cutoff:
        return False
    # У недатированного sitemap допускаем только заголовок с сильной связкой
    # социальной темы и проблемы. Первый warmup фиксирует текущий хвост sitemap.
    if (
        cutoff
        and resolved_date is None
        and candidate.discovered_via.startswith("sitemap:")
        and not title_signal
        and not allow_undated_sitemap
    ):
        return False
    return True


def metadata_prefilter(
    candidate: Candidate,
    settings: dict[str, Any],
) -> MetadataPrefilterDecision:
    """Cheap title+official-summary triage used only for telemetry in Core 1.

    It never hard-rejects a candidate. Editorial inclusion still depends on the
    full existing relevance function after extraction.
    """
    title = normalize_space(repair_mojibake(candidate.title))
    summary = normalize_space(repair_mojibake(candidate.summary))
    combined = normalize_space(f"{title} {summary}")
    topic = settings.get("topic", {})
    category_terms = [
        term
        for values in topic.get("categories", {}).values()
        for term in values
    ]
    problem_terms = [
        *topic.get("explicit_complaint_terms", []),
        *topic.get("negative_condition_terms", []),
        *topic.get("institutional_finding_terms", []),
        *topic.get("persistence_terms", []),
    ]
    has_topic = contains_any(combined, category_terms)
    has_problem = contains_any(combined, problem_terms)
    title_signal = (
        contains_any(title, category_terms)
        and contains_any(title, problem_terms)
    )

    if has_topic and has_problem:
        return MetadataPrefilterDecision(
            status="strong",
            reason="topic and problem signal already present in metadata",
            title_signal=title_signal,
        )
    if has_topic or has_problem:
        return MetadataPrefilterDecision(
            status="possible",
            reason="partial metadata signal; body still required",
            title_signal=title_signal,
        )
    return MetadataPrefilterDecision(
        status="needs_text",
        reason="metadata is inconclusive; body remains eligible for processing",
        title_signal=False,
    )


def process_candidate_detailed(
    candidate: Candidate,
    settings: dict[str, Any],
    cutoff: dt.datetime | None = None,
    allow_undated_sitemap: bool = False,
    recovery: RecoveryController | None = None,
    recovery_retry: bool = False,
) -> tuple[ArticleResult | None, CandidateProcessingTelemetry]:
    processing_started = time.perf_counter()
    profile = effective_source_profile(candidate.source, settings)
    trace = CandidateProcessingTelemetry(
        url_class=classify_source_url(
            candidate.url,
            candidate.source.domain,
            profile,
        ),
        recovery_retry=recovery_retry,
    )

    def finish(
        result: ArticleResult | None,
    ) -> tuple[ArticleResult | None, CandidateProcessingTelemetry]:
        trace.processing_seconds = time.perf_counter() - processing_started
        return result, trace

    prefilter = metadata_prefilter(candidate, settings)
    trace.prefilter_status = prefilter.status

    if recovery is None:
        # Keep compatibility with existing adapters/tests that monkeypatch the
        # historical two-argument extract_article callable.
        extracted = extract_article(candidate, settings)
    else:
        extracted = extract_article(candidate, settings, recovery=recovery)
    metadata_summary = ""
    if isinstance(extracted, ArticleExtraction):
        title = normalize_space(repair_mojibake(extracted.title))
        text = repair_mojibake(extracted.text)
        extracted_published_at = extracted.published_at
        metadata_summary = repair_mojibake(extracted.metadata_summary)
        trace.transport = extracted.transport
        trace.transport_status = extracted.transport_status
        trace.extraction_strategy = extracted.extraction_strategy
        trace.text_length = len(text)
        trace.html_length = extracted.html_length
        trace.metadata_only = extracted.extraction_strategy == "metadata_description"
        trace.extraction_failed = not bool(text)
        trace.transport_circuit_skipped = extracted.transport_circuit_skipped
        trace.http_seconds = extracted.http_seconds
        trace.extraction_seconds = extracted.extraction_seconds
        trace.chromium_seconds = extracted.chromium_seconds
        trace.chromium_attempts = extracted.chromium_attempts
        trace.http_attempts = extracted.http_attempts
        trace.transport_status_code = extracted.transport_status_code
        trace.transport_failure_class = extracted.transport_failure_class
        trace.http_observations = extracted.http_observations
    else:
        title, text = extracted
        extracted_published_at = ""
        trace.text_length = len(text or "")
        trace.extraction_failed = not bool(text)

    trace.event_published_at = extracted_published_at or candidate.published_at
    event_summary = normalize_space(" ".join([
        candidate.summary,
        metadata_summary,
        candidate.inline_text[:700],
    ]))
    apply_event_fingerprint(
        trace,
        infer_event_fingerprint(
            title or candidate.title,
            event_summary,
            text,
        ),
    )

    # Degraded mode is deliberately non-editorial: metadata-only, empty-body
    # and transport failures enter the retry queue but can never be included
    # automatically in the public report.
    if trace.transport_status == "failed" or trace.metadata_only or trace.extraction_failed:
        if trace.transport_status == "failed":
            trace.degraded_reason = "transport_failed"
        elif trace.metadata_only:
            trace.degraded_reason = "metadata_only"
        else:
            trace.degraded_reason = "extraction_failed"
        trace.final_stage = "degraded_queued"
        return finish(None)

    if recovery_retry:
        trace.recovery_recovered = True

    candidate_title = normalize_space(repair_mojibake(candidate.title))
    if not title or title_is_technical(title):
        title = candidate_title
    if not title:
        title = candidate.url

    decision = evaluate_relevance(
        title,
        candidate.summary,
        text,
        candidate.source,
        settings,
    )
    if not decision.relevant:
        trace.rejection_reason = decision.reason
        trace.final_stage = "relevance_rejected"
        return finish(None)
    trace.relevance_passed = True

    resolved_date, _date_source = resolve_publication_datetime(
        candidate,
        extracted_published_at,
    )
    if resolved_date:
        trace.event_published_at = resolved_date.isoformat()
    if not publication_is_allowed(
        candidate,
        resolved_date,
        cutoff,
        decision.title_signal,
        allow_undated_sitemap=allow_undated_sitemap,
    ):
        trace.final_stage = "date_rejected"
        return finish(None)
    trace.publication_allowed = True

    excerpt = exact_excerpt(
        text,
        candidate.summary,
        decision,
        settings,
    )
    if not excerpt:
        trace.final_stage = "excerpt_empty"
        return finish(None)
    trace.excerpt_built = True

    # A precise excerpt can resolve geography/object/problem that was absent
    # from feed metadata. It may enrich the fingerprint but never falls back to
    # the source newsroom locality.
    apply_event_fingerprint(
        trace,
        infer_event_fingerprint(
            title,
            normalize_space(" ".join([candidate.summary, excerpt])),
            text[:1600],
        ),
    )

    language_sample = " ".join([title, excerpt, text[:900]])
    detected_language = detect_language(
        language_sample,
        candidate.source.language,
    )

    result = ArticleResult(
        source_name=candidate.source.name,
        source_type=candidate.source.media_type,
        country=candidate.source.country,
        locality=candidate.source.locality,
        priority=candidate.source.priority,
        source_language=detected_language,
        title=repair_mojibake(title),
        title_generated=bool(candidate.title_generated),
        url=candidate.url,
        published_at=(
            resolved_date.isoformat()
            if resolved_date else candidate.published_at
        ),
        category=decision.category,
        subcategory=decision.subcategory,
        excerpt=repair_mojibake(excerpt),
        signal_type=decision.signal_type,
        official_response=decision.official_response,
        score=decision.score,
        matched_terms=", ".join(decision.matched_terms),
        discovered_via=candidate.discovered_via,
        text_length=len(text),
        event_region=trace.event_region,
        event_locality=trace.event_locality,
        event_object=trace.event_object,
        event_problem=trace.event_problem,
        event_signature=trace.event_signature,
    )
    trace.final_stage = "included"
    return finish(result)


def process_candidate(
    candidate: Candidate,
    settings: dict[str, Any],
    cutoff: dt.datetime | None = None,
    allow_undated_sitemap: bool = False,
) -> ArticleResult | None:
    result, _trace = process_candidate_detailed(
        candidate,
        settings,
        cutoff,
        allow_undated_sitemap,
    )
    return result


def priority_value(priority: str) -> int:
    return {"A": 0, "B": 1, "C": 2}.get(priority, 9)


def article_identity(result: ArticleResult) -> str:
    return canonicalize_url(result.url)


_EVENT_STOPWORDS = {
    "этот", "эта", "эти", "того", "также", "который", "которая", "которые",
    "после", "перед", "между", "через", "только", "своего", "своей", "свои",
    "более", "менее", "будет", "были", "было", "есть", "стали", "сказал",
    "сообщили", "рассказали", "отметили", "заявили", "минске", "беларуси",
    "жители", "горожане", "люди", "публикации", "материал", "новости",
}

_EVENT_DEVELOPMENT_MARKERS = (
    "после жалоб", "после публикации", "после обращения",
    "решили проблему", "устранили", "отремонтировали",
    "возбудили дело", "вынес суд",
    "на следующий день", "праз скаргі", "пасля публікацыі", "адказалі",
    "вырашылі праблему", "адрамантавалі",
)

_EVENT_ANALYSIS_MARKERS = (
    "анализ", "аналитик", "мнение", "колонка", "разбираемся",
    "что это значит", "почему это важно", "эксперт", "дискуссия",
    "аналіз", "меркаванне", "разбіраемся", "эксперт",
)

_EVENT_ADDRESS_PATTERN = re.compile(
    r"(?<![а-яёіўa-z0-9])(?:"
    r"ул(?:ица|ице|ицы|ицу|\.)?|вул(?:іца|іцы|іцу|\.)?|"
    r"проспект(?:е|а|у)?|пр-т|переул(?:ок|ке|ка)|завул(?:ак|ку|ка)|"
    r"шоссе|набережн(?:ая|ой)|площад(?:ь|и)|пл\."
    r")\s+(?:имени\s+|імя\s+)?"
    r"([а-яёіўa-z0-9][а-яёіўa-z0-9'’.-]*(?:\s+[а-яёіўa-z][а-яёіўa-z'’.-]*){0,2})"
)

_EVENT_ADDRESS_REVERSE_PATTERN = re.compile(
    r"(?<![а-яёіўa-z0-9])([а-яёіўa-z0-9][а-яёіўa-z0-9'’.-]*)\s+"
    r"(?:ул(?:ица|ице|ицы|ицу)|вул(?:іца|іцы|іцу)|"
    r"проспект(?:е|а|у)?|переул(?:ок|ке|ка)|завул(?:ак|ку|ка)|"
    r"шоссе|набережн(?:ая|ой)|площад(?:ь|и))"
)

_EVENT_ADDRESS_TAIL_STOPWORDS = {
    "дом", "дома", "доме", "домов", "корпус", "корпуса", "возле",
    "около", "районе", "остался", "осталась", "остались", "жители",
    "жилец", "жильцы", "нет", "без", "у", "на", "в", "по",
}


def _event_tokens(value: str) -> list[str]:
    words = re.findall(r"[а-яёіўa-z0-9]+", normalize_space(value).lower())
    return [
        word for word in words
        if len(word) >= 4 and word not in _EVENT_STOPWORDS
    ]


def _event_ngrams(tokens: list[str], size: int) -> set[tuple[str, ...]]:
    if len(tokens) < size:
        return set()
    return {
        tuple(tokens[index:index + size])
        for index in range(len(tokens) - size + 1)
    }


def _event_address_anchors(result: ArticleResult) -> set[str]:
    """Return explicit street/place anchors without guessing source locality.

    Event signatures intentionally remain coarse for recovery.  They are not
    sufficient for editorial consolidation: two water outages in Minsk can
    share one signature while happening on different streets.  Explicit
    address anchors therefore act as a hard conflict guard.
    """
    value = normalized_search_text(f"{result.title}. {result.excerpt[:900]}")
    anchors: set[str] = set()
    for match in _EVENT_ADDRESS_PATTERN.finditer(value):
        words = match.group(1).split()
        if words and words[0] not in _EVENT_ADDRESS_TAIL_STOPWORDS:
            # The first token is intentionally conservative.  Capturing more
            # without a named-entity parser tends to absorb ordinary prose
            # after a one-word street name ("улице Ленина остались...").
            anchors.add(words[0])
    for match in _EVENT_ADDRESS_REVERSE_PATTERN.finditer(value):
        if match.group(1) not in _EVENT_ADDRESS_TAIL_STOPWORDS:
            anchors.add(match.group(1))
    return anchors


def _event_numeric_facts(result: ArticleResult) -> set[str]:
    """Extract explicit numeric facts used as a conservative conflict guard."""
    value = normalized_search_text(f"{result.title}. {result.excerpt[:600]}")
    facts: set[str] = set()
    for raw in re.findall(r"(?<![а-яёіўa-z0-9])\d+(?:[.,]\d+)?", value):
        normalized = raw.replace(",", ".").lstrip("0") or "0"
        if normalized.isdigit() and 1900 <= int(normalized) <= 2099:
            continue
        facts.add(normalized)
    return facts


_EVENT_SEMANTIC_CONCEPT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "retail": tuple(re.compile(value) for value in (
        r"магазин[а-яёіў]*", r"торгов[а-яёіў]*", r"продаж[а-яёіў]*",
        r"крама[а-яёіў]*", r"гандл[а-яёіў]*",
    )),
    "product": tuple(re.compile(value) for value in (
        r"продукт[а-яёіў]*", r"товар[а-яёіў]*", r"продукц[а-яёіў]*",
        r"тавар[а-яёіў]*", r"харчов[а-яёіў]*", r"шоколад[а-яёіў]*",
        r"какао-порош[а-яёіў]*", r"спре[йя][а-яёіў]*\s+для\s+ног",
    )),
    "unsafe": tuple(re.compile(value) for value in (
        r"опасн[а-яёіў]*", r"небезопасн[а-яёіў]*", r"небяспечн[а-яёіў]*",
        r"просроч[а-яёіў]*", r"пратэрмін[а-яёіў]*",
        r"не\s+соответств[а-яёіў]*", r"не\s+адпавяда[а-яёіў]*",
        r"нарушени[а-яёіў]*\s+качеств[а-яёіў]*",
    )),
    "enforcement": tuple(re.compile(value) for value in (
        r"(?:кгк|кдк|март|госстандарт)", r"проверк[а-яёіў]*",
        r"выяв[а-яёіў]*", r"обнаруж[а-яёіў]*", r"изъял[а-яёіў]*",
        r"снял[а-яёіў]*\s+с\s+продаж", r"канфіскава[а-яёіў]*",
    )),
    "natural_water": tuple(re.compile(value) for value in (
        r"(?:рек[а-яёіў]*|озер[а-яёіў]*|вадаём[а-яёіў]*|водоем[а-яёіў]*)",
    )),
    "low_water": tuple(re.compile(value) for value in (
        r"обмел[а-яёіў]*", r"маловод[а-яёіў]*", r"узровен[а-яёіў]*\s+вод",
        r"уровен[а-яёіў]*\s+вод[а-яёіў]*.*(?:низк|упал|снизил)",
    )),
    "housing": tuple(re.compile(value) for value in (
        r"дом[а-яёіў]*", r"жиль[яёа-яіў]*", r"жылл[яёа-яіў]*",
    )),
    "construction_defect": tuple(re.compile(value) for value in (
        r"трещин[а-яёіў]*", r"расколін[а-яёіў]*", r"дефект[а-яёіў]*",
        r"не\s+хватает\s+(?:кле|раствор)", r"плох[а-яёіў]*\s+качеств[а-яёіў]*",
        r"(?:дом|жиль|жылл)[а-яёіў]*.*качеств[а-яёіў]*",
        r"качеств[а-яёіў]*.*(?:дом|жиль|жылл)[а-яёіў]*",
    )),
    "complaint": tuple(re.compile(value) for value in (
        r"жал[оу][а-яёіў]*", r"пожаловал[а-яёіў]*", r"скарг[а-яёіў]*",
        r"претензи[а-яёіў]*", r"прэтэнзі[а-яёіў]*",
    )),
    "financial_service": tuple(re.compile(value) for value in (
        r"payoneer", r"банковск[а-яёіў]*\s+счет[а-яёіў]*",
        r"вывод[а-яёіў]*\s+средств", r"вывад[а-яёіў]*\s+сродк",
    )),
    "access_restriction": tuple(re.compile(value) for value in (
        r"финансов[а-яёіў]*\s+ограничени[а-яёіў]*",
        r"не\s+поддержива[а-яёіў]*", r"закрыва[а-яёіў]*\s+счет[а-яёіў]*",
        r"ограничени[а-яёіў]*\s+на\s+вывод[а-яёіў]*",
    )),
    "belarus_affected": tuple(re.compile(value) for value in (
        r"белорус[а-яёіў]*", r"беларус[а-яёіў]*",
    )),
    "food_contamination": tuple(re.compile(value) for value in (
        r"кишечн[а-яёіў]*\s+палоч", r"стафилокок", r"s\.?\s*aureus",
        r"колиформ", r"микробиологическ[а-яёіў]*\s+наруш",
    )),
    "confectionery": tuple(re.compile(value) for value in (
        r"пирожн[а-яёіў]*", r"десерт[а-яёіў]*", r"кондитерск[а-яёіў]*",
        r"улитк[а-яёіў]*", r"кокоск[а-яёіў]*",
    )),
    "pinsk_food_anchor": tuple(re.compile(value) for value in (
        r"пинск[а-яёіў]*", r"пінск[а-яёіў]*", r"пинск[а-яёіў]*\s+кооппром",
    )),
    "communal_housing": tuple(re.compile(value) for value in (
        r"общежити[а-яёіў]*", r"коммунальн[а-яёіў]*", r"ку[пп]",
        r"жировк[а-яёіў]*", r"жильц[а-яёіў]*",
    )),
    "water_leak": tuple(re.compile(value) for value in (
        r"утечк[а-яёіў]*\s+вод", r"протечк[а-яёіў]*", r"текл[а-яёіў]*\s+вод",
        r"неисправн[а-яёіў]*\s+(?:кран|арматур)",
    )),
    "billing_redress": tuple(re.compile(value) for value in (
        r"платил[а-яёіў]*.*за.*(?:утеч|протеч)", r"списал[а-яёіў]*.*жильц",
        r"лишн[а-яёіў]*\s+сумм[а-яёіў]*.*жиров", r"вернул[а-яёіў]*.*рубл",
    )),
    "store_employment": tuple(re.compile(value) for value in (
        r"магазин[а-яёіў]*", r"продавц[а-яёіў]*", r"работник[а-яёіў]*",
        r"нанимател[а-яёіў]*",
    )),
    "hiring_failure": tuple(re.compile(value) for value in (
        r"не\s+может\s+найти", r"год\s+ищем", r"никто\s+не\s+хочет\s+идти",
        r"не\s+может\s+найти\s+продавц",
    )),
    "salary_discussion": tuple(re.compile(value) for value in (
        r"зарплат[а-яёіў]*", r"1500\s+рубл", r"3000\+?",
    )),
    "charged_brand": tuple(re.compile(value) for value in (
        r"чаржед", r"charged",
    )),
    "foot_spray": tuple(re.compile(value) for value in (
        r"спре[йя][а-яёіў]*\s+для\s+ног", r"средств[а-яёіў]*\s+для\s+ног",
    )),
    "boric_acid": tuple(re.compile(value) for value in (
        r"борн[а-яёіў]*\s+кислот",
    )),
    "cocoa_powder": tuple(re.compile(value) for value in (
        r"какао-порош", r"какао\s+порош",
    )),
    "employment_count": tuple(re.compile(value) for value in (
        r"(?:числ|количеств)[а-яёіў]*\s+занят[а-яёіў]*\s+в\s+экономик",
        r"занят[а-яёіў]*\s+в\s+экономик[а-яёіў]*.*(?:снизил|сократил|уменьшил)",
    )),
    "inactive_population_count": tuple(re.compile(value) for value in (
        r"(?:сколько|числ|количеств)[а-яёіў]*.*(?:нетунеядц|не\s+занят[а-яёіў]*\s+в\s+экономик)",
        r"(?:нетунеядц|не\s+занят[а-яёіў]*\s+в\s+экономик).*"
        r"(?:сколько|числ|количеств|свеж[а-яёіў]*\s+данн)",
    )),
    "public_works_control": tuple(re.compile(value) for value in (
        r"(?:кгк|комитет[а-яёіў]*\s+госконтрол)[а-яёіў]*.*"
        r"(?:мост|путепровод|строительств)",
    )),
    "overspend": tuple(re.compile(value) for value in (
        r"(?:необоснованн|излишн|завышенн)[а-яёіў]*\s+(?:трат|расход|стоимост|объем)",
    )),
    "cyprus": tuple(re.compile(value) for value in (r"кипр[а-яёіў]*",)),
    "residency_permit": tuple(re.compile(value) for value in (
        r"вид[а-яёіў]*\s+на\s+жительств",
    )),
    "expired_passport": tuple(re.compile(value) for value in (
        r"просроченн[а-яёіў]*\s+паспорт",
    )),
}


def _event_semantic_profile(result: ArticleResult) -> tuple[set[str], set[str]]:
    """Return language-neutral event families and their supporting concepts."""
    value = normalized_search_text(f"{result.title}. {result.excerpt[:1000]}")
    concepts = {
        concept
        for concept, patterns in _EVENT_SEMANTIC_CONCEPT_PATTERNS.items()
        if any(pattern.search(value) for pattern in patterns)
    }
    families: set[str] = set()
    if {"product", "unsafe"} <= concepts and concepts & {"retail", "enforcement"}:
        families.add("retail_product_safety")
    if {"natural_water", "low_water"} <= concepts:
        families.add("natural_water_level")
    if {"housing", "construction_defect", "complaint"} <= concepts:
        families.add("housing_construction_defect")
    if {"financial_service", "access_restriction", "belarus_affected"} <= concepts:
        families.add("financial_service_access")
    if {"food_contamination", "confectionery", "pinsk_food_anchor"} <= concepts:
        families.add("pinsk_confectionery_safety")
    if {"food_contamination", "confectionery"} <= concepts:
        families.add("confectionery_contamination")
    if {"communal_housing", "water_leak", "billing_redress"} <= concepts:
        families.add("communal_water_overcharge")
    if {"store_employment", "hiring_failure", "salary_discussion"} <= concepts:
        families.add("retail_hiring_shortage")
    if {"product", "unsafe", "charged_brand"} <= concepts:
        families.add("charged_chocolate_recall")
    if {"product", "foot_spray", "boric_acid"} <= concepts:
        families.add("boric_foot_spray_recall")
    if {"product", "cocoa_powder"} <= concepts and concepts & {
        "unsafe", "enforcement"
    }:
        families.add("cocoa_powder_recall")
    if "employment_count" in concepts:
        families.add("employment_contraction_statistic")
    if "inactive_population_count" in concepts:
        families.add("inactive_population_statistic")
    if {"public_works_control", "overspend"} <= concepts:
        families.add("public_works_overspend")
    if {"cyprus", "residency_permit", "expired_passport"} <= concepts:
        families.add("cyprus_residency_facilitation")
    return families, concepts


def _strong_semantic_event_match(
    left: ArticleResult,
    right: ArticleResult,
    shared_numbers: set[str],
) -> bool:
    """Join multilingual rewrites only when family, facts and scope agree."""
    if (
        left.event_region
        and right.event_region
        and normalize_space(left.event_region).casefold()
        != normalize_space(right.event_region).casefold()
    ):
        return False
    left_families, left_concepts = _event_semantic_profile(left)
    right_families, right_concepts = _event_semantic_profile(right)
    shared_families = left_families & right_families
    if not shared_families:
        return False
    left_value = normalized_search_text(f"{left.title}. {left.excerpt[:1200]}")
    right_value = normalized_search_text(f"{right.title}. {right.excerpt[:1200]}")
    if "natural_water_level" in shared_families:
        named_waters = ("припят", "днепр", "неман", "сож", "березин", "двин")
        if any(anchor in left_value and anchor in right_value for anchor in named_waters):
            return True
    if shared_families & {
        "charged_chocolate_recall",
        "boric_foot_spray_recall",
        "cocoa_powder_recall",
        "cyprus_residency_facilitation",
    }:
        return True
    if "employment_contraction_statistic" in shared_families and shared_numbers:
        return True
    if "inactive_population_statistic" in shared_families:
        return True
    if "public_works_overspend" in shared_families and (
        shared_numbers or len(left_concepts & right_concepts) >= 3
    ):
        return True
    if "confectionery_contamination" in shared_families:
        contamination_anchors = (
            re.compile(r"кишечн[а-яёіў]*\s+палоч"),
            re.compile(r"стафилокок"), re.compile(r"s\.?\s*aureus"),
            re.compile(r"пинск[а-яёіў]*"), re.compile(r"улитк[а-яёіў]*"),
            re.compile(r"кокоск[а-яёіў]*"),
        )
        if any(
            pattern.search(left_value) and pattern.search(right_value)
            for pattern in contamination_anchors
        ):
            return True
    if len(left_concepts & right_concepts) < 3:
        return False
    if shared_numbers:
        return True
    if shared_families & {
        "pinsk_confectionery_safety",
        "communal_water_overcharge",
        "retail_hiring_shortage",
    }:
        return True
    if not shared_families & {
        "housing_construction_defect", "financial_service_access",
    }:
        return False
    # Rewrites about the same disputed building often quote different sums
    # and dates. A large shared vocabulary (including person/place anchors)
    # is safer here than requiring an identical number.
    left_tokens = set(_event_tokens(f"{left.title}. {left.excerpt[:1200]}"))
    right_tokens = set(_event_tokens(f"{right.title}. {right.excerpt[:1200]}"))
    return len(left_tokens & right_tokens) >= 10


def _event_marker_profile(result: ArticleResult) -> tuple[bool, bool]:
    # The title is authoritative.  Expert quotations inside an ordinary news
    # report must not turn that report into a standalone analytical article.
    value = normalized_search_text(result.title)
    return (
        contains_any(value, _EVENT_DEVELOPMENT_MARKERS),
        contains_any(value, _EVENT_ANALYSIS_MARKERS),
    )


def _result_datetime(result: ArticleResult) -> dt.datetime | None:
    return parse_datetime(result.published_at)


def _same_event_scope(left: ArticleResult, right: ArticleResult) -> bool:
    left_region = normalize_space(left.event_region).casefold()
    right_region = normalize_space(right.event_region).casefold()
    if left_region and right_region and left_region != right_region:
        return False
    if left.event_locality or right.event_locality:
        # A source's configured locality describes its newsroom, not
        # necessarily the event. When either article has resolved event
        # geography, an unresolved counterpart stays generic.
        left_locality = normalize_space(left.event_locality).lower()
        right_locality = normalize_space(right.event_locality).lower()
    else:
        left_locality = normalize_space(left.locality).lower()
        right_locality = normalize_space(right.locality).lower()

    def generic_locality(value: str) -> bool:
        return (
            value in {"", "беларусь"}
            or "область" in value
            or "вобласць" in value
            or "регион" in value
            or "рэгіён" in value
        )

    if (
        left_locality != right_locality
        and not generic_locality(left_locality)
        and not generic_locality(right_locality)
    ):
        return False

    left_date = _result_datetime(left)
    right_date = _result_datetime(right)
    if left_date and right_date and abs((left_date - right_date).total_seconds()) > 72 * 3600:
        return False
    return True


def _looks_like_same_event(left: ArticleResult, right: ArticleResult) -> bool:
    left_numbers = _event_numeric_facts(left)
    right_numbers = _event_numeric_facts(right)
    strong_semantic_match = _strong_semantic_event_match(
        left, right, left_numbers & right_numbers
    )
    if not _same_event_scope(left, right) and not strong_semantic_match:
        return False
    left_development, left_analysis = _event_marker_profile(left)
    right_development, right_analysis = _event_marker_profile(right)

    # Analysis/opinion is editorially independent even when it quotes most of
    # the underlying news.  A development can consolidate with another report
    # of that same development, but never with the original event.  These are
    # hard guards and do not depend on the publication interval.
    if left_analysis or right_analysis:
        return False
    if left_development != right_development:
        return False

    left_addresses = _event_address_anchors(left)
    right_addresses = _event_address_anchors(right)
    if left_addresses and right_addresses and left_addresses.isdisjoint(right_addresses):
        return False

    if (
        left.event_signature
        and right.event_signature
        and left.event_signature != right.event_signature
        and not strong_semantic_match
    ):
        return False

    if strong_semantic_match:
        return True

    # This named signature represents one reported national shortage of BZD
    # platform wagons.  Syndicated versions were classified into different
    # broad categories (road/transport/deficit), so the normal same-category
    # guard would leave three cards for the same event.  The signature is
    # narrower than a generic shortage and has already passed scope and
    # analysis/development guards above.
    if (
        left.event_signature == right.event_signature
        == "беларусь|rail_platform_wagons|absence_shortage"
    ):
        return True

    # One specific school issued appearance restrictions, and the same public
    # reaction was reported both as a short social post and as a full article.
    # The exact event signature is narrow enough to keep independent school
    # disputes separate while presenting this one resonance as one card.
    if (
        left.event_signature == right.event_signature
        == "барановичи|school_appearance_rules|public_resonance"
    ):
        return True

    left_title = set(_event_tokens(left.title))
    right_title = set(_event_tokens(right.title))
    same_category = left.category == right.category
    title_union = left_title | right_title
    title_jaccard = (
        len(left_title & right_title) / len(title_union)
        if title_union else 0.0
    )

    left_text_tokens = _event_tokens(f"{left.title}. {left.excerpt[:1200]}")
    right_text_tokens = _event_tokens(f"{right.title}. {right.excerpt[:1200]}")
    shared_four = (
        _event_ngrams(left_text_tokens, 4)
        & _event_ngrams(right_text_tokens, 4)
    )
    shared_four_count = len(shared_four)

    # Different category assignments can hide one copied event report.  In
    # that narrow case, a long shared passage may override extra numeric
    # details.  Within one category, materially different numbers remain a
    # hard conflict because they often identify distinct events.
    if (
        left_numbers
        and right_numbers
        and left_numbers.isdisjoint(right_numbers)
        and (left.category == right.category or shared_four_count < 6)
    ):
        return False

    shared_three = (
        _event_ngrams(left_text_tokens, 3)
        & _event_ngrams(right_text_tokens, 3)
    )
    shared_three_count = len(shared_three)

    left_set = set(left_text_tokens)
    right_set = set(right_text_tokens)
    smaller = min(len(left_set), len(right_set))
    containment = (
        len(left_set & right_set) / smaller
        if smaller else 0.0
    )
    same_signature = bool(
        left.event_signature
        and left.event_signature == right.event_signature
    )
    exact_event_locality = bool(
        normalize_space(left.event_locality).casefold()
        and normalize_space(left.event_locality).casefold()
        == normalize_space(right.event_locality).casefold()
    )
    shared_numbers = left_numbers & right_numbers

    # A recovery signature is deliberately coarse (locality + object +
    # problem) and may never be the sole reason to merge.  Independent URLs
    # require overlapping event evidence as well.  False negatives are safer
    # here than hiding two distinct public problems in one report card.
    if same_signature and same_category:
        return bool(
            (shared_four_count >= 1 and title_jaccard >= 0.12)
            or (
                shared_three_count >= 2
                and title_jaccard >= 0.18
                and containment >= 0.24
            )
            or (title_jaccard >= 0.42 and containment >= 0.30)
            or (
                exact_event_locality
                and len(left_set & right_set) >= 12
                and containment >= 0.20
            )
        )

    if same_category:
        return bool(
            (shared_four_count >= 8 and containment >= 0.55)
            or
            (shared_four_count >= 2 and title_jaccard >= 0.18)
            or (
                shared_four_count >= 1
                and title_jaccard >= 0.28
                and containment >= 0.30
            )
            or (
                shared_three_count >= 3
                and title_jaccard >= 0.12
                and containment >= 0.38
            )
            or (
                exact_event_locality
                and bool(shared_numbers)
                and len(left_set & right_set) >= 10
                and containment >= 0.18
            )
        )

    # Category disagreement is allowed only for near-rewrites with strong
    # textual identity; otherwise two consequences of one broad topic stay
    # separate.
    return bool(
        (
            shared_four_count >= 2
            and title_jaccard >= 0.30
            and containment >= 0.42
        )
        or (
            shared_four_count >= 6
            and len(left_set & right_set) >= 15
            and containment >= 0.30
        )
    )


def _dedup_preference(
    result: ArticleResult,
) -> tuple[int, int, int, int, int, int, str]:
    # The monitoring contract prefers a full website article to its Telegram
    # retelling.  Within the same source class, an original title, an official
    # response and a fuller extraction are more useful to the reader than a
    # slightly higher keyword score.
    source_type_rank = 0 if result.source_type == "website" else 1
    return (
        priority_value(result.priority),
        source_type_rank,
        int(result.title_generated),
        -int(result.official_response),
        -result.text_length,
        -result.score,
        result.source_name,
    )


def deduplicate_results(results: list[ArticleResult]) -> list[ArticleResult]:
    # First remove technical variants of one URL, including tracking-only
    # query parameters.  Then conservatively consolidate independent rewrites
    # of the same concrete event.  Different places, developments, analysis
    # and materially different facts stay as separate cards.
    by_url: dict[str, ArticleResult] = {}
    for result in sorted(results, key=_dedup_preference):
        by_url.setdefault(canonicalize_url(result.url), result)

    groups: list[list[ArticleResult]] = []
    for result in sorted(by_url.values(), key=_dedup_preference):
        matching_group = next(
            (
                group for group in groups
                if _looks_like_same_event(result, group[0])
            ),
            None,
        )
        if matching_group is None:
            groups.append([result])
        else:
            matching_group.append(result)

    consolidated: list[ArticleResult] = []
    for group in groups:
        ordered = sorted(group, key=_dedup_preference)
        primary = ordered[0]
        related_by_source: dict[str, tuple[str, str]] = {}
        for item in ordered[1:]:
            source_key = item.source_name.casefold()
            if source_key == primary.source_name.casefold():
                continue
            related_by_source.setdefault(
                source_key,
                (item.source_name, canonicalize_url(item.url)),
            )
        primary.related_coverage = tuple(
            related_by_source[source_key]
            for source_key in sorted(related_by_source)
        )
        consolidated.append(primary)
    return consolidated


def represented_publication_count(results: Iterable[ArticleResult]) -> int:
    return sum(1 + len(result.related_coverage) for result in results)


def prune_state(state: dict[str, Any], retain_days: int) -> None:
    cutoff = utc_now() - dt.timedelta(days=retain_days)
    seen = state.setdefault("seen", {})
    stale = []
    for url, metadata in seen.items():
        timestamp = parse_datetime(metadata.get("first_seen"))
        if timestamp and timestamp < cutoff:
            stale.append(url)
    for url in stale:
        seen.pop(url, None)
    title_cache = state.setdefault("title_cache", {})
    for url in list(title_cache):
        if url not in seen:
            title_cache.pop(url, None)


def write_rejected_signals_csv(
    path: Path,
    outcomes: dict[str, tuple[Candidate, CandidateProcessingTelemetry]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for candidate, trace in outcomes.values():
        relevant_rejection = trace.prefilter_status == "strong" and trace.final_stage == "relevance_rejected"
        extraction_failure = trace.final_stage == "degraded_queued" and trace.degraded_reason == "extraction_failed"
        if not (relevant_rejection or extraction_failure):
            continue
        reason = trace.rejection_reason or (
            "html fetched, no article text extracted" if extraction_failure else ""
        )
        rows.append({"url": candidate.url, "source": candidate.source.name, "title": candidate.title,
                     "prefilter_status": trace.prefilter_status, "final_stage": trace.final_stage,
                     "reason": reason, "html_length": trace.html_length, "text_length": trace.text_length})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["url", "source", "title", "prefilter_status", "final_stage", "reason", "html_length", "text_length"])
        writer.writeheader(); writer.writerows(rows)


def build_country_coverage(
    sources: list[Source],
    candidates: list[Candidate],
    unseen: list[Candidate],
    results: list[ArticleResult],
    errors: list[str],
) -> list[dict[str, Any]]:
    countries: dict[str, dict[str, Any]] = {}
    for source in sources:
        row = countries.setdefault(
            source.country,
            {
                "country": source.country,
                "sources_checked": 0,
                "sources_with_candidates": 0,
                "candidates": 0,
                "unseen": 0,
                "results": 0,
                "errors": 0,
            },
        )
        row["sources_checked"] += 1

    active_sources = {
        (candidate.source.country, candidate.source.name)
        for candidate in candidates
    }
    for country, _source_name in active_sources:
        countries[country]["sources_with_candidates"] += 1

    for candidate in candidates:
        countries[candidate.source.country]["candidates"] += 1
    for candidate in unseen:
        countries[candidate.source.country]["unseen"] += 1
    for result in results:
        countries[result.country]["results"] += 1

    for source in sources:
        if any(error.startswith(source.name + ":") for error in errors):
            countries[source.country]["errors"] += 1

    return [countries[name] for name in sorted(countries)]


def build_source_coverage(
    sources: list[Source],
    candidates: list[Candidate],
    unseen: list[Candidate],
    results: list[ArticleResult],
    errors: list[str],
    collection_metrics: dict[tuple[str, str], SourceCollectionMetrics] | None = None,
    processing_metrics: dict[tuple[str, str], SourceProcessingMetrics] | None = None,
    settings: dict[str, Any] | None = None,
    recovery: RecoveryController | None = None,
) -> list[dict[str, Any]]:
    collection_metrics = collection_metrics or {}
    processing_metrics = processing_metrics or {}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sources:
        key = (source.country, source.name)
        cmetrics = collection_metrics.get(key, SourceCollectionMetrics())
        pmetrics = processing_metrics.get(key, SourceProcessingMetrics())
        profile = effective_source_profile(source, settings)
        rows[key] = {
            "country": source.country,
            "locality": source.locality,
            "source_region": source.country,
            "source_locality": source.locality,
            "event_region": "; ".join(sorted(pmetrics.event_regions)),
            "event_locality": "; ".join(sorted(pmetrics.event_localities)),
            "source": source.name,
            "source_type": source.media_type,
            "domain": source.domain,
            "priority": source.priority,
            "language_configured": source.language,
            "protected": bool(profile.get("protected", False)),
            "transport_order": " > ".join(profile.get("transport_order", [])),
            "extraction_order": " > ".join(profile.get("extraction_order", [])),
            "feed_candidates": cmetrics.feed_candidates,
            "sitemap_candidates": cmetrics.sitemap_candidates,
            "listing_candidates": cmetrics.listing_candidates,
            "homepage_candidates": cmetrics.homepage_candidates,
            "telegram_candidates": cmetrics.telegram_candidates,
            "merged_candidates": cmetrics.merged_candidates,
            "selected_candidates": cmetrics.selected_candidates,
            "selected_feed": cmetrics.selected_feed,
            "selected_sitemap": cmetrics.selected_sitemap,
            "selected_listing": cmetrics.selected_listing,
            "selected_homepage": cmetrics.selected_homepage,
            "selected_telegram": cmetrics.selected_telegram,
            "selected_fresh": cmetrics.selected_fresh,
            "selected_current": cmetrics.selected_current,
            "selected_soft": cmetrics.selected_soft,
            "soft_tail_budget": cmetrics.soft_tail_budget,
            "clipped_soft": cmetrics.clipped_soft,
            "telegram_site_duplicates": cmetrics.telegram_site_duplicates,
            "source_limit": cmetrics.source_limit,
            "source_limit_hit": cmetrics.source_limit_hit,
            "soft_limit_ceiling": cmetrics.soft_limit_ceiling,
            "selected_overflow": cmetrics.selected_overflow,
            "selected_protected_title": cmetrics.selected_protected_title,
            "clipped_candidates": cmetrics.clipped_candidates,
            "endpoint_total": cmetrics.endpoint_total,
            "endpoint_ok": cmetrics.endpoint_ok,
            "endpoint_failed": cmetrics.endpoint_failed,
            "endpoint_degraded": cmetrics.endpoint_degraded,
            "endpoint_circuit_skipped": cmetrics.endpoint_circuit_skipped,
            "endpoint_tail_probes": cmetrics.endpoint_tail_probes,
            "discovery_seconds": round(cmetrics.discovery_seconds, 3),
            "endpoint_discovery_seconds": round(
                cmetrics.endpoint_discovery_seconds, 3
            ),
            "endpoint_http_seconds": round(cmetrics.endpoint_http_seconds, 3),
            "feed_limit_hit": cmetrics.feed_limit_hit,
            "sitemap_limit_hit": cmetrics.sitemap_limit_hit,
            "listing_limit_hit": cmetrics.listing_limit_hit,
            "homepage_limit_hit": cmetrics.homepage_limit_hit,
            "telegram_limit_hit": cmetrics.telegram_limit_hit,
            "clipped_fresh": cmetrics.clipped_fresh,
            "clipped_unseen": cmetrics.clipped_unseen,
            "clipped_undated": cmetrics.clipped_undated,
            "clipped_prefilter_strong": cmetrics.clipped_prefilter_strong,
            "clipped_prefilter_possible": cmetrics.clipped_prefilter_possible,
            "clipped_prefilter_needs_text": cmetrics.clipped_prefilter_needs_text,
            "clipped_protected_title": cmetrics.clipped_protected_title,
            "clipped_feed": cmetrics.clipped_feed,
            "clipped_sitemap": cmetrics.clipped_sitemap,
            "clipped_listing": cmetrics.clipped_listing,
            "clipped_homepage": cmetrics.clipped_homepage,
            "clipped_telegram": cmetrics.clipped_telegram,
            "open_circuits": recovery.open_circuits_for_source(source.name) if recovery else 0,
            "degraded_queue_active": recovery.queue_count_for_source(source.name) if recovery else 0,
            "processed": pmetrics.processed,
            "prefilter_strong": pmetrics.prefilter_strong,
            "prefilter_possible": pmetrics.prefilter_possible,
            "prefilter_needs_text": pmetrics.prefilter_needs_text,
            "fetch_ok": pmetrics.fetch_ok,
            "fetch_failed": pmetrics.fetch_failed,
            "extraction_full": pmetrics.extraction_full,
            "extraction_metadata_only": pmetrics.extraction_metadata_only,
            "extraction_failed": pmetrics.extraction_failed,
            "relevance_passed": pmetrics.relevance_passed,
            "relevance_rejected": pmetrics.relevance_rejected,
            "date_rejected": pmetrics.date_rejected,
            "excerpt_empty": pmetrics.excerpt_empty,
            "included": pmetrics.included,
            "degraded_queued": pmetrics.degraded_queued,
            "recovery_retried": pmetrics.recovery_retried,
            "recovery_recovered": pmetrics.recovery_recovered,
            "event_geo_resolved": pmetrics.event_geo_resolved,
            "event_signature_ready": pmetrics.event_signature_ready,
            "event_echo_hits": pmetrics.event_echo_hits,
            "event_echo_current": pmetrics.event_echo_current,
            "event_echo_state": pmetrics.event_echo_state,
            "event_echo_degraded_prioritized": pmetrics.event_echo_degraded_prioritized,
            "transport_circuit_skipped": pmetrics.transport_circuit_skipped,
            "transport_requests": pmetrics.transport_requests,
            "transport_official_mirror": pmetrics.transport_official_mirror,
            "transport_chromium": pmetrics.transport_chromium,
            "transport_telegram_inline": pmetrics.transport_telegram_inline,
            "transport_amp": pmetrics.transport_amp,
            "transport_feed_metadata": pmetrics.transport_feed_metadata,
            "extraction_source_specific": pmetrics.extraction_source_specific,
            "extraction_embedded_json": pmetrics.extraction_embedded_json,
            "extraction_json_ld": pmetrics.extraction_json_ld,
            "extraction_generic_html": pmetrics.extraction_generic_html,
            "extraction_metadata_description": pmetrics.extraction_metadata_description,
            "extraction_feed_summary": pmetrics.extraction_feed_summary,
            "processing_seconds": round(pmetrics.processing_seconds, 3),
            "processing_max_seconds": round(pmetrics.processing_max_seconds, 3),
            "http_seconds": round(pmetrics.http_seconds, 3),
            "extraction_seconds": round(pmetrics.extraction_seconds, 3),
            "chromium_seconds": round(pmetrics.chromium_seconds, 3),
            "chromium_attempts": pmetrics.chromium_attempts,
            "http_attempts": pmetrics.http_attempts,
            "candidates": 0,
            "unseen": 0,
            "results": 0,
            "error": "",
            "access_status": "",
            "access_status_reason": "",
            "admission_status": "",
            "partial_extraction_loss": 0,
            "blind_zone_status": "",
            "blind_zone_reason": "",
        }

    for candidate in candidates:
        rows[(candidate.source.country, candidate.source.name)]["candidates"] += 1
    for candidate in unseen:
        rows[(candidate.source.country, candidate.source.name)]["unseen"] += 1
    for result in results:
        rows[(result.country, result.source_name)]["results"] += 1

    for source in sources:
        matching = [
            error
            for error in errors
            if error.startswith(source.name + ":")
        ]
        if matching:
            rows[(source.country, source.name)]["error"] = " | ".join(matching)

    for row in rows.values():
        processed = int(row["processed"])
        selected = int(row["selected_candidates"])
        endpoint_total = int(row["endpoint_total"])
        endpoint_failed = int(row["endpoint_failed"])
        endpoint_skipped = int(row["endpoint_circuit_skipped"])
        extraction_loss = (
            int(row["extraction_failed"])
            + int(row["extraction_metadata_only"])
        )
        row["partial_extraction_loss"] = (
            extraction_loss if int(row["extraction_full"]) > 0 else 0
        )
        if int(row["clipped_protected_title"]) > 0:
            row["admission_status"] = "protected_title_clipped"
        elif int(row["clipped_candidates"]) > 0:
            row["admission_status"] = (
                "soft_admission_limited"
                if int(row["clipped_soft"]) == int(row["clipped_candidates"])
                and int(row["clipped_fresh"]) == 0
                else (
                    "soft_limit_expanded_clipped"
                    if int(row["selected_overflow"]) > 0
                    else "source_clipped"
                )
            )
        elif int(row["selected_overflow"]) > 0:
            row["admission_status"] = "soft_limit_expanded"
        else:
            row["admission_status"] = "within_limit"

        # Transport failure must be evaluated before extraction failure: a page
        # that never loaded cannot accurately be labelled an extractor defect.
        if (
            processed > 0
            and int(row["fetch_failed"]) == processed
        ) or (
            selected == 0
            and endpoint_total > 0
            and endpoint_failed + endpoint_skipped >= endpoint_total
        ):
            row["access_status"] = "transport_blocked"
            row["access_status_reason"] = "all attempted access paths failed or were circuit-skipped"
        elif (
            # Do not alarm on one transient miss.  A warning starts at three
            # failed pages and 20% of the attempted material: run 27 lost
            # 11/35 article fetches from «Аршанская газета» while discovery
            # itself remained healthy.
            processed >= 3
            and int(row["fetch_failed"]) >= 3
            and int(row["fetch_failed"]) * 5 >= processed
        ):
            row["access_status"] = "partial_transport_loss"
            row["access_status_reason"] = (
                f"{int(row['fetch_failed'])} of {processed} attempted article "
                "fetches failed"
            )
        elif (
            processed > 0
            and int(row["extraction_full"]) == 0
            and extraction_loss > 0
        ):
            row["access_status"] = "extraction_blind"
            row["access_status_reason"] = "processed pages yielded no full article text"
        elif bool(row["protected"]) and (
            int(row["open_circuits"]) > 0
            or int(row["degraded_queue_active"]) > 0
            or int(row["chromium_attempts"]) > 0
        ):
            row["access_status"] = "protected_recovery"
            row["access_status_reason"] = "protected source used recovery or Chromium"
        elif selected > 0 or int(row["candidates"]) > 0:
            row["access_status"] = "healthy_active"
            row["access_status_reason"] = "candidate discovery produced selectable URLs"
        elif endpoint_failed > 0 or int(row["endpoint_degraded"]) > 0 or row["error"]:
            row["access_status"] = "unexpected_zero"
            row["access_status_reason"] = "zero candidates with failed or degraded discovery"
        elif int(row["endpoint_ok"]) > 0:
            row["access_status"] = "healthy_no_recent"
            row["access_status_reason"] = "discovery worked but produced no candidates in this run"
        else:
            row["access_status"] = "inactive_or_stale"
            row["access_status_reason"] = "no active successful discovery path was observed"

        # Backward-compatible composite field retained for existing reports.
        if (
            row["access_status"] in {
                "transport_blocked", "extraction_blind",
                "partial_transport_loss", "protected_recovery", "unexpected_zero",
                "inactive_or_stale",
            }
        ):
            row["blind_zone_status"] = row["access_status"]
            row["blind_zone_reason"] = row["access_status_reason"]
        elif row["admission_status"] == "protected_title_clipped":
            row["blind_zone_status"] = "protected_title_clipped"
            row["blind_zone_reason"] = (
                "bounded soft ceiling still clipped headline-protected URLs"
            )
        elif row["admission_status"] in {
            "source_clipped", "soft_limit_expanded_clipped",
        }:
            row["blind_zone_status"] = "source_clipped"
            row["blind_zone_reason"] = (
                "local soft source ceiling clipped discovered URLs"
            )
        else:
            row["blind_zone_status"] = row["access_status"]
            row["blind_zone_reason"] = row["access_status_reason"]

    return [
        rows[key]
        for key in sorted(
            rows,
            key=lambda item: (
                item[0],
                priority_value(rows[item]["priority"]),
                item[1],
            ),
        )
    ]


def source_access_alerts(source_coverage: list[dict[str, Any]]) -> list[str]:
    """Expose material source blind zones in the normal report as warnings.

    Source-level transport failures used to exist only in the coverage CSV and
    debug telemetry.  They are not processing exceptions, but they materially
    reduce the report's completeness and must be visible to an operator.
    """
    alerts: list[str] = []
    for row in source_coverage:
        status = str(row.get("access_status", ""))
        if status not in {
            "transport_blocked", "partial_transport_loss", "unexpected_zero",
        }:
            continue
        source = str(row.get("source", "источник"))
        country = str(row.get("country", ""))
        reason = str(row.get("access_status_reason", ""))
        alerts.append(
            "Предупреждение покрытия: "
            f"{country} / {source}: {status}; {reason}. "
            "Техническая причина — в social_access_telemetry CSV."
        )
    return alerts


def write_coverage_csv(
    path: Path,
    source_coverage: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "region", "locality", "source_region", "source_locality",
        "event_region", "event_locality",
        "source", "source_type", "domain", "priority", "language_configured",
        "protected", "transport_order", "extraction_order",
        "feed_candidates", "sitemap_candidates", "listing_candidates",
        "homepage_candidates", "telegram_candidates", "merged_candidates",
        "selected_candidates", "selected_feed", "selected_sitemap",
        "selected_listing", "selected_homepage", "selected_telegram",
        "selected_fresh", "selected_current", "selected_soft",
        "soft_tail_budget", "clipped_soft", "telegram_site_duplicates",
        "source_limit", "source_limit_hit", "soft_limit_ceiling",
        "selected_overflow", "selected_protected_title",
        "clipped_candidates",
        "endpoint_total", "endpoint_ok", "endpoint_failed",
        "endpoint_degraded", "endpoint_circuit_skipped", "endpoint_tail_probes",
        "discovery_seconds", "endpoint_discovery_seconds", "endpoint_http_seconds",
        "feed_limit_hit", "sitemap_limit_hit", "listing_limit_hit",
        "homepage_limit_hit", "telegram_limit_hit",
        "clipped_fresh", "clipped_unseen", "clipped_undated",
        "clipped_prefilter_strong", "clipped_prefilter_possible",
        "clipped_prefilter_needs_text", "clipped_protected_title",
        "clipped_feed", "clipped_sitemap",
        "clipped_listing", "clipped_homepage", "clipped_telegram",
        "open_circuits", "degraded_queue_active",
        "candidates", "unseen",
        "processed", "prefilter_strong", "prefilter_possible",
        "prefilter_needs_text", "fetch_ok", "fetch_failed",
        "extraction_full", "extraction_metadata_only", "extraction_failed",
        "relevance_passed", "relevance_rejected", "date_rejected",
        "excerpt_empty", "included", "degraded_queued",
        "recovery_retried", "recovery_recovered",
        "event_geo_resolved", "event_signature_ready", "event_echo_hits",
        "event_echo_current", "event_echo_state",
        "event_echo_degraded_prioritized", "transport_circuit_skipped",
        "transport_requests", "transport_official_mirror",
        "transport_chromium", "transport_telegram_inline",
        "transport_amp", "transport_feed_metadata",
        "extraction_source_specific", "extraction_embedded_json",
        "extraction_json_ld", "extraction_generic_html",
        "extraction_metadata_description", "extraction_feed_summary",
        "processing_seconds", "processing_max_seconds", "http_seconds",
        "extraction_seconds", "chromium_seconds", "chromium_attempts",
        "http_attempts", "access_status", "access_status_reason",
        "admission_status", "partial_extraction_loss",
        "blind_zone_status", "blind_zone_reason",
        "results", "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in source_coverage:
            writer.writerow({
                "region": row["country"],
                "locality": row.get("locality", ""),
                "source_region": row.get("source_region", row["country"]),
                "source_locality": row.get("source_locality", row.get("locality", "")),
                "event_region": row.get("event_region", ""),
                "event_locality": row.get("event_locality", ""),
                "source": row["source"],
                "source_type": row.get("source_type", ""),
                "domain": row["domain"],
                "priority": row["priority"],
                "language_configured": row["language_configured"],
                "protected": row.get("protected", False),
                "transport_order": row.get("transport_order", ""),
                "extraction_order": row.get("extraction_order", ""),
                "feed_candidates": row.get("feed_candidates", 0),
                "sitemap_candidates": row.get("sitemap_candidates", 0),
                "listing_candidates": row.get("listing_candidates", 0),
                "homepage_candidates": row.get("homepage_candidates", 0),
                "telegram_candidates": row.get("telegram_candidates", 0),
                "merged_candidates": row.get("merged_candidates", 0),
                "selected_candidates": row.get("selected_candidates", 0),
                "selected_feed": row.get("selected_feed", 0),
                "selected_sitemap": row.get("selected_sitemap", 0),
                "selected_listing": row.get("selected_listing", 0),
                "selected_homepage": row.get("selected_homepage", 0),
                "selected_telegram": row.get("selected_telegram", 0),
                "selected_fresh": row.get("selected_fresh", 0),
                "selected_current": row.get("selected_current", 0),
                "selected_soft": row.get("selected_soft", 0),
                "soft_tail_budget": row.get("soft_tail_budget", 0),
                "clipped_soft": row.get("clipped_soft", 0),
                "telegram_site_duplicates": row.get(
                    "telegram_site_duplicates", 0
                ),
                "source_limit": row.get("source_limit", 0),
                "source_limit_hit": row.get("source_limit_hit", False),
                "soft_limit_ceiling": row.get("soft_limit_ceiling", 0),
                "selected_overflow": row.get("selected_overflow", 0),
                "selected_protected_title": row.get(
                    "selected_protected_title", 0
                ),
                "clipped_candidates": row.get("clipped_candidates", 0),
                "endpoint_total": row.get("endpoint_total", 0),
                "endpoint_ok": row.get("endpoint_ok", 0),
                "endpoint_failed": row.get("endpoint_failed", 0),
                "endpoint_degraded": row.get("endpoint_degraded", 0),
                "endpoint_circuit_skipped": row.get("endpoint_circuit_skipped", 0),
                "endpoint_tail_probes": row.get("endpoint_tail_probes", 0),
                "discovery_seconds": row.get("discovery_seconds", 0),
                "endpoint_discovery_seconds": row.get("endpoint_discovery_seconds", 0),
                "endpoint_http_seconds": row.get("endpoint_http_seconds", 0),
                "feed_limit_hit": row.get("feed_limit_hit", False),
                "sitemap_limit_hit": row.get("sitemap_limit_hit", False),
                "listing_limit_hit": row.get("listing_limit_hit", False),
                "homepage_limit_hit": row.get("homepage_limit_hit", False),
                "telegram_limit_hit": row.get("telegram_limit_hit", False),
                "clipped_fresh": row.get("clipped_fresh", 0),
                "clipped_unseen": row.get("clipped_unseen", 0),
                "clipped_undated": row.get("clipped_undated", 0),
                "clipped_prefilter_strong": row.get("clipped_prefilter_strong", 0),
                "clipped_prefilter_possible": row.get("clipped_prefilter_possible", 0),
                "clipped_prefilter_needs_text": row.get(
                    "clipped_prefilter_needs_text", 0
                ),
                "clipped_protected_title": row.get(
                    "clipped_protected_title", 0
                ),
                "clipped_feed": row.get("clipped_feed", 0),
                "clipped_sitemap": row.get("clipped_sitemap", 0),
                "clipped_listing": row.get("clipped_listing", 0),
                "clipped_homepage": row.get("clipped_homepage", 0),
                "clipped_telegram": row.get("clipped_telegram", 0),
                "open_circuits": row.get("open_circuits", 0),
                "degraded_queue_active": row.get("degraded_queue_active", 0),
                "candidates": row["candidates"],
                "unseen": row["unseen"],
                "processed": row.get("processed", 0),
                "prefilter_strong": row.get("prefilter_strong", 0),
                "prefilter_possible": row.get("prefilter_possible", 0),
                "prefilter_needs_text": row.get("prefilter_needs_text", 0),
                "fetch_ok": row.get("fetch_ok", 0),
                "fetch_failed": row.get("fetch_failed", 0),
                "extraction_full": row.get("extraction_full", 0),
                "extraction_metadata_only": row.get("extraction_metadata_only", 0),
                "extraction_failed": row.get("extraction_failed", 0),
                "relevance_passed": row.get("relevance_passed", 0),
                "relevance_rejected": row.get("relevance_rejected", 0),
                "date_rejected": row.get("date_rejected", 0),
                "excerpt_empty": row.get("excerpt_empty", 0),
                "included": row.get("included", 0),
                "degraded_queued": row.get("degraded_queued", 0),
                "recovery_retried": row.get("recovery_retried", 0),
                "recovery_recovered": row.get("recovery_recovered", 0),
                "event_geo_resolved": row.get("event_geo_resolved", 0),
                "event_signature_ready": row.get("event_signature_ready", 0),
                "event_echo_hits": row.get("event_echo_hits", 0),
                "event_echo_current": row.get("event_echo_current", 0),
                "event_echo_state": row.get("event_echo_state", 0),
                "event_echo_degraded_prioritized": row.get(
                    "event_echo_degraded_prioritized", 0
                ),
                "transport_circuit_skipped": row.get("transport_circuit_skipped", 0),
                "transport_requests": row.get("transport_requests", 0),
                "transport_official_mirror": row.get("transport_official_mirror", 0),
                "transport_chromium": row.get("transport_chromium", 0),
                "transport_telegram_inline": row.get("transport_telegram_inline", 0),
                "transport_amp": row.get("transport_amp", 0),
                "transport_feed_metadata": row.get(
                    "transport_feed_metadata", 0
                ),
                "extraction_source_specific": row.get("extraction_source_specific", 0),
                "extraction_embedded_json": row.get("extraction_embedded_json", 0),
                "extraction_json_ld": row.get("extraction_json_ld", 0),
                "extraction_generic_html": row.get("extraction_generic_html", 0),
                "extraction_metadata_description": row.get(
                    "extraction_metadata_description", 0
                ),
                "extraction_feed_summary": row.get(
                    "extraction_feed_summary", 0
                ),
                "processing_seconds": row.get("processing_seconds", 0),
                "processing_max_seconds": row.get("processing_max_seconds", 0),
                "http_seconds": row.get("http_seconds", 0),
                "extraction_seconds": row.get("extraction_seconds", 0),
                "chromium_seconds": row.get("chromium_seconds", 0),
                "chromium_attempts": row.get("chromium_attempts", 0),
                "http_attempts": row.get("http_attempts", 0),
                "access_status": row.get("access_status", ""),
                "access_status_reason": row.get("access_status_reason", ""),
                "admission_status": row.get("admission_status", ""),
                "partial_extraction_loss": row.get("partial_extraction_loss", 0),
                "blind_zone_status": row.get("blind_zone_status", ""),
                "blind_zone_reason": row.get("blind_zone_reason", ""),
                "results": row["results"],
                "error": row["error"],
            })


def write_access_telemetry_csv(
    path: Path,
    sources: list[Source],
    collection_metrics: dict[tuple[str, str], SourceCollectionMetrics],
    processing_outcomes: dict[
        str, tuple[Candidate, CandidateProcessingTelemetry]
    ],
    source_coverage: list[dict[str, Any]],
    run_timing: dict[str, float],
) -> None:
    """Write diagnostic-only access and cost observations for one run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "record_type", "region", "source", "priority", "channel",
        "article_url", "url", "outcome", "status_code", "failure_class",
        "attempts", "seconds", "candidates", "probe_mode", "transport",
        "extraction_strategy", "final_stage", "processing_seconds",
        "http_seconds", "extraction_seconds", "chromium_seconds",
        "chromium_attempts", "access_status", "admission_status",
        "partial_extraction_loss", "blind_zone_status", "detail",
    ]
    source_lookup = {
        (source.country, source.name): source for source in sources
    }
    coverage_lookup = {
        (row["country"], row["source"]): row for row in source_coverage
    }

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()

        for stage, seconds in run_timing.items():
            writer.writerow({
                "record_type": "run_stage",
                "channel": stage,
                "seconds": round(max(0.0, seconds), 3),
            })

        for key in sorted(source_lookup):
            source = source_lookup[key]
            cmetrics = collection_metrics.get(key, SourceCollectionMetrics())
            coverage_row = coverage_lookup.get(key, {})
            writer.writerow({
                "record_type": "source_summary",
                "region": source.country,
                "source": source.name,
                "priority": source.priority,
                "seconds": round(cmetrics.discovery_seconds, 3),
                "http_seconds": coverage_row.get("http_seconds", 0),
                "extraction_seconds": coverage_row.get("extraction_seconds", 0),
                "chromium_seconds": coverage_row.get("chromium_seconds", 0),
                "chromium_attempts": coverage_row.get("chromium_attempts", 0),
                "candidates": cmetrics.selected_candidates,
                "access_status": coverage_row.get("access_status", ""),
                "admission_status": coverage_row.get("admission_status", ""),
                "partial_extraction_loss": coverage_row.get(
                    "partial_extraction_loss", 0
                ),
                "blind_zone_status": coverage_row.get("blind_zone_status", ""),
                "detail": (
                    f"merged={cmetrics.merged_candidates}; "
                    f"clipped={cmetrics.clipped_candidates}; "
                    f"clipped_unseen={cmetrics.clipped_unseen}; "
                    f"selected_fresh={cmetrics.selected_fresh}; "
                    f"selected_current={cmetrics.selected_current}; "
                    f"selected_soft={cmetrics.selected_soft}; "
                    f"soft_tail_budget={cmetrics.soft_tail_budget}; "
                    f"source_limit={cmetrics.source_limit}; "
                    f"soft_limit_ceiling={cmetrics.soft_limit_ceiling}; "
                    f"selected_overflow={cmetrics.selected_overflow}; "
                    f"selected_protected_title={cmetrics.selected_protected_title}; "
                    f"clipped_protected_title={cmetrics.clipped_protected_title}; "
                    f"clipped_soft={cmetrics.clipped_soft}; "
                    f"telegram_site_duplicates={cmetrics.telegram_site_duplicates}; "
                    f"endpoint_http_seconds={cmetrics.endpoint_http_seconds:.3f}"
                ),
            })
            for observation in cmetrics.endpoint_observations:
                writer.writerow({
                    "record_type": "discovery_http",
                    "region": source.country,
                    "source": source.name,
                    "priority": source.priority,
                    "channel": observation.channel,
                    "url": observation.endpoint,
                    "outcome": observation.outcome,
                    "status_code": observation.status_code,
                    "failure_class": observation.failure_class,
                    "attempts": observation.attempts,
                    "seconds": round(observation.seconds, 3),
                    "candidates": observation.candidates,
                    "probe_mode": observation.probe_mode,
                    "detail": observation.detail,
                })

        ordered_outcomes = sorted(
            processing_outcomes.values(),
            key=lambda item: (
                item[0].source.country,
                item[0].source.name,
                canonicalize_url(item[0].url),
            ),
        )
        for candidate, trace in ordered_outcomes:
            writer.writerow({
                "record_type": "article_processing",
                "region": candidate.source.country,
                "source": candidate.source.name,
                "priority": candidate.source.priority,
                "article_url": canonicalize_url(candidate.url),
                "outcome": trace.transport_status,
                "status_code": trace.transport_status_code,
                "failure_class": trace.transport_failure_class,
                "attempts": trace.http_attempts,
                "transport": trace.transport,
                "extraction_strategy": trace.extraction_strategy,
                "final_stage": trace.final_stage,
                "processing_seconds": round(trace.processing_seconds, 3),
                "http_seconds": round(trace.http_seconds, 3),
                "extraction_seconds": round(trace.extraction_seconds, 3),
                "chromium_seconds": round(trace.chromium_seconds, 3),
                "chromium_attempts": trace.chromium_attempts,
                "detail": "; ".join(filter(None, [
                    f"prefilter={trace.prefilter_status}",
                    (
                        f"rejection_reason={trace.rejection_reason}"
                        if trace.rejection_reason else ""
                    ),
                ])),
            })
            for observation in trace.http_observations:
                writer.writerow({
                    "record_type": "article_http",
                    "region": candidate.source.country,
                    "source": candidate.source.name,
                    "priority": candidate.source.priority,
                    "article_url": canonicalize_url(candidate.url),
                    "url": observation.url,
                    "outcome": observation.outcome,
                    "status_code": observation.status_code,
                    "failure_class": observation.failure_class,
                    "attempts": observation.attempts,
                    "seconds": round(observation.seconds, 3),
                    "transport": trace.transport,
                    "final_stage": trace.final_stage,
                    "detail": observation.detail,
                })



def format_report_datetime(value: str, settings: dict[str, Any]) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return value or "время не определено"
    timezone_name = str(settings.get("monitor", {}).get("timezone", "Europe/Minsk"))
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = UTC
    return parsed.astimezone(timezone).strftime("%d.%m.%Y, %H:%M")


def build_html_report(
    results: list[ArticleResult],
    errors: list[str],
    settings: dict[str, Any],
    warmup: bool,
    coverage: list[dict[str, Any]] | None = None,
) -> str:
    report_title = html.escape(settings["report"]["title"])
    today = local_now(settings).date().isoformat()
    category_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}
    for result in results:
        category_counts[result.category] = category_counts.get(result.category, 0) + 1
        region_counts[result.country] = region_counts.get(result.country, 0) + 1

    category_summary = " · ".join(
        f"{html.escape(name)}: {count}"
        for name, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
    ) or "релевантных публикаций не найдено"
    region_summary = " · ".join(
        f"{html.escape(name)}: {count}"
        for name, count in sorted(region_counts.items(), key=lambda item: (-item[1], item[0]))
    )
    publication_count = represented_publication_count(results)

    blocks: list[str] = []
    for result in results:
        published = html.escape(format_report_datetime(result.published_at, settings))
        locality = f" · {html.escape(result.locality)}" if result.locality else ""
        generated_note = (
            '<div class="generated-note">У Telegram-сообщения нет отдельного заголовка; '
            'в качестве заголовка дословно использованы его первые слова.</div>'
            if result.title_generated else ""
        )
        response_badge = (
            '<span class="response">есть ответ организации или властей</span>'
            if result.official_response else ""
        )
        related_coverage = ""
        if result.related_coverage:
            related_links = ", ".join(
                (
                    f'<a href="{html.escape(url, quote=True)}">'
                    f'{html.escape(source_name)}</a>'
                )
                for source_name, url in result.related_coverage
            )
            related_coverage = (
                '<p class="also-covered"><em>Этот сюжет также освещали:</em> '
                f'{related_links}.</p>'
            )
        blocks.append(f"""
        <article class="item">
          <div class="meta">{html.escape(result.country)}{locality} · {html.escape(result.source_name)}
          · {published} · язык {html.escape(result.source_language)}</div>
          <h2>{html.escape(result.title)}</h2>
          {generated_note}
          <div class="tags"><span class="category">{html.escape(result.category)}</span>
          <span class="signal">{html.escape(result.signal_type)}</span>{response_badge}</div>
          <p class="excerpt">{html.escape(result.excerpt)}</p>
          <p class="source-link"><strong>Источник:</strong>
          <a href="{html.escape(result.url, quote=True)}">{html.escape(result.url)}</a></p>
          {related_coverage}
          <div class="technical">Приоритет {html.escape(result.priority)} · балл {result.score}
          · обнаружено: {html.escape(result.discovered_via)}</div>
        </article>
        """)

    warmup_note = ""
    if warmup:
        warmup_note = """
        <div class="notice">
          Это первый запуск нового репозитория. Создана исходная база уже известных
          ссылок; со следующего запуска в отчёт будут попадать только новые материалы.
        </div>
        """

    coverage_block = ""
    if coverage:
        rows = []
        for row in coverage:
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(row['country']))}</td>"
                f"<td>{int(row['sources_checked'])}</td>"
                f"<td>{int(row['sources_with_candidates'])}</td>"
                f"<td>{int(row['candidates'])}</td>"
                f"<td>{int(row['unseen'])}</td>"
                f"<td>{int(row['results'])}</td>"
                f"<td>{int(row['errors'])}</td>"
                "</tr>"
            )
        checked_total = sum(int(row["sources_checked"]) for row in coverage)
        active_total = sum(int(row["sources_with_candidates"]) for row in coverage)
        coverage_block = f"""
        <details class="coverage">
          <summary><strong>Контроль охвата:</strong> проверено источников {checked_total},
          дали хотя бы одну ссылку {active_total}</summary>
          <p class="coverage-note">Ноль ссылок не всегда означает ошибку: у источника
          могло не быть свежих материалов в доступной ленте.</p>
          <table>
            <thead><tr><th>Регион</th><th>Проверено</th><th>Дали ссылки</th>
            <th>Кандидаты</th><th>Новые</th><th>В отчёте</th><th>Ошибки</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </details>
        """

    errors_block = ""
    if errors:
        items = "".join(f"<li>{html.escape(error)}</li>" for error in errors[:50])
        errors_block = f"""
        <details>
          <summary>Источники с ошибками: {len(errors)}</summary>
          <ul>{items}</ul>
        </details>
        """

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{report_title}</title>
<style>
body {{font-family: Arial, sans-serif; max-width: 940px; margin: 24px auto; color:#202124; line-height:1.5; padding:0 14px}}
header {{border-bottom:3px solid #244f73; margin-bottom:20px}}
h1 {{color:#244f73; margin-bottom:6px}}
.item {{border-bottom:1px solid #d9dce1; padding:20px 0}}
.item h2 {{font-size:21px; margin:7px 0 6px}}
.item a {{color:#174ea6; word-break:break-word}}
.meta,.technical {{font-size:12px; color:#666}}
.tags {{margin:8px 0}}
.category,.signal,.response {{display:inline-block; font-size:12px; padding:3px 8px; border-radius:12px; margin:0 5px 4px 0}}
.category {{background:#e8f0fe}}
.signal {{background:#fce8e6}}
.response {{background:#e6f4ea}}
.generated-note {{font-size:13px; color:#5f6368; margin:5px 0}}
.excerpt {{background:#f8f9fa; border-left:4px solid #5f7f9d; padding:12px 14px; white-space:normal}}
.source-link {{font-size:14px}}
.also-covered {{font-size:14px; margin-top:6px}}
.notice {{background:#fff3cd; border:1px solid #ffe69c; padding:12px; margin:14px 0}}
summary {{cursor:pointer; margin-top:20px}}
table {{border-collapse:collapse; width:100%; margin:10px 0 18px; font-size:12px}}
th,td {{border:1px solid #dadce0; padding:6px 7px; text-align:right}}
th:first-child,td:first-child {{text-align:left}}
.coverage-note {{font-size:12px; color:#666}}
</style>
</head>
<body>
<header>
  <h1>{report_title}</h1>
  <p>{today} · сюжетов: <strong>{len(results)}</strong> · публикаций с учётом резонанса:
  <strong>{publication_count}</strong></p>
  <p>{category_summary}</p>
  {f'<p class="technical">География: {region_summary}</p>' if region_summary else ''}
  <p class="technical">Сборка мониторинга: {html.escape(MONITOR_BUILD)}</p>
</header>
{warmup_note}
{coverage_block}
{''.join(blocks) if blocks else '<p>Новых релевантных публикаций нет.</p>'}
{errors_block}
</body>
</html>
"""


def write_csv_report(path: Path, results: list[ArticleResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "region", "locality", "source", "source_type", "priority",
        "source_language", "published_at", "category", "subcategory",
        "signal_type", "official_response", "score", "title",
        "title_generated", "exact_excerpt", "url", "matched_terms",
        "discovered_via", "text_length",
        "event_region", "event_locality", "event_object", "event_problem",
        "event_signature", "event_echo", "event_echo_anchor",
        "event_echo_sources", "also_covered_by", "also_covered_urls",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow({
                "region": result.country,
                "locality": result.locality,
                "source": result.source_name,
                "source_type": result.source_type,
                "priority": result.priority,
                "source_language": result.source_language,
                "published_at": result.published_at,
                "category": result.category,
                "subcategory": result.subcategory,
                "signal_type": result.signal_type,
                "official_response": result.official_response,
                "score": result.score,
                "title": result.title,
                "title_generated": result.title_generated,
                "exact_excerpt": result.excerpt,
                "url": result.url,
                "matched_terms": result.matched_terms,
                "discovered_via": result.discovered_via,
                "text_length": result.text_length,
                "event_region": result.event_region,
                "event_locality": result.event_locality,
                "event_object": result.event_object,
                "event_problem": result.event_problem,
                "event_signature": result.event_signature,
                "event_echo": result.event_echo,
                "event_echo_anchor": result.event_echo_anchor,
                "event_echo_sources": result.event_echo_sources,
                "also_covered_by": ", ".join(
                    source_name for source_name, _url in result.related_coverage
                ),
                "also_covered_urls": " | ".join(
                    url for _source_name, url in result.related_coverage
                ),
            })


def unique_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value).strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def email_recipients(settings: dict[str, Any]) -> list[str]:
    configured = settings.get("report", {}).get("email_recipients", [])
    if isinstance(configured, str):
        configured = [configured]
    environment = os.getenv("REPORT_TO", "")
    environment_values = environment.split(",") if environment else []
    return unique_values([*configured, *environment_values])


def mask_email_address(address: str) -> str:
    value = normalize_space(address)
    if "@" not in value:
        return "***"
    local, domain = value.rsplit("@", 1)
    local_masked = (local[:1] + "***") if local else "***"
    domain_parts = domain.split(".")
    if domain_parts:
        domain_parts[0] = (
            domain_parts[0][:1] + "***" if domain_parts[0] else "***"
        )
    return f"{local_masked}@{'.'.join(domain_parts)}"


def smtp_refusal_detail(value: Any) -> str:
    if not isinstance(value, tuple) or not value:
        return "rejected"
    code = value[0]
    response = value[1] if len(value) > 1 else ""
    if isinstance(response, bytes):
        response = response.decode("utf-8", errors="replace")
    cleaned = normalize_space(str(response))[:160]
    return f"{code} {cleaned}".strip()


def record_smtp_delivery_result(
    recipients: list[str],
    refused: dict[str, Any],
    delivery_errors: list[str] | None = None,
) -> bool:
    refused_by_key = {
        normalize_space(address).casefold(): detail
        for address, detail in refused.items()
    }
    statuses: list[str] = []
    rejected: list[str] = []
    for recipient in recipients:
        key = normalize_space(recipient).casefold()
        masked = mask_email_address(recipient)
        if key in refused_by_key:
            detail = smtp_refusal_detail(refused_by_key[key])
            statuses.append(f"{masked}=rejected({detail})")
            rejected.append(f"{masked}: {detail}")
        else:
            statuses.append(f"{masked}=accepted")
    LOG.info("SMTP результат по адресатам: %s", "; ".join(statuses))
    if not rejected:
        return True
    accepted_count = len(recipients) - len(rejected)
    level = "частичный отказ" if accepted_count else "отказ всех адресатов"
    warning = (
        f"EMAIL_DELIVERY: {level}; принято {accepted_count}/{len(recipients)}; "
        f"отклонено: {'; '.join(rejected)}"
    )
    LOG.warning(warning)
    if delivery_errors is not None:
        delivery_errors.append(warning)
    return False


def send_email(
    html_report: str,
    csv_path: Path,
    coverage_path: Path | None,
    results_count: int,
    warmup: bool,
    settings: dict[str, Any],
    delivery_errors: list[str] | None = None,
) -> bool:
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    recipients = email_recipients(settings)
    if not username or not password or not recipients:
        LOG.warning(
            "Почта не отправлена: отсутствуют SMTP_USERNAME, SMTP_PASSWORD "
            "или список получателей."
        )
        return False

    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    security_mode = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    sender = os.getenv("REPORT_FROM", "").strip() or username
    subject_suffix = "инициализация" if warmup else f"{results_count} публикаций"
    report_date = local_now(settings).date().isoformat()

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = (
        f"Социально-экономический мониторинг Беларуси — {report_date} — {subject_suffix}"
    )
    message.set_content(
        "Отчёт сформирован в HTML. Вложения содержат таблицу публикаций "
        "и контроль покрытия источников."
    )
    message.add_alternative(html_report, subtype="html")

    if csv_path.exists():
        message.add_attachment(
            csv_path.read_bytes(), maintype="text", subtype="csv", filename=csv_path.name
        )
    if coverage_path and coverage_path.exists():
        message.add_attachment(
            coverage_path.read_bytes(), maintype="text", subtype="csv", filename=coverage_path.name
        )

    context = ssl.create_default_context()
    LOG.info(
        "SMTP адресаты (маскировано): %s",
        ", ".join(mask_email_address(value) for value in recipients),
    )
    try:
        if security_mode == "starttls":
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(username, password)
                refused = server.send_message(message)
        else:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(username, password)
                refused = server.send_message(message)
    except smtplib.SMTPRecipientsRefused as exc:
        return record_smtp_delivery_result(
            recipients, exc.recipients, delivery_errors
        )
    return record_smtp_delivery_result(
        recipients,
        refused if isinstance(refused, dict) else {},
        delivery_errors,
    )


def telegram_chat_ids() -> list[str]:
    raw = os.getenv("TELEGRAM_CHAT_IDS", "")
    return unique_values(raw.split(",") if raw else [])


def telegram_summary_text(
    results: list[ArticleResult],
    warmup: bool,
    settings: dict[str, Any],
) -> str:
    report_date = local_now(settings).date().isoformat()
    heading = "<b>Социально-экономический мониторинг Беларуси</b>"
    if warmup:
        return (
            f"{heading}\n{report_date}\n\n"
            "Первый запуск завершён: создана исходная база ссылок. "
            "Со следующего запуска будут поступать только новые публикации."
        )
    if not results:
        return f"{heading}\n{report_date}\n\nНовых релевантных публикаций не найдено."

    telegram_settings = settings.get("report", {}).get("telegram", {})
    limit = int(telegram_settings.get("max_listed_articles", 12))
    publication_count = represented_publication_count(results)
    lines = [
        heading,
        (
            f"{report_date} · сюжетов: <b>{len(results)}</b> · "
            f"публикаций: <b>{publication_count}</b>"
        ),
        "",
    ]
    for index, result in enumerate(results[:limit], start=1):
        title = html.escape(result.title)
        source = html.escape(result.source_name)
        region = html.escape(result.country)
        locality = f" · {html.escape(result.locality)}" if result.locality else ""
        category = html.escape(result.category)
        url = html.escape(result.url, quote=True)
        lines.append(
            f'{index}. <a href="{url}">{title}</a>\n'
            f"   {region}{locality} · {source} · {category}"
        )
        if result.related_coverage:
            related_links = ", ".join(
                f'<a href="{html.escape(related_url, quote=True)}">'
                f'{html.escape(related_source)}</a>'
                for related_source, related_url in result.related_coverage
            )
            lines.append(f"   <i>Этот сюжет также освещали:</i> {related_links}.")
    if len(results) > limit:
        lines.append(f"\nЕщё публикаций в полном отчёте: {len(results) - limit}")
    return "\n".join(lines)[:4000]


def telegram_api_request(
    method: str,
    token: str,
    data: dict[str, str],
    files: dict[str, Any] | None = None,
) -> bool:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = requests.post(
            url,
            data=data,
            files=files,
            timeout=45,
        )
        if response.status_code != 200:
            LOG.warning(
                "Telegram %s: HTTP %s: %s",
                method,
                response.status_code,
                response.text[:500],
            )
            return False
        payload = response.json()
        if not payload.get("ok"):
            LOG.warning("Telegram %s вернул ошибку: %s", method, payload)
            return False
        return True
    except (requests.RequestException, ValueError) as exc:
        LOG.warning("Ошибка Telegram %s: %s", method, exc)
        return False


def send_telegram(
    results: list[ArticleResult],
    html_path: Path,
    csv_path: Path,
    warmup: bool,
    settings: dict[str, Any],
) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = telegram_chat_ids()
    if not token or not chat_ids:
        LOG.info(
            "Telegram не настроен: TELEGRAM_BOT_TOKEN или "
            "TELEGRAM_CHAT_IDS отсутствуют."
        )
        return False

    telegram_settings = settings.get("report", {}).get("telegram", {})
    send_message_enabled = bool(
        telegram_settings.get("send_message", True)
    )
    attach_html = bool(telegram_settings.get("attach_html", True))
    attach_csv = bool(telegram_settings.get("attach_csv", True))
    summary_text = telegram_summary_text(results, warmup, settings)
    all_ok = True

    for chat_id in chat_ids:
        if send_message_enabled:
            all_ok = telegram_api_request(
                "sendMessage",
                token,
                {
                    "chat_id": chat_id,
                    "text": summary_text,
                    "parse_mode": "HTML",
                },
            ) and all_ok

        if attach_html and html_path.exists():
            with html_path.open("rb") as document:
                all_ok = telegram_api_request(
                    "sendDocument",
                    token,
                    {
                        "chat_id": chat_id,
                        "caption": "Полный социально-экономический HTML-отчёт",
                    },
                    {"document": (html_path.name, document, "text/html")},
                ) and all_ok

        if attach_csv and csv_path.exists():
            with csv_path.open("rb") as document:
                all_ok = telegram_api_request(
                    "sendDocument",
                    token,
                    {
                        "chat_id": chat_id,
                        "caption": "Таблица социально-экономических публикаций CSV",
                    },
                    {"document": (
                        csv_path.name,
                        document,
                        "text/csv",
                    )},
                ) and all_ok

    return all_ok


def run_monitor(project_root: Path, dry_run: bool = False) -> dict[str, Any]:
    run_started = time.perf_counter()
    settings = load_settings(project_root / "config" / "settings.yaml")
    sources = load_sources(project_root / "config" / "sources.csv")
    state_path = project_root / "data" / "state.json"
    cache_path = project_root / "data" / "discovery_cache.json"
    state = load_json(state_path, {"version": 2, "initialized": False, "seen": {}})
    discovery_cache = load_json(cache_path, {})

    now = utc_now()
    recovery = RecoveryController(state, settings, now)
    recovery.prune()
    local_time = local_now(settings)
    weekday = local_time.weekday()
    lookback_hours = (
        int(settings["monitor"].get("monday_lookback_hours", 96))
        if weekday == 0
        else int(settings["monitor"].get("lookback_hours", 36))
    )
    cutoff = now - dt.timedelta(hours=lookback_hours)
    max_workers = int(settings["monitor"].get("max_workers", 8))
    setup_seconds = time.perf_counter() - run_started

    LOG.info("Сборка мониторинга: %s", MONITOR_BUILD)
    LOG.info("Источников: %d; период: %d часов", len(sources), lookback_hours)
    all_candidates: list[Candidate] = []
    errors: list[str] = []
    collection_metrics: dict[
        tuple[str, str], SourceCollectionMetrics
    ] = {}
    processing_metrics: dict[
        tuple[str, str], SourceProcessingMetrics
    ] = {}
    seen: dict[str, Any] = state.setdefault("seen", {})
    seen_urls = set(seen)

    discovery_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                collect_source_candidates,
                source,
                settings,
                discovery_cache,
                cutoff,
                recovery,
                seen_urls,
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            candidates, error, metrics = future.result()
            all_candidates.extend(candidates)
            collection_metrics[(source.country, source.name)] = metrics
            if error:
                errors.append(error)
    discovery_seconds = time.perf_counter() - discovery_started

    admission_started = time.perf_counter()
    all_candidates, ranking_contracts = rank_candidates_core33(
        all_candidates,
        cutoff,
        settings,
    )
    ranking_tier_counts = candidate_ranking_tier_counts(ranking_contracts)
    LOG.info(
        "Core 3.3 candidate ranking: %s",
        "; ".join(
            f"{tier}={ranking_tier_counts.get(tier, 0)}"
            for tier in _CORE33_RANKING_LEVELS
        ),
    )

    # Покрытие считается по полному набору кандидатов до глобального лимита.
    # Иначе источники, стоящие позже в очереди (особенно приоритет C),
    # ошибочно получают ноль в coverage.csv.
    coverage_candidates = list(all_candidates)

    max_candidates = candidate_processing_capacity(sources, settings)

    all_candidates = coverage_candidates[:max_candidates]
    LOG.info(
        "Кандидатов собрано до глобального лимита: %d",
        len(coverage_candidates),
    )
    LOG.info(
        "Лимит обработки кандидатов: %d; направлено в обработку: %d",
        max_candidates,
        len(all_candidates),
    )
    if len(coverage_candidates) > len(all_candidates):
        LOG.warning(
            "Глобальный лимит отсёк %d кандидатов; "
            "coverage.csv всё равно будет рассчитан по полному набору.",
            len(coverage_candidates) - len(all_candidates),
        )

    unseen = [
        candidate for candidate in all_candidates
        if canonicalize_url(candidate.url) not in seen
        and not recovery.should_defer_url(candidate.url)
    ]
    coverage_unseen = [
        candidate for candidate in coverage_candidates
        if canonicalize_url(candidate.url) not in seen
    ]
    queued_due = recovery.due_candidates(sources)
    retry_keys = {canonicalize_url(item.url) for item in queued_due}
    processing_candidates = deduplicate_candidates([*unseen, *queued_due])
    warmup = (
        not bool(state.get("initialized"))
        and bool(settings["monitor"].get("warmup_on_first_run", True))
    )
    admission_seconds = time.perf_counter() - admission_started

    results: list[ArticleResult] = []
    processing_outcomes: dict[str, tuple[Candidate, CandidateProcessingTelemetry]] = {}
    processing_started = time.perf_counter()
    if not warmup:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    process_candidate_detailed,
                    candidate,
                    settings,
                    cutoff,
                    True,
                    recovery,
                    canonicalize_url(candidate.url) in retry_keys,
                ): candidate
                for candidate in processing_candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    result, trace = future.result()
                    processing_outcomes[canonicalize_url(candidate.url)] = (candidate, trace)
                    if result:
                        results.append(result)
                except Exception as exc:
                    errors.append(
                        f"{candidate.source.name}: обработка {candidate.url}: "
                        f"{type(exc).__name__}: {exc}"
                    )
    if not warmup:
        apply_event_echo(
            processing_outcomes,
            results,
            recovery,
            settings,
        )
        # Event Echo is applied after all candidates complete, so processing
        # metrics are aggregated here rather than inside the futures loop.
        for candidate, trace in processing_outcomes.values():
            key = (candidate.source.country, candidate.source.name)
            processing_metrics.setdefault(
                key, SourceProcessingMetrics()
            ).add(trace)
    processing_seconds = time.perf_counter() - processing_started

    postprocess_started = time.perf_counter()
    relevant_publications_before_consolidation = len(results)
    results = deduplicate_results(results)
    related_coverage_publications = sum(
        len(result.related_coverage) for result in results
    )
    results.sort(
        key=lambda item: (
            priority_value(item.priority),
            item.category,
            item.country,
            item.source_name,
            item.published_at,
        )
    )
    # Only fully processed candidates enter seen. Degraded candidates are kept
    # in a bounded retry queue and therefore cannot disappear silently after a
    # metadata-only or failed extraction.
    if not warmup:
        for url_key, (candidate, trace) in processing_outcomes.items():
            if trace.final_stage == "degraded_queued":
                recovery.queue_degraded(candidate, trace)
                continue
            recovery.remove_from_queue(candidate.url)
            seen[url_key] = {
                "first_seen": now.isoformat(),
                "source": candidate.source.name,
                "region": candidate.source.country,
                "locality": candidate.source.locality,
                "title": candidate.title,
            }
    else:
        for candidate in unseen:
            seen[canonicalize_url(candidate.url)] = {
                "first_seen": now.isoformat(),
                "source": candidate.source.name,
                "region": candidate.source.country,
                "locality": candidate.source.locality,
                "title": candidate.title,
            }

    if not warmup:
        for result in results:
            recovery.remember_event_seed(result)

    coverage = build_country_coverage(
        sources,
        coverage_candidates,
        coverage_unseen,
        results,
        errors,
    )
    source_coverage = build_source_coverage(
        sources,
        coverage_candidates,
        coverage_unseen,
        results,
        errors,
        collection_metrics,
        processing_metrics,
        settings,
        recovery,
    )
    # A failed source is a coverage warning rather than a fatal run error.
    # Add it before both the HTML and error artefacts are written so the
    # operator does not have to infer an outage from an empty candidate count.
    errors.extend(source_access_alerts(source_coverage))
    postprocess_seconds = time.perf_counter() - postprocess_started


    state["initialized"] = True
    state["last_run"] = now.isoformat()
    state["last_results_count"] = len(results)
    state["last_candidates_count"] = len(all_candidates)
    state["last_candidates_collected_count"] = len(coverage_candidates)
    state["last_candidate_limit"] = max_candidates
    prune_state(state, int(settings["monitor"].get("retain_seen_days", 120)))

    date_stamp = local_time.strftime("%Y-%m-%d")
    reports_dir = project_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    html_path = reports_dir / f"social_report_{date_stamp}.html"
    csv_path = reports_dir / f"social_articles_{date_stamp}.csv"
    errors_path = reports_dir / f"social_errors_{date_stamp}.txt"
    coverage_path = reports_dir / f"social_coverage_{date_stamp}.csv"
    access_telemetry_path = reports_dir / f"social_access_telemetry_{date_stamp}.csv"
    debug_path = project_root / "debug" / f"rejected_signals_{date_stamp}.csv"

    report_started = time.perf_counter()
    html_report = build_html_report(
        results, errors, settings, warmup, coverage
    )
    html_path.write_text(html_report, encoding="utf-8")
    write_csv_report(csv_path, results)
    write_coverage_csv(coverage_path, source_coverage)
    write_rejected_signals_csv(debug_path, processing_outcomes)
    errors_path.write_text("\n".join(errors), encoding="utf-8")
    report_seconds = time.perf_counter() - report_started

    if not dry_run:
        save_json(state_path, state)
        save_json(cache_path, discovery_cache)

    should_send = (
        warmup
        or bool(results)
        or bool(settings["monitor"].get("send_empty_report", True))
    )
    email_sent = False
    telegram_sent = False
    delivery_started = time.perf_counter()
    if should_send and not dry_run:
        email_sent = send_email(
            html_report,
            csv_path,
            coverage_path,
            len(results),
            warmup,
            settings,
            errors,
        )
        telegram_sent = send_telegram(
            results,
            html_path,
            csv_path,
            warmup,
            settings,
        )
    delivery_seconds = time.perf_counter() - delivery_started
    # Delivery happens after report generation.  Persist any per-recipient
    # SMTP warning into the artifact so a partial refusal is visible even when
    # the workflow itself completed and another recipient received the email.
    errors_path.write_text("\n".join(errors), encoding="utf-8")

    run_timing = {
        "setup_seconds": setup_seconds,
        "discovery_seconds": discovery_seconds,
        "candidate_admission_seconds": admission_seconds,
        "processing_seconds": processing_seconds,
        "postprocess_seconds": postprocess_seconds,
        "report_seconds": report_seconds,
        "delivery_seconds": delivery_seconds,
        "total_seconds_before_telemetry_write": time.perf_counter() - run_started,
    }
    telemetry_write_started = time.perf_counter()
    write_access_telemetry_csv(
        access_telemetry_path,
        sources,
        collection_metrics,
        processing_outcomes,
        source_coverage,
        run_timing,
    )
    telemetry_write_seconds = time.perf_counter() - telemetry_write_started

    summary = {
        "build": MONITOR_BUILD,
        "sources": len(sources),
        "candidates": len(all_candidates),
        "candidates_collected": len(coverage_candidates),
        "candidate_limit": max_candidates,
        "candidate_ranking_mode": "core3.3_soft_limits",
        "candidate_ranking_tiers": ranking_tier_counts,
        "protected_title_candidates": ranking_tier_counts.get(
            "protected_title", 0
        ),
        "unseen_candidates": len(unseen),
        "unseen_candidates_collected": len(coverage_unseen),
        "relevant_results": len(results),
        "relevant_publications_before_consolidation": (
            relevant_publications_before_consolidation
        ),
        "related_coverage_publications": related_coverage_publications,
        "processed_candidates": sum(
            metrics.processed for metrics in processing_metrics.values()
        ),
        "fetch_failed": sum(
            metrics.fetch_failed for metrics in processing_metrics.values()
        ),
        "extraction_failed": sum(
            metrics.extraction_failed for metrics in processing_metrics.values()
        ),
        "metadata_only": sum(
            metrics.extraction_metadata_only
            for metrics in processing_metrics.values()
        ),
        "degraded_queued": sum(
            metrics.degraded_queued for metrics in processing_metrics.values()
        ),
        "recovery_retried": sum(
            metrics.recovery_retried for metrics in processing_metrics.values()
        ),
        "recovery_recovered": sum(
            metrics.recovery_recovered for metrics in processing_metrics.values()
        ),
        "event_geo_resolved": sum(
            metrics.event_geo_resolved for metrics in processing_metrics.values()
        ),
        "event_signature_ready": sum(
            metrics.event_signature_ready for metrics in processing_metrics.values()
        ),
        "event_echo_hits": sum(
            metrics.event_echo_hits for metrics in processing_metrics.values()
        ),
        "event_echo_degraded_prioritized": sum(
            metrics.event_echo_degraded_prioritized
            for metrics in processing_metrics.values()
        ),
        "event_seed_count": recovery.event_seed_count(),
        "degraded_queue_active": recovery.active_queue_count(),
        "clipped_candidates": sum(
            metrics.clipped_candidates for metrics in collection_metrics.values()
        ),
        "clipped_fresh": sum(
            metrics.clipped_fresh for metrics in collection_metrics.values()
        ),
        "clipped_unseen": sum(
            metrics.clipped_unseen for metrics in collection_metrics.values()
        ),
        "clipped_undated": sum(
            metrics.clipped_undated for metrics in collection_metrics.values()
        ),
        "selected_fresh": sum(
            metrics.selected_fresh for metrics in collection_metrics.values()
        ),
        "selected_current": sum(
            metrics.selected_current for metrics in collection_metrics.values()
        ),
        "selected_soft": sum(
            metrics.selected_soft for metrics in collection_metrics.values()
        ),
        "clipped_soft": sum(
            metrics.clipped_soft for metrics in collection_metrics.values()
        ),
        "soft_limit_overflow_selected": sum(
            metrics.selected_overflow for metrics in collection_metrics.values()
        ),
        "selected_protected_title": sum(
            metrics.selected_protected_title
            for metrics in collection_metrics.values()
        ),
        "clipped_protected_title": sum(
            metrics.clipped_protected_title
            for metrics in collection_metrics.values()
        ),
        "telegram_site_duplicates": sum(
            metrics.telegram_site_duplicates
            for metrics in collection_metrics.values()
        ),
        "channel_limits_hit": sum(
            int(metrics.feed_limit_hit)
            + int(metrics.sitemap_limit_hit)
            + int(metrics.listing_limit_hit)
            + int(metrics.homepage_limit_hit)
            + int(metrics.telegram_limit_hit)
            for metrics in collection_metrics.values()
        ),
        "discovery_seconds": round(discovery_seconds, 3),
        "candidate_admission_seconds": round(admission_seconds, 3),
        "processing_seconds": round(processing_seconds, 3),
        "postprocess_seconds": round(postprocess_seconds, 3),
        "report_seconds": round(report_seconds, 3),
        "delivery_seconds": round(delivery_seconds, 3),
        "telemetry_write_seconds": round(telemetry_write_seconds, 3),
        "total_seconds": round(time.perf_counter() - run_started, 3),
        "http_seconds_sum": round(sum(
            metrics.http_seconds for metrics in processing_metrics.values()
        ), 3),
        "discovery_http_seconds_sum": round(sum(
            metrics.endpoint_http_seconds
            for metrics in collection_metrics.values()
        ), 3),
        "source_discovery_seconds_sum": round(sum(
            metrics.discovery_seconds for metrics in collection_metrics.values()
        ), 3),
        "candidate_processing_seconds_sum": round(sum(
            metrics.processing_seconds for metrics in processing_metrics.values()
        ), 3),
        "extraction_seconds_sum": round(sum(
            metrics.extraction_seconds for metrics in processing_metrics.values()
        ), 3),
        "chromium_seconds_sum": round(sum(
            metrics.chromium_seconds for metrics in processing_metrics.values()
        ), 3),
        "chromium_attempts": sum(
            metrics.chromium_attempts for metrics in processing_metrics.values()
        ),
        "blind_zone_status_counts": {
            status: sum(
                row.get("blind_zone_status") == status for row in source_coverage
            )
            for status in sorted({
                str(row.get("blind_zone_status", ""))
                for row in source_coverage
                if row.get("blind_zone_status")
            })
        },
        "regions_checked": len(coverage),
        "sources_with_candidates": sum(
            int(row["sources_with_candidates"]) for row in coverage
        ),
        "sources_without_candidates": sum(
            int(row["sources_checked"]) - int(row["sources_with_candidates"])
            for row in coverage
        ),
        "errors": len(errors),
        "warmup": warmup,
        "email_sent": email_sent,
        "telegram_sent": telegram_sent,
        "html_report": str(html_path),
        "csv_report": str(csv_path),
        "coverage_report": str(coverage_path),
        "access_telemetry_report": str(access_telemetry_path),
    }
    LOG.info("Итог: %s", json.dumps(summary, ensure_ascii=False))
    return summary
