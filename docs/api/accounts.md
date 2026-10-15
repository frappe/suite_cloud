# Accounts

Module `suite_cloud.api.mail.accounts`, plus `mail.meta.get_account_options`. The conventions are in the [overview](README.md).

| Method | Verb | Purpose |
| --- | --- | --- |
| `mail.accounts.list_accounts` | GET | A page of accounts |
| `mail.accounts.get_account` | GET | One account with its disk usage |
| `mail.accounts.get_quotas` | GET | Disk quota and usage for up to 500 addresses at once |
| `mail.accounts.create_account` | POST | Creates a mailbox and returns its app password once |
| `mail.accounts.update_account` | PATCH | Changes the name, description, quotas, locale, time zone or receiving |
| `mail.accounts.set_account_enabled` | PUT | Enables or disables login and delivery |
| `mail.accounts.set_password` | PUT | Sets the account's own password |
| `mail.accounts.rotate_app_password` | POST | Mints a new app password and revokes the previous one |
| `mail.accounts.create_app_password` | POST | Creates an extra app password, returned once |
| `mail.accounts.set_aliases` | PUT | Replaces the alias set |
| `mail.accounts.add_alias` | POST | Adds one alias |
| `mail.accounts.remove_alias` | DELETE | Removes one alias |
| `mail.accounts.set_alias_enabled` | PUT | Turns one alias on or off |
| `mail.accounts.set_groups` | PUT | Replaces the account's group memberships |
| `mail.accounts.delete_account` | DELETE | Removes the account and its mail |
| `mail.meta.get_account_options` | GET | The locales, time zones and optional quota names the cluster accepts |

## The account object

```json
{
  "email": "jane@acme.example",
  "domain": "acme.example",
  "enabled": true,
  "disable_receiving": false,
  "display_name": "Jane Doe",
  "description": null,
  "disk_quota_gb": 5,
  "quotas": { "maxDiskQuota": 5368709120, "maxEmails": 100000 },
  "used_disk_bytes": 734003200,
  "locale": "en-US",
  "time_zone": "Europe/Berlin",
  "aliases": [ { "email": "j.doe@acme.example", "enabled": true, "description": null } ],
  "groups": ["sales@acme.example"],
  "mailing_lists": ["all@acme.example"],
  "created_at": "2026-02-01T09:00:00+00:00"
}
```

`used_disk_bytes` comes from the cluster. `get_account` asks for it on every call; a `list_accounts` page fetches it for the whole page in one cluster call; `create_account` and the write methods answer `null` for it. `mailing_lists` names the lists that deliver to any of the account's addresses, primary or alias.

### Quotas

Every account has a disk quota above 0 GB. It defaults to the site's default quota, and the quotas of the site's accounts and groups together may not exceed the site's total disk; a create or increase beyond the remaining room is a 422.

`quotas` is an object of Stalwart `StorageQuota` names to limits. `maxDiskQuota` (bytes) is the disk row every account has; the others are optional counts (messages, mailboxes, Sieve scripts, calendars, contact cards, app passwords and so on). `get_account_options` lists the optional names. On a create or update, `quotas` replaces the optional rows (`{}` lifts them all) and keeps the disk row unless it names `maxDiskQuota`; `disk_quota_gb` sets the disk row in GB and wins over a `maxDiskQuota` inside `quotas`.

### Aliases

An alias is an extra address that delivers to the account. The input to `create_account` and `set_aliases` is a JSON array of addresses, or of objects `{"email", "enabled", "description"}` with `enabled` defaulting to true. At most 100 aliases per object. An address may be a primary address or an alias exactly once across the whole directory; a taken one is a 409.

`set_aliases` replaces the whole set. `add_alias`, `remove_alias` and `set_alias_enabled` change one alias under a row lock, so two admins editing the same account never drop each other's rows. Adding an alias that is already there, or removing one that is not, changes nothing and returns the account.

## `list_accounts`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `domain` | string | | Only accounts on this domain |
| `search` | string | | Matches the address or display name |
| `start` | integer | 0 | Offset |
| `limit` | integer | 50 | Page length, at most 200 |

Answers `{"items": [account, …], "total": n}` ordered by address.

## `get_account`

Takes `email`. Returns the account object with its usage.

## `get_quotas`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `emails` | array of strings, required | Up to 500 addresses, as a JSON body on a QUERY, or as the array's JSON text on a GET short enough for a request line |

Answers `{"<email>": {"disk_quota_gb": 5, "used_disk_bytes": 734003200}, …}` for the site's accounts among them. Addresses that are not the site's accounts are left out. The allotment is one query and the usage one cluster call per batch, so a list page can show both for every row.

## `create_account`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `email` | string, required | | On an active domain of the site |
| `password` | string, required | | At least 8 characters; never stored by Suite Cloud |
| `display_name` | string | | |
| `description` | string | | |
| `aliases` | array | | See aliases |
| `groups` | array of strings | | Group addresses of the site, at most 500 |
| `mailing_lists` | array of strings | | List addresses of the site the account joins as a recipient, at most 500 |
| `disk_quota_gb` | number | the site's default | See quotas |
| `quotas` | object | | See quotas |
| `locale` | string | `en-US` | One of `get_account_options` |
| `time_zone` | string | | One of `get_account_options` |
| `disable_receiving` | boolean | `false` | Send-only: the account logs in and sends, and mail addressed to it bounces |

Answers 201 with the account object plus `app_password`, the password the site uses for the account's mail access. It is returned only here; `rotate_app_password` issues a fresh one. Suite Cloud stores neither the app password nor the account's own password. An inactive domain or a reached limit is a 422; a taken address is a 409; an unknown group or list is a 404.

## `update_account`

Takes `email` and any of `display_name`, `description`, `disk_quota_gb`, `quotas`, `locale`, `time_zone` and `disable_receiving`. Only the fields passed change. Returns the account object.

## `set_account_enabled`

Takes `email` and `enabled`. A disabled account cannot log in; mail addressed to it is still delivered unless `disable_receiving` is set. Its data stays. Returns the account object.

## `set_password`

Takes `email` and `password` (at least 8 characters). Answers 200 with `{"data": null}`.

## `rotate_app_password`, `create_app_password`

`rotate_app_password` takes `email` and answers `{"app_password": "…"}`; the previous app password stops working. `create_app_password` takes `email` and `description` (default `Suite`) and answers `{"secret": "…"}` for an extra app password that Suite Cloud does not store.

## `set_aliases`, `add_alias`, `remove_alias`, `set_alias_enabled`

| Method | Parameters |
| --- | --- |
| `set_aliases` | `email`, `aliases` (array; empty or omitted removes every alias) |
| `add_alias` | `email`, `alias`, `description` |
| `remove_alias` | `email`, `alias`; the primary address cannot be removed (422) |
| `set_alias_enabled` | `email`, `alias`, `enabled`; an alias that is not there is a 404 |

Each returns the account object.

## `set_groups`

Takes `email` and `groups`, an array of the site's group addresses (at most 500; empty or omitted leaves every group). Returns the account object.

## `delete_account`

Takes `email`. Answers 204. The mailbox and its mail are removed from the cluster.

## `get_account_options`

Takes no parameters. Answers the values the cluster accepts, cached for an hour:

```json
{
  "locales": [ { "value": "en-US", "label": "English (United States)" } ],
  "time_zones": [ { "value": "Europe/Berlin", "label": "Europe/Berlin" } ],
  "quotas": [ { "value": "maxEmails", "label": "Maximum number of emails" } ]
}
```
