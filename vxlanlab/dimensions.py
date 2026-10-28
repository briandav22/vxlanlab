"""Kentik custom dimensions for the lab's subnets, through the v5 API.

Four dimensions (segment, tenant, role, zone), each as a source/destination
pair. Values come from subnets.csv, grouped so each value is one populator
whose addr lists every subnet with that value: c_src_tenant gets one
UNDERLAY populator matching src addr in all the underlay subnets, and
c_dst_tenant the same with direction dst.

Dimensions are looked up by name and reused; only missing ones are created.
Populators go up in one call per dimension through the batch API with
replace_all, so each dimension ends up holding exactly what the CSV says
(anything else on it is removed) and running it twice is safe.

Calls are spaced out (delay seconds apart) and an HTTP 429 is retried with
backoff, honoring Retry-After when sent.

Standard library only. Credentials come from the caller, never from here.
"""
from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from .kentik import KentikError

API_URL = "https://api.kentik.com/api/v5"   # EU: https://api.kentik.eu/api/v5
SUBNETS_CSV = os.path.join(os.path.dirname(__file__), "subnets.csv")

# Dimension suffix -> subnets.csv column.
DIMENSIONS = [("segment", "segment"), ("tenant", "tenant_vrf"), ("role", "role"), ("zone", "zone")]
DIRECTIONS = [("src", "Src"), ("dst", "Dst")]


def load_subnets(path: str = SUBNETS_CSV) -> List[Dict[str, str]]:
    with open(path, newline="") as f:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    return [r for r in rows if r.get("subnet")]


def plan(rows: List[Dict[str, str]]) -> List[Dict]:
    """The dimensions to have, each with its populators: one per value, addr listing its subnets."""
    out = []
    for suffix, column in DIMENSIONS:
        for direction, label in DIRECTIONS:
            by_value = {}   # value -> subnets, in CSV order
            for r in rows:
                if r.get(column):
                    by_value.setdefault(r[column], []).append(r["subnet"])
            out.append({"name": "c_%s_%s" % (direction, suffix),
                        "display_name": "%s %s" % (label, suffix.capitalize()),
                        "type": "string",
                        "upserts": [{"value": v, "criteria": [{"direction": direction, "addr": addrs}]}
                                    for v, addrs in by_value.items()]})
    return out


def retry_after(value) -> Optional[float]:
    """Seconds from a Retry-After header given in seconds; None if absent or a date."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class Client:
    def __init__(self, email: str, token: str, base_url: str = API_URL, timeout: float = 30,
                 delay: float = 1.0, retries: int = 6, sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.delay = delay        # seconds between calls
        self.retries = retries    # 429 retries per call
        self.sleep = sleep
        self._last = None
        self.headers = {
            "X-CH-Auth-Email": email,
            "X-CH-Auth-API-Token": token,
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, body: Optional[Dict] = None) -> Dict:
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.retries + 1):
            if self._last is not None:
                wait = self.delay - (time.monotonic() - self._last)
                if wait > 0:
                    self.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:2000]
                if e.code == 429 and attempt < self.retries:
                    backoff = retry_after(e.headers.get("Retry-After"))
                    if backoff is None:
                        backoff = min(60, 2 ** (attempt + 1))
                    print("  rate limited, waiting %gs (retry %d of %d)" % (backoff, attempt + 1, self.retries))
                    self.sleep(backoff)
                    continue
                raise KentikError("%s %s -> HTTP %d: %s" % (method, path, e.code, detail)) from None
            except urllib.error.URLError as e:
                raise KentikError("%s %s -> %s" % (method, path, e.reason)) from None
        raise AssertionError("unreachable")

    def dimensions(self) -> Dict[str, Dict]:
        """Dimension name -> dimension, populators included."""
        resp = self._call("GET", "/customdimensions")
        dims = resp.get("customDimensions", []) if isinstance(resp, dict) else resp
        return {d["name"]: d for d in dims if d.get("name")}

    def create_dimension(self, dim: Dict) -> Dict:
        body = {k: dim[k] for k in ("name", "type", "display_name")}
        resp = self._call("POST", "/customdimension", body)
        return resp.get("customDimension", resp)

    def replace_populators(self, name: str, upserts: List[Dict]) -> Dict:
        """Make these the dimension's only populators, in one batch call."""
        return self._call("POST", "/batch/customdimensions/%s/populators" % name,
                          {"replace_all": True, "complete": True, "upserts": upserts})
