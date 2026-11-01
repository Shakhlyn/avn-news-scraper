import pytest
from typer.testing import CliRunner

from avn_scraper.cli import app

runner = CliRunner()


@pytest.fixture
def env(database, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")


def test_migrate_has_nothing_to_do_on_a_current_database(env):
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0 and "Nothing to apply" in result.output


def test_stats_on_an_empty_database(env):
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0 and "No articles saved yet" in result.output


def test_export_and_reclassify_on_an_empty_database(env):
    assert runner.invoke(app, ["export"]).exit_code == 0
    result = runner.invoke(app, ["reclassify"])
    assert result.exit_code == 0 and "Exported to" in result.output


def test_a_missing_database_is_a_plain_message(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")

    async def no_wait(_):
        pass

    monkeypatch.setattr("avn_scraper.scraper.storage.postgres.asyncio.sleep", no_wait)
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 1 and "Cannot reach Postgres" in result.output


@pytest.mark.parametrize("value", ['["*"]', "avnbd.org"])
def test_serve_names_a_bad_setting(monkeypatch, value):
    monkeypatch.setenv("ALLOWED_HOSTS", value)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1 and "allowed_hosts" in result.output.lower()
