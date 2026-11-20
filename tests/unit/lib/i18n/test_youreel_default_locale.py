"""YouReel: the default locale is English unless ARCREEL_DEFAULT_LOCALE says otherwise.
YouReel：除非 ARCREEL_DEFAULT_LOCALE 另行指定，默认语言为英文。"""

from lib.i18n import default_locale_from


def test_production_default_is_english() -> None:
    assert default_locale_from({}) == "en"


def test_override_and_unknown_values() -> None:
    assert default_locale_from({"ARCREEL_DEFAULT_LOCALE": "zh"}) == "zh"
    assert default_locale_from({"ARCREEL_DEFAULT_LOCALE": "de"}) == "en"
