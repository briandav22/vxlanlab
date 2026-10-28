"""Kentik custom dimensions for the lab's subnets, through the v5 API.

Four dimensions (segment, tenant, role, zone), each as a source/destination
pair: c_src_tenant gets one direction=src populator per subnet and
c_dst_tenant the same with direction=dst. Values come from subnets.csv.

Dimensions are looked up by name and reused; only missing ones are created.
For each dimension, populators are keyed by (addr, direction): missing ones
are added and ones with a different value are updated, so running it twice
is safe. Populators for subnets no longer in the CSV are left alone unless
prune is asked for.

The v5 API rate-limits hard, so calls are spaced out (delay seconds apart)
and an HTTP 429 is retried with backoff, honoring Retry-After when sent.

Standard library only. Credentials come from the caller, never from here.
"""
from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

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
    """The dimensions to have, each with the populators it should hold."""
    out = []
    for suffix, column in DIMENSIONS:
        for direction, label in DIRECTIONS:
            pops = [{"value": r[column], "direction": direction, "addr": r["subnet"]}
                    for r in rows if r.get(column)]
            out.append({"name": "c_%s_%s" % (direction, suffix),
                        "display_name": "%s %s" % (label, suffix.capitalize()),
                        "type": "string", "populators": pops})
    return out


def diff(want: List[Dict], have: List[Dict]) -> Tuple[List[Dict], List[Tuple[Dict, Dict]], List[Dict]]:
    """Split wanted populators into (add, update, stale) against what a dimension has.

    update pairs are (existing, wanted). stale are existing populators whose
    (addr, direction) isn't wanted.
    """
    def key(p):
        return (str(p.get("addr") or "").strip(), p.get("direction") or "either")

    existing = {key(p): p for p in have}
    wanted = {key(p) for p in want}
    add, update = [], []
    for p in want:
        old = existing.get(key(p))
        if old is None:
            add.append(p)
        elif str(old.get("value")) != p["value"]:
            update.append((old, p))
    stale = [p for k, p in existing.items() if k not in wanted]
    return add, update, stale


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

    def create_populator(self, dim_id, pop: Dict) -> Dict:
        resp = self._call("POST", "/customdimension/%s/populator" % dim_id, {"populator": pop})
        return resp.get("populator", resp)

    def update_populator(self, dim_id, pop_id, pop: Dict) -> Dict:
        resp = self._call("PUT", "/customdimension/%s/populator/%s" % (dim_id, pop_id), {"populator": pop})
        return resp.get("populator", resp)

    def delete_populator(self, dim_id, pop_id) -> Dict:
        return self._call("DELETE", "/customdimension/%s/populator/%s" % (dim_id, pop_id))
