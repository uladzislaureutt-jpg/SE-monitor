"""Guardrail against the bug class found in report-29/30/31: a short regex
token used bare (no word-boundary anchor, no surrounding-word context) will
eventually collide with an unrelated word that happens to contain it as a
substring. Three confirmed production incidents, all in EVENT_OBJECT /
EVENT_PROBLEM patterns:

  - "санитар"  matched "санитарно-эпидемиологическая служба" (unrelated)
  - "трав"     matched "травма", then (after a first, incomplete patch)
               also matched "отравились" (poisoned) -- the prefix side of
               the same bare token, missed because the first patch only
               guarded the suffix side
  - "травл"    (different dictionary) matched "отравление" -- same family
               of false friend, in a pattern this session hadn't touched
  - "остановк", "косил", "дерев", "сток", "двор" -- found by manually
    re-auditing every remaining bare token after the above, and confirmed
    to collide with "приостановка", "закосил", "одеревенела", "восток"/
    "исток"/"листок", "дворец" respectively

Manual re-audits do not scale -- each pass found a real bug the previous
pass missed. This test does not (cannot) prove any pattern is linguistically
safe; regex has no notion of "real word." What it does is mechanical: it
freezes today's reviewed set of bare short tokens, so a *newly added* bare
token is caught at review time instead of surfacing three runs later in a
production report. Extending an existing tuple with a new bare entry, or
introducing a new dict with bare entries, will fail this test until the
entry is either anchored (see the word-boundary idiom already used for
"парк", "трав", "остановк"...) or deliberately added to REVIEWED_BASELINE
below with a comment explaining why it's accepted as-is.
"""

from __future__ import annotations

import social_monitor as sm


def _is_bare_and_short(pattern: str, max_len: int = 10) -> bool:
    """Heuristic for "risky-shaped": no context anchor, no lookaround, and
    short enough that an accidental substring collision is plausible.
    Patterns using \\s (multi-word phrases), (?< / (?! (lookaround guards),
    or longer than max_len are excluded from this check -- not because
    they're guaranteed safe, but because they're outside the specific
    failure shape this test targets.
    """
    has_guard = "\\s" in pattern or "(?<" in pattern or "(?!" in pattern
    return not has_guard and len(pattern) <= max_len


def _scan(definitions) -> set[tuple[str, str]]:
    found = set()
    for key, _label, patterns in definitions:
        for p in patterns:
            if _is_bare_and_short(p):
                found.add((key, p))
    return found


# Reviewed as of update 75 (2026-08-31/09-01). Every entry here has been
# manually checked against plausible RU/BE prefixes and suffixes. Most are
# safe because the root has no real-word collision. A few are KNOWN,
# ACCEPTED residual ambiguity -- flagged individually below rather than
# silently passing -- because fixing them needs semantic judgement a regex
# can't carry, not just another anchor:
#
#   - "дефицит" (absence_shortage): also means "бюджетный дефицит" (budget
#     deficit, a macro-economic concept, different domain). Left as-is:
#     the broader relevance pipeline's macro-exclusion filters are the
#     right layer to catch pure macro stories, not this tagging dict.
#   - "приют" (animals): can mean a shelter for people, not just animals.
#   - "садик" (education): diminutive of "сад" (garden/orchard) as well as
#     "детский садик" (kindergarten).
#   - "крыша" (housing): dated slang for mafia "protection", alongside the
#     literal "roof". Judged low real-world frequency in this monitor's
#     domain.
#   - "запрет" / "забарон" (access_restriction): correctly matches ANY
#     kind of ban/prohibition, not specifically "restricted access to a
#     public service" -- a scope-breadth issue rather than a false-friend
#     homonym, and not fixable by anchoring.
#   - "закры[тл]" (absence_shortage): same kind of breadth issue ("closed"
#     in any sense, not just a shuttered service).
#
# None of the above are silently accepted; they're deliberately unresolved
# scope calls, revisit if they cause a confirmed leak like the others did.
EVENT_OBJECT_BASELINE: set[tuple[str, str]] = {
    ("food_product", "пирож"), ("food_product", "десерт"), ("food_product", "кондитер"),
    ("parking", "парков"), ("parking", "паркінг"), ("parking", "машынамесц"),
    ("road", "асфальт"), ("road", "тротуар"), ("road", "тратуар"),
    ("road", "разметк"), ("road", "размец"),
    ("public_transport", "автобус"), ("public_transport", "аўтобус"),
    ("public_transport", "маршрут"), ("public_transport", "прыпынк"),
    ("public_transport", "вокзал"), ("public_transport", "поезд"),
    ("public_transport", "цягнік"),
    ("water_supply", "водоснаб"), ("water_supply", "водопровод"),
    ("water_supply", "пітн.*вад"), ("water_supply", "горяч.*вод"),
    ("water_supply", "гарач.*вад"),
    ("natural_water", "озер"), ("natural_water", "возер"),
    ("natural_water", "водо[её]м"), ("natural_water", "вада[её]м"),
    ("natural_water", "пруд"), ("natural_water", "берег"), ("natural_water", "бераг"),
    ("natural_water", "днепр"), ("natural_water", "дняпро"),
    ("natural_water", "припят"), ("natural_water", "прыпяц"),
    ("natural_water", "неман"), ("natural_water", "нёман"),
    ("natural_water", "сож"), ("natural_water", "буг"),
    ("waste", "мусор"), ("waste", "смец"), ("waste", "свалк"), ("waste", "звалк"),
    ("greenery", "покос"), ("greenery", "дрэў"), ("greenery", "сквер"),
    ("lighting", "освещ"), ("lighting", "асвятл"), ("lighting", "фонар"),
    ("housing", "жкх"), ("housing", "подъезд"), ("housing", "пад'езд"),
    ("housing", "подвал"), ("housing", "падвал"), ("housing", "лифт"),
    ("housing", "ліфт"), ("housing", "крыша"), ("housing", "дах"), ("housing", "двар"),
    ("healthcare", "поликлин"), ("healthcare", "паліклін"),
    ("healthcare", "больниц"), ("healthcare", "бальніц"),
    ("healthcare", "врач"), ("healthcare", "урач"),
    ("healthcare", "медицин"), ("healthcare", "медыцын"),
    ("retail", "магазин"), ("retail", "крама"), ("retail", "торгов"), ("retail", "гандл"),
    ("telecom", "интернет"), ("telecom", "інтэрнэт"), ("telecom", "iptv"),
    ("telecom", "телевид"), ("telecom", "тэлебач"),
    ("labor", "зарплат"), ("labor", "работник"), ("labor", "працаўнік"),
    ("labor", "наймальнік"), ("labor", "ўмов.*прац"),
    ("education", "школ"), ("education", "детск.*сад"), ("education", "садик"),
    ("animals", "животн"), ("animals", "жыв[её]л"), ("animals", "собак"),
    ("animals", "сабак"), ("animals", "кошк"), ("animals", "катоў"),
    ("animals", "приют"), ("animals", "прытул"),
    ("memorial", "кладбищ"), ("memorial", "могілк"),
    ("memorial", "мемориал"), ("memorial", "мемарыял"),
    ("memorial", "памятник"), ("memorial", "помнік"),
}

EVENT_PROBLEM_BASELINE: set[tuple[str, str]] = {
    ("public_resonance", "возмущ"),
    ("contamination", "стафилокок"), ("contamination", "колиформ"),
    ("bullying", "буллинг"),
    ("outage", "отключ"), ("outage", "адключ"),
    ("outage", "перебо"), ("outage", "перабо"),
    ("nonpayment", "невыплат"), ("nonpayment", "списал"), ("nonpayment", "списан"),
    ("pollution", "загряз"), ("pollution", "забрудж"), ("pollution", "сцёк"),
    ("pollution", "нечистот"), ("pollution", "брудн.*вод"),
    ("access_restriction", "запрет"), ("access_restriction", "забарон"),
    ("maintenance", "нескош"), ("maintenance", "непокош"),
    ("maintenance", "зарос"), ("maintenance", "зарас"),
    ("maintenance", "мусор"), ("maintenance", "смец"),
    ("damage", "разбит"), ("damage", "разбіт"), ("damage", "разруш"),
    ("damage", "разбур"), ("damage", "выбоин"),
    ("damage", "неисправ"), ("damage", "няспраў"),
    ("absence_shortage", "нехват"), ("absence_shortage", "дефицит"),
    ("absence_shortage", "адсутн"), ("absence_shortage", "отсутств"),
    ("absence_shortage", "закры[тл]"),
    ("safety", "опасн"), ("safety", "небезпеч"), ("safety", "небяспеч"),
    ("safety", "угроз"), ("safety", "пагроз"),
    ("safety", "травм"), ("safety", "траўм"),
    ("work_conditions", "жар[аыу]"), ("work_conditions", "температур"),
    ("work_conditions", "тэмператур"), ("work_conditions", "ўмов.*прац"),
    ("service_quality", "няякасн"), ("service_quality", "плох.*связ"),
}


def test_no_new_unreviewed_bare_object_patterns():
    live = _scan(sm.EVENT_OBJECT_PATTERNS)
    new = live - EVENT_OBJECT_BASELINE
    removed = EVENT_OBJECT_BASELINE - live
    assert not new, (
        f"New bare/unanchored short pattern(s) added to EVENT_OBJECT_PATTERNS "
        f"without lint review: {sorted(new)}. Either anchor with a word-boundary "
        f"guard (see 'трав', 'остановк', 'двор' for examples) or add to "
        f"EVENT_OBJECT_BASELINE in this file with a comment explaining why "
        f"the bare form is safe."
    )
    assert not removed, (
        f"Pattern(s) removed/changed from the reviewed baseline: {sorted(removed)}. "
        f"If intentional (e.g. anchored or reworded), update EVENT_OBJECT_BASELINE "
        f"to match."
    )


def test_no_new_unreviewed_bare_problem_patterns():
    live = _scan(sm.EVENT_PROBLEM_PATTERNS)
    new = live - EVENT_PROBLEM_BASELINE
    removed = EVENT_PROBLEM_BASELINE - live
    assert not new, (
        f"New bare/unanchored short pattern(s) added to EVENT_PROBLEM_PATTERNS "
        f"without lint review: {sorted(new)}. Either anchor with a word-boundary "
        f"guard or add to EVENT_PROBLEM_BASELINE in this file with a comment "
        f"explaining why the bare form is safe."
    )
    assert not removed, (
        f"Pattern(s) removed/changed from the reviewed baseline: {sorted(removed)}. "
        f"If intentional, update EVENT_PROBLEM_BASELINE to match."
    )
