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
    assert index._seen[fp] == "reuters.com"
