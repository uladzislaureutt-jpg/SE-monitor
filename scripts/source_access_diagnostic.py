#!/usr/bin/env python3
"""Read-only availability and production-extraction diagnostic for candidates.

The script imports only pure production helpers.  It never runs the monitor,
reads or writes state/cache, changes source configuration, or sends delivery.
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
    production_article_probes: int = 0
    production_probe_adapter: str = "robust_article"
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
            "production_article_probes": self.production_article_probes,
            "production_probe_adapter": self.production_probe_adapter,
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
    return (urlparse(value).hostname or "").lower().removeprefix("www.")


def clean_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def same_site_listing_urls(
    listing_url: str,
    links: Iterable[tuple[str, str]],
    expected_domain: str,
) -> list[str]:
    """Return only de-duplicated same-site hrefs.

    This intentionally makes no independent judgement about whether a link is
    an article.  That decision belongs exclusively to production
    ``classify_source_url()`` below.
    """
    results: list[str] = []
    seen: set[str] = set()
    expected = expected_domain.lower().removeprefix("www.")
    for href, _ in links:
        absolute = clean_url(urljoin(listing_url, href))
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or normal_host(absolute) != expected:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        results.append(absolute)
    return results


def production_article_urls(
    source: dict[str, str],
    listing_url: str,
    links: Iterable[tuple[str, str]],
) -> tuple[list[str], int]:
    """Classify every same-site listing link using production code only."""
    if _production is None:
        raise RuntimeError(f"social_monitor.py unavailable: {PRODUCTION_MODULE_ERROR}")
    candidates = same_site_listing_urls(listing_url, links, source["domain"])
    articles = [
        url for url in candidates
        if _production.classify_source_url(url, source["domain"]) == "article"
    ]
    return articles, len(candidates)


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
    source: dict[str, str], article_url: str, article_html: str,
) -> dict[str, object]:
    """Extract a production-classified article with the real extractor only."""
    if _production is None:
        raise RuntimeError(f"social_monitor.py unavailable: {PRODUCTION_MODULE_ERROR}")
    domain = source["domain"]
    try:
        # The planned source row must use this adapter if the source is later
        # accepted; the report records it explicitly instead of pretending to
        # test an as-yet nonexistent sources.csv row.
        probe_source = _production.Source(
            enabled=True, country="Беларусь", country_code="BY-XX",
            locality=source.get("locality_hint", "") or "Беларусь",
            rank=1, priority="B", name=source["name"], media_type="website",
            domain=domain, start_url=source["start_url"],
            language=(source.get("language_hint", "ru") or "ru").split(",")[0],
            adapter="robust_article",
        )
        probe_candidate = _production.Candidate(
            source=probe_source, url=article_url, discovered_via="diagnostic",
        )
        extracted = _production.extract_article_from_html(probe_candidate, article_html)
        return {
            "production_extraction_strategy": extracted.extraction_strategy or "empty",
            "production_text_chars": len(extracted.text),
        }
    except Exception as exc:  # Defensive: an extractor crash must not abort the run.
        return {
            "production_extraction_strategy": f"error: {type(exc).__name__}: {exc}",
            "production_text_chars": 0,
        }


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

    record.production_checked = True
    try:
        candidates, candidates_seen = production_article_urls(
            source, listing.final_url or source["listing_url"], parsed_listing.links,
        )
    except Exception as exc:
        record.status = "collector_error"
        record.errors.append(f"production classifier: {type(exc).__name__}: {exc}")
        record.note = "Production-классификатор недоступен; результат не может считаться техническим подтверждением."
        return record

    record.production_candidates_seen = candidates_seen
    record.production_articles_recognized = len(candidates)
    record.article_candidates = len(candidates)
    if not candidates:
        record.status = "discovery_not_recognized" if candidates_seen else "homepage_only"
        record.note = (
            "Ссылки на странице найдены, но production classify_source_url() "
            "ни одну не признал статьёй — нужна проверка URL-паттерна до "
            "включения в config/sources.csv."
            if candidates_seen else
            "Лента доступна, но на ней не найдено ссылок того же домена."
        )
        return record

    best_production_text = -1
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
        record.production_article_probes += 1
        article_parsed = parse_html(article.body)
        text_length = len(" ".join(article_parsed.text))
        production_result = real_production_check(source, article.final_url or article_url, article.body)
        production_text = int(production_result["production_text_chars"])
        if production_text > best_production_text:
            best_production_text = production_text
            record.best_article_url = article.final_url or article_url
            record.best_article_status = article.status
            record.best_article_text_chars = text_length
            record.best_article_title = " ".join(article_parsed.h1 or article_parsed.title)[:250]
            record.production_extraction_strategy = str(production_result["production_extraction_strategy"])
            record.production_text_chars = production_text

    if record.production_article_probes == 0:
        record.status = "collector_error"
        record.note = "Production распознал статьи, но ни одну из первых диагностических проб не удалось скачать."
    elif record.production_text_chars < PRODUCTION_EXTRACTION_MIN_CHARS:
        record.status = "extract_blocked_by_pipeline"
        record.note = (
            f"Production-классификатор выбрал только статьи; максимум текста "
            f"после extract_article_from_html() — {record.production_text_chars} символов. "
            "Нужен адресный селектор/предочистка, а не отказ от источника."
        )
    elif record.newest_date_age_days is not None and record.newest_date_age_days <= RECENCY_DAYS:
        record.status = "pass_recent"
        record.note = (
            "Лента свежая; production-классификатор и production-экстрактор "
            f"подтвердили статью ({record.production_extraction_strategy})."
        )
    else:
        record.status = "extract_ok_no_fresh_date"
        record.note = (
            "Production-классификатор и production-экстрактор подтвердили "
            "статью, но свежесть ленты не доказана датой; проверить пагинацию."
        )
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
    if _production is None:
        print(
            "FATAL: social_monitor.py could not be imported; refusing a "
            "non-production diagnostic: " + PRODUCTION_MODULE_ERROR,
            file=sys.stderr,
        )
        return 2
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
