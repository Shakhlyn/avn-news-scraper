import pytest

from avn_scraper.export import _sheet_name
from avn_scraper.lock import RunInProgress, run_lock


def test_sheet_names_valid_and_unique():
    used = {"all"}
    names = [_sheet_name(label, used) for label in
             ["Digital Security Act / Cyber laws", "A" * 40, "A" * 40, "All", "x[1]:*?"]]
    assert all(len(n) <= 31 and not set(n) & set("[]:*?/\\") for n in names)
    assert len({n.lower() for n in names}) == len(names)
    assert names[0] == "Digital Security Act - Cyber la"


def test_second_run_is_refused_while_first_holds_lock(tmp_path):
    lock = tmp_path / ".run.lock"
    with run_lock(lock):
        with pytest.raises(RunInProgress):
            with run_lock(lock):
                pass
    with run_lock(lock):  # released after the first run ends
        pass
