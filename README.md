# Suite Cloud

Suite Cloud deploys and manages [Stalwart](https://stalw.art) mail clusters for
[Frappe Suite](https://github.com/frappe/suite) sites and adds an app-level tenancy layer on top of
Stalwart's flat directory. Frappe Cloud registers a site here, the site manages its domains and
accounts through Suite Cloud's API, and Suite Cloud pushes every change to the site's cluster over
Stalwart's JMAP management API. End users keep talking JMAP to the cluster directly.

```
Frappe Cloud ──(API key, role "Frappe Cloud")──▶ suite_cloud.api.fc.*
Suite site   ──(site key, Frappe-Authorization-Source: Suite Site)──▶ suite_cloud.api.site.*
                                                                        │ ownership checks
                                                                        ▼
                                          Stalwart Cluster ◀── JMAP (Bearer API key) ── Suite Cloud
Suite site users ──(JMAP, app passwords)──────────────────────────────▲
Suite Cloud ──(Ansible over SSH)──▶ node and gateway VPSes
Suite Cloud ──(dns-lexicon)──▶ DNS provider of each DNS Zone
```

## What it manages

| DocType | Purpose |
| --- | --- |
| Stalwart Store | Connection settings of a data, blob, search or in-memory backend (PostgreSQL, MySQL, RocksDB, S3, ElasticSearch, Meilisearch, Redis...) |
| Stalwart Cluster | A cluster: ingress hostname (`mail.blr.example.com`), its zone, stores, ACME settings, admin credentials, SSH keypair, generated configuration plan |
| Stalwart Node | One VPS running the Stalwart binary; provisioning, health, ingress DNS membership, upgrades |
| Egress Gateway, Egress IP Pool | Outbound-only Stalwart instances and the IP pools they host; domains, sites or the cluster route outbound mail through a pool |
| DNS Zone | A domain Suite Cloud publishes records into (`frappemail.com`) and the provider credentials for it; clusters pick one |
| DNS Record | Records in a DNS Zone (node, ingress round-robin, SPF include, egress), owned by the document that needs them |
| Server Job | An Ansible playbook run against a node or gateway, tracked task by task; variables are built at run time so no secret is stored |
| Suite Site | A Frappe Suite site bound to one cluster, with its API key/secret and limits |
| Mail Domain, Mail Account, Mail Group, Mailing List | The site's directory; each document pushes itself to the cluster inside its own save |
| Suite Cloud Settings | Default DNS TTL, Stalwart versions and download URLs, ACME defaults, public URL, job timeout |

## Install

```sh
bench get-app https://github.com/frappe/suite_cloud
bench --site yoursite install-app suite_cloud
```

`ansible` must be available on the bench host (`apt install ansible`; Frappe Cloud installs it from
`pyproject.toml`), together with the `community.general` collection for the firewall tasks.
Installing creates the roles `Suite Cloud Manager` (desk), `Suite Site` and `Frappe Cloud` (API only)
and the service user every site request runs as.

## Setting up a cluster

1. **Settings and zone**: set the ACME contact email and, for staging, the Let's Encrypt staging
   directory. Create a DNS Zone for the domain the infrastructure lives under (`example.com`) with
   the provider credentials that manage it; mark it default. Further zones can be added later and
   each cluster picks the zone its hostnames live under.
2. **Stores**: create a PostgreSQL (or MySQL) data store, an S3 blob store and a Redis in-memory
   store. RocksDB/FileSystem only work for single-node clusters. The Redis store doubles as the
   cluster coordinator.
3. **Cluster**: create a Stalwart Cluster with hostname `mail.blr.example.com`. Its zone is
   `blr.example.com`; nodes, pools and gateways get single labels under it. Copy the SSH public key.
4. **First node**: add the public key to the VPS, set the PTR of its IP to the node hostname,
   create a Stalwart Node (`n1.blr.example.com`, IPv4), run *Verify SSH*, then *Provision*. The
   job installs the pinned Stalwart binary and CLI, writes the systemd unit and firewall, boots
   Stalwart in bootstrap mode to apply the store settings, applies the cluster plan in recovery
   mode (roles, coordinator, ACME DNS-01 provider, default domain with a wildcard certificate,
   system settings) and restarts normally with the recovery credentials removed.
5. Once the certificate is live, *Finish Bootstrap* (also attempted every five minutes) mints the
   management API key, records the node id and marks the cluster **Active**. The cluster hostname
   now resolves to the node.
6. **More nodes**: create and provision them the same way. They only receive `config.json`
   (the data store) and their environment; the shared database holds everything else. Each node
   joins the ingress round-robin when Stalwart's registry reports it active.

*Drain* removes a node from the ingress records, *Upgrade Nodes* on the cluster rolls the pinned
version out one node at a time, *Sync Config* pushes the generated plan and *Check Drift* reports
differences without changing anything.

### Egress pools

Create an Egress Gateway (`out1.blr.example.com`) on a VPS that has the extra public addresses
configured, provision it, then create an Egress IP Pool listing the addresses with their EHLO names
(`ded1.blr.example.com`, PTR set at the provider). Assign the pool to a Mail Domain, a Suite Site
or as the cluster default. The cluster relays the matching sender domains to
`<pool>.out.blr.example.com` over authenticated STARTTLS and the gateway delivers from the pool's
addresses. The `spf.blr.example.com` include lists every node and pool address.

## Frappe Cloud API

Authenticate with the API key/secret of a user carrying the `Frappe Cloud` role.

| Method | Purpose |
| --- | --- |
| `suite_cloud.api.fc.create_site(site, cluster=None, region=None, fc_reference=None, ...)` | Creates the Suite Site (cluster by name, region or default) and returns `jmap_url`, `mail_hostname`, `suite_cloud_url`, `api_key`, `api_secret` (shown once) |
| `get_site(site)` | Status, cluster, limits and usage |
| `rotate_site_secret(site)` | New secret, shown once |
| `suspend_site(site)`, `resume_site(site)`, `archive_site(site, delete_data=False)` | Lifecycle |

## Site API

The site sends `Authorization: token <api_key>:<api_secret>` and
`Frappe-Authorization-Source: Suite Site`. Every method lives under `suite_cloud.api.site`; objects of
other sites are reported as not found, refused Stalwart changes come back as HTTP 422 with the
server's error type, and each site gets 300 requests per minute.

| Module | Methods |
| --- | --- |
| `site` | `ping` |
| `site.domains` | `list_domains`, `get_domain`, `create_domain`, `update_domain`, `delete_domain`, `get_dns_records`, `refresh_dns_records`, `verify_dns_records` |
| `site.accounts` | `list_accounts(domain, search, start, limit)`, `get_account`, `create_account(email, password, aliases, groups, mailing_lists, disk_quota_gb, ...)`, `update_account`, `set_account_enabled`, `set_password`, `create_app_password`, `set_aliases`, `set_groups`, `delete_account` |
| `site.groups` | `list_groups`, `get_group`, `create_group`, `update_group`, `set_group_aliases`, `set_group_members`, `delete_group` |
| `site.mailing_lists` | `list_mailing_lists`, `get_mailing_list`, `create_mailing_list`, `update_mailing_list`, `set_mailing_list_aliases`, `set_recipients`, `delete_mailing_list` |
| `site.meta` | `get_account_options` (locales and time zones) |

`get_dns_records` returns the records a domain owner has to publish: MX to the cluster hostname,
SPF as `v=spf1 include:spf.<zone> -all`, the DKIM selectors Stalwart rotates, DMARC and TLS
reporting. A domain is created on the cluster disabled and only starts receiving and sending
mail once `verify_dns_records` finds the mandatory records published: publishing them is the
proof that the site controls the domain (MX, SPF, DMARC and at least one DKIM selector).
Verification is retried hourly; resolver failures never change a domain's state. SRV, autoconfig and MTA-STS records are listed only when
`publish_client_discovery_records` is set, because the cluster holds no certificate for customer
hostnames.

## Configuration

Every settings value can also be set in `site_config.json` under a `suite_cloud` key; settings win
when both are set:

```json
{ "suite_cloud": { "acme_contact_email": "ops@example.com", "stalwart_version": "v0.16.20" } }
```

`suite_cloud.verify_stalwart_tls` may be set to `false` on a development site talking to a cluster
with a staging certificate.

## Development

```sh
bench --site yoursite run-tests --app suite_cloud --test-category all
```

Tests run against an in-process fake of Stalwart's JMAP management API
(`suite_cloud/tests/fake_stalwart.py`); nothing reaches a real server or DNS provider.

Details still to confirm against a live Stalwart 0.16 cluster are listed in the plan
(`stalwart-cli describe <Object>` shows the exact field names): DnsServer variants beyond
Cloudflare/Route53, `NetworkListener.bind` encoding, the `listener` variable in connection
strategies, `Domain.dnsManagement.publishRecords` accepting an empty list, and the release
archive names in the download URL templates.

## License

AGPL-3.0
