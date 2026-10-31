import pytest


@pytest.fixture(autouse=True)
def _console_in_tests(tmp_path, monkeypatch):
    """Les CLI écrivent leur journal dans tmp (jamais dans data/logs du dépôt) et sans couleur."""
    monkeypatch.setenv("CHAMPIS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("NO_COLOR", "1")
