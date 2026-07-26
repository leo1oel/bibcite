"""Citing a page that no index knows about.

Blogs, documentation and standards pages are cited constantly and appear in
none of the academic sources, so the page itself has to be the source.
"""

from bibcite.resolve import _web_entry, classify
from bibcite.sources import WebPage, _meta_content, _site_author, _title_tag, _year_in_path


def test_a_plain_url_is_a_webpage_and_the_others_still_are_not():
    assert classify("https://karpathy.github.io/2015/05/21/rnn-effectiveness/")[0] == "webpage"
    assert classify("http://example.edu/notes")[0] == "webpage"
    # The identifiers that resolve properly must keep their own paths.
    assert classify("https://arxiv.org/abs/1706.03762") == ("arxiv", "1706.03762")
    assert classify("https://doi.org/10.1038/s41586-021-03819-2")[0] == "doi"
    assert classify("10.1109/CVPR.2016.90")[0] == "doi"
    assert classify("Attention Is All You Need")[0] == "title"


def test_entry_is_misc_so_conference_styles_can_print_it():
    # @online is biblatex-only: a NeurIPS or IEEE .bst drops the entry.
    entry = _web_entry(WebPage(url="https://example.org/post", title="A Post", year="2024"))
    assert entry["ENTRYTYPE"] == "misc"
    assert entry["howpublished"] == r"\url{https://example.org/post}"
    assert entry["url"] == "https://example.org/post"
    assert entry["year"] == "2024"


def test_a_page_with_no_byline_is_attributed_to_its_site():
    entry = _web_entry(WebPage(url="https://example.org/post", title="A Post", site="Example"))
    # Braced, so the .bst treats it as a corporate name and does not invert it.
    assert entry["author"] == "{Example}"


def test_a_byline_wins_over_the_site():
    entry = _web_entry(
        WebPage(url="https://example.org/post", title="A Post", authors=["Ada Lovelace"], site="Example")
    )
    assert entry["author"] == "Ada Lovelace"


def test_the_site_author_drops_hosting_suffixes():
    # `karpathygithubio…` makes an unreadable citation key.
    assert _site_author("karpathy.github.io") == "Karpathy"
    assert _site_author("www.distill.pub") == "Distill"
    assert _site_author("docs.python.org") == "Python"


def test_a_dateless_page_takes_the_year_from_its_own_url():
    assert _year_in_path("https://karpathy.github.io/2015/05/21/rnn-effectiveness/") == "2015"
    assert _year_in_path("https://example.org/blog/2016/misread-tsne/") == "2016"
    # Not from a query string, where any number at all can appear.
    assert _year_in_path("https://example.org/page?id=2019") == ""
    assert _year_in_path("https://example.org/about") == ""


def test_the_title_loses_the_site_name_templates_append():
    assert _title_tag("<title>How to Use t-SNE | Distill</title>") == "How to Use t-SNE"
    assert _title_tag("<title>Plain Title</title>") == "Plain Title"
    # A title that is only a site name must survive rather than become empty.
    assert _title_tag("<title>Distill</title>") == "Distill"


def test_meta_is_read_in_either_attribute_order():
    assert _meta_content('<meta property="og:title" content="Hello">', "og:title") == "Hello"
    assert _meta_content('<meta content="Hello" name="og:title">', "og:title") == "Hello"
    assert _meta_content('<meta property="og:title" content="A &amp; B">', "og:title") == "A & B"
    assert _meta_content("<html></html>", "og:title") == ""
