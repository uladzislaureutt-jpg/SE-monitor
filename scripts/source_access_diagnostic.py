#!/usr/bin/env python3
"""Read-only availability and extraction diagnostic for S-monitor candidates.

The script deliberately does not import the monitor, does not read or write its
state/cache, and does not update source configuration. It uses only public HTTP
GET requests and stores a compact diagnostic report under the requested folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

# Result Event Integrity lesson (vkurier.by, 2026-08-28..30, reports 18-24):
# a from-scratch reimplementation of "does this look like an article" can
# silently disagree with what production actually does, in either
# direction — vkurier.by looked fully accessible to a naive text-length
# check for over a week while the real pipeline extracted zero characters
# (its class="...sidebar-right" on <body> was decomposed whole by the
# noise-removal pass), and a naive check can just as easily call a source
# "extractable" using page-wide text (nav/footer/sidebar included) that the
# real per-source selectors would never isolate as the article body. This
# diagnostic now imports the actual production functions — pure, read-only,
# no state/cache/network side effects beyond what this script already does
# — so a "pass" here means production would actually admit and extract the
# same page, not just that this script's own heuristic liked it.
PRODUCTION_MODULE_ERROR = ""
try:
    _repo_root = Path(__file__).resolve().parents[1]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    import social_monitor as _production  # noqa: E402  (conditional import)
except Exception as _exc:  # pragma: no cover - exercised only outside the repo
    _production = None
    PRODUCTION_MODULE_ERROR = f"{type(_exc).__name__}: {_exc}"

PRODUCTION_EXTRACTION_MIN_CHARS = 500


USER_AGENT = "S-monitor-source-access-diagnostic/1.0 (+read-only; contact: local-admin)"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_200_000
MAX_LISTING_TEXT_CHARS = 220_000
MAX_ARTICLE_PROBES = 4
MAX_WORKERS = 3
RECENCY_DAYS = 120

COMMON_DISCOVERY_PATHS = (
    "/feed/",
    "/feed",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/news-sitemap.xml",
)

SKIP_LINK_PARTS = (
    "/tag/",
    "/category/",
    "/author/",
    "/page/",
    "/search",
    "/contacts",
    "/contact",
    "/about",
    "/login",
    "/register",
    "/advert",
    "/reklam",
    "/privacy",
)
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".mp4", ".mp3", ".zip")
BLOCK_MARKERS = ("cloudflare", "access denied", "just a moment", "captcha", "временно ограничен")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self.title: list[str] = []
        self.h1: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "a" and attrs_map.get("href"):
            self.links.append((attrs_map["href"] or "", ""))
        self._in_title = tag == "title"
        self._in_h1 = tag == "h1"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        clean = " ".join(unescape(data).split())
        if not clean or self._skip_depth:
            return
        self.text.append(clean)
        if self._in_title:
            self.title.append(clean)
        if self._in_h1:
            self.h1.append(clean)


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    final_url: str = ""
    content_type: str = ""
    body: str = ""
    error: str = ""
    elapsed_ms: int | None = None


@dataclass
class Diagnostic:
    source_id: str
    name: str
    domain: str
    start_url: str
    listing_url: str
    status: str = "collector_error"
    start_http_status: int | None = None
    start_final_url: str = ""
    listing_text_chars: int = 0
    newest_date: str = ""
    newest_date_age_days: int | None = None
    discovery_checked: int = 0
    rss_or_sitemap_hits: list[str] = field(default_factory=list)
    article_candidates: int = 0
    best_article_url: str = ""
    best_article_status: int | None = None
    best_article_title: str = ""
    best_article_text_chars: int = 0
    production_checked: bool = False
    production_articles_recognized: int = 0
    production_candidates_seen: int = 0
    production_extraction_strategy: str = ""
    production_text_chars: int = 0
    errors: list[str] = field(default_factory=list)
    note: str = ""

    def to_row(self) -> dict[str, str | int | None]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "domain": self.domain,
            "status": self.status,
            "start_http_status": self.start_http_status,
            "start_final_url": self.start_final_url,
            "listing_text_chars": self.listing_text_chars,
            "newest_date": self.newest_date,
            "newest_date_age_days": self.newest_date_age_days,
            "discovery_checked": self.discovery_checked,
            "rss_or_sitemap_hits": " | ".join(self.rss_or_sitemap_hits),
            "article_candidates": self.article_candidates,
            "best_article_url": self.best_article_url,
            "best_article_status": self.best_article_status,
            "best_article_title": self.best_article_title,
            "best_article_text_chars": self.best_article_text_chars,
            "production_checked": self.production_checked,
            "production_articles_recognized": self.production_articles_recognized,
            "production_candidates_seen": self.production_candidates_seen,
            "production_extraction_strategy": self.production_extraction_strategy,
            "production_text_chars": self.production_text_chars,
            "errors": " | ".join(self.errors),
            "note": self.note,
        }


def fetch(url: str) -> FetchResult:
    started = datetime.now(timezone.utc)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5"})
    try:
        context = ssl.create_default_context()
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="replace")
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            return FetchResult(
                url=url,
                status=getattr(response, "status", 200),
                final_url=response.geturl(),
                content_type=response.headers.get("Content-Type", ""),
                body=body,
                elapsed_ms=elapsed,
            )
    except HTTPError as exc:
        return FetchResult(url=url, status=exc.code, error=f"HTTP {exc.code}: {exc.reason}")
    except (URLError, TimeoutError, ValueError) as exc:
        return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # Defensive: diagnostics must continue for other sources.
        return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")


def parse_html(html: str) -> PageParser:
    parser = PageParser()
    parser.feed(html)
    parser.close()
    return parser


def normal_host(value: str) -> str:
    return urlparse(value).netloc.lower().removeprefix("www.")


def clean_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def candidate_article_urls(listing_url: str, links: Iterable[tuple[str, str]], expected_domain: str) -> list[str]:
    results: list[tuple[int, str]] = []
    seen: set[str] = set()
    expected = expected_domain.lower().removeprefix("www.")
    listing_clean = clean_url(listing_url).rstrip("/")
    for href, _ in links:
        absolute = clean_url(urljoin(listing_url, href))
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or normal_host(absolute) != expected:
            continue
        path = parsed.path.lower()
        if not path or absolute.rstrip("/") == listing_clean or any(item in path for item in SKIP_LINK_PARTS):
            continue
        if path.endswith(SKIP_EXTENSIONS) or parsed.query.startswith("page="):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        score = 0
        if re.search(r"/(20\d{2})/\d{2}/", path):
            score += 8
        if "/news/" in path or "/novosti/" in path or "/item/" in path:
            score += 5
        if path.endswith(".html"):
            score += 4
        if len(path.strip("/")) > 24:
            score += 2
        if parsed.query:
            score -= 1
        results.append((score, absolute))
    return [url for _, url in sorted(results, key=lambda item: (-item[0], item[1]))][:20]


def newest_listing_date(text: str) -> date | None:
    candidates: list[date] = []
    for year, month, day in re.findall(r"\b(20\d{2})[-./](\d{1,2})[-./](\d{1,2})\b", text):
        try:
            candidates.append(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    for day, month, year in re.findall(r"\b(\d{1,2})[-./](\d{1,2})[-./](20\d{2})\b", text):
        try:
            candidates.append(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    return max(candidates) if candidates else None


def endpoint_urls(source: dict[str, str]) -> list[str]:
    base = f"{urlparse(source['start_url']).scheme}://{urlparse(source['start_url']).netloc}"
    urls = [source["listing_url"]]
    urls.extend(urljoin(base, path) for path in COMMON_DISCOVERY_PATHS)
    return list(dict.fromkeys(urls))


def is_feed_or_sitemap(result: FetchResult) -> bool:
    value = (result.content_type + " " + result.body[:1000]).lower()
    return "xml" in value or "<rss" in value or "<feed" in value or "<urlset" in value or "<sitemapindex" in value


def real_production_check(
    source: dict[str, str],
    listing_url: str,
    links: Iterable[tuple[str, str]],
    best_article_url: str,
    best_article_html: str,
) -> dict[str, object]:
    """Run the actual production URL classifier and article extractor
    against what this diagnostic already fetched, read-only. Returns a dict
    with production_* fields; empty/zero values if social_monitor could not
    be imported (e.g. the script was run outside a full repo checkout).

    This never touches data/state.json, data/discovery_cache.json, or
    config/sources.csv, and never calls run_monitor()/main() — only pure
    classification and extraction functions.
    """
    result: dict[str, object] = {
        "production_articles_recognized": 0,
        "production_candidates_seen": 0,
        "production_extraction_strategy": "",
        "production_text_chars": 0,
    }
    if _production is None:
        return result

    domain = source["domain"]
    seen_hosts: set[str] = set()
    candidate_urls: list[str] = []
    for href, _ in links:
        absolute = clean_url(urljoin(listing_url, href))
        if normal_host(absolute) != domain.lower().removeprefix("www.") or absolute in seen_hosts:
            continue
        seen_hosts.add(absolute)
        candidate_urls.append(absolute)
    result["production_candidates_seen"] = len(candidate_urls)
    try:
        result["production_articles_recognized"] = sum(
            1 for url in candidate_urls
            if _production.classify_source_url(url, domain) == "article"
        )
    except Exception as exc:  # Defensive: a classifier crash must not abort the run.
        result["production_articles_recognized"] = f"error: {type(exc).__name__}: {exc}"

    if not best_article_url or not best_article_html:
        return result
    try:
        # adapter="robust_article" opts into the broadest extraction cascade
        # (source-specific selectors -> JSON-LD -> generic HTML, including
        # the scored "best container" fallback) so this diagnostic gives a
        # new source the same benefit of the doubt production's most
        # permissive existing adapter would, rather than under-reporting
        # what a properly configured entry in config/sources.csv could do.
        probe_source = _production.Source(
            enabled=True, country="Беларусь", country_code="BY-XX",
            locality=source.get("locality_hint", "") or "Беларусь",
            rank=1, priority="B", name=source["name"], media_type="website",
            domain=domain, start_url=source["start_url"],
            language=(source.get("language_hint", "ru") or "ru").split(",")[0],
            adapter="robust_article",
        )
        probe_candidate = _production.Candidate(
            source=probe_source, url=best_article_url, discovered_via="diagnostic",
        )
        extracted = _production.extract_article_from_html(probe_candidate, best_article_html)
        result["production_extraction_strategy"] = extracted.extraction_strategy or "empty"
        result["production_text_chars"] = len(extracted.text)
    except Exception as exc:  # Defensive: an extractor crash must not abort the run.
        result["production_extraction_strategy"] = f"error: {type(exc).__name__}: {exc}"
    return result


def diagnose(source: dict[str, str]) -> Diagnostic:
    record = Diagnostic(
        source_id=source["source_id"],
        name=source["name"],
        domain=source["domain"],
        start_url=source["start_url"],
        listing_url=source["listing_url"],
    )
    listing = fetch(source["listing_url"])
    record.start_http_status = listing.status
    record.start_final_url = listing.final_url
    if listing.error:
        record.errors.append(listing.error)
    if listing.status in {401, 403, 429}:
        record.status = "blocked"
        record.note = "Доступ ограничен HTTP-статусом; не обходить ограничение, проверить легальный маршрут."
        return record
    if listing.status is None or not (200 <= listing.status < 400):
        record.status = "collector_error"
        record.note = "Стартовая лента не получена."
        return record

    lowered = listing.body.lower()
    if any(marker in lowered for marker in BLOCK_MARKERS) and len(listing.body) < 120_000:
        record.status = "blocked"
        record.note = "Получена страница антибот-защиты или ограничения доступа."
        return record

    parsed_listing = parse_html(listing.body)
    listing_text = " ".join(parsed_listing.text)[:MAX_LISTING_TEXT_CHARS]
    record.listing_text_chars = len(listing_text)
    recent = newest_listing_date(listing_text)
    if recent:
        record.newest_date = recent.isoformat()
        record.newest_date_age_days = (date.today() - recent).days

    for endpoint in endpoint_urls(source):
        if endpoint == source["listing_url"]:
            continue
        record.discovery_checked += 1
        discovered = fetch(endpoint)
        if discovered.status and 200 <= discovered.status < 400 and is_feed_or_sitemap(discovered):
            record.rss_or_sitemap_hits.append(discovered.final_url or endpoint)

    candidates = candidate_article_urls(listing.final_url or source["listing_url"], parsed_listing.links, source["domain"])
    record.article_candidates = len(candidates)
    if not candidates:
        record.status = "homepage_only"
        record.note = "Лента доступна, но диагностике не удалось надежно выделить статью: нужен адресный адаптер или иной listing URL."
        return record

    best_text = 0
    best_article_html = ""
    for article_url in candidates[:MAX_ARTICLE_PROBES]:
        article = fetch(article_url)
        if article.error:
            record.errors.append(article.error)
            continue
        if article.status in {401, 403, 429}:
            record.errors.append(f"article {article.status}: {article_url}")
            continue
        if not article.status or not (200 <= article.status < 400):
            continue
        article_parsed = parse_html(article.body)
        article_text = " ".join(article_parsed.text)
        text_length = len(article_text)
        if text_length > best_text:
            best_text = text_length
            best_article_html = article.body
            record.best_article_url = article.final_url or article_url
            record.best_article_status = article.status
            record.best_article_text_chars = text_length
            record.best_article_title = " ".join(article_parsed.h1 or article_parsed.title)[:250]

    record.production_checked = _production is not None
    production_result = real_production_check(
        source, listing.final_url or source["listing_url"], parsed_listing.links,
        record.best_article_url, best_article_html,
    )
    record.production_articles_recognized = production_result["production_articles_recognized"]
    record.production_candidates_seen = production_result["production_candidates_seen"]
    record.production_extraction_strategy = production_result["production_extraction_strategy"]
    record.production_text_chars = production_result["production_text_chars"]

    if _production is None:
        record.errors.append(f"production module unavailable: {PRODUCTION_MODULE_ERROR}")

    # Result Event Integrity lesson: decide primarily on what production's
    # own classifier/extractor would actually do, not on this script's own
    # simplified heuristics. vkurier.by passed a naive "500+ chars of
    # page-wide text" check for over a week while production extracted zero
    # characters — this diagnostic must not repeat that mismatch for the
    # next 14 sources.
    if record.production_checked:
        if record.production_articles_recognized == 0 and record.production_candidates_seen > 0:
            record.status = "discovery_not_recognized"
            record.note = (
                "Ссылки на странице найдены, но производственный "
                "classify_source_url() ни одну не признал статьёй — "
                "нужна проверка URL-паттерна (см. is_probable_article_url) "
                "до включения в config/sources.csv, а не после."
            )
        elif record.production_text_chars < PRODUCTION_EXTRACTION_MIN_CHARS:
            record.status = "extract_blocked_by_pipeline"
            record.note = (
                f"Наивный парсер нашёл {record.best_article_text_chars} "
                "симв. текста на странице, но реальный "
                "extract_article_from_html() — "
                f"{record.production_text_chars}. Именно так выглядел "
                "vkurier.by все прогоны до диагностики по сырому HTML: "
                "нужен SOURCE_CONTENT_SELECTORS/SOURCE_PRECLEAN_CONTENT_SELECTORS "
                "для этого домена, а не отказ от источника."
            )
        elif record.newest_date_age_days is not None and record.newest_date_age_days <= RECENCY_DAYS:
            record.status = "pass_recent"
            record.note = (
                "Лента свежая, ссылка на статью найдена, "
                f"production-экстрактор ({record.production_extraction_strategy}) "
                "подтвердил извлекаемость текста."
            )
        else:
            record.status = "extract_ok_no_fresh_date"
            record.note = (
                "Production-экстрактор подтвердил извлекаемость текста, но "
                "свежесть ленты не подтверждена датой; проверить дату и "
                "пагинацию вручную."
            )
        return record

    # Fallback path: social_monitor.py was not importable (e.g. the script
    # ran outside a full repo checkout). Less reliable — flagged in the note
    # and in the production_checked column so this is never silently
    # mistaken for a production-verified result.
    if record.best_article_text_chars < 500:
        record.status = "homepage_only"
        record.note = (
            "[БЕЗ production-проверки] Статья открывается недостаточно "
            "полно; нужен анализ HTML-структуры или специализированный "
            "экстрактор."
        )
    elif record.newest_date_age_days is not None and record.newest_date_age_days <= RECENCY_DAYS:
        record.status = "pass_recent"
        record.note = (
            "[БЕЗ production-проверки, только наивный парсер] Лента "
            "свежая, ссылка на статью найдена, текст доступен для "
            "извлечения."
        )
    else:
        record.status = "extract_ok_no_fresh_date"
        record.note = "Текст извлекается, но свежесть ленты не подтверждена датой; проверить дату и пагинацию вручную."
    return record


def load_candidates(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"source_id", "name", "domain", "start_url", "listing_url"}
    missing = required - set(rows[0] if rows else {})
    if missing:
        raise ValueError(f"В CSV не хватает обязательных колонок: {', '.join(sorted(missing))}")
    return rows


def write_reports(records: list[Diagnostic], output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    csv_path = output_dir / f"source_access_diagnostic_{stamp}.csv"
    json_path = output_dir / f"source_access_diagnostic_{stamp}.json"
    md_path = output_dir / f"source_access_diagnostic_{stamp}.md"
    rows = [record.to_row() for record in records]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["source_id"])
        writer.writeheader()
        writer.writerows(rows)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)

    groups: dict[str, list[Diagnostic]] = {}
    for record in records:
        groups.setdefault(record.status, []).append(record)
    lines = ["# Диагностика доступа и извлекаемости источников", "", f"Проверено источников: {len(records)}", ""]
    for status in sorted(groups):
        lines.append(f"## {status} ({len(groups[status])})")
        for record in groups[status]:
            lines.append(f"- **{record.name}** — {record.note}")
            if record.best_article_url:
                lines.append(f"  - статья: {record.best_article_url}")
            if record.errors:
                lines.append(f"  - ошибки: {'; '.join(record.errors)}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only source availability and extraction diagnostic")
    parser.add_argument("--candidates", required=True, type=Path, help="CSV with diagnostic candidates")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directory for diagnostic reports")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Maximum concurrent sources (default: 3)")
    args = parser.parse_args()
    sources = load_candidates(args.candidates)
    records: list[Diagnostic] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, MAX_WORKERS))) as executor:
        futures = {executor.submit(diagnose, source): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:
                records.append(Diagnostic(
                    source_id=source["source_id"],
                    name=source["name"],
                    domain=source["domain"],
                    start_url=source["start_url"],
                    listing_url=source["listing_url"],
                    status="collector_error",
                    errors=[f"unhandled: {type(exc).__name__}: {exc}"],
                    note="Непредвиденная ошибка диагностического скрипта.",
                ))
    records.sort(key=lambda record: record.source_id)
    csv_path, json_path, md_path = write_reports(records, args.out_dir)
    for record in records:
        print(f"{record.status:28} {record.source_id:26} {record.name}")
    print(f"\nReports: {csv_path}, {json_path}, {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
