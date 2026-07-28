from smartboi.dedup import DedupIndex, fingerprint, normalize_headline, source_domain


def test_normalize_headline_collapses_punctuation_and_case():
    a = normalize_headline("Big Co. Announces New Plant!!")
    b = normalize_headline("big co announces new plant")
    assert a == b


def test_normalize_headline_collapses_whitespace():
    assert normalize_headline("Foo   Bar\tBaz") == "foo bar baz"


def test_source_domain_strips_www():
    assert source_domain("https://www.reuters.com/article/x") == "reuters.com"
    assert source_domain("https://apnews.com/article/y") == "apnews.com"
    assert source_domain("not a url") == ""


def test_fingerprint_same_story_different_formatting_collapses():
    fp1 = fingerprint("UCTT", "Applied Materials Orders Surge!!", "2026-07-21T10:00:00")
    fp2 = fingerprint("UCTT", "applied materials orders surge", "2026-07-21")
    assert fp1 == fp2


def test_fingerprint_differs_by_symbol_or_day():
    fp1 = fingerprint("UCTT", "Same headline", "2026-07-21")
    fp2 = fingerprint("ICHR", "Same headline", "2026-07-21")
    fp3 = fingerprint("UCTT", "Same headline", "2026-07-22")
    assert fp1 != fp2
    assert fp1 != fp3


def test_dedup_index_persists_across_instances(tmp_path):
    path = tmp_path / "dedup_index.json"
    index = DedupIndex(path)
    fp = fingerprint("UCTT", "Some story", "2026-07-21")
    assert not index.is_duplicate(fp)
    index.register(fp, "reuters.com")
    assert index.is_duplicate(fp)

    reloaded = DedupIndex(path)
    assert reloaded.is_duplicate(fp)


def test_dedup_index_register_is_idempotent(tmp_path):
    index = DedupIndex(tmp_path / "dedup_index.json")
    fp = fingerprint("UCTT", "Some story", "2026-07-21")
    index.register(fp, "reuters.com")
    index.register(fp, "bloomberg.com")  # should not overwrite the first-seen domain
    assert index.domain_for(fp) == "reuters.com"


def test_dedup_index_prunes_old_entries_on_load(tmp_path):
    path = tmp_path / "dedup_index.json"
    index = DedupIndex(path, max_age_days=90)
    old_fp = fingerprint("UCTT", "Ancient story", "2026-01-01")
    new_fp = fingerprint("UCTT", "Fresh story", "2026-07-21")
    index.register(old_fp, "reuters.com", registered_at="2026-01-01T00:00:00+00:00")
    index.register(new_fp, "reuters.com")

    reloaded = DedupIndex(path, max_age_days=90)
    assert not reloaded.is_duplicate(old_fp)
    assert reloaded.is_duplicate(new_fp)


def test_dedup_index_reads_legacy_flat_format(tmp_path):
    import json

    path = tmp_path / "dedup_index.json"
    fp = fingerprint("UCTT", "Legacy story", "2026-07-20")
    path.write_text(json.dumps({fp: "reuters.com"}))  # pre-timestamp format

    index = DedupIndex(path)
    assert index.is_duplicate(fp)
    assert index.domain_for(fp) == "reuters.com"


# --- Near-duplicate (reworded syndication) detection ---

from smartboi.dedup import headline_tokens, near_duplicate


def test_headline_tokens_strip_stopwords_and_suffixes():
    assert headline_tokens("Acme Corp wins the Navy contract") == {"acme", "wins", "navy", "contract"}


def test_near_duplicate_catches_light_rewording():
    assert near_duplicate(
        "Acme Corp wins $50M Navy contract",
        "Acme wins $50M Navy contract",
    )


def test_near_duplicate_keeps_opposite_stories_distinct():
    # 3 of 5 identity tokens shared (0.6) -- must stay below the threshold,
    # these are opposite outcomes, not one reworded story.
    assert not near_duplicate(
        "Acme wins big Navy contract",
        "Acme loses big Navy contract",
    )


def test_near_duplicate_empty_headlines_never_match():
    assert not near_duplicate("", "Acme wins Navy contract")
    assert not near_duplicate("the a an", "the a an")


def test_find_near_duplicate_same_day(tmp_path):
    index = DedupIndex(tmp_path / "dedup_index.json")
    fp = fingerprint("UCTT", "Acme Corp wins $50M Navy contract", "2026-07-21")
    index.register(fp, "reuters.com")
    match = index.find_near_duplicate("UCTT", "Acme wins $50M Navy contract", "2026-07-21")
    assert match == fp


def test_find_near_duplicate_previous_day(tmp_path):
    # Wire copy republished after UTC midnight used to get a fresh
    # fingerprint purely from the date rollover.
    index = DedupIndex(tmp_path / "dedup_index.json")
    fp = fingerprint("UCTT", "Acme Corp wins $50M Navy contract", "2026-07-21")
    index.register(fp, "reuters.com")
    match = index.find_near_duplicate("UCTT", "Acme wins $50M Navy contract", "2026-07-22")
    assert match == fp


def test_find_near_duplicate_respects_symbol_and_distance(tmp_path):
    index = DedupIndex(tmp_path / "dedup_index.json")
    fp = fingerprint("UCTT", "Acme Corp wins $50M Navy contract", "2026-07-21")
    index.register(fp, "reuters.com")
    # Different symbol: no match.
    assert index.find_near_duplicate("ICHR", "Acme wins $50M Navy contract", "2026-07-21") is None
    # Two days later: outside the same/previous-day window.
    assert index.find_near_duplicate("UCTT", "Acme wins $50M Navy contract", "2026-07-23") is None
    # A genuinely different story: no match.
    assert index.find_near_duplicate("UCTT", "Acme CFO resigns unexpectedly", "2026-07-21") is None
