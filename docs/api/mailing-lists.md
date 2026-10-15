# Mailing lists

Module `suite_cloud.api.mail.mailing_lists`. The conventions are in the [overview](README.md). A mailing list is an address that delivers to every recipient address, inside or outside the platform. Recipients are stored one by one, not on the list, so a list can hold hundreds of thousands of addresses.

| Method | Verb | Purpose |
| --- | --- | --- |
| `mail.mailing_lists.list_mailing_lists` | GET | A page of lists |
| `mail.mailing_lists.get_mailing_list` | GET | One list |
| `mail.mailing_lists.create_mailing_list` | POST | Creates a list, with its first recipients |
| `mail.mailing_lists.update_mailing_list` | PATCH | Changes the description |
| `mail.mailing_lists.set_aliases` | PUT | Replaces the alias set |
| `mail.mailing_lists.add_alias` | POST | Adds one alias |
| `mail.mailing_lists.remove_alias` | DELETE | Removes one alias |
| `mail.mailing_lists.set_alias_enabled` | PUT | Turns one alias on or off |
| `mail.mailing_lists.list_recipients` | GET | A page of one list's recipients |
| `mail.mailing_lists.list_all_recipients` | GET | A page of every recipient of every list of the site |
| `mail.mailing_lists.add_recipients` | POST | Adds up to 5000 recipients |
| `mail.mailing_lists.remove_recipients` | DELETE | Removes up to 5000 recipients |
| `mail.mailing_lists.set_recipients` | PUT | Replaces the whole recipient set, for small lists |
| `mail.mailing_lists.delete_mailing_list` | DELETE | Removes the list |

## The list object

```json
{
  "email": "all@acme.example",
  "domain": "acme.example",
  "description": "Everyone",
  "recipient_count": 1250,
  "aliases": [ { "email": "everyone@acme.example", "enabled": true, "description": null } ],
  "created_at": "2026-02-01T09:00:00+00:00"
}
```

Recipients are not part of the object; `list_recipients` pages through them.

## `list_mailing_lists`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `search` | string | | Matches the address or description |
| `start` | integer | 0 | Offset |
| `limit` | integer | 100 | Page length, at most 500 |

Answers `{"items": [list, …], "total": n}` ordered by address.

## `get_mailing_list`

Takes `email`. Returns the list object.

## `create_mailing_list`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `email` | string, required | On an active domain of the site |
| `description` | string | |
| `aliases` | array | As for [accounts](accounts.md#aliases) |
| `recipients` | array of strings | Up to 5000 addresses |

Answers 201 with the list object. If adding the recipients fails, the list is not created. A taken address is a 409; an inactive domain or the site's list limit is a 422.

## `update_mailing_list`

Takes `email` and `description`. Returns the list object.

## `set_aliases`, `add_alias`, `remove_alias`, `set_alias_enabled`

The same parameters and rules as for [accounts](accounts.md#set_aliases-add_alias-remove_alias-set_alias_enabled). Each returns the list object.

## Recipients

A recipient is any address. An address under a domain that another site holds is refused as not found (404), so a list cannot inject mail into another site's mailboxes. Addresses are lowercased before they are stored or matched.

### `list_recipients`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `email` | string, required | | The list |
| `search` | string | | Matches the recipient address |
| `start` | integer | 0 | Offset |
| `limit` | integer | 200 | Page length, at most 1000 |

Answers `{"items": [{"email": "a@example.com", "enabled": true}, …], "total": n}` ordered by address.

### `list_all_recipients`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `start` | integer | 0 | Offset |
| `limit` | integer | 1000 | Page length, at most 1000 |

Answers `{"items": [{"mailing_list": "all@acme.example", "email": "a@example.com", "enabled": true}, …], "total": n}` ordered by list, then address. A site mirrors every list in a handful of calls this way instead of one call per list, which would exhaust its rate limit once it holds a few hundred lists.

### `add_recipients`, `remove_recipients`

Each takes `email` and `recipients`, an array of up to 5000 addresses. Addresses already on the list, or already absent, are skipped. Only the changed addresses are pushed to the cluster. Each answers `{"added": n, "recipient_count": n}` or `{"removed": n, "recipient_count": n}`.

### `set_recipients`

Takes `email` and `recipients`, an array of up to 5000 addresses that becomes the whole list; empty or omitted removes every recipient. Meant for small lists; large lists should add and remove in batches. Returns the list object.

## `delete_mailing_list`

Takes `email`. Answers 204. The recipients go with the list.
