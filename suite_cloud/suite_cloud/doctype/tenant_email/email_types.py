from dataclasses import dataclass
from urllib.parse import urlparse

import frappe
from frappe import _

MAX_PARAM_LENGTH = 2000


class InvalidEmailError(frappe.ValidationError):
    """The site asked for an email Suite Cloud does not send, or with the wrong parameters."""

    http_status_code = 400


@dataclass(frozen=True)
class EmailType:
    """One email a site may ask for. Its template is ``templates/tenant_emails/<name>.html``, and
    each of ``images`` an image of ``public/images/emails`` that the template embeds by name."""

    subject: str  # Jinja, over the parameters
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    links: tuple[str, ...] = ("link",)  # URLs that must lead to the tenant's own site
    images: tuple[str, ...] = ()

    @property
    def params(self) -> tuple[str, ...]:
        return self.required + self.optional


EMAIL_TYPES = {
    "password-reset": EmailType("Reset Password", ("link",)),
    "account-invite": EmailType(
        "You have been invited by {{ inviter }} to join Frappe Mail", ("inviter", "link")
    ),
    "signup-code": EmailType("Frappe Mail - Verification Code", ("code", "expires_in_minutes"), links=()),
    "drive-signup-code": EmailType("Frappe Drive - OTP", ("code",), links=()),
    "drive-invite": EmailType(
        "Frappe Drive - Invitation", ("inviter", "link"), ("team_name",), images=("drive-logo.png",)
    ),
    "drive-share": EmailType(
        "Frappe Drive - {{ item_type | capitalize }} Shared",
        ("sharer", "item_type", "item_name", "link"),
        images=("drive-logo.png",),
    ),
    "writer-mention": EmailType(
        "Frappe Drive - Mention in {{ document }}", ("author", "document", "link"), images=("drive-logo.png",)
    ),
    "sheet-share": EmailType("{{ sharer }} shared a sheet with you", ("sharer", "sheet", "access", "link")),
    "calendar-rsvp": EmailType(
        '{{ responder_name }} responded "{{ response }}": {{ title }}',
        (
            "title",
            "responder_name",
            "responder_email",
            "response",
            "when",
            "organizer_name",
            "organizer_email",
        ),
        ("timezone", "location"),
        links=(),
        images=("calendar-logo.png",),
    ),
    "data-exchange": EmailType(
        "{{ app }} Data {{ action }} {{ 'Completed' if succeeded else 'Failed' }}",
        ("app", "action", "succeeded", "link"),
    ),
    "meet-recording": EmailType(
        "{% if status == 'Failed' %}Your recording of {{ room }} could not be processed"
        "{% elif status == 'Partial' %}Your partial recording of {{ room }} is ready"
        "{% else %}Your recording of {{ room }} is ready{% endif %}",
        ("room", "recorded_at", "status"),
        ("link",),
        images=("meet-logo.png",),
    ),
}


def get_email_type(name: str) -> EmailType:
    if name not in EMAIL_TYPES:
        frappe.throw(_("Suite Cloud does not send {0} emails.").format(name), InvalidEmailError)
    return EMAIL_TYPES[name]


def validate_params(email_type: EmailType, params: dict, site_hosts: set[str]) -> None:
    """Every required parameter and no other, short scalars, and links only to the tenant's site
    when Suite Cloud knows where that is."""

    if not isinstance(params, dict):
        frappe.throw(_("Send the parameters as a JSON object."), InvalidEmailError)
    if missing := [p for p in email_type.required if params.get(p) in (None, "")]:
        frappe.throw(_("Missing parameters: {0}.").format(", ".join(missing)), InvalidEmailError)
    if unknown := sorted(set(params) - set(email_type.params)):
        frappe.throw(_("Unknown parameters: {0}.").format(", ".join(unknown)), InvalidEmailError)
    for name, value in params.items():
        if not isinstance(value, str | int | bool) or len(str(value)) > MAX_PARAM_LENGTH:
            frappe.throw(
                _("{0} must be text of at most {1} characters.").format(name, MAX_PARAM_LENGTH),
                InvalidEmailError,
            )
    for name in email_type.links:
        if params.get(name):
            assert_site_link(name, str(params[name]), site_hosts)


def assert_site_link(name: str, link: str, site_hosts: set[str]) -> None:
    """A plain web address. A backslash, a space or a ``user@`` part is refused outright: a browser
    reads such a link's host differently than Python does, e.g. ``https://evil.example\\@site``."""

    url = urlparse(link)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or "\\" in link
        or any(c.isspace() or not c.isprintable() for c in link)
    ):
        frappe.throw(_("{0} must be a web address.").format(name), InvalidEmailError)
    if site_hosts and url.hostname.lower() not in site_hosts:
        frappe.throw(_("{0} must lead to the tenant's own site.").format(name), InvalidEmailError)


def render(name: str, email_type: EmailType, params: dict) -> tuple[str, str]:
    """The subject and HTML body."""

    subject = frappe.render_template(email_type.subject, params, is_path=False)
    body = frappe.render_template(f"templates/tenant_emails/{name.replace('-', '_')}.html", params)
    return " ".join(subject.split()), body  # one line: a header cannot carry the site's newlines


def get_inline_images(email_type: EmailType) -> list[dict]:
    """The images the template embeds, as ``get_email`` takes them."""

    images = []
    for filename in email_type.images:
        with open(frappe.get_app_path("suite_cloud", "public", "images", "emails", filename), "rb") as f:
            images.append({"filename": filename, "filecontent": f.read()})
    return images
