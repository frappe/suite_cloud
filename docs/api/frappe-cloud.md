# Frappe Cloud

Module `suite_cloud.api.fc`. The caller is a Frappe user with the **Frappe Cloud** role, or an operator with System Manager; see [authentication](README.md#authentication). Errors follow the [common table](README.md#errors).

| Method | Verb | Purpose |
| --- | --- | --- |
| `fc.create_site` | POST | Registers a site and returns its credentials once |
| `fc.get_site` | GET | Status, cluster, limits and usage |
| `fc.update_site` | PATCH | Changes the title, contact, limits or allowed addresses |
| `fc.rotate_site_secret` | POST | Issues a new secret, shown once |
| `fc.suspend_site` | POST | Locks the site out; its key still authenticates so it sees a 403 naming the suspension |
| `fc.resume_site` | POST | Lets a suspended site back in |
| `fc.archive_site` | POST | Retires the site, optionally deleting its directory objects from the cluster |

`get_site` and `update_site` also accept POST.

## `create_site`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `site` | string, required | The site's domain, for example `acme.frappe.cloud` |
| `cluster` | string | The hostname of the Stalwart cluster to place the site on |
| `region` | string | A region the cluster must serve, used when `cluster` is not given |
| `fc_reference` | string | Frappe Cloud's own identifier for the site |
| `title` | string | Display name |
| `contact_email` | string | Address for site-specific notices |
| `max_domains`, `max_accounts`, `max_groups`, `max_mailing_lists` | integer | Object limits; unset means no limit |
| `max_disk_gb` | number | Total disk the site's accounts and groups may be allotted together |
| `default_disk_quota_gb` | number | The disk quota a new account or group gets when none is passed |
| `allowed_ips` | array of strings | The outbound addresses or CIDR ranges of the server hosting the site. Set, only requests from them may use the site's key. |

The cluster is chosen in this order: the named cluster, else one that serves the region with the default cluster preferred, else the default cluster, else a cluster that serves every region. A cluster with no regions serves any region. Naming a cluster that is not active is a 422.

Answers 201 with the credentials, the site profile and the Suite Cloud URL. The secret is shown only this once.

```json
{
  "api_key": "0a1b2c3d4e5f6a7",
  "api_secret": "…",
  "authorization_source": "Suite Site",
  "site": "acme.frappe.cloud",
  "cluster": "c1.frappemail.com",
  "status": "Active",
  "jmap_url": "https://c1.frappemail.com",
  "mail_hostname": "c1.frappemail.com",
  "limits": { "…": "…" },
  "usage": { "…": "…" },
  "suite_cloud_url": "https://cloud.frappemail.com"
}
```

A site that already exists is a 409.

## `get_site`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `site` | string, required | The site's domain |

Returns the site profile as [`ping`](site.md#ping) does, plus `suite_cloud_url`.

## `update_site`

Takes `site` and any of `title`, `contact_email`, `max_domains`, `max_accounts`, `max_groups`, `max_mailing_lists`, `max_disk_gb`, `default_disk_quota_gb` and `allowed_ips`, with the meanings above. Only the fields passed change. An empty `allowed_ips` list lifts the address restriction. `allowed_ips` also accepts newline separated text. Returns the site profile plus `suite_cloud_url`.

## `rotate_site_secret`

Takes `site`. Returns `{"api_key", "api_secret", "authorization_source"}` with the new secret, shown once. The old secret stops working at once.

## `suspend_site`, `resume_site`, `archive_site`

Each takes `site` and returns the site profile. `archive_site` also takes `delete_data` (boolean, default false); with it, every domain, account, group and list of the site is removed from the cluster in a background job. An archived site cannot be suspended or resumed.
