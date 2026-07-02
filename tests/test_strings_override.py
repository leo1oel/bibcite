import bibcite.venues as venues


def test_strings_override_via_env(tmp_path, monkeypatch):
    custom = tmp_path / "strings.bib"
    custom.write_text(
        "%%%% Conferences %%%%\n"
        '@string{MYCONF = "My Very Own Conference (MYCONF)"}\n'
    )
    monkeypatch.setenv("BIBCITE_STRINGS", str(custom))
    monkeypatch.setattr(venues, "_table", None)  # reset cache
    v = venues.canonicalize("MYCONF")
    assert v is not None and v.name == "My Very Own Conference (MYCONF)"
    assert venues.canonicalize("CVPR") is None  # vendored table not loaded
    monkeypatch.setattr(venues, "_table", None)  # don't leak into other tests


def test_vendored_default(monkeypatch):
    monkeypatch.delenv("BIBCITE_STRINGS", raising=False)
    monkeypatch.setattr(venues, "_table", None)
    assert venues.canonicalize("CVPR", 2020) is not None
