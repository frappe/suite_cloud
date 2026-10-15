# Domains

Module `suite_cloud.api.mail.domains`. The conventions are in the [overview](README.md). The full life cycle, from the ownership check to the hourly re-verification, is described in [How a domain goes live](../../README.md#how-a-domain-goes-live).

| Method | Verb | Purpose |
| --- | --- | --- |
| `mail.domains.check_domain` | GET | Whether the domain can be added, and the record that proves ownership |
| `mail.domains.list_domains` | GET | Every domain of the site |
| `mail.domains.get_domain` | GET | One domain with its DNS records |
| `mail.domains.create_domain` | POST | Adds the domain once its ownership record resolves |
| `mail.domains.update_domain` | PATCH | Changes delivery settings or enables and disables the domain |
| `mail.domains.delete_domain` | DELETE | Removes the domain and everything on it |
| `mail.domains.get_dns_records` | GET | The records the owner must publish, and their state |
| `mail.domains.refresh_dns_records` | POST | Re-reads the records the cluster expects |
| `mail.domains.verify_dns_records` | POST | Checks the records on public resolvers now |

A domain is **active** when it is enabled and verified. Only then does the cluster accept mail for it, and only then may the site create accounts, groups and lists on it. Disabling a domain drops its verification, so enabling it again needs a fresh check.

## The domain object

```json
{
  "domain": "acme.example",
  "enabled": true,
  "description": null,
  "catch_all_address": "office@acme.example",
  "sub_addressing": true,
  "allow_relaying": false,
  "publish_client_discovery_records": false,
  "is_verified": true,
  "last_verified_at": "2026-03-04T05:06:07+00:00",
  "created_at": "2026-02-01T09:00:00+00:00",
  "dns_record_groups": [ { "key": "authentication_records", "label": "Email Authentication", "description": "…", "is_mandatory": true } ],
  "dns_records": [ { "…": "…" } ]
}
```

`list_domains` leaves out `dns_record_groups` and `dns_records`; `get_domain` and `create_domain` carry them.

| Field | Meaning |
| --- | --- |
| `catch_all_address` | Receives mail for local parts that match no account. `null` means such mail is rejected. |
| `sub_addressing` | Whether `user+tag@domain` delivers to `user@domain` |
| `allow_relaying` | Whether the cluster forwards mail for addresses it does not hold to the domain's MX, for split delivery |
| `publish_client_discovery_records` | Whether the SRV, autoconfig and MTA-STS records for mail client auto-setup are listed. Off by default, because the cluster has no certificate for customer hostnames. |

## `check_domain`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `domain` | string, required | The domain to add |

Stores nothing. Answers the TXT record the owner must publish at the apex. The record is the same for every domain the site ever adds.

```json
{
  "domain": "acme.example",
  "ownership_record": { "type": "TXT", "host": "@", "fqdn": "acme.example", "value": "frappe-suite-verification=<token>" }
}
```

A domain the site already holds is a 409. A domain of the mail infrastructure itself is a 422. Whether another site holds the domain is told only to a proven owner, at `create_domain`.

## `create_domain`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `domain` | string, required | | The domain, with its ownership record published |
| `description` | string | | Free text |
| `catch_all_address` | string | | See the domain object |
| `sub_addressing` | boolean | `true` | |
| `allow_relaying` | boolean | `false` | |
| `publish_client_discovery_records` | boolean | `false` | |

Answers 201 with the domain object, including the records to publish next. The domain is created disabled on the cluster until it is verified. Until the ownership record resolves, the error names the record. A domain another site holds is a 409 (`Domain … is not available.`); the site's domain limit is a 422.

## `update_domain`

Takes `domain` and any of `description`, `catch_all_address`, `sub_addressing`, `allow_relaying`, `publish_client_discovery_records` and `enabled`. Only the fields passed change; an empty `catch_all_address` clears it. Returns the domain object.

## `delete_domain`

Takes `domain`. Answers 204. The domain must be empty: an account, group, list or alias still on it is a 422. Reports received about the domain are kept.

## `get_dns_records`, `refresh_dns_records`, `verify_dns_records`

Each takes `domain` and answers the record set:

```json
{
  "domain": "acme.example",
  "is_verified": false,
  "groups": [ { "key": "routing_records", "label": "Inbound Mail Routing", "description": "…", "is_mandatory": false } ],
  "records": [
    {
      "group": "authentication_records",
      "category": "DKIM",
      "type": "TXT",
      "host": "frappemail-rsa._domainkey",
      "fqdn": "frappemail-rsa._domainkey.acme.example",
      "value": "v=DKIM1; k=rsa; p=…",
      "priority": null,
      "weight": null,
      "port": null,
      "ttl": 3600,
      "is_mandatory": true,
      "is_verified": false,
      "last_checked_at": null
    }
  ]
}
```

The groups are `authentication_records` (SPF, DKIM, DMARC; mandatory), `routing_records` (MX), `transport_security_records` (TLS reporting, MTA-STS), `discovery_records` and `autoconfig_records`. The last two appear only with `publish_client_discovery_records` on. A domain is verified once SPF, DMARC and at least one DKIM selector resolve; MX is optional, for a domain that only sends.

`refresh_dns_records` re-reads the zone the cluster generates, for example to pick up a DKIM key that was still generating when the domain was created. `verify_dns_records` resolves every record on public resolvers and adds `inconclusive`, the number of records a resolver failure said nothing about; those keep their previous state. Verification also runs hourly for every unverified domain.
