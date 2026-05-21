# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [SemVer](https://semver.org/).

## [1.1.1] — 2026-05-21

### Fixed

- TLS handshake against VCD on Python 3.14 / OpenSSL 3.x where the default
  security level (SECLEVEL=2) is stricter than the cipher set VCD cells
  advertise, causing `SSLV3_ALERT_HANDSHAKE_FAILURE` or
  `SSL UNEXPECTED_EOF_WHILE_READING` on every API call.
  Now the requests session uses an `ssl.SSLContext` with `SECLEVEL=0`, which
  matches what curl/Firefox negotiate. No effect on Python 3.12 or older.

## [1.1.0] — 2026-05-21

### Added

- **`delete-k8s` action** — selectively remove a stuck CAPVCD k8s cluster from
  an org without nuking the entire tenant. Use this when the Kubernetes
  Container Clusters plugin leaves a half-created cluster you can't delete
  from the tenant UI (e.g. because the RDE owner is `system`).
  Cleans up, in order:
  1. The vApp matching the cluster name (powerOff + undeploy + delete).
  2. All `cse-<clusterName>-*` API access tokens.
  3. The CAPVCD RDE entity itself.
- **Orphan CSE token sweep** — after a `delete-k8s` run, offers to scan the
  org for stray `cse-*` tokens whose target cluster no longer exists.
  Lets you wipe the build-up from a long history of failed cluster attempts.
- **`--version` flag** and a version banner printed at login.

### Changed

- User and group discovery now uses CloudAPI (`/cloudapi/1.0.0/users`,
  `/cloudapi/1.0.0/groups`) with `orgEntityRef.id` filter, falling back to
  the legacy admin org body. Older API versions sometimes returned empty
  `users` sections in the admin org body, so this populates the inventory
  reliably across VCD 10.4 / 10.5 / 10.6.
- Token cleanup inside `delete-k8s` no longer skips tokens whose owner
  differs from the RDE owner — that mismatch is exactly the failure mode
  we're cleaning up. The mismatch is logged as a warning instead.

### Fixed

- `api.query()` now paginates the legacy `/api/query` endpoint, which VCD
  defaults to 25 records per page. Before this, environments with more
  than 25 orgs (or more than 25 of anything) silently truncated the menu.
- VM display in inventory no longer shows the ESXi `hostName` — that field
  is the hypervisor host the VM runs on, not the guest OS hostname, and
  showing it confused operators.

## [1.0.0] — 2026-05-20

Initial release.

### Added

- Interactive menu listing every org with vApp/VM/k8s counters.
- CLI mode for automation.
- Actions: `inventory`, `disable`, `stop-only`, `delete`.
- Workload-aware sub-menu when deleting an org with live vApps/VMs/RDEs.
- Full tenant teardown via REST API only — vApps, catalogs, networks, edges
  (including LB virtual services, pools, and SEG assignments), firewall
  groups, VDC groups (with DFW), application port profiles, certificates,
  Org VDCs, RDEs (CAPVCD / kube extensions), users, groups, organization.
- Veeam Backup & Replication integration detection via org metadata.
- INI-file based credentials with env var overrides.
- Bash launcher with optional venv auto-setup.

[1.1.1]: https://github.com/Alex-Electron/VMware-Cloud-Director/releases/tag/v1.1.1
[1.1.0]: https://github.com/Alex-Electron/VMware-Cloud-Director/releases/tag/v1.1.0
[1.0.0]: https://github.com/Alex-Electron/VMware-Cloud-Director/releases/tag/v1.0.0
