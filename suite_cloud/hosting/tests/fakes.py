"""In-process stand-ins for Central, Pilot and a site's REST API, behind the client factories."""

import itertools
import secrets
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from suite_cloud.hosting.central import CentralRejectedError
from suite_cloud.hosting.central.pilot import PilotConflict, PilotNotReady
from suite_cloud.hosting.central.site import SiteError

REGION = "zz1"
PROXY_ZONE = f"{REGION}.frappe.test"


class FakeCentral:
    """Creations succeed on the first status read and boot at once unless told otherwise."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.actions: dict[str, dict] = {}
        self.arguments: dict[str, dict] = {}  # request key -> arguments
        self.servers: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_creations = False
        self.size = {"vcpus": 2.0, "memory_megabytes": 8192, "disk_gigabytes": 80.0}  # of new servers
        self.boot = True
        self.images = [
            {"id": "img-old", "created_at": 1, "tags": {}},
            {"id": "img-new", "created_at": 2, "tags": {}},
            {"id": "img-baked", "created_at": 3, "tags": {"has_site": "1"}},
        ]

    def my_capabilities(self) -> list[str]:
        return ["cluster:view", "server:console", "server:create", "server:view"]

    def my_teams(self) -> list[dict]:
        # Central folds the title into ``label``: the team name, else the owner's email, else the id.
        return [
            {"name": "TEAM-00042", "label": "Suite Cloud"},
            {"name": "TEAM-00043", "label": "ops@frappe.test"},
            {"name": "TEAM-00007", "label": "TEAM-00007"},
        ]

    def list_instances(self) -> list[dict]:
        return [
            {
                "region": REGION,
                "display_name": "Test Region",
                "provider": "Fake",
                "country_code": "IN",
                "status": "Active",
                "reachable": True,
            }
        ]

    def list_images(self, region: str, offering: str) -> list[dict]:
        return self.images

    def eligible_plans(self, region: str, offering: str, image_id: str) -> dict:
        # Central's shape: rows grouped by sub-category, the id under "plan".
        return {
            "plans": {
                "General": [{"plan": "plan-general", "title": "General 4 GB", "rate": 20.0}],
                "Compute": [{"plan": "plan-small", "title": "Compute 2 GB", "rate": 12.0}],
            }
        }

    def create_server(self, **arguments) -> dict:
        self.calls.append(("create_server", arguments))
        key = arguments["request_key"]
        if key in self.arguments:
            if self.arguments[key] != arguments:
                raise CentralRejectedError("Request key reused with other settings.", "VALIDATION_ERROR")
            return self.find_action(key)
        self.arguments[key] = arguments
        name = f"action-{next(self.ids)}-{secrets.token_hex(3)}"  # unique across tests, as on Central
        failed = self.fail_creations
        self.actions[name] = {
            "action": name,
            "key": key,
            "status": "Failed" if failed else "Queued",
            "resource_id": None,
            "title": arguments["title"],
            "error": {"message": "Region at capacity.", "remediation": "Try later."} if failed else None,
        }
        return self.public(name)

    def find_action(self, key: str) -> dict:
        return self.public(next(n for n, a in self.actions.items() if a["key"] == key))

    def action_status(self, name: str) -> dict:
        action = self.actions[name]
        if action["status"] == "Queued":
            resource_id = f"vm-{next(self.ids)}"
            action.update(status="Succeeded", resource_id=resource_id)
            self.servers[resource_id] = {
                "resource_id": resource_id,
                "plan": self.arguments[action["key"]]["plan"],
                "name": resource_id,
                "status": "Running" if self.boot else "Provisioning",
                "gateway_url": f"https://{resource_id}.gateway.test" if self.boot else None,
                "pilot_audience": "audience" if self.boot else None,
                **self.size,
                "ipv6_address": "fd00::1",
                "public_ipv4": None,
                "public_ipv6": None,
            }
        return self.public(name)

    def retry_action(self, name: str) -> dict:
        self.calls.append(("retry_action", name))
        self.actions[name].update(status="Queued", error=None)
        return self.public(name)

    def registry(self) -> dict:
        return {
            "servers": list(self.servers.values()),
            "sites": [],
            "creations": [self.public(n) for n in self.actions],
        }

    def get_bench_link(self, resource_id: str) -> str:
        return f"https://{resource_id}.gateway.test/?sid=token-{resource_id}"

    def terminate_server(self, resource_id: str) -> dict:
        self.calls.append(("terminate_server", resource_id))
        return {"action": "terminate", "status": "Queued"}

    def public(self, name: str) -> dict:
        return {k: v for k, v in self.actions[name].items() if k != "key"}


class FakePilot:
    """One fake for every server: tasks finish at once, with ``fail_next`` for a failing one."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.sites = {"site.local"}
        self.apps: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.outputs: dict[str, str] = {}
        self.idempotent: dict[str, str] = {}
        self.pending: dict[str, str] = {}  # task -> site it creates
        self.wildcards = [f".{PROXY_ZONE}"]
        self.letsencrypt_email = "ops@frappe.test"
        self.production = True
        self.taken: set[str] = set()  # labels another server holds
        self.attached: dict[str, set] = {}
        self.primary: dict[str, str] = {}
        self.token = "token-1"
        self.not_ready = False
        self.hold_tasks = False
        self.calls: list[tuple] = []

    def check(self) -> None:
        if self.not_ready:
            raise PilotNotReady("Pilot is booting.")

    def task(self, status: str = "success", output: str = "") -> dict:
        task_id = f"task-{next(self.ids)}"
        self.tasks[task_id] = {"task_id": task_id, "status": "running" if self.hold_tasks else status}
        self.outputs[task_id] = output
        return dict(self.tasks[task_id])

    def finish(self, task_id: str, status: str = "success") -> None:
        self.tasks[task_id]["status"] = status
        if status == "success" and task_id in self.pending:
            self.sites.add(self.pending.pop(task_id))

    # --- host -------------------------------------------------------------------

    def wildcard_domains(self) -> list[str]:
        self.check()
        return self.wildcards

    def settings(self) -> dict:
        self.check()
        return {"production": {"enabled": self.production}, "letsencrypt": {"email": self.letsencrypt_email}}

    def list_sites(self) -> list[str]:
        self.check()
        return sorted(self.sites)

    def list_apps(self) -> list[dict]:
        self.check()
        return self.apps

    def install_app(self, repo: str, branch: str) -> dict:
        self.calls.append(("install_app", repo, branch))
        self.apps.append({"name": "suite", "current_commit": "abc123"})
        return self.task()

    # --- sites ------------------------------------------------------------------

    def create_site(self, name: str, apps: list[str], idempotency_key: str) -> dict:
        self.check()
        self.calls.append(("create_site", name, idempotency_key))
        active = self.idempotent.get(idempotency_key)
        if active and self.tasks[active]["status"] in ("queued", "running"):
            return dict(self.tasks[active])
        if name in self.sites:
            raise PilotConflict(f"Site {name} already exists.", "site_name_conflict")
        if name.split(".")[0] in self.taken:
            task = self.task("failed", f"Registering {name}\nError: label already taken by another server")
        else:
            task = self.task()
            self.pending[task["task_id"]] = name
            if task["status"] == "success":
                self.finish(task["task_id"])
        self.idempotent[idempotency_key] = task["task_id"]
        return task

    def drop_site(self, name: str) -> dict:
        self.calls.append(("drop_site", name))
        self.sites.discard(name)
        return self.task()

    def site_login_url(self, site: str) -> str:
        return f"https://{site}/app?sid=admin-sid"

    def get_task(self, task_id: str) -> dict:
        return dict(self.tasks[task_id])

    def task_output(self, task_id: str) -> str:
        return self.outputs[task_id]

    # --- domains ----------------------------------------------------------------

    def dns_records(self, site: str, domain: str) -> dict:
        self.calls.append(("dns_records", site, domain))
        return {
            "cname": [{"type": "CNAME", "host": domain, "value": f"proxy.{PROXY_ZONE}."}],
            "txt": [{"type": "TXT", "host": f"_frappe-verification.{domain}", "value": self.token}],
            "a": [{"type": "A", "host": domain, "value": "203.0.113.5"}],
        }

    def add_domain(self, site: str, domain: str) -> dict:
        self.calls.append(("add_domain", site, domain))
        if domain in self.attached.setdefault(site, set()):
            raise PilotConflict(f"Domain {domain} is already attached to this site.", "domain_conflict")
        self.attached[site].add(domain)
        return self.task()

    def set_primary(self, site: str, domain: str) -> dict:
        self.primary[site] = domain
        return self.task()

    def remove_domain(self, site: str, domain: str) -> dict:
        self.attached.get(site, set()).discard(domain)
        return self.task()


class FakeSite:
    """What Configure and Mail write to a site, keyed by hostname."""

    def __init__(self) -> None:
        self.singles: dict[str, dict] = {}
        self.docs: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple] = []
        self.smtp_failures = 0

    def get_doc(self, doctype: str, name: str) -> dict | None:
        return self.docs.get((doctype, name))

    def set_single(self, doctype: str, values: dict) -> None:
        self.singles.setdefault(doctype, {}).update(values)

    def call(self, method: str, **kwargs) -> None:
        self.calls.append((method, kwargs))

    def run_doc_method(self, doctype: str, name: str, method: str, **kwargs) -> dict:
        self.calls.append((method, kwargs))
        return {"title": "ok"}

    def ensure_user(self, values: dict) -> None:
        self.docs.setdefault(("User", values["email"]), values)

    def ensure_email_account(self, values: dict) -> None:
        if self.smtp_failures:
            self.smtp_failures -= 1
            raise SiteError("Invalid login or password", "ValidationError", 417)
        self.docs[("Email Account", values["email_account_name"])] = values


@contextmanager
def install(central: FakeCentral, pilot: FakePilot, site: FakeSite):
    with ExitStack() as stack:
        stack.enter_context(patch("suite_cloud.hosting.central.get_central_client", return_value=central))
        stack.enter_context(patch("suite_cloud.hosting.central.pilot.get_pilot_client", return_value=pilot))
        stack.enter_context(patch("suite_cloud.hosting.central.site.get_site_client", return_value=site))
        yield
