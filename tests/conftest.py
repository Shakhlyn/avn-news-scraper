import pytest

from avn_scraper.classify import Classifier
from avn_scraper.config import load_config


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def classifier(cfg):
    return Classifier(cfg.keywords)
