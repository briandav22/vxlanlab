"""Onboard the lab's switches into Kentik through the Device API (v202504beta2).

One router device per fabric switch: sending IP and NMS polling IP are both
its mgmt IP, SNMP goes through a credential from the Kentik credential vault.
Devices Kentik doesn't have yet go to batch_create; devices it already has
(matched by name) go to batch_update with their id, so running it twice is safe.

With a site, the switches also go into its architecture, one layer per tier
(Site API v202509): Border/Spine/Leaf for the 3-tier lab, Edge/Superspine/
Spine/Leaf/Top of Rack for the 5-stage one. The topology's site_layers says
which roles go where. That's how Kentik knows each device's role; the Device
API has no writable role field.

Standard library only. Credentials come from the caller, never from here.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

from .fabric import Fabric

API_URL = "https://grpc.api.kentik.com"   # EU: https://grpc.api.kentik.eu
DEVICE_PATH = "/device/v202504beta2/device"
SITE_PATH = "/site/v202509/sites"

# Site layers, top to bottom, and the topology roles that go in each. A topology
# can set its own with "site_layers"; this is the 3-tier lab's.
LAYERS = [("Border", ("border",)), ("Spine", ("spine",)), ("Leaf", ("leaf",))]


def layers_for(fabric: Fabric) -> List[Tuple[str, Tuple[str, ...]]]:
    custom = fabric.cfg.get("site_layers")
    if not custom:
        return LAYERS
    return [(name, tuple(roles)) for name, roles in custom]


class KentikError(Exception):
    pass


def kentik_name(name: str, underscores: bool = False) -> str:
    """The Kentik device name. Same as sysName unless underscores is set."""
    return name.replace("-", "_") if underscores else name


def build_devices(fabric: Fabric, plan_id: Optional[int], site_id: Optional[int] = None,
                  agent_id: Optional[str] = None, credential: Optional[str] = None,
                  template_id: Optional[int] = None, sample_rate: Optional[int] = None,
                  snmp_port: int = 161, underscores: bool = False,
                  only: Sequence[str] = ()) -> List[Dict]:
    """DeviceConcise objects for the lab's switches, without ids."""
    rate = sample_rate or fabric.cfg.get("sample_rate", 1024)
    out = []
    for d in fabric.devices.values():
        if only and d.name not in only:
            continue
        dev = {
            "deviceName": kentik_name(d.name, underscores),
            "deviceSubtype": "router",
            "deviceDescription": "vxlanlab %s%s" % (d.role, " (VTEP %s)" % d.vtep if d.vtep else ""),
            "sendingIps": [d.mgmt_ip],
            "deviceSampleRate": rate,
            "deviceBgpType": "none",
            "deviceBgpFlowspec": False,
            "minimizeSnmp": False,   # required for routers on create
        }
        if plan_id is not None:
            dev["planId"] = plan_id
        if site_id is not None:
            dev["siteId"] = site_id
        if agent_id:
            nms = {"agentId": str(agent_id), "ipAddress": d.mgmt_ip}
            if credential:
                nms["snmp"] = {"credentialName": credential, "port": snmp_port}
            dev["nms"] = nms
        if template_id is not None:
            dev["monitoringTemplateId"] = template_id
        out.append(dev)
    return out


def new_site(title: str, layers=LAYERS) -> Dict:
    """A data-center site with one empty layer per tier (Border, Spine and Leaf by default)."""
    return {"title": title, "type": "SITE_TYPE_DATA_CENTER",
            "architecture": [{"layers": [{"name": name, "deviceIds": []}]} for name, _ in layers]}


def layer_assignments(fabric: Fabric, ids: Dict[str, str], underscores: bool = False) -> Dict[str, List[str]]:
    """Layer name -> Kentik device ids, for the switches that have an id."""
    out = {}
    for name, roles in layers_for(fabric):
        out[name] = [ids[kentik_name(d.name, underscores)] for d in fabric.devices.values()
                     if d.role in roles and kentik_name(d.name, underscores) in ids]
    return out


def with_layers(site: Dict, assign: Dict[str, List[str]]) -> Dict:
    """A copy of site with each device in its layer and no other.

    Layers and devices that aren't the lab's are left alone. Missing layers are
    added as new rows, in assign's order (top to bottom).
    """
    ours = {i for ids in assign.values() for i in ids}
    arch = json.loads(json.dumps(site.get("architecture") or []))
    seen = set()
    for row in arch:
        for layer in row.get("layers", []):
            name = layer.get("name")
            keep = [i for i in layer.get("deviceIds", []) if i not in ours]
            if name in assign and name not in seen:
                keep += assign[name]
                seen.add(name)
            layer["deviceIds"] = keep
    for name in assign:
        if name not in seen:
            arch.append({"layers": [{"name": name, "deviceIds": list(assign[name])}]})
    return dict(site, architecture=arch)


def split_existing(devices: List[Dict], existing: Dict[str, str]) -> Tuple[List[Dict], List[Dict]]:
    """(to_create, to_update). existing maps Kentik device name -> id."""
    create, update = [], []
    for dev in devices:
        dev_id = existing.get(dev["deviceName"])
        if dev_id:
            update.append(dict(dev, id=dev_id))
        else:
            create.append(dev)
    return create, update


class Client:
    def __init__(self, email: str, token: str, base_url: str = API_URL, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "X-CH-Auth-Email": email,
            "X-CH-Auth-API-Token": token,
            "Content-Type": "application/json",
        }

    def _call(self, method: str, path: str, body: Optional[Dict] = None) -> Dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:2000]
            raise KentikError("%s %s -> HTTP %d: %s" % (method, path, e.code, detail)) from None
        except urllib.error.URLError as e:
            raise KentikError("%s %s -> %s" % (method, path, e.reason)) from None
        return json.loads(raw) if raw else {}

    def existing_ids(self) -> Dict[str, str]:
        """Device name -> id for every device in the account."""
        resp = self._call("GET", DEVICE_PATH + "?" + urllib.parse.urlencode({"view": "DEVICE_VIEW_BASIC"}))
        return {d["deviceName"]: d["id"] for d in resp.get("devices", []) if d.get("deviceName")}

    def batch_create(self, devices: List[Dict]) -> Dict:
        return self._call("POST", DEVICE_PATH + "/batch_create", {"devices": devices})

    def batch_update(self, devices: List[Dict]) -> Dict:
        return self._call("PUT", DEVICE_PATH + "/batch_update", {"devices": devices})

    def sites(self) -> List[Dict]:
        return self._call("GET", SITE_PATH).get("sites", [])

    def create_site(self, site: Dict) -> Dict:
        return self._call("POST", SITE_PATH, {"site": site}).get("site", {})

    def update_site(self, site: Dict) -> Dict:
        return self._call("PUT", SITE_PATH + "/" + urllib.parse.quote(str(site["id"])), {"site": site}).get("site", {})

    def create(self, device: Dict) -> Dict:
        """Create one device. Unlike batch_create, a failure comes back as an HTTP error with a reason."""
        return self._call("POST", DEVICE_PATH, {"device": device})

    def update(self, device: Dict) -> Dict:
        return self._call("PUT", DEVICE_PATH + "/" + urllib.parse.quote(device["id"]), {"device": device})


def batch_failures(resp: Dict, sent: List[Dict], key: str) -> List[Dict]:
    """The devices in sent that a batch call didn't apply.

    batch_create and batch_update answer 200 even when devices fail. They list
    failures in failedDevices (names for create, ids for update), and only the
    devices that worked under devices. Anything sent that is in neither counts
    as failed too.
    """
    done = {str(d.get(key)) for d in resp.get("devices", [])}
    failed = {str(x) for x in resp.get("failedDevices", [])}
    return [d for d in sent if str(d[key]) in failed or str(d[key]) not in done]
