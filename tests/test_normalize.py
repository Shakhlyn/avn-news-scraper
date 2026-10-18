import unicodedata

from avn_scraper.normalize import detect_language, normalize


def test_bangla_digits_become_ascii():
    assert normalize("৫৭ ধারা") == "57 ধারা"


def test_zero_width_joiners_removed():
    assert normalize("র‍্যাব") == normalize("র্যাব")


def test_nukta_forms_unified():
    precomposed = "য়"            # য়
    decomposed = "য়"       # য + nukta
    assert normalize(precomposed) == normalize(decomposed)
    assert normalize("আয়নাঘর") == normalize(unicodedata.normalize("NFD", "আয়নাঘর"))


def test_case_and_whitespace():
    assert normalize("  Border\n  KILLING ") == "border killing"


def test_detect_language():
    assert detect_language("সীমান্তে বিএসএফের গুলিতে যুবক নিহত") == "bn"
    assert detect_language("Youth killed in BSF firing at border") == "en"
