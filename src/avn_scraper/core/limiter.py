"""Rate limits per client address, kept in memory."""

from slowapi import Limiter
from slowapi.util import get_remote_address

DEFAULT_LIMIT = "120/minute"
WRITE_LIMIT = "30/minute"
SCRAPE_LIMIT = "5/minute"

limiter = Limiter(key_func=get_remote_address, default_limits=[DEFAULT_LIMIT])
