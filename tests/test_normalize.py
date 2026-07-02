from bibcite.normalize import (
    clean_title,
    first_author_last_name,
    first_significant_word,
    make_key,
    mini_hash,
    norm_title,
)


def test_mini_hash():
    assert mini_hash("Attention Is All You Need!") == "attentionisallyouneed"
    assert mini_hash("A-B c", "_") == "a_b_c"


def test_norm_title_matches_across_formatting():
    a = "Attention is All\n you need."
    b = "Attention Is All You Need"
    assert norm_title(a) == norm_title(b)


def test_clean_title_strips_dblp_period():
    assert clean_title("Attention is All you Need.") == "Attention is All you Need"


def test_first_significant_word_skips_stopwords():
    assert first_significant_word("The Attention Mechanism") == "attention"
    assert first_significant_word("A Study of Things") == "study"


def test_first_author_last_name_both_forms():
    assert first_author_last_name("Ashish Vaswani and Noam Shazeer") == "vaswani"
    assert first_author_last_name("Vaswani, Ashish and Shazeer, Noam") == "vaswani"


def test_make_key():
    key = make_key("Ashish Vaswani and Noam Shazeer", 2017, "Attention Is All You Need")
    assert key == "vaswani2017attention"
