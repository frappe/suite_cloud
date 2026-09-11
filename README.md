# Suite Cloud

Suite Cloud is the app that runs the mail servers behind [Frappe Suite](https://github.com/frappe/suite).
It sets up [Stalwart](https://stalw.art) mail clusters, keeps them configured, and lets many Suite
sites share one cluster safely.

## How it works, in short

- **Frappe Cloud** tells Suite Cloud when a new Suite site is created. Suite Cloud picks a cluster
  for the site and hands back the mail server URL plus a site key and secret.
- **A Suite site** never talks to the mail server's admin API itself. When it wants to add a
  domain, create a mailbox, change a password and so on, it asks Suite Cloud. Suite Cloud checks
  that the site owns what it is touching, saves the change in its own records, and pushes it to
  the cluster in the same step.
- **End users** (the people reading and sending mail) connect straight to the cluster with their
  own app passwords. Suite Cloud is not in that path.
- **Operators** use the desk to create clusters, add servers, manage DNS and watch jobs.

```
Frappe Cloud ──────────────▶ Suite Cloud ◀────────────── Suite site (admin changes)
                                  │
                                  │ pushes configuration and directory changes
                                  ▼
                           Stalwart cluster ◀──────────── end users (mail clients)
                                  ▲
Suite Cloud ── SSH + Ansible ─────┘ (installs and updates the servers)
Suite Cloud ── DNS provider API ──▶ publishes records in each DNS Zone
Suite Cloud ── public resolvers ──▶ checks the records a customer domain must carry
```

The app has two modules, so one CODEOWNERS line can cover each product:

| Module | Folder | Holds |
| --- | --- | --- |
| **Suite Cloud** | `suite_cloud/suite_cloud` | What every hosted product shares: DNS Zone, DNS Record, Server Job, Suite Site, Suite Cloud Settings, plus `provisioning` (Ansible over SSH) and `dns` (provider client, resolver). |
| **Cloud Mail** | `suite_cloud/cloud_mail` | The mail product: Stalwart Store, Cluster, Node, Egress Gateway and IP Pool, the mail directory (Mail Domain, Mail Account, Mail Group, Mailing List), and the `stalwart`, `cluster` and `tenancy` packages. |

The site-facing API is split the same way: `suite_cloud.api.site` for what any product needs
(authentication, `ping`, the site profile) and `suite_cloud.api.mail` for the mail directory.

## The pieces

| Record | What it is |
| --- | --- |
| **DNS Zone** | A domain you own (for example `frappemail.com`) plus the login for the DNS provider that manages it. Suite Cloud publishes server records there. You can have several zones; each cluster picks one. |
| **Stalwart Store** | Where a cluster keeps its data: a PostgreSQL or MySQL database, an S3 bucket for message bodies, Redis for shared memory, and optionally a search engine. |
| **Stalwart Cluster** | One mail service with its public hostname (`mail.c1.frappemail.com`), its stores, its admin credentials, an SSH key for its servers and the regions Frappe Cloud may place sites from. |
| **Stalwart Node** | One server (VPS) that runs Stalwart for a cluster. Nodes share the same stores, so any node can serve any user. |
| **Egress Gateway** and **Egress IP Pool** | Optional extra servers used only for sending. A pool is a set of IP addresses; a domain, a site or a whole cluster can be told to send through it. |
| **DNS Record** | One record Suite Cloud keeps in a zone (a node's address, the cluster's round-robin entry, the SPF list, and so on). Records are created and removed together with the thing that needs them. |
| **Server Job** | One run of an install or update script on a node or gateway, with the result of every step. Secrets are looked up when the job runs and never stored in it. |
| **Suite Site** | A Frappe Suite site: which cluster it uses, its API key and secret, its title and contact email, its limits (domains, accounts, groups, lists, total disk) and the token its domains are verified with. |
| **Mail Domain, Mail Account, Mail Group, Mailing List** | The mail directory of a site. Saving one of these pushes it to the cluster in the same step, so the records and the cluster never drift apart. Each carries the id Stalwart gave it. |
| **Suite Cloud Settings** | Defaults: DNS time-to-live, Stalwart version and download URLs, certificate settings, the public URL of this Suite Cloud, job timeout. |

## Install

```sh
bench get-app https://github.com/frappe/suite_cloud
bench --site yoursite install-app suite_cloud
```

The bench host needs `ansible` (`apt install ansible`; Frappe Cloud installs it from
`pyproject.toml`) and the `community.general` Ansible collection, which the firewall steps use.

Installing creates three roles and one user:

- **Suite Cloud Manager**: operators who use the desk.
- **Suite Site** and **Frappe Cloud**: API-only roles with no access to any record. The endpoints
  check ownership themselves.
- A service user that every site request runs as.

## Setting up a cluster, step by step

1. **Settings.** Enter the email address that should receive certificate notices. While testing,
   switch the ACME directory to the Let's Encrypt staging URL so you do not hit rate limits.
2. **DNS Zone.** Create a zone for the domain your servers will live under (`frappemail.com`) and
   enter the DNS provider credentials for it. Mark it as the default. Add more zones later if some
   clusters should live under a different domain.
3. **Stores.** Create a PostgreSQL (or MySQL) data store, an S3 blob store and a Redis in-memory
   store. Redis also coordinates the nodes. RocksDB and local files only work for a single-node
   cluster.
4. **Cluster.** Create a Stalwart Cluster. Give it a short label, or leave it blank to get `c1`,
   `c2` and so on. The label and the zone make the hostname, `mail.c1.frappemail.com`, and the
   default domain `c1.frappemail.com` that nodes, gateways and pools get their names under. The
   label is only a namespace: a cluster may have servers in several regions. List the regions
   Frappe Cloud may place sites from, or none to serve every region. Copy the SSH public key.
5. **First node.** Put that public key on a fresh VPS and set the reverse DNS (PTR) of its IP to the
   node's hostname. Create a Stalwart Node (`n1.c1.frappemail.com` with its IPv4), click
   **Verify SSH**, then **Provision**. The job installs Stalwart and its CLI, sets up the system
   service and firewall, starts Stalwart once to write the store settings, applies the cluster
   configuration (roles, coordinator, certificate provider, wildcard certificate, system settings),
   and restarts it normally with the temporary admin credential removed.
6. **Finish bootstrap.** Once the certificate is issued, **Finish Bootstrap** (also tried every
   five minutes) creates the management API key and marks the cluster **Active**. The cluster
   hostname now points at this node.
7. **More nodes.** Create and provision them the same way. They only receive the database
   connection; everything else is read from the shared database. A node is added to the cluster
   hostname's DNS as soon as Stalwart reports it active.

Day-to-day buttons: **Drain** takes a node out of DNS before maintenance, **Upgrade Nodes** rolls a
new Stalwart version out one node at a time, **Sync Config** pushes the generated configuration
again, and **Check Drift** reports what differs from it without changing anything.

### Sending through dedicated IPs

1. Create an Egress Gateway (`out1.c1.frappemail.com`) on a VPS that already has the extra public
   IPs configured, and provision it.
2. Create an Egress IP Pool listing those IPs with a hostname for each (`ded1.c1.frappemail.com`,
   with reverse DNS set at the provider).
3. Assign the pool to a Mail Domain, to a Suite Site, or as the cluster default.

Mail from the matching sender domains is relayed to the gateway over an authenticated, encrypted
connection and leaves from the pool's addresses. The `spf.c1.frappemail.com` record that customer
domains include always lists every node and pool address, so SPF stays valid whichever path a
message takes.

## API for Frappe Cloud

Call these with the API key and secret of a user that has the **Frappe Cloud** role.

| Method | What it does |
| --- | --- |
| `suite_cloud.api.fc.create_site(site, cluster=None, region=None, fc_reference=None, title=None, contact_email=None, max_domains=None, max_accounts=None, max_groups=None, max_mailing_lists=None, max_disk_gb=None, default_disk_quota_gb=None)` | Registers a site, choosing a cluster by its hostname, by a region it serves (default cluster preferred), or the default; a cluster with no regions serves any region. Returns the mail server URL, the Suite Cloud URL and the site's key and secret. The secret is shown only this once. |
| `get_site(site)` | Status, cluster, title, contact email, limits and current usage. |
| `update_site(site, title=None, contact_email=None, max_domains=None, ...)` | Changes the display name, the address for site-specific notices, or the limits. Omitted fields stay as they are. |
| `rotate_site_secret(site)` | Issues a new secret, shown once. |
| `suspend_site(site)`, `resume_site(site)`, `archive_site(site, delete_data=False)` | Turns a site off, back on, or retires it. |

## API for Suite sites

The site sends two headers: `Authorization: token <api_key>:<api_secret>` and
`Frappe-Authorization-Source: Suite Site`. Frappe's own API-key authentication resolves the key
against the Suite Site record, so the request runs as the shared site service user.

Rules that apply everywhere:

- Anything that belongs to another site is reported as "not found", never as "forbidden".
- If the cluster refuses a change, the site gets HTTP 422 with Stalwart's error type.
- Each site may make 300 requests per minute.
- Errors carry Frappe's `exc_type`, so a client can tell a duplicate (`DuplicateEntryError`)
  from a limit (`ValidationError`), a rate limit (`TooManyRequestsError`), a missing object
  (`DoesNotExistError`) or a suspended site (`SiteSuspendedError`).

| Module | Methods |
| --- | --- |
| `suite_cloud.api.site` | `ping`, `update_site_profile(title, contact_email)` |
| `suite_cloud.api.mail.domains` | `check_domain`, `list_domains`, `get_domain`, `create_domain`, `update_domain`, `delete_domain`, `get_dns_records`, `refresh_dns_records`, `verify_dns_records` |
| `suite_cloud.api.mail.accounts` | `list_accounts(domain, search, start, limit)`, `get_account`, `get_quotas(emails)`, `create_account`, `update_account`, `set_account_enabled`, `set_password`, `rotate_app_password`, `create_app_password`, `set_aliases`, `add_alias`, `remove_alias`, `set_alias_enabled`, `set_groups`, `delete_account` |
| `suite_cloud.api.mail.groups` | `list_groups(search, start, limit)`, `get_group`, `create_group`, `update_group`, `set_group_aliases`, `add_group_alias`, `remove_group_alias`, `set_group_alias_enabled`, `set_group_members`, `delete_group` |
| `suite_cloud.api.mail.mailing_lists` | `list_mailing_lists(search, start, limit)`, `get_mailing_list`, `create_mailing_list`, `update_mailing_list`, `set_mailing_list_aliases`, `add_mailing_list_alias`, `remove_mailing_list_alias`, `set_mailing_list_alias_enabled`, `list_recipients`, `add_recipients`, `remove_recipients`, `set_recipients`, `delete_mailing_list` |
| `suite_cloud.api.mail.meta` | `get_account_options` (the locales and time zones a mailbox can use) |

Listings answer `{"items": [...], "total": n}`. `search` matches the address, and the display
name or description; `limit` is capped (200 accounts, 500 groups or lists per page). Account and
group rows carry `used_disk_bytes`, fetched for the whole page in one cluster call
(`x:Account/get` in batches of the session's `maxObjectsInGet`), and `get_quotas` answers the
allotment and usage for up to 500 addresses at once. A single `get_account` or `get_group` asks
the cluster for its usage as well.

Every account and group has a disk quota above 0 GB, defaulting to the site's default quota. A
site may carry a total disk quota; when it does, the quotas of its accounts and groups together may
not exceed it, and a create or quota increase beyond the remaining room is refused. Domains,
accounts, groups and lists are counted against the site's other limits the same way.

Every quota an account or group has is a Mail Quota row: Stalwart's `StorageQuota` names with a
limit. `maxDiskQuota` (bytes) is the one row every account and group must have; it defaults to the
site's default quota and is what the site's total is checked against. The others (messages,
mailboxes, Sieve scripts, calendars, contact cards, app passwords and so on) are optional counts.
The API shows them as `quotas`, an object of name to limit, next to `disk_quota_gb`, the disk row
in GB. `create_account`, `update_account`, `create_group` and `update_group` take both: `quotas`
replaces the optional rows (`{}` lifts them all) and keeps the disk row unless it names
`maxDiskQuota`; `disk_quota_gb` sets the disk row in GB. `get_account_options` lists the optional
names the cluster accepts.

`create_account` needs a password of at least 8 characters and returns, once, an app password
minted for the account; the site uses it for that account's mail access. `rotate_app_password`
issues a new one and revokes the old one. Suite Cloud keeps the app password encrypted on the Mail
Account and never keeps the account's password. An API key (Bearer token) for the account exists
only when an operator creates one from the Mail Account form.

Aliases can be replaced as a set (`set_aliases`) or changed one at a time (`add_alias`,
`remove_alias`, `set_alias_enabled`). The one-at-a-time calls lock the parent row for the change,
so two admins editing the same account never drop each other's rows; the dashboard uses those.

Recipients of a mailing list are separate documents, not rows on the list, so a list can hold
hundreds of thousands of addresses. `list_recipients` pages through them, `add_recipients` and
`remove_recipients` change them in batches of up to 5000 and push only the changed addresses to
the cluster, and `set_recipients` replaces the whole list, which is meant for small lists.

`update_site_profile` lets the site publish its workspace name as the Suite Site title and a
contact email for site-specific notices; the Suite app sends both whenever Suite Settings change.

### How a domain goes live

1. The site calls `check_domain`. Nothing is stored yet: the answer is a TXT record the domain
   owner must publish at the apex, `frappe-suite-verification=<token>`. The token is fixed for the
   life of the site, so every domain it adds asks for the same record and no other site can
   produce it. This is what keeps one tenant from claiming another tenant's domain on a shared
   cluster. Operators adding a domain from the desk vouch for it themselves and skip this step.
2. Once the record resolves, `create_domain` creates the Mail Domain and the domain on the
   cluster, disabled.
3. `get_dns_records` lists what the owner must publish next: an MX record pointing at the
   cluster, an SPF record of the form `v=spf1 include:spf.<zone> -all`, the DKIM keys Stalwart
   generates and rotates, and DMARC and TLS reporting records. The rows come from Stalwart's
   `dnsZoneFile`, with SPF rewritten so egress gateways are covered.
4. The owner publishes them. `verify_dns_records` checks on public resolvers; once SPF, DMARC and
   at least one DKIM selector resolve, the domain is verified. MX is optional: a domain may use the
   cluster for sending only.
5. A domain is **active** when it is enabled and verified; only then does Stalwart accept mail for
   it, and only then may the site create accounts, groups and lists on it. Disabling a domain drops
   its verification on purpose, so enabling it again needs a fresh check.
6. Verification is retried every hour, and rotated DKIM selectors are picked up hourly for verified
   domains and daily for all. A temporary DNS failure never turns a working domain off.

A domain carries three delivery settings a site may change: a catch-all address for local parts
that match no account, sub-addressing (`user+tag@`), and relaying, which makes the cluster forward
mail for addresses it does not hold to the domain's MX instead of rejecting it, so a domain can
keep some mailboxes on another server (split delivery).

Records for mail client auto-setup (SRV, autoconfig, MTA-STS) are listed only when the domain has
`publish_client_discovery_records` turned on, because the cluster has no certificate for customer
hostnames.

## Configuration

Every value in Suite Cloud Settings can also be placed in `site_config.json` under a `suite_cloud`
key. When both are set, the settings record wins.

```json
{ "suite_cloud": { "acme_contact_email": "ops@example.com", "stalwart_version": "v0.16.20" } }
```

On a development site that talks to a cluster with a staging certificate, set
`suite_cloud.verify_stalwart_tls` to `false`.

## Scheduled work

| When | What |
| --- | --- |
| Every 5 minutes | Retry failed Server Jobs; poll nodes that are provisioning or draining. |
| Hourly | Verify unverified domains; refresh DNS records of domains whose DKIM keys are rotating. |
| Daily | Verify every DNS Record in the zones; refresh every domain's records; check every cluster for configuration drift; check the PTR records of nodes and pool addresses. |

## Development

```sh
bench --site yoursite run-tests --app suite_cloud --test-category all
```

Tests live next to their module: `suite_cloud/tests` for the generic app (settings, fixtures)
and `suite_cloud/cloud_mail/tests` for mail, including the fake Stalwart
(`suite_cloud/cloud_mail/tests/fake_stalwart.py`), an in-process JMAP management server the real
client talks to unchanged. Nothing reaches a real server or a real DNS provider. Tests that create
sites use names under `.frappe.test` and clean up only those, so they can run on a site that also
holds real clusters.

CI (`.github/workflows/ci.yml`) runs one job per module folder (Core, Mail) plus Ruff, so a
future product adds a matrix entry rather than a workflow.

The bootstrap path has been run end to end against a real single-node cluster on Stalwart
v0.16.20 (DigitalOcean DNS, Let's Encrypt wildcard certificate through DNS-01). Lessons that
are now built in: Stalwart encodes every `Set` as `{"value": true}` and every `List` as an
index-keyed object; secrets are `{"@type": "Value", "secret": ...}` unions; Let's Encrypt
rejects a wildcard order that also names a host it covers; the built-in roles are provisioned
only on a normal start that finds no Role objects, so the recovery-stage plan creates none;
and bootstrap's admin password is never disclosed, so the recovery-stage plan sets it.

Still unverified on a live server: the egress gateway (relay listeners, connection strategy),
multi-node registry leases with a Redis coordinator, and node upgrades.

## License

AGPL-3.0
