import os
import uuid

import psycopg
import pytest
from dotenv import load_dotenv

from avn_scraper.classify import Classifier
from avn_scraper.config import PROJECT_ROOT, load_config
from avn_scraper.storage import connect, migrate


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def classifier(cfg):
    return Classifier(cfg.keywords)


@pytest.fixture
def database(monkeypatch):
    """A throwaway schema with the real migrations, dropped afterwards. DATABASE_URL
    points at it for the length of the test, so nothing reaches the real tables."""
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("set TEST_DATABASE_URL (see .env.example)")
    schema = f"test_{uuid.uuid4().hex}"
    admin = psycopg.connect(url, autocommit=True)
    admin.execute(f"CREATE SCHEMA {schema}")
    test_url = f"{url}{'&' if '?' in url else '?'}options=-csearch_path%3D{schema}"
    monkeypatch.setenv("DATABASE_URL", test_url)
    conn = connect(test_url)
    migrate(conn)
    conn.close()
    yield test_url
    admin.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()
