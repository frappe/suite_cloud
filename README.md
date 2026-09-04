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
  the cluster.
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
```

## The pieces

| Record | What it is |
| --- | --- |
| **DNS Zone** | A domain you own (for example `frappemail.com`) plus the login for the DNS provider that manages it. Suite Cloud publishes server records there. You can have several zones; each cluster picks one. |
| **Stalwart Store** | Where a cluster keeps its data: a PostgreSQL or MySQL database, an S3 bucket for message bodies, Redis for shared memory, and optionally a search engine. |
| **Stalwart Cluster** | One mail service with its public hostname (`mail.blr.frappemail.com`), its stores, its admin credentials and an SSH key for its servers. |
| **Stalwart Node** | One server (VPS) that runs Stalwart for a cluster. Nodes share the same stores, so any node can serve any user. |
| **Egress Gateway** and **Egress IP Pool** | Optional extra servers used only for sending. A pool is a set of IP addresses; a domain, a site or a whole cluster can be told to send through it. |
| **DNS Record** | One record Suite Cloud keeps in a zone (a node's address, the cluster's round-robin entry, the SPF list, and so on). Records are created and removed together with the thing that needs them. |
| **Server Job** | One run of an install or update script on a node or gateway, with the result of every step. Secrets are looked up when the job runs and never stored in it. |
| **Suite Site** | A Frappe Suite site: which cluster it uses, its API key and secret, and its limits. |
| **Mail Domain, Mail Account, Mail Group, Mailing List** | The mail directory of a site. Saving one of these pushes it to the cluster in the same step, so the records and the cluster never drift apart. |
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
4. **Cluster.** Create a Stalwart Cluster with hostname `mail.blr.frappemail.com`. Everything the
   cluster owns gets a name directly under `blr.frappemail.com`. Copy the SSH public key shown on
   the form.
5. **First node.** Put that public key on a fresh VPS and set the reverse DNS (PTR) of its IP to the
   node's hostname. Create a Stalwart Node (`n1.blr.frappemail.com` with its IPv4), click
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

1. Create an Egress Gateway (`out1.blr.frappemail.com`) on a VPS that already has the extra public
   IPs configured, and provision it.
2. Create an Egress IP Pool listing those IPs with a hostname for each (`ded1.blr.frappemail.com`,
   with reverse DNS set at the provider).
3. Assign the pool to a Mail Domain, to a Suite Site, or as the cluster default.

Mail from the matching sender domains is relayed to the gateway over an authenticated, encrypted
connection and leaves from the pool's addresses. The `spf.blr.frappemail.com` record that customer
domains include always lists every node and pool address, so SPF stays valid whichever path a
message takes.

## API for Frappe Cloud

Call these with the API key and secret of a user that has the **Frappe Cloud** role.

| Method | What it does |
| --- | --- |
| `suite_cloud.api.fc.create_site(site, cluster=None, region=None, fc_reference=None, ...)` | Registers a site, choosing a cluster by name, by region, or the default one. Returns the mail server URL, the Suite Cloud URL and the site's key and secret. The secret is shown only this once. |
| `get_site(site)` | Status, cluster, limits and current usage. |
| `rotate_site_secret(site)` | Issues a new secret, shown once. |
| `suspend_site(site)`, `resume_site(site)`, `archive_site(site, delete_data=False)` | Turns a site off, back on, or retires it. |

## API for Suite sites

The site sends two headers: `Authorization: token <api_key>:<api_secret>` and
`Frappe-Authorization-Source: Suite Site`. All methods live under `suite_cloud.api.site`.

Rules that apply everywhere:

- Anything that belongs to another site is reported as "not found", never as "forbidden".
- If the cluster refuses a change, the site gets HTTP 422 with Stalwart's error type.
- Each site may make 300 requests per minute.

| Module | Methods |
| --- | --- |
| `site` | `ping` |
| `site.domains` | `list_domains`, `get_domain`, `create_domain`, `update_domain`, `delete_domain`, `get_dns_records`, `refresh_dns_records`, `verify_dns_records` |
| `site.accounts` | `list_accounts`, `get_account`, `create_account`, `update_account`, `set_account_enabled`, `set_password`, `create_app_password`, `set_aliases`, `set_groups`, `delete_account` |
| `site.groups` | `list_groups`, `get_group`, `create_group`, `update_group`, `set_group_aliases`, `set_group_members`, `delete_group` |
| `site.mailing_lists` | `list_mailing_lists`, `get_mailing_list`, `create_mailing_list`, `update_mailing_list`, `set_mailing_list_aliases`, `set_recipients`, `delete_mailing_list` |
| `site.meta` | `get_account_options` (the locales and time zones a mailbox can use) |

### How a domain goes live

1. The site creates the domain. Suite Cloud creates it on the cluster, but disabled.
2. `get_dns_records` lists what the domain owner must publish: an MX record pointing at the
   cluster, an SPF record of the form `v=spf1 include:spf.<zone> -all`, the DKIM keys Stalwart
   generates and rotates, and DMARC and TLS reporting records.
3. The owner publishes them. `verify_dns_records` checks; once MX, SPF, DMARC and at least one DKIM
   record are found, the domain is enabled and starts receiving and sending mail. Publishing the
   records is how the site proves it controls the domain.
4. Verification is retried every hour. A temporary DNS failure never turns a working domain off.

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

## Development

```sh
bench --site yoursite run-tests --app suite_cloud --test-category all
```

The tests run against a fake Stalwart that lives inside the test process
(`suite_cloud/tests/fake_stalwart.py`). Nothing reaches a real server or a real DNS provider.

A few details have not yet been checked against a live Stalwart 0.16 cluster and are listed in the
plan. Run `stalwart-cli describe <Object>` on a node to confirm the exact field names for: DNS
provider types other than Cloudflare and Route53, the format of `NetworkListener.bind`, the
`listener` variable in connection strategies, whether `Domain.dnsManagement.publishRecords` accepts
an empty list, and the file names in the download URL templates.

## License

AGPL-3.0
