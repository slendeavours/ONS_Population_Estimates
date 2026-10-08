"""Shared DWP Stat-Xplore API client (used by S8b; reusable by S19).

The API key comes from the environment (StatXplore_API_Key or
STATXPLORE_API_KEY, loaded from .env). It is never printed or logged, and is
only read when a call is made, not at import time.
"""

import os
import sys
import json
import time
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

# Repository root first, then its parent, where the shared .env sits.
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE.parent / ".env")
load_dotenv(_HERE.parent.parent / ".env")

API_ROOT = "https://stat-xplore.dwp.gov.uk/webapi/rest/v1"

_last_api_call = 0.0


def get_api_key():
    key = (
        os.environ.get("StatXplore_API_Key", "")
        or os.environ.get("STATXPLORE_API_KEY", "")
    ).strip()
    if not key:
        sys.exit("HARD STOP: StatXplore_API_Key missing from environment.")
    return key


def headers():
    return {"APIKey": get_api_key(), "Content-Type": "application/json"}


def throttle():
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_api_call = time.time()


def api_get(path, retries=3):
    url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
    throttle()
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers(), timeout=120)
            if r.status_code == 503 and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  503 maintenance, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return json.loads(r.text), r.headers
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  Error: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def api_get_all_pages(path):
    all_children = []
    url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
    while url:
        data, hdrs = api_get(url)
        all_children.extend(data.get("children", []))
        link = hdrs.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return all_children


def api_post(path, body, retries=5):
    url = f"{API_ROOT}/{path.lstrip('/')}"
    throttle()
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=headers(), json=body, timeout=120)
            if r.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  {r.status_code} on POST, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, requests.exceptions.ReadTimeout) as e:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  Error on POST: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
