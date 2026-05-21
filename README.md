# VCD Tenant Nuker

By Alexander Lavrinovich.

A script that deletes a VMware Cloud Director organization properly — through
the REST API, in the right dependency order, without going anywhere near the
database.

The old way of dealing with a stuck VCD tenant was to delete rows from Postgres
by hand. That worked, but it left phantom state, broke audit trails, and felt
wrong every time. This script does it through the API: it discovers everything
the tenant owns, tears it down in the right order, and tells you honestly when
something won't go.

## What it does

- Lists every org with counters: VDCs, vApps, VMs (running/total), k8s clusters / RDEs.
- Lets you pick an action per org: show inventory, disable, stop everything,
  delete, or **delete a single stuck k8s cluster** (CSE/CAPVCD) without nuking the org.
- If the org has live workloads when you pick **delete**, asks again:
  1. leave it alone
  2. stop everything and wait
  3. stop everything and delete
- On full delete, walks the dependency tree in order: vApps → catalogs → networks →
  edges (with all LB cleanup) → firewall groups → VDC groups → app port profiles →
  certs → VDCs → RDEs → users → org.
- For a single k8s cluster cleanup: deletes the vApp the cluster created, all
  `cse-<clusterName>-*` access tokens, and the CAPVCD RDE itself. Optionally
  sweeps orphan `cse-*` tokens left over from past failed attempts.
- Detects Veeam integration via org metadata and reminds you to clean the tenant
  out of Veeam B&R after VCD is done (the script can't touch Veeam itself).
- Has a `-v` flag that prints every HTTP request, which is what you want when
  something fails for a weird reason.

## What it does not do

- Touch the database. Ever. If the API can't delete a thing, the script stops
  and tells you why. No `expect` scripts, no SSH into the VCD node, no `psql`.
- Clean up Veeam backups. Those live on a separate server. The script just
  surfaces the endpoint URL so you know where to go next.
- Touch your CMP, billing, monitoring, or anything else outside VCD.

## Requirements

- Python 3.10+
- `requests` and `urllib3` (the bash launcher installs them in a venv if missing)
- VCD system administrator credentials
- API version 37.0+ (VCD 10.4 and up). Older versions might work but I haven't tried.

## Install

```bash
git clone https://github.com/Alex-Electron/VMware-Cloud-Director.git
cd VMware-Cloud-Director
cp nuke_vcd.conf.example nuke_vcd.conf
chmod 600 nuke_vcd.conf
$EDITOR nuke_vcd.conf
chmod +x nuke-vcd.sh
```

Then either:

```bash
./nuke-vcd.sh             # bash launcher (handles venv if needed)
python3 nuke_vcd_tenant.py
```

Both work the same.

## Config

`nuke_vcd.conf` is plain INI:

```ini
[vcd]
base         = https://vcd.example.com
user         = administrator
password     = your_password
api_version  = 37.0
```

The script looks for it here, in order:

1. `--config /path` flag
2. `$VCD_CONFIG` env var
3. `./nuke_vcd.conf` next to the script or in the working dir
4. `~/.nuke_vcd.conf`

Env vars override the file:

| Variable          | What                                  |
|-------------------|---------------------------------------|
| `VCD_BASE`        | VCD URL, no trailing `/api`           |
| `VCD_USER`        | System admin user, without `@system`  |
| `VCD_PASS`        | Password                              |
| `VCD_API_VERSION` | API version, default `37.0`           |
| `VCD_CONFIG`      | Path to INI file                      |

## Usage

### Menu (no arguments)

```bash
./nuke-vcd.sh
```

You get something like this:

```
=== Organizations ===
    #  NAME                       ENB  VDC vApp   VMs(on/all) k8s/RDE
    1  SC-Edu                     on     0    0           0/0       0
   ...
   29* sc-pwerner                 off    1    3          0/10       0
   ...

  q  quit
Pick org:
```

A `*` next to the name means the org has active workloads. Pick one and you
stay on its action menu until you back out or delete it:

```
=== Actions for 'sc-pwerner' ===
  [1] inventory     — show resources (dry-run)
  [2] disable       — block the organization (do not delete)
  [3] stop-only     — stop vApps/VMs (do not delete)
  [4] delete        — delete the organization and all of its resources
  [5] delete-k8s    — remove a stuck CAPVCD cluster (RDE + vApp + CSE tokens)
  [q] back
```

If you pick **delete** on an org that has workloads, you get the safety prompt
with the actual VM list:

```
[!] Organization 'sc-pwerner' has live workloads:
  vApps=3  VMs=10  powered=0  k8s/RDE=0

  vApps:
    - app1                            status=POWERED_OFF   vdc=sc-pwerner-ovdc-01
    ...

  VMs:
    - [off] vm-master                    vApp=app1  ip=10.1.0.10
    ...

  [1] nothing          — leave everything alone, exit
  [2] stop-only        — stop all vApps/VMs and wait (DO NOT delete the tenant)
  [3] stop-and-delete  — stop everything and delete the whole tenant
```

And then one more prompt asks you to type the org name to confirm.

### CLI

```bash
./nuke-vcd.sh sc-foo --dry-run                            # inventory only
./nuke-vcd.sh sc-foo --action disable                     # block it
./nuke-vcd.sh sc-foo --action stop-only                   # stop workloads, keep tenant
./nuke-vcd.sh sc-foo --action delete                      # delete, with prompts
./nuke-vcd.sh sc-foo --action delete --yes --on-vms stop-and-delete   # no prompts
./nuke-vcd.sh sc-foo --action delete-k8s                  # selective k8s cluster cleanup
./nuke-vcd.sh sc-foo --action delete -v                   # verbose HTTP log
./nuke-vcd.sh --version
```

#### Selective k8s cluster cleanup (`delete-k8s`)

For when the Kubernetes Container Clusters plugin leaves a half-created CAPVCD
cluster you can't delete from the tenant UI — typically because the cluster
RDE owner is `system` (you created it from the provider portal) and the tenant
user has no rights to it. The script enumerates all CAPVCD clusters in the org,
lets you pick one (or all), then deletes:

1. The vApp matching the cluster name (if it exists), powering it off and
   undeploying first.
2. Every `cse-<clusterName>-*` API access token in VCD.
3. The CAPVCD RDE itself.

After the cluster is gone, it offers to scan the org for **orphan `cse-*` tokens**
— leftover access tokens whose target cluster no longer exists. Pile of these
builds up over time if you've tried provisioning many clusters and a few failed.

### Flags

| Flag               | What it does                                              |
|--------------------|-----------------------------------------------------------|
| `org`              | Org name. Without it you get the menu.                    |
| `--action`         | `inventory` / `disable` / `stop-only` / `delete` / `delete-k8s`. Default `delete`. |
| `--dry-run`        | Inventory only. Same as `--action inventory`.             |
| `--yes`, `-y`      | Skip confirmation prompts.                                |
| `--on-vms`         | `ask` (default), `nothing`, `stop-only`, `stop-and-delete`. |
| `--keep-vdc`       | Clean the VDC but don't delete it.                        |
| `--keep-org`       | Delete everything inside, keep the org itself.            |
| `--config`         | Path to the INI file.                                     |
| `--verbose`, `-v`  | Print every HTTP request.                                 |
| `--version`        | Print the script version and exit.                        |

## What gets deleted, in what order

Each step waits for its task to finish before the next one starts.

1. **vApps** — `powerOff` then `undeploy` with the right XML body. Skipping
   this is the most common reason a vApp delete fails with "Stop the vApp and try again".
2. **vApps deleted.**
3. **Independent disks** that aren't attached to any VM.
4. **Catalogs** — every vApp template and media item inside the catalog goes first,
   otherwise the catalog delete returns 409. Published catalogs are unpublished first.
5. **Org VDC networks** — once vApps are gone, vApp-network refs go with them and
   the routed network can finally be deleted.
6. **Edge Gateways**. Before deleting the edge itself, the script:
   - clears user firewall rules
   - deletes NAT rules
   - deletes IPSec tunnels
   - deletes static routes
   - disables DHCP forwarder
   - deletes every LB Virtual Service, LB Pool, and Service Engine Group assignment
     (skip this and edge delete returns 403 "load balancer services enabled")
7. **Firewall Groups** — the "phantom" objects. These used to be the reason people
   touched the database. They survive after edges and VDC groups and block the
   final `delete org`.
8. **VDC Groups** — clear DFW rules, drop user-defined DFW policies, delete the group.
9. **Application Port Profiles** (tenant scope only).
10. **Certificate Library / Trusted Certificates** scoped to the org.
11. **Org VDCs** — disable, then `DELETE ?recursive=true&force=true`.
12. **RDEs (Runtime Defined Entities)** — CAPVCD k8s clusters, kube extensions, anything
    custom. These reference `org_member` rows and otherwise give you a foreign key
    violation on the very last step.
13. **Users and groups** — fetched from the admin org body, since the `user` query
    has no org filter in this API version.
14. **Sweep pass.** Rediscovers the org and removes any firewall groups or RDEs
    that came back or got missed.
15. **Organization** — disable, then `DELETE ?recursive=true&force=true`.

## Errors you'll probably see

`You must delete this Organization's Firewall Groups before you can delete the organization.`
This was the original reason for writing the script. The new version finds
firewall groups owned by every edge gateway and VDC group and removes them
before the final delete. If you still see it, run with `-v` and look for which
firewall groups are left after the sweep — probably a new owner type the script
doesn't enumerate yet.

`Edge gateway has load balancer services enabled.`
LB cleanup has to go in order: virtual services, then pools, then SEG
assignments. And the listing endpoints are scoped per edge
(`edgeGateways/{id}/loadBalancer/virtualServiceSummaries`), not flat.
If the script gets this error after running, the LB is probably in a half-broken
state — check what's actually under each summary endpoint.

`Could not execute JDBC batch update ... violates foreign key constraint "fk_custom_entity2org_member" on table "defined_entity"`
That's an RDE (typically a CAPVCD cluster or a kube extension) that references
a tenant user. The script finds every RDE whose `org.id` matches the target
org and deletes them before the final `delete org`. If you see this on a real
run with the current code, it means a new entity type appeared.

`Unsupported query: adminVApp.orgName`
Older code used `filter=orgName==...` for vApp/VM/disk queries. That filter
field doesn't exist in API 37.0. The script now iterates by VDC name instead.

Menu only shows 25 orgs.
Pagination bug. Fixed — `api.query()` now walks every page. Pull the latest
version if you see this.

## Layout

```
.
├── nuke_vcd_tenant.py    # all the logic
├── nuke-vcd.sh           # bash launcher (venv setup + config + exec)
├── nuke_vcd.conf.example # credentials template
└── README.md             # this file
```

## Tested on

- VCD 10.4, API 37.0
- NSX-T edge gateways. Legacy NSX-V is **not** covered — if you still have those,
  the `cleanup_edge` function will need new code.
- CAPVCD k8s clusters (`vmware/capvcdCluster/1.3.0`)
- Veeam Backup & Replication 12 (detection only)

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the version history.

## Contributing

If you hit something this script doesn't handle — a new resource type, a
weird API quirk, an edge case in a VCD version I haven't tested — please
[open an issue](https://github.com/Alex-Electron/VMware-Cloud-Director/issues/new/choose).
Bug reports and feature requests both go through the same place; pick the
template that fits.

PRs are very welcome. For anything bigger than a typo or a one-liner, please
open an issue first so we don't end up duplicating work. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the short version of the conventions.

## Author

Alexander Lavrinovich — [github.com/Alex-Electron](https://github.com/Alex-Electron)

## License

MIT. See [LICENSE](LICENSE).

## A warning

The script asks you to type the org name verbatim before deleting it. Use
`--yes` in automation only when you've already validated the org name in your
caller. There is no undo — VCD doesn't have one.
