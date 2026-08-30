import importlib.util
import sys
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "source_access_diagnostic.py"
SPEC = importlib.util.spec_from_file_location("source_access_diagnostic", MODULE)
MODULE_UNDER_TEST = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE_UNDER_TEST
SPEC.loader.exec_module(MODULE_UNDER_TEST)


class SourceAccessDiagnosticTests(unittest.TestCase):
    def test_same_site_listing_urls_only_collects_links_without_article_heuristics(self):
        links = [
            ("/news/obshchestvo/important-story.html", ""),
            ("/category/obshchestvo/", ""),
            ("/contacts", ""),
            ("/assets/logo.png", ""),
        ]
        result = MODULE_UNDER_TEST.same_site_listing_urls("https://example.by/news", links, "example.by")
        self.assertEqual(result, [
            "https://example.by/news/obshchestvo/important-story.html",
            "https://example.by/category/obshchestvo/",
            "https://example.by/contacts",
            "https://example.by/assets/logo.png",
        ])

    def test_newest_listing_date_accepts_two_common_formats(self):
        result = MODULE_UNDER_TEST.newest_listing_date("Свежий материал: 29.08.2026; архив: 2026-08-25")
        self.assertEqual(result.isoformat(), "2026-08-29")

    def test_same_site_matching_ignores_a_default_port(self):
        self.assertEqual(MODULE_UNDER_TEST.normal_host("https://www.orshanka.by:443/?p=1"), "orshanka.by")

    def test_endpoint_urls_are_unique_and_include_feed_and_sitemap(self):
        source = {"start_url": "https://example.by/news", "listing_url": "https://example.by/news"}
        result = MODULE_UNDER_TEST.endpoint_urls(source)
        self.assertEqual(result[0], "https://example.by/news")
        self.assertIn("https://example.by/feed/", result)
        self.assertIn("https://example.by/sitemap.xml", result)


class ProductionIntegrationTests(unittest.TestCase):
    """The diagnostic's whole purpose is to predict what production will
    do; a from-scratch heuristic that silently disagrees with production is
    worse than no diagnostic at all. See the vkurier.by investigation
    (2026-08-28..30, reports 18-24): a naive page-wide text-length check
    called the source fully accessible for over a week while the real
    extractor (via extract_article_from_html) returned zero characters,
    because <body class="...sidebar-right"> — the theme's own layout hint,
    not a real sidebar widget — matched a broad noise-removal pattern and
    was decomposed whole, taking the real article with it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if MODULE_UNDER_TEST._production is None:  # pragma: no cover
            raise unittest.SkipTest(
                "social_monitor.py not importable — run from within a full "
                "S-monitor checkout to exercise the production integration."
            )

    def test_real_article_is_recognized_and_extracted(self):
        html = (
            "<html><body class=\"wp-theme-reboot sidebar-right\">"
            "<main><article class=\"post-1\">"
            "<h1>Заголовок</h1>"
            "<div class=\"entry-content\">"
            "<p>Первый абзац содержательной статьи с достаточным "
            "количеством текста для прохождения порога извлечения "
            "контента источника, чтобы тест был реалистичным.</p>"
            "<p>Второй абзац продолжает раскрывать тему, добавляя "
            "детали и уточнения по обсуждаемому вопросу.</p>"
            "</div></article></main></body></html>"
        )
        source = {
            "domain": "vkurier.by", "name": "Тест",
            "start_url": "https://vkurier.by/",
            "locality_hint": "Витебск", "language_hint": "ru",
        }
        result = MODULE_UNDER_TEST.real_production_check(
            source, "https://vkurier.by/238617", html,
        )
        self.assertEqual(result["production_extraction_strategy"], "source_specific")
        self.assertGreater(result["production_text_chars"], 100)

    def test_body_level_noise_class_does_not_wipe_extraction(self):
        # Regression test for the exact vkurier.by root cause: a structural
        # tag (<body>) carrying a class that happens to match a noise
        # pattern (here [class*='sidebar']) must not delete the whole page.
        production = MODULE_UNDER_TEST._production
        self.assertIn("html", production.CONTENT_NOISE_PROTECTED_TAGS)
        self.assertIn("body", production.CONTENT_NOISE_PROTECTED_TAGS)
        self.assertIn("main", production.CONTENT_NOISE_PROTECTED_TAGS)
        self.assertIn("article", production.CONTENT_NOISE_PROTECTED_TAGS)

    def test_unrecognized_url_is_not_promoted_to_an_article(self):
        # A listing page whose links production's classifier does not
        # consider articles at all (e.g. an unhandled URL scheme, mirroring
        # the pre-1.9 vkurier.by bare-numeric-id gap) must not be reported
        # the same way as "no candidates found" or "extraction failed" —
        # each needs a different fix.
        self.assertEqual(
            MODULE_UNDER_TEST._production.classify_source_url("https://example.by/x", "example.by"),
            "unknown",
        )

    def test_wordpress_query_profiles_accept_posts_but_not_service_pages(self):
        production = MODULE_UNDER_TEST._production
        for domain in ("hoiniki.by", "klich.by", "orshanka.by"):
            self.assertEqual(
                production.classify_source_url(f"https://www.{domain}/?p=171886", domain),
                "article",
            )
            self.assertNotEqual(
                production.classify_source_url(f"https://www.{domain}/?page_id=2", domain),
                "article",
            )

    def test_diagnostic_probes_only_production_classified_links(self):
        source = {
            "domain": "orshanka.by", "name": "Тест",
            "start_url": "https://www.orshanka.by/", "language_hint": "ru",
        }
        articles, seen = MODULE_UNDER_TEST.production_article_urls(
            source,
            "https://www.orshanka.by/",
            [("/?p=171886", ""), ("/?cat=4035", ""), ("/?page_id=2", "")],
        )
        self.assertEqual(seen, 3)
        self.assertEqual(articles, ["https://www.orshanka.by/?p=171886"])

    def test_pvestnik_profile_rejects_archives_and_keeps_real_articles(self):
        production = MODULE_UNDER_TEST._production
        self.assertNotEqual(
            production.classify_source_url("https://www.pvestnik.by/2026/08/03/", "pvestnik.by"),
            "article",
        )
        self.assertEqual(
            production.classify_source_url("https://www.pvestnik.by/152230-2/", "pvestnik.by"),
            "article",
        )
