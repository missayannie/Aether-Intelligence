"""Shared HTTP for every Square Enix Lodestone request (na.finalfantasyxiv.com).

The Lodestone sits behind an AWS WAF bot-challenge. curl_cffi's browser
impersonation gets through most of the time, but the challenge arrives in short
bursts — when it does, the response is a JS challenge page (HTTP 200!) instead of
the content, so a naive caller silently parses garbage and reports "not found".

The character importer already retried through this; every Lodestone caller needs
the same treatment, so the logic lives here and they all share it:

  - detect the challenge by its markers (it is NOT signalled by a status code)
  - retry with a short backoff, since the burst usually passes in a second or two
  - raise LodestoneBlocked when it doesn't, so callers can fall back to a cache or
    tell the player what happened instead of pretending the page was empty
"""
from __future__ import annotations

import time

# Markers that identify the AWS WAF challenge page (both appear in its inline JS).
_WAF_MARKERS = ("awsWafCookieDomainList", "gokuProps")


class LodestoneBlocked(Exception):
    """The Lodestone's AWS WAF bot-challenge blocked the request (it needs a real
    browser to solve). Surfaced so the UI can explain instead of showing 'not found'."""


def is_waf_challenge(html: str) -> bool:
    return any(m in html for m in _WAF_MARKERS)


def waf_get(session, url: str, params: dict | None = None,
            timeout: float = 20.0, retries: int = 3) -> str:
    """GET a Lodestone page, retrying through the intermittent WAF challenge.

    `session` is a curl_cffi Session (browser-impersonating — a plain HTTP client
    gets challenged every time). Returns the page HTML, or raises LodestoneBlocked
    if the challenge is still in the way after `retries` attempts.
    """
    for attempt in range(retries):
        html = session.get(url, params=params, timeout=timeout).text
        if not is_waf_challenge(html):
            return html
        if attempt < retries - 1:
            time.sleep(0.8 * (attempt + 1))  # the burst usually clears quickly
    raise LodestoneBlocked()
