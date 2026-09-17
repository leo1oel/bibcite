import pytest

from bibcite.venues import canonicalize


@pytest.mark.parametrize(
    "raw,year,macro",
    [
        # DBLP short venue names (acronyms)
        ("CVPR", 2023, "CVPR"),
        ("ICCV", 2021, "ICCV"),
        ("ICML", 2022, "ICML"),
        ("ICLR", 2024, "ICLR"),
        ("NeurIPS", 2022, "NeurIPS"),
        # NIPS/NeurIPS year rule
        ("NIPS", 2017, "NIPS"),
        ("Advances in Neural Information Processing Systems", 2017, "NIPS"),
        ("Neural Information Processing Systems", 2022, "NeurIPS"),
        ("NeurIPS", 2016, "NIPS"),
        # WACV year rule
        ("WACV", 2015, "WACV_until_2016"),
        ("WACV", 2020, "WACV"),
        # Semantic Scholar style full names
        ("International Conference on Learning Representations", None, "ICLR"),
        ("International Conference on Machine Learning", None, "ICML"),
        ("Computer Vision and Pattern Recognition", 2020, "CVPR"),
        ("IEEE/CVF Conference on Computer Vision and Pattern Recognition", 2020, "CVPR"),
        (
            "2019 IEEE/CVF International Conference on Computer Vision (ICCV)",
            2019,
            "ICCV",
        ),
        ("Annual Meeting of the Association for Computational Linguistics", None, "ACL"),
        ("EACL (Volume 1: Long Papers)", 2026, "EACL"),
        (
            "Proceedings of the 19th Conference of the European Chapter of the Association for Computational Linguistics (Volume 1: Long Papers)",
            2026,
            "EACL",
        ),
        (
            "Conference on Empirical Methods in Natural Language Processing",
            None,
            "EMNLP",
        ),
        ("Conference on Robot Learning", None, "CoRL"),
        ("European Conference on Computer Vision", None, "ECCV"),
        # Journals, including DBLP abbreviations
        ("IEEE Trans. Pattern Anal. Mach. Intell.", None, "TPAMI"),
        ("IEEE Transactions on Pattern Analysis and Machine Intelligence", None, "TPAMI"),
        ("J. Mach. Learn. Res.", None, "JMLR"),
        ("Journal of Machine Learning Research", None, "JMLR"),
        ("Int. J. Comput. Vis.", None, "IJCV"),
        ("Transactions on Machine Learning Research", None, "TMLR"),
        ("IEEE Robotics Autom. Lett.", None, "RAL"),
        ("Robotics: Science and Systems", None, "RSS"),
        # Workshops must not collapse onto the main conference
        ("ICCV Workshops", 2019, "ICCVW"),
        ("CVPR Workshops", 2022, "CVPRW"),
        (
            "IEEE/CVF Conference on Computer Vision and Pattern Recognition Workshops",
            2022,
            "CVPRW",
        ),
    ],
)
def test_canonicalize(raw, year, macro):
    v = canonicalize(raw, year)
    assert v is not None, f"no match for {raw!r}"
    assert v.macro == macro


def test_no_match_returns_none():
    assert canonicalize("Some Obscure Regional Symposium Nobody Knows") is None
    assert canonicalize("") is None


@pytest.mark.parametrize(
    "raw,macro",
    [
        # DBLP's abbreviation for TOG used to resolve through a bare "ACM"
        # acronym to whichever ACM-prefixed entry came first in strings.bib,
        # printing 3D Gaussian Splatting and MaskedMimic as TOCHI articles.
        ("ACM Trans. Graph.", "TOG"),
        ("ACM Transactions on Graphics", "TOG"),
        ("ACM Trans. Comput. Hum. Interact.", "TOCHI"),
        ("ACM Trans. Hum. Robot Interact.", "THRI"),
        # ... and the same trap for "IEEE", which pointed at Proceedings of
        # the IEEE. That venue still has to resolve from its full spelling.
        ("IEEE Trans. Robotics", "TOR"),
        ("IEEE Trans. Inf. Theory", "TIT"),
        ("IEEE Trans. Image Process.", "TIP"),
        ("Proceedings of the IEEE", "PIEEE"),
        ("Commun. ACM", "CACM"),
        ("Int. J. Robotics Res.", "IJRR"),
    ],
)
def test_abbreviated_journal_names_expand_to_their_own_venue(raw, macro):
    v = canonicalize(raw)
    assert v is not None, f"no match for {raw!r}"
    assert v.macro == macro


def test_publisher_prefixes_are_not_venue_acronyms():
    from bibcite.venues import get_table

    table = get_table()
    for org in ("acm", "ieee", "cvf", "rsj"):
        assert table._acr.get(org) is None, f"{org!r} is registered as an acronym"
    # An unknown ACM/IEEE venue must stay unmapped rather than borrow the
    # identity of an unrelated one; the raw string is then kept as-is.
    assert canonicalize("ACM Symposium on Nothing In Particular") is None
    assert canonicalize("IEEE Journal of Nothing In Particular") is None


def test_categories_drive_entry_type():
    assert canonicalize("CVPR", 2020).entry_type == "inproceedings"
    assert canonicalize("CVPR", 2020).bib_field == "booktitle"
    assert canonicalize("IEEE Trans. Pattern Anal. Mach. Intell.").entry_type == "article"
    assert canonicalize("Transactions on Machine Learning Research").entry_type == "article"
    assert canonicalize("Robotics: Science and Systems").entry_type == "inproceedings"
