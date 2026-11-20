"""Shared HTTP session with retries and back-off."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "gb-power-dash/0.1 (+https://github.com/)"


def make_session(total_retries: int = 5, backoff: float = 1.5) -> requests.Session:
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff,            # 0, 3, 6, 12, 24 s ...
        status_forcelist=(429, 500, 502, 503, 504, 520, 522, 524, 599),  # 52x: CDN timeouts; 599: ENTSO-E gateway timeout
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,             # let the caller see the final response body
    )
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
