import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def isolated_settings(settings):
    """Fast in-memory cache and no real network geocoder during tests."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.NOMINATIM_URL = ""
    settings.STOP_PENALTY_USD = 5.0
    cache.clear()
    yield
    cache.clear()
