"""Result Event Integrity 1.15 — root cause of vkurier.by extraction_blind,
found via a real view-source of https://vkurier.by/238617 provided
directly by the user (2026-08-30).

<body class="wp-singular ... sidebar-right"> — the WordPress theme's own
"sidebar on the right" layout hint on <body> — matched the broad
[class*='sidebar'] pattern in CONTENT_NOISE_SELECTORS, and tag.decompose()
removed the entire <body>, taking the real .entry-content article with it.
Confirmed directly against the real HTML: extraction_strategy was "empty"
(0 chars) before this fix and "source_specific" (6388 chars) after it, with
the Result Event Integrity 1.14 vkurier.by SOURCE_PRECLEAN_CONTENT_SELECTORS
entry removed to isolate this fix specifically.

Not vkurier.by-specific: any source whose theme puts a layout/utility class
containing one of the noise substrings (sidebar, widget, social, comment,
share...) directly on <html>/<body>/<main>/<article> was silently
unrecoverable before this fix.
"""
from bs4 import BeautifulSoup

import social_monitor


def test_sidebar_class_on_body_no_longer_deletes_the_whole_page():
    # The exact real-world shape: a WordPress theme's own layout hint on
    # <body>, not a real sidebar widget.
    html = """
    <html><body class="wp-singular single-post sidebar-right">
    <div id="primary">
    <main id="main">
    <article id="post-1" class="post type-post">
      <h1>Заголовок</h1>
      <div class="entry-content">
        <p>Первый абзац содержательной статьи с достаточным количеством
        текста для прохождения порога длины извлечения контента.</p>
        <p>Второй абзац продолжает раскрывать тему, добавляя детали.</p>
      </div>
    </article>
    </main>
    </div>
    <aside class="widget-area">Виджет сайдбара, который должен быть удалён</aside>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    remaining = soup.select_one(".entry-content")
    assert remaining is not None
    social_monitor.remove_content_noise(soup)
    survivor = soup.select_one(".entry-content")
    assert survivor is not None, ".entry-content was deleted along with <body>"
    assert "Первый абзац содержательной статьи" in survivor.get_text()


def test_real_sidebar_widgets_are_still_removed():
    # A genuine sidebar widget (not on a structural tag) must still go.
    html = """
    <html><body>
    <div class="entry-content"><p>Реальная статья с текстом, которого
    достаточно для прохождения порога длины извлечения.</p></div>
    <div class="widget-sidebar">Мусор из виджета сайдбара</div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    social_monitor.remove_content_noise(soup)
    assert soup.select_one(".widget-sidebar") is None
    assert soup.select_one(".entry-content") is not None


def test_vkurier_real_article_extracts_via_root_cause_fix_alone():
    # Confirms the general fix resolves the real case even without the
    # Result Event Integrity 1.14 vkurier.by-specific preclean override —
    # that entry remains as defense-in-depth, not the primary fix.
    html = (
        "<html><body class=\"wp-theme-reboot sidebar-right\">"
        "<main><article class=\"post-238617\">"
        "<h1>Под Россонами задержали около 350 участников семейного слёта</h1>"
        "<div class=\"entry-content\" itemprop=\"articleBody\">"
        "<p>Около 350 человек, включая семьи с детьми, задержали на берегу "
        "озера Белое возле деревни Межно в Россонском районе, приехавших на "
        "ежегодную встречу «Семья радуги».</p>"
        "<p>Массовое задержание произошло 29 июля, людей забирали целыми "
        "семьями, несколько детей провели ночь в изоляторе.</p>"
        "</div></article></main></body></html>"
    )
    src = social_monitor.Source(
        enabled=True, country="Беларусь", country_code="BY-VI",
        locality="Витебск", rank=1, priority="A", name="Витебский курьер",
        media_type="website", domain="vkurier.by",
        start_url="https://vkurier.by/", language="ru",
        adapter="numeric_articles",
    )
    saved = social_monitor.SOURCE_PRECLEAN_CONTENT_SELECTORS.pop("vkurier.by", None)
    try:
        soup = BeautifulSoup(html, "html.parser")
        text = social_monitor.extract_source_specific_article_text(soup, src)
        assert "задержали на берегу" in text
    finally:
        if saved is not None:
            social_monitor.SOURCE_PRECLEAN_CONTENT_SELECTORS["vkurier.by"] = saved
