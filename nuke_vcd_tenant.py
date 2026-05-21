#!/usr/bin/env python3
# Author: Alexander Lavrinovich <lavrinovich.alex@gmail.com> (https://github.com/Alex-Electron)
# License: MIT
# Version: 1.1.1
"""
nuke_vcd_tenant.py — delete a VMware Cloud Director organization through the API.

Without arguments, you get a menu of every org with counters. Pick one, pick
what to do (inventory, disable, stop, delete). With an org name on the command
line it runs that action directly.

When delete is picked and the org has live workloads, a second prompt asks
whether to leave it alone, stop everything and wait, or stop and delete.

The script never talks to the database. If the API can't delete something,
it says so and stops.

Credentials come from nuke_vcd.conf (INI). Lookup order:
  --config /path  >  $VCD_CONFIG  >  ./nuke_vcd.conf  >  ~/.nuke_vcd.conf
Env overrides: VCD_BASE, VCD_USER, VCD_PASS, VCD_API_VERSION, VCD_CONFIG.

Examples:
    python3 nuke_vcd_tenant.py                          # menu
    python3 nuke_vcd_tenant.py sc-foo --dry-run         # inventory
    python3 nuke_vcd_tenant.py sc-foo --action delete   # with prompts
    python3 nuke_vcd_tenant.py sc-foo --action delete --yes --on-vms stop-and-delete
"""

import argparse
import configparser
import json
import os
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass, field

import requests
import urllib3
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Workaround: some VCD deployments (or middleboxes in front of them) close TLSv1.3
# handshakes from Python 3.14+. Force max TLS version to 1.2 to avoid sporadic
# SSLEOFError: UNEXPECTED_EOF_WHILE_READING during the handshake.
def _make_ctx():
    # Python 3.14 ships OpenSSL with SECLEVEL=2 by default. That cipher subset
    # is narrower than what VCD cells advertise and the handshake fails with
    # SSLV3_ALERT_HANDSHAKE_FAILURE. Drop to SECLEVEL=0 to match curl/Firefox.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for ciphers in ('DEFAULT:@SECLEVEL=0', 'ALL:@SECLEVEL=0'):
        try:
            ctx.set_ciphers(ciphers)
            break
        except ssl.SSLError:
            continue
    return ctx


class _TLS12Adapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = _make_ctx()
        return super().init_poolmanager(*args, **kwargs)
    def proxy_manager_for(self, *args, **kwargs):
        kwargs['ssl_context'] = _make_ctx()
        return super().proxy_manager_for(*args, **kwargs)


# ---------------------------------------------------------------------------
# Configuration — populated from INI file, env vars, or defaults
# ---------------------------------------------------------------------------

VCD_BASE    = 'https://vcd.example.com'
VCD_URL     = ''
CLOUD_API   = ''
USER        = 'administrator'
PASS        = ''
API_VERSION = '37.0'

POLL_INTERVAL = 3
TASK_TIMEOUT  = 30 * 60

__version__ = '1.1.1'
VERBOSE = False


def load_config(config_path: str | None = None) -> dict:
    """Load creds from an INI file. Env vars win over the file."""
    cfg = {}
    paths_to_try = []
    if config_path:
        paths_to_try.append(config_path)
    elif os.getenv('VCD_CONFIG'):
        paths_to_try.append(os.getenv('VCD_CONFIG'))
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        paths_to_try += [
            os.path.join(os.getcwd(), 'nuke_vcd.conf'),
            os.path.join(script_dir, 'nuke_vcd.conf'),
            os.path.expanduser('~/.nuke_vcd.conf'),
        ]

    chosen = None
    for p in paths_to_try:
        if p and os.path.isfile(p):
            chosen = p; break
    if chosen:
        cp = configparser.ConfigParser()
        cp.read(chosen)
        if cp.has_section('vcd'):
            for k in ('base', 'user', 'password', 'api_version'):
                if cp.has_option('vcd', k):
                    cfg[k] = cp.get('vcd', k)
        cfg['__path'] = chosen

    # env wins over the file
    for env_k, cfg_k in [('VCD_BASE','base'), ('VCD_USER','user'),
                         ('VCD_PASS','password'), ('VCD_API_VERSION','api_version')]:
        if os.getenv(env_k):
            cfg[cfg_k] = os.getenv(env_k)
    return cfg


def apply_config(cfg: dict):
    global VCD_BASE, VCD_URL, CLOUD_API, USER, PASS, API_VERSION
    VCD_BASE    = cfg.get('base', VCD_BASE).rstrip('/')
    USER        = cfg.get('user', USER)
    PASS        = cfg.get('password', PASS)
    API_VERSION = cfg.get('api_version', API_VERSION)
    VCD_URL     = f"{VCD_BASE}/api"
    CLOUD_API   = f"{VCD_BASE}/cloudapi/1.0.0"
    if not PASS:
        err("Password is not set. Create nuke_vcd.conf (see nuke_vcd.conf.example) "
            "or export VCD_PASS.")
        sys.exit(2)


# ---------------------------------------------------------------------------
# Pretty printers
# ---------------------------------------------------------------------------

class C:
    OK = '\033[92m'; WARN = '\033[93m'; ERR = '\033[91m'
    HEAD = '\033[96m'; DIM = '\033[2m'; BOLD = '\033[1m'; END = '\033[0m'

def info(m): print(f"[*] {m}")
def ok(m):   print(f"{C.OK}[OK]{C.END} {m}")
def warn(m): print(f"{C.WARN}[!] {m}{C.END}")
def err(m):  print(f"{C.ERR}[X] {m}{C.END}")
def head(m): print(f"\n{C.HEAD}=== {m} ==={C.END}")
def dim(m):  print(f"{C.DIM}    {m}{C.END}")
def vlog(m):
    if VERBOSE: print(f"{C.DIM}    [http] {m}{C.END}")
def step(action, target):
    print(f"  -> {C.BOLD}{action}{C.END} {target}")


def _short_err(text: str, max_len: int = 600) -> str:
    """Pull minorErrorCode + message out of a VCD error body. Strip the Java stacktrace."""
    try:
        d = json.loads(text)
        msg = d.get('message') or d.get('details') or text[:max_len]
        code = d.get('minorErrorCode')
        return f"[{code}] {msg}" if code else msg
    except Exception:
        return text[:max_len]


# ---------------------------------------------------------------------------
# VCD API client
# ---------------------------------------------------------------------------

class VCDError(Exception):
    pass


class VCD:
    def __init__(self):
        self.s = requests.Session(); self.s.verify = False
        self.s.mount('https://', _TLS12Adapter())
        self.token = None

    def _xml_h(self):
        h = {'Accept': f'application/*+json;version={API_VERSION}'}
        if self.token: h['Authorization'] = f'Bearer {self.token}'
        return h

    def _json_h(self):
        h = {'Accept': f'application/json;version={API_VERSION}',
             'Content-Type': f'application/json;version={API_VERSION}'}
        if self.token: h['Authorization'] = f'Bearer {self.token}'
        return h

    def login(self):
        r = self.s.post(f"{VCD_URL}/sessions",
                        auth=(f"{USER}@system", PASS),
                        headers=self._xml_h())
        if r.status_code != 200:
            raise VCDError(f"Login failed: {r.status_code} {_short_err(r.text)}")
        self.token = r.headers['X-VMWARE-VCLOUD-ACCESS-TOKEN']

    # --- low level
    def _req(self, method, url, json_api=False, body=None):
        h = self._json_h() if json_api else self._xml_h()
        kw = {}
        if body is not None: kw['data'] = json.dumps(body)
        r = self.s.request(method, url, headers=h, **kw)
        vlog(f"{method} {url} -> {r.status_code}")
        if VERBOSE and r.status_code >= 400:
            dim(f"    response: {_short_err(r.text, 400)}")
        return r

    def get   (self, url, json_api=False):           return self._req('GET',    url, json_api)
    def delete(self, url, json_api=False):           return self._req('DELETE', url, json_api)
    def post  (self, url, json_api=False, body=None):return self._req('POST',   url, json_api, body)
    def put   (self, url, json_api=False, body=None):return self._req('PUT',    url, json_api, body)

    # --- listing
    def list_all(self, cloudapi_path, page_size=128):
        """Walk every page of a CloudAPI list endpoint."""
        out, page = [], 1
        sep = '&' if '?' in cloudapi_path else '?'
        while True:
            url = f"{CLOUD_API}/{cloudapi_path}{sep}page={page}&pageSize={page_size}"
            r = self.get(url, json_api=True)
            if r.status_code != 200:
                raise VCDError(f"GET {url} -> {r.status_code} {_short_err(r.text)}")
            d = r.json()
            out.extend(d.get('values', []))
            if page >= d.get('pageCount', 1) or not d.get('values'):
                break
            page += 1
        return out

    def query(self, q_type, filt='', page_size=128):
        """Legacy /api/query with pagination. VCD defaults to 25 records per page, so
        without this you only see the first 25 of anything."""
        base = f"{VCD_URL}/query?type={q_type}&format=records&pageSize={page_size}"
        if filt:
            base += f"&filter={urllib.parse.quote(filt, safe='=,!;()*')}"
        out, page = [], 1
        while True:
            r = self.get(f"{base}&page={page}")
            if r.status_code != 200:
                vlog(f"query {q_type}: {r.status_code} {_short_err(r.text, 200)}")
                return out
            d = r.json()
            recs = d.get('record', [])
            out.extend(recs)
            n_pages = d.get('numberOfPages') or 1
            if page >= n_pages or not recs:
                return out
            page += 1

    # --- tasks
    def wait_task(self, task_href, label='task'):
        """Poll a legacy XML task until it's done or breaks."""
        if not task_href: return True
        start = time.time()
        last = ''
        while True:
            r = self.get(task_href)
            if r.status_code != 200:
                raise VCDError(f"{label}: task fetch failed: {r.status_code} {_short_err(r.text)}")
            d = r.json()
            st = d.get('status')
            if st != last:
                vlog(f"task {label}: {st}")
                last = st
            if st == 'success':
                return True
            if st in ('error', 'aborted', 'canceled'):
                e = d.get('error') or {}
                msg = e.get('message') if isinstance(e, dict) else None
                raise VCDError(f"{label} failed: {msg or d.get('details') or st}")
            if time.time() - start > TASK_TIMEOUT:
                raise VCDError(f"{label} timeout")
            time.sleep(POLL_INTERVAL)

    def submit(self, method, url, json_api=False, body=None, label='operation', tolerate=()):
        """Fire a request, wait for the task if VCD returns 202. `tolerate` is a list
        of status codes we treat as success (404 on a retry is the usual one)."""
        r = self._req(method, url, json_api=json_api, body=body)
        if r.status_code in tolerate:
            vlog(f"{label}: tolerated {r.status_code}")
            return True
        if r.status_code in (200, 204):
            return True
        if r.status_code == 202:
            loc = r.headers.get('Location')
            if not loc: return True
            return self.wait_task(loc, label=label)
        raise VCDError(f"{label}: {method} {url} -> {r.status_code} {_short_err(r.text)}")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass
class Inventory:
    org_name: str
    org_uuid: str = ''
    org_urn: str = ''
    org_href: str = ''         # /api/admin/org/<uuid>
    org_enabled: bool = True
    vdcs: list = field(default_factory=list)
    vapps: list = field(default_factory=list)
    vms: list = field(default_factory=list)
    disks: list = field(default_factory=list)
    catalogs: list = field(default_factory=list)
    networks: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    vdc_groups: list = field(default_factory=list)
    fw_groups: list = field(default_factory=list)
    app_port_profiles: list = field(default_factory=list)
    cert_lib: list = field(default_factory=list)
    trusted_certs: list = field(default_factory=list)
    users: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    rdes:  list = field(default_factory=list)  # list of (typeNss, entity dict)
    veeam_endpoint: str = ''                    # Veeam B&R URL if the org has a Veeam mapping
    org_metadata: list = field(default_factory=list)  # [(key, domain, value)]


def discover(api: VCD, org_name: str) -> Inventory:
    inv = Inventory(org_name=org_name)
    recs = api.query('adminOrgs', f'name=={org_name}') or \
           api.query('organization', f'name=={org_name}')
    if not recs:
        # fallback: direct admin listing
        r = api.get(f"{VCD_URL}/admin/orgs/query?filter=name=={org_name}&format=records")
        if r.status_code == 200:
            recs = r.json().get('record', [])
    if not recs:
        raise VCDError(f"Organization '{org_name}' not found")
    href = recs[0]['href']
    inv.org_uuid = href.rstrip('/').split('/')[-1]
    inv.org_urn  = f"urn:vcloud:org:{inv.org_uuid}"
    inv.org_href = f"{VCD_URL}/admin/org/{inv.org_uuid}"
    inv.org_enabled = bool(recs[0].get('isEnabled', True))
    vlog(f"org href={inv.org_href}  urn={inv.org_urn}")

    inv.vdcs     = api.query('adminOrgVdc',  f'orgName=={org_name}')
    inv.catalogs = api.query('adminCatalog', f'orgName=={org_name}')

    # adminVApp/adminVM/adminDisk don't accept orgName as a filter on this API version.
    # Loop by VDC name instead.
    for vdc in inv.vdcs:
        vname = vdc.get('name')
        inv.vapps += api.query('adminVApp', f'vdcName=={vname}')
        inv.vms   += api.query('adminVM',   f'vdcName=={vname}')
        inv.disks += api.query('adminDisk', f'vdcName=={vname}')

    # Users/Groups: legacy `user` query has no org filter. CloudAPI users endpoint
    # accepts orgEntityRef.id; fall back to the admin org body if it's empty.
    try:
        for v in api.list_all(f'users?filter=orgEntityRef.id=={inv.org_urn}'):
            inv.users.append({
                'name': v.get('name'),
                'href': f"{VCD_URL}/admin/user/{(v.get('id') or '').split(':')[-1]}",
                'roleName': (v.get('roleEntityRef') or {}).get('name'),
                'id': v.get('id'),
            })
    except VCDError as e:
        dim(f"cloudapi users: {e}")
    try:
        for v in api.list_all(f'groups?filter=orgEntityRef.id=={inv.org_urn}'):
            inv.groups.append({
                'name': v.get('name'),
                'href': f"{VCD_URL}/admin/group/{(v.get('id') or '').split(':')[-1]}",
                'id': v.get('id'),
            })
    except VCDError as e:
        dim(f"cloudapi groups: {e}")
    # Fallback for admin org body (some older API versions still need this)
    if not inv.users:
        try:
            r = api.get(inv.org_href)
            if r.status_code == 200:
                d = r.json()
                for u in ((d.get('users') or {}).get('user') or []):
                    inv.users.append({'name': u.get('name'), 'href': u.get('href')})
                for g in ((d.get('groups') or {}).get('group') or []):
                    inv.groups.append({'name': g.get('name'), 'href': g.get('href')})
        except Exception as e:
            dim(f"org details for users/groups: {e}")

    def in_org(v):
        return (v.get('orgRef') or {}).get('id') == inv.org_urn or \
               (v.get('orgRef') or {}).get('name') == org_name

    try:
        inv.networks = [v for v in api.list_all('orgVdcNetworks') if in_org(v)]
    except VCDError as e: warn(f"discover orgVdcNetworks: {e}")
    try:
        inv.edges = [v for v in api.list_all('edgeGateways') if in_org(v)]
    except VCDError as e: warn(f"discover edgeGateways: {e}")
    try:
        inv.vdc_groups = api.list_all(f'vdcGroups?filter=orgId=={inv.org_urn}')
    except VCDError as e: warn(f"discover vdcGroups: {e}")

    owners = [(e['id'], e['name']) for e in inv.edges] + \
             [(g['id'], g['name']) for g in inv.vdc_groups]
    for own_id, own_name in owners:
        try:
            for v in api.list_all(f'firewallGroups/summaries?filter=ownerRef.id=={own_id}'):
                inv.fw_groups.append(v)
        except VCDError as e:
            warn(f"discover firewallGroups for {own_name}: {e}")

    try:
        inv.app_port_profiles = api.list_all(
            f'applicationPortProfiles?filter=_context=={inv.org_urn};scope==TENANT')
    except VCDError as e: dim(f"applicationPortProfiles: {e}")

    for path, dest in [('ssl/certificateLibrary', 'cert_lib'),
                       ('ssl/trustedCertificates', 'trusted_certs')]:
        try:
            items = [v for v in api.list_all(path)
                     if (v.get('orgRef') or {}).get('id') == inv.org_urn]
            setattr(inv, dest, items)
        except VCDError as e: dim(f"{path}: {e}")

    # Org metadata: Veeam mapping URL, billing tags, whatever else got stuffed in here.
    try:
        r = api.get(f"{inv.org_href}/metadata")
        if r.status_code == 200:
            for e in (r.json().get('metadataEntry') or []):
                key = e.get('key')
                dom = (e.get('domain') or {}).get('value') or ''
                val = (e.get('typedValue') or {}).get('value')
                inv.org_metadata.append((key, dom, val))
                if key and 'veeam' in key.lower():
                    inv.veeam_endpoint = str(val or '')
    except Exception as e:
        dim(f"org metadata: {e}")

    # RDEs: CAPVCD k8s clusters, kube extensions, anything custom. These reference
    # org_member rows and will block delete org with a foreign key violation.
    try:
        types = api.list_all('entityTypes')
        for t in types:
            tns = f"{t['vendor']}/{t['nss']}/{t['version']}"
            try:
                for v in api.list_all(f'entities/types/{tns}'):
                    if (v.get('org') or {}).get('id') == inv.org_urn:
                        inv.rdes.append((tns, v))
            except VCDError as e:
                dim(f"  entities/types/{tns}: {e}")
    except VCDError as e:
        dim(f"entityTypes: {e}")

    return inv


def _fmt_vm(v: dict) -> str:
    """Format a VM record: VCD name, vApp, vCenter name, IP.
    `hostName` is left out on purpose. In VCD that field is the ESXi host the VM
    runs on, not the guest OS hostname. Showing it confuses people."""
    name = v.get('name') or '?'
    vapp = v.get('containerName') or '-'
    vc_name = v.get('vmNameInVc') or ''
    ip = v.get('ipAddress') or ''
    on = 'ON ' if is_vm_powered(v) else 'off'
    extra = []
    if vapp != name: extra.append(f"vApp={vapp}")
    if vc_name and vc_name != name: extra.append(f"vc={vc_name}")
    if ip: extra.append(f"ip={ip}")
    tail = ('  ' + '  '.join(extra)) if extra else ''
    return f"[{on}] {name:30s}{tail}"


def print_inventory(inv: Inventory):
    head(f"Inventory: org '{inv.org_name}'  ({inv.org_urn})  enabled={inv.org_enabled}")
    powered = [v for v in inv.vms if is_vm_powered(v)]
    rows = [
        ('Org VDCs',             inv.vdcs,        lambda v: f"{v.get('name')} enabled={v.get('isEnabled')} href={v.get('href')}"),
        ('vApps',                inv.vapps,       lambda v: f"{v.get('name'):30s} status={v.get('status')} vdc={v.get('vdcName')}"),
        ('VMs',                  inv.vms,         _fmt_vm),
        ('Independent disks',    inv.disks,       lambda v: f"{v.get('name')} vdc={v.get('vdcName')}"),
        ('Catalogs',             inv.catalogs,    lambda v: f"{v.get('name')} href={v.get('href')}"),
        ('Org VDC networks',     inv.networks,    lambda v: f"{v.get('name')} type={v.get('networkType')} owner={(v.get('ownerRef') or {}).get('name')}"),
        ('Edge Gateways',        inv.edges,       lambda v: f"{v.get('name')} owner={(v.get('ownerRef') or {}).get('name')}"),
        ('VDC Groups',           inv.vdc_groups,  lambda v: f"{v.get('name')} participating={len(v.get('participatingOrgVdcs') or [])}"),
        ('Firewall Groups',      inv.fw_groups,   lambda v: f"{v.get('typeValue')} '{v.get('name')}' owner={(v.get('ownerRef') or {}).get('name')}"),
        ('App Port Profiles',    inv.app_port_profiles, lambda v: v.get('name')),
        ('Certificate Library',  inv.cert_lib,    lambda v: v.get('alias')),
        ('Trusted Certificates', inv.trusted_certs, lambda v: v.get('alias')),
        ('Users',                inv.users,       lambda v: f"{v.get('name')} role={v.get('roleName')}"),
        ('Groups',               inv.groups,      lambda v: v.get('name')),
        ('RDEs (k8s, ext...)',   inv.rdes,        lambda v: f"{v[0]}: {v[1].get('name')}"),
        ('Org metadata',         inv.org_metadata, lambda v: f"{v[0]} ({v[1]}) = {v[2]}"),
    ]
    for label, items, fmt in rows:
        marker = '!' if (label == 'Firewall Groups' and items) or (label == 'VMs' and powered) else ' '
        print(f" {marker} {label:25s}: {len(items)}")
        # Show every vApp/VM. Everything else stops at 25 to keep the screen sane.
        limit = len(items) if label in ('VMs','vApps') else 25
        for it in items[:limit]:
            dim(f"  - {fmt(it)}")
        if len(items) > limit:
            dim(f"  ... +{len(items)-limit} more")
    if powered:
        warn(f"Powered-on VMs: {len(powered)}")
    print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_vm_powered(v):
    st = str(v.get('status') or '')
    if st in ('POWERED_ON', '4'): return True
    if v.get('isDeployed') in (True, 'true', 'True'): return True
    return False


def admin_url_from_href(href, kind):
    """Rewrite an href into the /api/admin/<kind>/<uuid> form. As sysadmin you need
    this URL for delete operations; the tenant /api/<kind>/<uuid> form returns 405."""
    uuid = href.rstrip('/').split('/')[-1]
    return f"{VCD_URL}/admin/{kind}/{uuid}"


# ---------------------------------------------------------------------------
# Deletion steps
# ---------------------------------------------------------------------------

def _post_vapp_action(api: VCD, action_url: str, body_xml: str | None = None,
                       content_type: str | None = None, label: str = ''):
    """POST a vApp action with an XML body. The vApp actions API still wants XML;
    sending JSON works for some endpoints but `undeploy` ignores it silently."""
    h = api._xml_h()
    if content_type:
        h['Content-Type'] = f"{content_type};version={API_VERSION}"
    r = api.s.post(action_url, headers=h, data=body_xml)
    vlog(f"POST {action_url} -> {r.status_code}")
    if r.status_code == 202:
        api.wait_task(r.headers.get('Location'), label=label or 'vApp action')
        return True
    if r.status_code in (200, 204, 400):
        return r.status_code != 400
    return False


def shutdown_vapps(api: VCD, inv: Inventory):
    """powerOff + undeploy every vApp so the VMs inside get stopped cleanly.
    Without this the next step (delete vApp) returns 400 'Stop the vApp and try again'."""
    if not inv.vapps:
        head("Stop vApps (0)"); dim("none"); return
    head(f"Stop vApps ({len(inv.vapps)})")
    for va in inv.vapps:
        name, href = va.get('name'), va.get('href')
        status = va.get('status')
        # POWERED_ON or MIXED -> hit powerOff first. POWERED_OFF/SUSPENDED/RESOLVED are fine as-is.
        if status not in ('POWERED_OFF', 'RESOLVED', 'UNRESOLVED', 'SUSPENDED', '8', '0'):
            step("powerOff vApp", f"{name} (status={status})")
            try:
                api.submit('POST', f"{href}/power/action/powerOff",
                           label=f"powerOff {name}", tolerate=(400, 403))
            except VCDError as e:
                warn(f"  {name}: {e}")
        step("undeploy vApp", name)
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<UndeployVAppParams xmlns="http://www.vmware.com/vcloud/v1.5">'
                '<UndeployPowerAction>powerOff</UndeployPowerAction>'
                '</UndeployVAppParams>')
        try:
            _post_vapp_action(api, f"{href}/action/undeploy", body_xml=body,
                              content_type='application/vnd.vmware.vcloud.undeployVAppParams+xml',
                              label=f"undeploy {name}")
        except VCDError as e:
            dim(f"  undeploy {name}: {e}")


def delete_vapps(api: VCD, inv: Inventory):
    if not inv.vapps:
        head("Delete vApps (0)"); dim("none"); return
    head(f"Delete vApps ({len(inv.vapps)})")
    for va in inv.vapps:
        name, href = va.get('name'), va.get('href')
        step("delete vApp", name)
        try:
            api.submit('DELETE', href, label=f"delete vApp {name}", tolerate=(404,))
            ok(f"vApp {name}")
        except VCDError as e:
            err(f"  delete vApp {name}: {e}")


def delete_disks(api: VCD, inv: Inventory):
    head(f"Delete independent disks ({len(inv.disks)})")
    if not inv.disks: dim("none"); return
    for d in inv.disks:
        name = d.get('name')
        step("delete disk", name)
        try:
            api.submit('DELETE', d['href'], label=f"delete disk {name}", tolerate=(404,))
            ok(f"disk {name}")
        except VCDError as e:
            err(f"  {name}: {e}")


def delete_catalogs(api: VCD, inv: Inventory):
    head(f"Delete catalogs ({len(inv.catalogs)})")
    if not inv.catalogs: dim("none"); return
    for c in inv.catalogs:
        name = c.get('name')
        admin_href = admin_url_from_href(c['href'], 'catalog')

        # vApp templates first. Catalog delete returns 409 if anything is left inside.
        for tpl in api.query('adminVAppTemplate', f'catalogName=={name}'):
            tn, th = tpl.get('name'), tpl.get('href')
            step("delete vAppTemplate", tn)
            try:
                api.submit('DELETE', th, label=f"del template {tn}", tolerate=(404,))
            except VCDError as e:
                err(f"    {tn}: {e}")
        # Media items (ISOs).
        for m in api.query('adminMedia', f'catalogName=={name}'):
            mn, mh = m.get('name'), m.get('href')
            step("delete media", mn)
            try:
                api.submit('DELETE', mh, label=f"del media {mn}", tolerate=(404,))
            except VCDError as e:
                err(f"    {mn}: {e}")
        # If the catalog is published — unpublish first, otherwise delete is rejected.
        try:
            r = api.get(admin_href)
            if r.status_code == 200:
                d = r.json()
                if d.get('isPublished'):
                    dim(f"  catalog '{name}' isPublished=true — unpublishing")
                    try:
                        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                                '<PublishCatalogParams xmlns="http://www.vmware.com/vcloud/v1.5">'
                                '<IsPublished>false</IsPublished></PublishCatalogParams>')
                        h2 = api._xml_h()
                        h2['Content-Type'] = f'application/vnd.vmware.admin.publishCatalogParams+xml;version={API_VERSION}'
                        rr = api.s.post(f"{admin_href}/action/publish", headers=h2, data=body)
                        vlog(f"POST publish unpublish -> {rr.status_code}")
                    except Exception as e:
                        dim(f"  unpublish: {e}")
        except Exception as e:
            dim(f"  catalog details: {e}")
        # Sweep stray catalogItems. They can survive after their vAppTemplate/Media is gone.
        try:
            r = api.get(admin_href)
            if r.status_code == 200:
                items = ((r.json().get('catalogItems') or {}).get('catalogItem') or [])
                for it in items:
                    step("delete catalogItem", it.get('name'))
                    try:
                        api.submit('DELETE', it['href'], label=f"del catItem {it.get('name')}", tolerate=(404,))
                    except VCDError as e:
                        err(f"    {it.get('name')}: {e}")
        except Exception: pass
        # Catalog itself.
        step("delete catalog", f"{name} ({admin_href})")
        try:
            api.submit('DELETE', admin_href, label=f"delete catalog {name}", tolerate=(404,))
            ok(f"catalog {name}")
        except VCDError as e:
            err(f"  {name}: {e}")


def delete_networks(api: VCD, inv: Inventory):
    head(f"Delete Org VDC networks ({len(inv.networks)})")
    if not inv.networks: dim("none"); return
    for n in inv.networks:
        name = n.get('name')
        step("delete network", f"{name} type={n.get('networkType')}")
        try:
            api.submit('DELETE', f"{CLOUD_API}/orgVdcNetworks/{n['id']}",
                       json_api=True, label=f"delete network {name}", tolerate=(404,))
            ok(f"network {name}")
        except VCDError as e:
            err(f"  {name}: {e}")


def disable_lb_on_edge(api: VCD, edge: dict):
    """Tear down LB on an NSX-T edge. Order matters: virtual services, then pools,
    then SEG assignments. Listing lives at /edgeGateways/{id}/loadBalancer/*Summaries,
    delete at /loadBalancer/<type>/<id>. Get this wrong and edge delete returns 403
    'load balancer services enabled'."""
    eid = edge['id']; name = edge.get('name')
    info(f"  ----- LB cleanup on edge '{name}' -----")

    # 1) Virtual Services
    try:
        vss = api.list_all(f'edgeGateways/{eid}/loadBalancer/virtualServiceSummaries')
    except VCDError as e:
        dim(f"  VS list: {e}"); vss = []
    for vs in vss:
        step("delete LB virtualService", vs.get('name'))
        try:
            api.submit('DELETE', f"{CLOUD_API}/loadBalancer/virtualServices/{vs['id']}",
                       json_api=True, label=f"del VS {vs.get('name')}", tolerate=(404,))
        except VCDError as e:
            err(f"    {e}")

    # 2) Pools
    try:
        pools = api.list_all(f'edgeGateways/{eid}/loadBalancer/poolSummaries')
    except VCDError as e:
        dim(f"  pool list: {e}"); pools = []
    for p in pools:
        step("delete LB pool", p.get('name'))
        try:
            api.submit('DELETE', f"{CLOUD_API}/loadBalancer/pools/{p['id']}",
                       json_api=True, label=f"del pool {p.get('name')}", tolerate=(404,))
        except VCDError as e:
            err(f"    {e}")

    # 3) Service Engine Group assignments
    try:
        segs = api.list_all(f'loadBalancer/serviceEngineGroups/assignments?filter=gatewayRef.id=={eid}')
    except VCDError as e:
        dim(f"  SEG list: {e}"); segs = []
    for seg in segs:
        seg_id = seg.get('id')
        seg_name = (seg.get('serviceEngineGroupRef') or {}).get('name') or seg_id
        step("delete LB SEG assignment", seg_name)
        try:
            api.submit('DELETE',
                       f"{CLOUD_API}/loadBalancer/serviceEngineGroups/assignments/{seg_id}",
                       json_api=True, label=f"del SEG-assign {seg_name}", tolerate=(404,))
        except VCDError as e:
            err(f"    {seg_name}: {e}")


def cleanup_edge(api: VCD, edge: dict):
    eid = edge['id']; name = edge.get('name')
    info(f"  cleaning up services on edge '{name}' (id={eid})")
    # FW rules
    try:
        r = api.get(f"{CLOUD_API}/edgeGateways/{eid}/firewall/rules", json_api=True)
        if r.status_code == 200:
            cur = r.json()
            n = len(cur.get('userDefinedRules') or [])
            if n:
                step("clear FW rules", f"{n} on {name}")
                cur['userDefinedRules'] = []
                api.submit('PUT', f"{CLOUD_API}/edgeGateways/{eid}/firewall/rules",
                           json_api=True, body=cur, label=f"clear FW {name}")
    except VCDError as e:
        dim(f"  FW rules: {e}")
    # NAT
    try:
        rules = api.list_all(f'edgeGateways/{eid}/nat/rules')
        for rl in rules:
            step("delete NAT rule", rl.get('name'))
            api.submit('DELETE', f"{CLOUD_API}/edgeGateways/{eid}/nat/rules/{rl['id']}",
                       json_api=True, label=f"del NAT {rl.get('name')}", tolerate=(404,))
    except VCDError as e:
        dim(f"  NAT: {e}")
    # IPSec
    try:
        tuns = api.list_all(f'edgeGateways/{eid}/ipsec/tunnels')
        for t in tuns:
            step("delete IPSec tunnel", t.get('name'))
            api.submit('DELETE', f"{CLOUD_API}/edgeGateways/{eid}/ipsec/tunnels/{t['id']}",
                       json_api=True, label=f"del IPSec {t.get('name')}", tolerate=(404,))
    except VCDError as e:
        dim(f"  IPSec: {e}")
    # Static routes
    try:
        rts = api.list_all(f'edgeGateways/{eid}/routing/staticRoutes')
        for sr in rts:
            step("delete static route", sr.get('name') or sr.get('id'))
            api.submit('DELETE', f"{CLOUD_API}/edgeGateways/{eid}/routing/staticRoutes/{sr['id']}",
                       json_api=True, label="del static route", tolerate=(404,))
    except VCDError as e:
        dim(f"  routes: {e}")
    # DHCP forwarder
    try:
        r = api.get(f"{CLOUD_API}/edgeGateways/{eid}/dhcpForwarder", json_api=True)
        if r.status_code == 200:
            cfg = r.json()
            if cfg.get('enabled') or cfg.get('dhcpServers'):
                cfg['enabled'] = False
                cfg['dhcpServers'] = []
                step("disable DHCP forwarder", name)
                api.submit('PUT', f"{CLOUD_API}/edgeGateways/{eid}/dhcpForwarder",
                           json_api=True, body=cfg, label=f"disable DHCP fwd {name}")
    except VCDError as e:
        dim(f"  DHCP fwd: {e}")
    # LB last. Skip this and edge delete fails with 403.
    disable_lb_on_edge(api, edge)


def delete_fw_groups(api: VCD, inv: Inventory):
    head(f"Delete Firewall Groups ({len(inv.fw_groups)})")
    if not inv.fw_groups: dim("none"); return
    for g in inv.fw_groups:
        own = (g.get('ownerRef') or {}).get('name')
        step("delete fw-group", f"{g.get('typeValue')} '{g.get('name')}' owner={own}")
        try:
            api.submit('DELETE', f"{CLOUD_API}/firewallGroups/{g['id']}",
                       json_api=True, label=f"del fw-group {g.get('name')}", tolerate=(404,))
            ok(f"fw-group {g.get('name')}")
        except VCDError as e:
            err(f"  {g.get('name')}: {e}")


def delete_edges(api: VCD, inv: Inventory):
    head(f"Delete Edge Gateways ({len(inv.edges)})")
    if not inv.edges: dim("none"); return
    for e in inv.edges:
        name = e.get('name')
        cleanup_edge(api, e)
        step("delete edgeGateway", name)
        try:
            api.submit('DELETE', f"{CLOUD_API}/edgeGateways/{e['id']}",
                       json_api=True, label=f"delete edge {name}", tolerate=(404,))
            ok(f"edge {name}")
        except VCDError as e2:
            err(f"  {name}: {e2}")


def cleanup_vdc_group(api: VCD, g: dict):
    gid = g['id']; name = g.get('name')
    info(f"  cleaning up DFW in vdcGroup '{name}'")
    # Wipe rules from the default policy first.
    try:
        r = api.get(f"{CLOUD_API}/vdcGroups/{gid}/dfwPolicies/default/rules", json_api=True)
        if r.status_code == 200:
            cur = r.json()
            if cur.get('values'):
                step("clear DFW rules", f"{len(cur['values'])} rules in {name}")
                cur['values'] = []
                api.submit('PUT', f"{CLOUD_API}/vdcGroups/{gid}/dfwPolicies/default/rules",
                           json_api=True, body=cur, label=f"clear DFW rules {name}")
    except VCDError as e:
        dim(f"  DFW rules: {e}")
    # Then any user-added DFW policies.
    try:
        r = api.get(f"{CLOUD_API}/vdcGroups/{gid}/dfwPolicies", json_api=True)
        if r.status_code == 200:
            d = r.json()
            for p in (d.get('userDefinedPolicies') or []):
                step("delete DFW policy", p.get('name'))
                api.submit('DELETE', f"{CLOUD_API}/vdcGroups/{gid}/dfwPolicies/{p['id']}",
                           json_api=True, label=f"del DFW policy {p.get('name')}", tolerate=(404,))
    except VCDError as e:
        dim(f"  DFW policies: {e}")


def delete_vdc_groups(api: VCD, inv: Inventory):
    head(f"Delete VDC Groups ({len(inv.vdc_groups)})")
    if not inv.vdc_groups: dim("none"); return
    for g in inv.vdc_groups:
        name = g.get('name')
        cleanup_vdc_group(api, g)
        step("delete vdcGroup", name)
        try:
            api.submit('DELETE', f"{CLOUD_API}/vdcGroups/{g['id']}?force=true",
                       json_api=True, label=f"del vdc-group {name}", tolerate=(404,))
            ok(f"vdc-group {name}")
        except VCDError as e:
            err(f"  {name}: {e}")


def delete_app_port_profiles(api: VCD, inv: Inventory):
    head(f"Delete Application Port Profiles ({len(inv.app_port_profiles)})")
    if not inv.app_port_profiles: dim("none"); return
    for p in inv.app_port_profiles:
        step("delete app-port", p.get('name'))
        try:
            api.submit('DELETE', f"{CLOUD_API}/applicationPortProfiles/{p['id']}",
                       json_api=True, label=f"del app-port {p.get('name')}", tolerate=(404,))
            ok(f"app-port {p.get('name')}")
        except VCDError as e:
            err(f"  {p.get('name')}: {e}")


def delete_certs(api: VCD, inv: Inventory):
    for label, items, base in [
        ('certificate library', inv.cert_lib, 'ssl/certificateLibrary'),
        ('trusted certificates', inv.trusted_certs, 'ssl/trustedCertificates'),
    ]:
        head(f"Delete {label} ({len(items)})")
        if not items:
            dim("none"); continue
        for c in items:
            step(f"delete {label}", c.get('alias'))
            try:
                api.submit('DELETE', f"{CLOUD_API}/{base}/{c['id']}",
                           json_api=True, label=f"del cert {c.get('alias')}", tolerate=(404,))
                ok(f"{label} {c.get('alias')}")
            except VCDError as e:
                err(f"  {c.get('alias')}: {e}")


def disable_and_delete_vdcs(api: VCD, inv: Inventory, keep_vdc: bool):
    head(f"{'Cleanup' if keep_vdc else 'Delete'} Org VDCs ({len(inv.vdcs)})")
    if not inv.vdcs: dim("none"); return
    for v in inv.vdcs:
        href, name = v.get('href'), v.get('name')
        if v.get('isEnabled'):
            step("disable vdc", name)
            try:
                api.submit('POST', f"{href}/action/disable", label=f"disable vdc {name}", tolerate=(403, 400))
            except VCDError as e:
                dim(f"  disable: {e}")
        if keep_vdc:
            continue
        step("delete vdc", f"{name} (recursive&force)")
        try:
            api.submit('DELETE', f"{href}?recursive=true&force=true",
                       label=f"delete vdc {name}", tolerate=(404,))
            ok(f"vdc {name}")
        except VCDError as e:
            err(f"  {name}: {e}")


def _delete_rde_entity(api: VCD, rde_id: str, label: str) -> bool:
    """Delete a single RDE entity. Try /resolve first (some types require RESOLVED),
    then plain DELETE, then invokeHooks=false fallbacks. Returns True on success."""
    try:
        r = api.post(f"{CLOUD_API}/entities/{rde_id}/resolve", json_api=True)
        vlog(f"resolve {rde_id} -> {r.status_code}")
    except Exception: pass
    for url in [f"{CLOUD_API}/entities/{rde_id}",
                f"{CLOUD_API}/entities/{rde_id}?invokeHooks=false",
                f"{CLOUD_API}/entities/{rde_id}?invokeHooks=false&recursive=true"]:
        try:
            api.submit('DELETE', url, json_api=True,
                       label=f"del RDE {label}", tolerate=(404,))
            return True
        except VCDError as e:
            dim(f"  via {url}: {e}")
    return False


def find_k8s_clusters_in_org(api: VCD, org_urn: str) -> list[tuple]:
    """Return list of (type_nss, entity_dict) for CAPVCD clusters in the given org.
    Only the type versions actually present on this VCD are queried — others get 403."""
    out = []
    for ver in ('1.3.0', '1.2.0'):
        tns = f"vmware/capvcdCluster/{ver}"
        try:
            for v in api.list_all(f'entities/types/{tns}'):
                if (v.get('org') or {}).get('id') == org_urn:
                    out.append((tns, v))
        except VCDError as e:
            dim(f"  list {tns}: {e}")
    return out


def _delete_cse_tokens_for_cluster(api: VCD, cluster_name: str, owner_name: str = '') -> int:
    """Delete cse-<clusterName>-* tokens. The owner check is informational only —
    when CSE failed half-way, the token's owner can differ from the RDE owner."""
    n = 0
    try:
        for v in api.list_all('tokens'):
            tn = v.get('name') or ''
            own = (v.get('owner') or {}).get('name') or ''
            if tn.startswith(f"cse-{cluster_name}-") or tn == f"cse-{cluster_name}":
                if owner_name and own != owner_name:
                    warn(f"  token {tn} owner mismatch (token={own}, RDE={owner_name}) — deleting anyway")
                step("delete CSE token", f"{tn} owner={own}")
                try:
                    api.submit('DELETE', f"{CLOUD_API}/tokens/{v['id']}",
                               json_api=True, label=f"del token {tn}", tolerate=(404,))
                    n += 1
                except VCDError as e:
                    err(f"    {tn}: {e}")
    except VCDError as e:
        dim(f"  list tokens: {e}")
    return n


def _delete_cluster_vapp(api: VCD, cluster_name: str, vdc_name: str = '') -> bool:
    """Find and delete vApp matching the cluster name. Returns True if found+deleted."""
    candidates = api.query('adminVApp', f'name=={cluster_name}')
    if vdc_name:
        candidates = [c for c in candidates if c.get('vdcName') == vdc_name]
    if not candidates:
        return False
    for va in candidates:
        nm, href = va.get('name'), va.get('href')
        status = va.get('status')
        if status not in ('POWERED_OFF', 'RESOLVED', 'UNRESOLVED', 'SUSPENDED'):
            step("powerOff vApp", f"{nm} (status={status})")
            try: api.submit('POST', f"{href}/power/action/powerOff",
                            label=f"powerOff {nm}", tolerate=(400, 403))
            except VCDError as e: warn(f"  {nm}: {e}")
        step("undeploy vApp", nm)
        try:
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<UndeployVAppParams xmlns="http://www.vmware.com/vcloud/v1.5">'
                    '<UndeployPowerAction>powerOff</UndeployPowerAction>'
                    '</UndeployVAppParams>')
            _post_vapp_action(api, f"{href}/action/undeploy", body_xml=body,
                              content_type='application/vnd.vmware.vcloud.undeployVAppParams+xml',
                              label=f"undeploy {nm}")
        except VCDError as e: dim(f"  undeploy {nm}: {e}")
        step("delete vApp", nm)
        try:
            api.submit('DELETE', href, label=f"delete vApp {nm}", tolerate=(404,))
            ok(f"vApp {nm}")
        except VCDError as e:
            err(f"  delete vApp {nm}: {e}")
    return True


def delete_one_k8s_cluster(api: VCD, tns: str, ent: dict, vdcs: list = None):
    """Full cleanup of one CAPVCD cluster: vApp -> CSE tokens -> RDE."""
    name = ent.get('name'); rid = ent.get('id')
    owner = (ent.get('owner') or {}).get('name','')
    state = ent.get('state','?')
    head(f"Delete k8s cluster '{name}'  (type={tns}, state={state}, owner={owner})")

    # vApp: try every VDC name we know about
    vdc_names = [v.get('name') for v in (vdcs or [])]
    found_vapp = False
    if vdc_names:
        for vdc_n in vdc_names:
            if _delete_cluster_vapp(api, name, vdc_n):
                found_vapp = True
                break
    else:
        found_vapp = _delete_cluster_vapp(api, name)
    if not found_vapp:
        dim(f"  no vApp named '{name}' — already gone or never created")

    # CSE tokens
    n_tok = _delete_cse_tokens_for_cluster(api, name, owner_name=owner)
    dim(f"  deleted {n_tok} CSE token(s) for cluster '{name}'")

    # RDE
    step("delete RDE", f"{tns} {name}")
    if _delete_rde_entity(api, rid, name):
        ok(f"cluster '{name}' fully removed")
    else:
        err(f"RDE {name}: could not delete")


def _purge_orphan_cse_tokens(api: VCD, inv: Inventory):
    """Find cse-* tokens whose cluster name no longer exists as a RDE in this org.
    Useful for cleaning up after many failed cluster attempts."""
    head(f"Orphan CSE tokens cleanup in '{inv.org_name}'")
    clusters = find_k8s_clusters_in_org(api, inv.org_urn)
    live_names = {ent.get('name') for _, ent in clusters}
    org_users = {u.get('name') for u in inv.users}
    if not org_users:
        warn("user list for this org is empty — cse-* tokens with ANY owner will be considered")
    candidates = []
    try:
        for v in api.list_all('tokens'):
            tn = (v.get('name') or '')
            own = (v.get('owner') or {}).get('name') or ''
            if not tn.startswith('cse-'): continue
            # If we have an org user list, restrict to those owners.
            # If empty, accept all cse-* tokens (the cluster-name → live-cluster check
            # below is the real safety net).
            if org_users and own not in org_users: continue
            # strip prefix "cse-" and trailing "-<digits>" to get cluster guess
            cn = tn[4:]
            if '-' in cn:
                # the timestamp suffix is purely digits, last segment
                head_, _, last = cn.rpartition('-')
                if last.isdigit():
                    cn = head_
            if cn not in live_names:
                candidates.append((tn, own, v.get('id')))
    except VCDError as e:
        dim(f"  list tokens: {e}")
    if not candidates:
        ok("no orphan CSE tokens")
        return
    print(f"  found {len(candidates)} orphan cse-* tokens:")
    for tn, own, tid in candidates[:30]:
        dim(f"    - {tn:55s} owner={own}")
    if len(candidates) > 30:
        dim(f"    ... +{len(candidates)-30} more")
    conf = input(f"delete all {len(candidates)} orphan token(s)? [y/N]: ").strip().lower()
    if conf != 'y':
        warn("aborted"); return
    n = 0
    for tn, own, tid in candidates:
        step("delete CSE token", f"{tn} owner={own}")
        try:
            api.submit('DELETE', f"{CLOUD_API}/tokens/{tid}",
                       json_api=True, label=f"del token {tn}", tolerate=(404,))
            n += 1
        except VCDError as e:
            err(f"  {tn}: {e}")
    ok(f"deleted {n} orphan token(s)")


def menu_delete_k8s_cluster(api: VCD, inv: Inventory):
    """Interactive submenu: pick one or all stuck/failed clusters in this org."""
    head(f"k8s clusters in '{inv.org_name}'")
    clusters = find_k8s_clusters_in_org(api, inv.org_urn)
    if not clusters:
        ok("no CAPVCD clusters in this org")
        # offer orphan token cleanup even if user list came back empty —
        # the owner check inside _purge_orphan_cse_tokens just becomes a no-op filter
        ans = input("scan for orphan cse-* tokens (leftovers from past failed attempts)? [y/N]: ").strip().lower()
        if ans == 'y':
            _purge_orphan_cse_tokens(api, inv)
        return
    for i, (tns, ent) in enumerate(clusters, 1):
        capvcd = ((ent.get('entity') or {}).get('status') or {}).get('capvcd') or {}
        vke_state = (((ent.get('entity') or {}).get('status') or {}).get('vcdKe') or {}).get('state','-')
        phase = capvcd.get('phase','-')
        owner = (ent.get('owner') or {}).get('name','-')
        kube_ver = ((capvcd.get('upgrade') or {}).get('current') or {}).get('kubernetesVersion','-')
        print(f"  [{i}] {ent.get('name'):25s} state={ent.get('state'):10s} phase={str(phase):14s} vcdKe={vke_state:8s} owner={owner:15s} k8s={kube_ver}")
    print(f"  [a] all of the above")
    print(f"  [q] back")
    ans = input("Pick cluster: ").strip().lower()
    if ans in ('', 'q'): return
    selected = []
    if ans == 'a':
        selected = clusters
    elif ans.isdigit() and 1 <= int(ans) <= len(clusters):
        selected = [clusters[int(ans)-1]]
    else:
        warn("invalid choice"); return
    print()
    for tns, ent in selected:
        warn(f"about to delete cluster '{ent.get('name')}'")
    conf = input(f"type 'yes' to delete {len(selected)} cluster(s): ").strip().lower()
    if conf != 'yes':
        warn("aborted"); return
    for tns, ent in selected:
        try:
            delete_one_k8s_cluster(api, tns, ent, vdcs=inv.vdcs)
        except VCDError as e:
            err(str(e))
    # After deletion, offer to scrub stray cse-* tokens left from past attempts.
    print()
    ans = input("scan for orphan cse-* tokens too? [y/N]: ").strip().lower()
    if ans == 'y':
        _purge_orphan_cse_tokens(api, inv)


def delete_rdes(api: VCD, inv: Inventory):
    head(f"Delete RDEs / k8s clusters ({len(inv.rdes)})")
    if not inv.rdes: dim("none"); return
    for tns, v in inv.rdes:
        name = v.get('name'); rid = v.get('id')
        step("delete RDE", f"{tns}: {name}")
        if _delete_rde_entity(api, rid, name):
            ok(f"RDE {name}")
            continue
        # legacy fallback path (already covered inside _delete_rde_entity but kept for symmetry)
        for url in [f"{CLOUD_API}/entities/{rid}",
                    f"{CLOUD_API}/entities/{rid}?invokeHooks=false",
                    f"{CLOUD_API}/entities/{rid}?invokeHooks=false&recursive=true"]:
            try:
                api.submit('DELETE', url, json_api=True,
                           label=f"del RDE {name}", tolerate=(404,))
                ok(f"RDE {name}")
                break
            except VCDError as e:
                dim(f"  via {url}: {e}")
        else:
            err(f"  RDE {name}: could not delete")


def delete_users_and_groups(api: VCD, inv: Inventory):
    head(f"Delete users/groups ({len(inv.users)}/{len(inv.groups)})")
    if not inv.users and not inv.groups: dim("none"); return
    for u in inv.users:
        step("delete user", u.get('name'))
        try:
            api.submit('DELETE', u['href'], label=f"del user {u.get('name')}", tolerate=(404,))
            ok(f"user {u.get('name')}")
        except VCDError as e:
            err(f"  {u.get('name')}: {e}")
    for g in inv.groups:
        step("delete group", g.get('name'))
        try:
            api.submit('DELETE', g['href'], label=f"del group {g.get('name')}", tolerate=(404,))
            ok(f"group {g.get('name')}")
        except VCDError as e:
            err(f"  {g.get('name')}: {e}")


def delete_org(api: VCD, inv: Inventory):
    head(f"Delete organization '{inv.org_name}'")
    if inv.org_enabled:
        step("disable org", inv.org_name)
        try:
            api.submit('POST', f"{inv.org_href}/action/disable",
                       label="disable org", tolerate=(403, 400))
        except VCDError as e:
            warn(f"  disable: {e}")
    step("delete org", f"{inv.org_name} (recursive&force)")
    api.submit('DELETE', f"{inv.org_href}?recursive=true&force=true",
               label="delete org")
    ok(f"organization '{inv.org_name}' deleted")

    # The script can't reach into Veeam B&R, so just remind the operator if needed.
    if inv.veeam_endpoint:
        print()
        warn("This org had a Veeam mapping. VCD is done; Veeam isn't:")
        print(f"    Veeam B&R: {inv.veeam_endpoint}")
        print(f"    Tenant:    {inv.org_name}  (urn={inv.org_urn})")
        print(f"    Go clean up the tenant on the Veeam side:")
        print(f"      - remove it from Cloud Connect / Self-Service")
        print(f"      - decide what to do with its backup repository (keep on retention or delete)")
        print(f"      - check orchestrator jobs that still reference this org")


# ---------------------------------------------------------------------------
# Interactive menu: pick org -> pick action
# ---------------------------------------------------------------------------

def ask_vms_action(inv: Inventory) -> str:
    powered = [v for v in inv.vms if is_vm_powered(v)]
    print()
    warn(f"Organization '{inv.org_name}' has live workloads:")
    print(f"  vApps={len(inv.vapps)}  VMs={len(inv.vms)}  powered={len(powered)}  k8s/RDE={len(inv.rdes)}")
    if inv.vapps:
        print(f"\n  vApps:")
        for va in inv.vapps:
            dim(f"    - {va.get('name'):30s}  status={va.get('status'):12s}  vdc={va.get('vdcName')}")
    if inv.vms:
        print(f"\n  VMs:")
        for vm in inv.vms:
            dim(f"    - {_fmt_vm(vm)}")
    if inv.rdes:
        print(f"\n  k8s/RDE:")
        for tns, rde in inv.rdes:
            dim(f"    - {rde.get('name')}  ({tns})")
    print()
    print("  [1] nothing          — leave everything alone, exit")
    print("  [2] stop-only        — stop all vApps/VMs and wait (DO NOT delete the tenant)")
    print("  [3] stop-and-delete  — stop everything and delete the whole tenant")
    ans = input("Choice [1/2/3]: ").strip()
    return {'1': 'nothing', '2': 'stop-only', '3': 'stop-and-delete'}.get(ans, '')


def quick_org_stats(api: VCD) -> list[dict]:
    """List orgs with basic counters. VM/vApp counts need a per-VDC query each, so on
    a vCD with lots of tenants this takes a few seconds. That's the trade-off."""
    orgs = api.query('organization')
    out = []
    for o in orgs:
        name = o.get('name')
        href = o.get('href')
        uuid = href.rstrip('/').split('/')[-1]
        vdcs = api.query('adminOrgVdc', f'orgName=={name}')
        n_vapps = n_vms = n_powered = 0
        for vd in vdcs:
            vn = vd.get('name')
            n_vapps += len(api.query('adminVApp', f'vdcName=={vn}'))
            for vm in api.query('adminVM', f'vdcName=={vn}'):
                n_vms += 1
                if is_vm_powered(vm): n_powered += 1
        out.append({
            'name': name, 'uuid': uuid, 'href': href,
            'enabled': o.get('isEnabled'),
            'vdcs': len(vdcs),
            'vapps': n_vapps,
            'vms': n_vms,
            'powered': n_powered,
        })
    return out


def quick_rde_stats(api: VCD, orgs: list[dict]) -> dict[str, int]:
    """One pass over every entity type, count RDEs per org."""
    counts: dict[str, int] = {o['name']: 0 for o in orgs}
    org_id_to_name = {f"urn:vcloud:org:{o['uuid']}": o['name'] for o in orgs}
    try:
        types = api.list_all('entityTypes')
    except VCDError as e:
        dim(f"entityTypes: {e}")
        return counts
    for t in types:
        tns = f"{t['vendor']}/{t['nss']}/{t['version']}"
        try:
            for v in api.list_all(f'entities/types/{tns}'):
                oid = (v.get('org') or {}).get('id')
                nm = org_id_to_name.get(oid)
                if nm: counts[nm] = counts[nm] + 1
        except VCDError: pass
    return counts


def menu_pick_org(api: VCD) -> str | None:
    info("Collecting organization list (vApps/VMs/k8s)...")
    orgs = quick_org_stats(api)
    rdes = quick_rde_stats(api, orgs)
    orgs.sort(key=lambda o: o['name'])

    print()
    head("Organizations")
    print(f"  {'#':>3}  {'NAME':25s}  {'ENB':3s}  {'VDC':>3} {'vApp':>4} {'VMs(on/all)':>13} {'k8s/RDE':>7}")
    for i, o in enumerate(orgs, 1):
        on = f"{o['powered']}/{o['vms']}"
        mark = '*' if (o['powered'] or rdes.get(o['name'], 0) or o['vapps']) else ' '
        print(f"  {i:>3}{mark} {o['name']:25s}  {('on' if o['enabled'] else 'off'):3s}  "
              f"{o['vdcs']:>3} {o['vapps']:>4} {on:>13} {rdes.get(o['name'],0):>7}")
    print(f"\n  q  quit")
    while True:
        ans = input("Pick org: ").strip().lower()
        if ans in ('q', 'quit', 'exit', ''): return None
        if ans.isdigit() and 1 <= int(ans) <= len(orgs):
            return orgs[int(ans)-1]['name']
        warn("Invalid choice")


def menu_pick_action(org_name: str) -> str | None:
    print()
    head(f"Actions for '{org_name}'")
    print("  [1] inventory     — show resources (dry-run)")
    print("  [2] disable       — block the organization (do not delete)")
    print("  [3] stop-only     — stop vApps/VMs (do not delete)")
    print("  [4] delete        — delete the organization and all of its resources")
    print("  [5] delete-k8s    — remove a stuck CAPVCD cluster (RDE + vApp + CSE tokens)")
    print("  [q] back")
    ans = input("Choice [1-5/q]: ").strip().lower()
    return {'1': 'inventory', '2': 'disable',
            '3': 'stop-only', '4': 'delete',
            '5': 'delete-k8s'}.get(ans)


def menu_disable_org(api: VCD, inv: Inventory):
    head(f"Disable organization '{inv.org_name}'")
    if not inv.org_enabled:
        ok("already disabled")
        return
    try:
        api.submit('POST', f"{inv.org_href}/action/disable", label="disable org", tolerate=(403, 400))
        ok(f"organization '{inv.org_name}' is now disabled")
    except VCDError as e:
        err(f"disable: {e}")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def run_action(api: VCD, org_name: str, action: str, args):
    """Do the picked action on an org. action ∈ {inventory, disable, stop-only, delete}."""
    inv = discover(api, org_name)
    print_inventory(inv)

    if action == 'inventory':
        ok("inventory shown (action=inventory)")
        return
    if action == 'disable':
        if not args.yes:
            conf = input(f"Disable '{org_name}'? [y/N]: ").strip().lower()
            if conf != 'y': return
        menu_disable_org(api, inv)
        return
    if action == 'delete-k8s':
        menu_delete_k8s_cluster(api, inv)
        return

    has_vms = bool(inv.vapps) or bool(inv.vms) or bool(inv.rdes)
    mode = action
    # If you ask to delete an org with workloads inside, you get the second prompt
    # so you can stop and bail. Unless --on-vms was passed, in which case respect that.
    if action == 'delete' and has_vms and args.on_vms == 'ask':
        sub = ask_vms_action(inv)
        if not sub: err("Unrecognised choice"); return
        if sub == 'nothing': warn("exiting without changes"); return
        mode = sub  # 'stop-only' or 'stop-and-delete'
    elif action == 'delete':
        mode = 'stop-and-delete' if args.on_vms in ('stop-and-delete','ask') else args.on_vms
        if has_vms and mode == 'nothing': warn("exiting (nothing)"); return

    if mode == 'delete': mode = 'stop-and-delete'
    will_delete = (mode == 'stop-and-delete')

    if will_delete and not args.yes:
        print()
        warn(f"About to delete organization '{org_name}' and ALL of its resources.")
        conf = input("Type the organization name to confirm: ").strip()
        if conf != org_name:
            err("confirmation did not match"); return

    # --- shutdown phase ---
    if has_vms:
        shutdown_vapps(api, inv)
    if mode == 'stop-only':
        ok("vApps/VMs stopped — tenant preserved.")
        return

    # Reread vApps/VMs — their status (and the deployed flag) just changed.
    inv.vapps = []; inv.vms = []
    for vdc in inv.vdcs:
        vname = vdc.get('name')
        inv.vapps += api.query('adminVApp', f'vdcName=={vname}')
        inv.vms   += api.query('adminVM',   f'vdcName=={vname}')

    # --- full deletion ---
    delete_vapps(api, inv)
    delete_disks(api, inv)
    delete_catalogs(api, inv)
    delete_networks(api, inv)
    delete_edges(api, inv)
    delete_fw_groups(api, inv)
    delete_vdc_groups(api, inv)
    delete_app_port_profiles(api, inv)
    delete_certs(api, inv)
    disable_and_delete_vdcs(api, inv, keep_vdc=args.keep_vdc)
    delete_rdes(api, inv)
    delete_users_and_groups(api, inv)

    if args.keep_org:
        ok("done (--keep-org)")
        return

    # Rediscover and sweep anything that came back: firewall groups created by
    # edge teardown side effects, late-arriving RDEs, etc.
    inv2 = discover(api, org_name)
    if inv2.fw_groups:
        warn(f"{len(inv2.fw_groups)} firewall groups left — sweeping")
        delete_fw_groups(api, inv2)
    if inv2.rdes:
        warn(f"{len(inv2.rdes)} RDEs left — sweeping")
        delete_rdes(api, inv2)

    delete_org(api, inv)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('org', nargs='?', help='organization name; if omitted, the interactive menu is shown')
    ap.add_argument('--action', choices=['inventory','disable','stop-only','delete','delete-k8s'], default='delete',
                    help='action to perform (used in CLI mode)')
    ap.add_argument('--config', help='path to the INI credentials file (see nuke_vcd.conf.example)')
    ap.add_argument('--dry-run', action='store_true', help='inventory only — do not change anything')
    ap.add_argument('--yes', '-y', action='store_true', help='skip confirmation prompts')
    ap.add_argument('--on-vms', choices=['ask','nothing','stop-only','stop-and-delete'], default='ask',
                    help='what to do when vApps/VMs are present')
    ap.add_argument('--keep-vdc', action='store_true', help='clean VDC contents but keep the VDC itself')
    ap.add_argument('--keep-org', action='store_true', help='delete resources but keep the org')
    ap.add_argument('--verbose', '-v', action='store_true', help='verbose log — every HTTP request')
    ap.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = ap.parse_args()
    VERBOSE = args.verbose

    cfg = load_config(args.config)
    apply_config(cfg)
    if cfg.get('__path'):
        info(f"config: {cfg['__path']}")

    api = VCD()
    info(f"nuke_vcd_tenant.py v{__version__}")
    info(f"Login {VCD_URL} as {USER}@system  (api={API_VERSION})")
    api.login()
    ok("logged in")

    # CLI mode: org name given on the command line.
    if args.org:
        action = 'inventory' if args.dry_run else args.action
        run_action(api, args.org, action, args)
        return

    # Menu mode. Outer loop = org list. Inner loop stays on the picked org until
    # the user backs out (q) or the org gets deleted out from under us.
    while True:
        org_name = menu_pick_org(api)
        if not org_name: break
        while True:
            action = menu_pick_action(org_name)
            if not action: break  # 'q' or junk input — back to the org list
            try:
                run_action(api, org_name, action, args)
            except VCDError as e:
                err(str(e))
            # If the org is gone, no point asking what else to do with it.
            still = api.query('organization', f'name=={org_name}')
            if not still:
                ok(f"org '{org_name}' no longer exists — back to org list")
                break
            print()


if __name__ == '__main__':
    try:
        main()
    except VCDError as e:
        err(str(e)); sys.exit(2)
    except KeyboardInterrupt:
        warn("interrupted (Ctrl-C)"); sys.exit(130)
