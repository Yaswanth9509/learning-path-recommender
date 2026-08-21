"""Outbound email, over Brevo's HTTP API.

Deliberately not SMTP. Render's free tier blocks outbound traffic to ports 25,
465 and 587, so an `smtplib` transport works on a laptop and times out in
production — the worst possible failure, because it only appears once deployed.
Brevo's REST endpoint is plain HTTPS on 443, which nothing blocks.

The transport is swappable for one reason above all: tests never touch the
network. `NullMailer` records what would have been sent, so the reset flow can
be asserted end to end without a key, a socket, or a delivery.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SECONDS = 10

#: Brevo sits behind Cloudflare, which rejects `Python-urllib/3.x` outright —
#: HTTP 403, Cloudflare error 1010, "blocked based on your browser's
#: signature", with nothing in it that mentions the real cause. Any User-Agent
#: of our own is accepted. Measured against the live API: identical requests
#: fail without this header and succeed with it.
USER_AGENT = "rungs/1.0 (+https://github.com/Yaswanth9509)"


class MailError(RuntimeError):
    """Sending failed. Never shown to the caller of a recovery endpoint."""


@dataclass
class Message:
    to: str
    subject: str
    text: str

    def __post_init__(self) -> None:
        self.to = (self.to or "").strip()


@dataclass
class NullMailer:
    """Accepts everything, sends nothing, remembers it all.

    Used by the tests and by any deployment with no key configured. A missing
    key must never take the application down — password reset simply reports
    itself unavailable and the rest of the product carries on.
    """

    sent: list = field(default_factory=list)
    configured: bool = False

    def send(self, message: Message) -> None:
        self.sent.append(message)

    def last_to(self, address: str) -> Optional[Message]:
        for message in reversed(self.sent):
            if message.to.lower() == address.strip().lower():
                return message
        return None


@dataclass
class BrevoMailer:
    api_key: str
    sender: str
    sender_name: str = "Rungs"
    configured: bool = True

    def send(self, message: Message) -> None:
        payload = json.dumps({
            "sender": {"email": self.sender, "name": self.sender_name},
            "to": [{"email": message.to}],
            "subject": message.subject,
            "textContent": message.text,
        }).encode("utf-8")
        request = urllib.request.Request(
            BREVO_ENDPOINT,
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": USER_AGENT,
                # Brevo issues two credentials on the same settings page. This
                # is the *API* key; the SMTP key authenticates a relay we do
                # not use, and swapping them fails with an opaque 401.
                "api-key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status >= 300:
                    raise MailError(f"Brevo returned {response.status}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise MailError(f"Brevo rejected the message ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise MailError(f"could not reach Brevo: {exc.reason}") from exc


#: Render injects this into every web service: the full https:// URL of the
#: service. It is the onrender.com address specifically, not a custom domain,
#: which is why an explicit setting still wins.
RENDER_URL_VAR = "RENDER_EXTERNAL_URL"


def public_base_url() -> str:
    """Where this deployment answers, for building links that leave the app.

    Not inferred from the request. Behind Render's proxy the Host header is
    whatever the caller sent, so a link built from it follows the attacker's
    hostname rather than ours — and a reset link is exactly the wrong thing to
    point somewhere else.

    Falling back to Render's own variable removes a genuine trap: the hostname
    does not exist until the service has been created, so a deploy that needed
    `PUBLIC_BASE_URL` set by hand shipped its first reset links pointing at
    localhost, and only a second deploy fixed them. Set it explicitly for a
    custom domain, where Render's value would still name the onrender host.
    """
    for name in ("PUBLIC_BASE_URL", RENDER_URL_VAR):
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/")
    return "http://127.0.0.1:8000"


def build_mailer():
    """The configured transport, or a `NullMailer` when there is no key."""
    key = os.environ.get("BREVO_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM", "").strip()
    if not key or not sender:
        return NullMailer()
    return BrevoMailer(
        api_key=key,
        sender=sender,
        sender_name=os.environ.get("MAIL_FROM_NAME", "Rungs").strip()
        or "Rungs",
    )


RESET_SUBJECT = "Reset your Rungs password"

RESET_BODY = """\
Someone asked to reset the password on the account for {email}.

Open this link to choose a new one:

{link}

The link works once and expires in {minutes} minutes. If that was not you,
ignore this message — nothing has changed and the link will lapse on its own.

The first page load after a quiet period can take up to a minute while the
server wakes up.
"""


def reset_message(email: str, link: str, minutes: int) -> Message:
    return Message(
        to=email,
        subject=RESET_SUBJECT,
        text=RESET_BODY.format(email=email, link=link, minutes=minutes),
    )
