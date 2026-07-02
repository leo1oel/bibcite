from bibcite.resolve import _arxiv_only_entry, guess_entry_type
from bibcite.sources import ArxivMeta


def test_guess_entry_type_conferences():
    assert guess_entry_type("Some Obscure Conference on Widgets") == "inproceedings"
    assert guess_entry_type("International Workshop on Things") == "inproceedings"
    assert guess_entry_type("Annual Symposium on Stuff") == "inproceedings"


def test_guess_entry_type_journals():
    assert guess_entry_type("Journal of Widget Science") == "article"
    # "Proceedings" alone must not imply a conference.
    assert guess_entry_type("Proceedings of the National Academy of Sciences") == "article"
    assert guess_entry_type("Proceedings of the IEEE") == "article"


def test_arxiv_only_entry_is_misc():
    meta = ArxivMeta(
        arxiv_id="2303.08774",
        title="GPT-4 Technical Report",
        authors=["OpenAI"],
        year="2023",
        abs_url="https://arxiv.org/abs/2303.08774",
    )
    entry = _arxiv_only_entry(meta)
    assert entry["ENTRYTYPE"] == "misc"
    assert "journal" not in entry
    assert entry["howpublished"] == "arXiv preprint arXiv:2303.08774"
