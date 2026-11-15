import pytest

from tgdump import fetch


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A place of its own for the databases and the output folders of a test's runs."""
    monkeypatch.setattr(fetch, "DATA", tmp_path)
    monkeypatch.setenv("TG_OUT", str(tmp_path / "out"))
    return tmp_path
