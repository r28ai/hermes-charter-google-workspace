#!/usr/bin/env python3
"""Gmail for Hermes Agent, built on Charter.

The Gmail API is declared with Charter (https://github.com/r28ai/charter), and
Charter's runtime builds the wire format: the RFC 2822 message, its base64url
envelope, and address headers that stay valid for non-ASCII display names.
Nothing here assembles MIME by hand.

It reads the OAuth token the bundled google-workspace skill creates
(<HERMES_HOME>/google_token.json), so there is nothing new to set up.

Usage:
  python gws_charter.py gmail search "is:unread" [--max 10]
  python gws_charter.py gmail get MESSAGE_ID
  python gws_charter.py gmail send --to ADDR --subject TEXT --body TEXT
                                   [--cc ADDR] [--bcc ADDR] [--from ADDR] [--html]
                                   [--thread-id ID] [--dry-run]
  python gws_charter.py gmail reply MESSAGE_ID --body TEXT [--all] [--from ADDR] [--dry-run]

Output is JSON on stdout. Errors are JSON on stderr, with exit code 1.
"""

# No `from __future__ import annotations`: GetProfile's fields must be real
# objects at class creation, since Hermes may load this file by path, where
# pydantic cannot resolve string annotations.
import argparse
import asyncio
import base64
import html
import json
import os
import sys
from email import message_from_bytes, policy
from email.utils import getaddresses
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple

INSTALL_HINT = "python -m pip install 'charter-ai>=0.2.5'"

try:
    from charter import Path as PathParam
    from charter import oauth_tool_factory
    from charter.auth import OAuth2Client, OAuth2Server
    from charter.packs import gmail
    from charter.transforms import apply_transform
    from pydantic import BaseModel
except ImportError:  # pragma: no cover - only without the dependency
    print(json.dumps({"error": f"charter-ai is not installed. Run: {INSTALL_HINT}"}), file=sys.stderr)
    sys.exit(1)


GOOGLE = OAuth2Server(
    issuer="https://accounts.google.com",
    authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    token_endpoint_auth_method="client_secret_post",
    authorization_params={"access_type": "offline", "prompt": "consent"},
)

REPLY_HEADERS = ["From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References"]
SEARCH_HEADERS = ["From", "To", "Subject", "Date"]


class SkillError(Exception):
    """A failure the agent can act on, reported as JSON."""


# ---------------------------------------------------------------------------
# Credentials: reuse the bundled google-workspace skill's token
# ---------------------------------------------------------------------------


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home())
    except Exception:
        value = os.environ.get("HERMES_HOME", "").strip()
        return Path(value) if value else Path.home() / ".hermes"


def google_client() -> OAuth2Client:
    home = hermes_home()
    token_path = home / "google_token.json"
    if not token_path.exists():
        raise SkillError(
            f"No Google token at {token_path}. Run the google-workspace skill's setup once "
            "(it creates this file); this skill reuses it."
        )
    token = json.loads(token_path.read_text())
    client_id, client_secret = token.get("client_id"), token.get("client_secret")
    if not (client_id and client_secret):
        # Older tokens keep the client in the downloaded client-secret file.
        secret_path = home / "google_client_secret.json"
        if secret_path.exists():
            secret = json.loads(secret_path.read_text())
            app = secret.get("installed") or secret.get("web") or {}
            client_id, client_secret = app.get("client_id"), app.get("client_secret")
    if not (token.get("refresh_token") and client_id and client_secret):
        raise SkillError(
            f"{token_path} has no refresh_token/client_id/client_secret. "
            "Re-run the google-workspace skill's setup."
        )
    return OAuth2Client(
        GOOGLE, client_id=client_id, client_secret=client_secret, refresh_token=token["refresh_token"]
    )


# ---------------------------------------------------------------------------
# One endpoint the Gmail pack does not ship, declared here
# ---------------------------------------------------------------------------


class GetProfile(BaseModel):
    userId: Annotated[str, PathParam()] = "me"


class ListSendAs(BaseModel):
    userId: Annotated[str, PathParam()] = "me"


class Account:
    """Which addresses are "me": the account's own, plus its send-as aliases."""

    def __init__(self, credentials: OAuth2Client):
        google = oauth_tool_factory(
            pack="gmail-account",
            provider="google",
            base_url="https://gmail.googleapis.com/",
            credential_provider=credentials,
        )
        self.profile = google(
            name="users_get_profile",
            description="The authenticated account's Gmail address.",
            method="GET",
            url_template="gmail/v1/users/{userId}/profile",
            args_schema=GetProfile,
        )
        self.send_as = google(
            name="users_settings_send_as_list",
            description="The addresses this account can send as (its aliases).",
            method="GET",
            url_template="gmail/v1/users/{userId}/settings/sendAs",
            args_schema=ListSendAs,
        )

    async def addresses(self) -> Tuple[str, set]:
        primary = (await self.profile.ainvoke({})).get("emailAddress", "")
        if not primary:
            raise SkillError("users.getProfile returned no emailAddress.")
        mine = {primary.lower()}
        try:
            listed = await self.send_as.ainvoke({})
            mine |= {a["sendAsEmail"].lower() for a in (listed or {}).get("sendAs", []) if a.get("sendAsEmail")}
        except Exception:
            pass  # aliases refine the check; the account's own address is enough to go on
        return primary, mine


# ---------------------------------------------------------------------------
# Reply addressing
# ---------------------------------------------------------------------------


def _addresses(raw: Optional[str]) -> List[Tuple[str, str]]:
    return [(name, addr) for name, addr in getaddresses([raw or ""]) if addr and "@" in addr]


def _format(name: str, addr: str) -> str:
    if not name:
        return addr
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}" <{addr}>'


def _dedupe(addresses: List[Tuple[str, str]], skip: set) -> List[Tuple[str, str]]:
    seen, out = set(skip), []
    for name, addr in addresses:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            out.append((name, addr))
    return out


def reply_recipients(
    headers: Dict[str, str], primary: str, mine: set, reply_all: bool
) -> Tuple[List[str], List[str]]:
    """(to, cc) for a reply.

    Reply-To wins over From. Replying to a message you sent yourself, from your
    address or any send-as alias, goes to that message's recipients, never back
    to you. --all keeps everyone else on the thread. You are never a recipient,
    except on a note to yourself, whose reply comes back to you.
    """
    sender = _addresses(headers.get("reply-to") or headers.get("from"))
    original_to = _addresses(headers.get("to"))
    original_cc = _addresses(headers.get("cc"))

    if sender and all(addr.lower() in mine for _, addr in sender):
        to = original_to
        cc = original_cc if reply_all else []
    else:
        to = sender + (original_to if reply_all else [])
        cc = original_cc if reply_all else []

    to = _dedupe(to, mine)
    cc = _dedupe(cc, mine | {addr.lower() for _, addr in to})
    if not to:
        if any(addr.lower() in mine for _, addr in sender + original_to + original_cc):
            to = [("", primary)]
        else:
            raise SkillError("Cannot work out who to reply to from the original message's headers.")
    return [_format(*a) for a in to], [_format(*a) for a in cc]


def reply_subject(subject: str) -> str:
    # Case-insensitive: Outlook replies carry "RE:".
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _lower(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {name.lower(): value for name, value in (headers or {}).items()}


async def _deliver(content: Dict[str, Any], thread_id: Optional[str], dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        raw = apply_transform("rfc822_base64", content)
        message = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)
        return {
            "status": "dry-run",
            "threadId": thread_id or "",
            "headers": {name: str(value) for name, value in message.items()},
        }
    body: Dict[str, Any] = {"raw": content}
    if thread_id:
        body["threadId"] = thread_id
    result = await gmail.messages_send.ainvoke({"body": body})
    return {"status": "sent", "id": result.get("id", ""), "threadId": result.get("threadId", "")}


async def cmd_search(args) -> Any:
    listed = await gmail.messages_list.ainvoke({"q": args.query, "maxResults": args.max})
    ids = [m["id"] for m in (listed or {}).get("messages", [])]
    fetched = await asyncio.gather(*(
        gmail.messages_get.ainvoke({"id": mid, "format": "metadata", "metadataHeaders": SEARCH_HEADERS})
        for mid in ids
    ))
    out = []
    for message in fetched:
        h = _lower(message.get("headers"))
        out.append({
            "id": message.get("id"), "threadId": message.get("threadId"),
            "from": h.get("from", ""), "to": h.get("to", ""), "subject": h.get("subject", ""),
            # Gmail snippets arrive HTML-escaped ("You&#39;re")
            "date": h.get("date", ""), "snippet": html.unescape(message.get("snippet", "")),
        })
    return out


async def cmd_get(args) -> Any:
    message = await gmail.messages_get.ainvoke({"id": args.message_id, "format": "full"})
    h = _lower(message.get("headers"))
    return {
        "id": message.get("id"), "threadId": message.get("threadId"),
        "from": h.get("from", ""), "to": h.get("to", ""), "cc": h.get("cc", ""),
        "subject": h.get("subject", ""), "date": h.get("date", ""),
        "labels": message.get("labelIds", []), "body": message.get("bodyText", "").replace("\r\n", "\n"),
        "attachments": message.get("attachments", []),
    }


async def cmd_send(args) -> Any:
    content: Dict[str, Any] = {"to": args.to, "subject": args.subject, "body": args.body}
    if args.html:
        content["mimeType"] = "text/html"
    for key, value in (("cc", args.cc), ("bcc", args.bcc), ("from", args.from_header)):
        if value:
            content[key] = value
    return await _deliver(content, args.thread_id, args.dry_run)


async def cmd_reply(args, account: "Account") -> Any:
    original = await gmail.messages_get.ainvoke(
        {"id": args.message_id, "format": "metadata", "metadataHeaders": REPLY_HEADERS}
    )
    h = _lower(original.get("headers"))
    primary, mine = await account.addresses()
    to, cc = reply_recipients(h, primary, mine, args.all)

    content: Dict[str, Any] = {"to": to, "subject": reply_subject(h.get("subject", "")), "body": args.body}
    if cc:
        content["cc"] = cc
    if args.from_header:
        content["from"] = args.from_header
    if h.get("message-id"):
        content["in_reply_to"] = h["message-id"]
        content["references"] = " ".join(x for x in (h.get("references", ""), h["message-id"]) if x)
    return await _deliver(content, original.get("threadId"), args.dry_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gmail for Hermes Agent, built on Charter.")
    service = parser.add_subparsers(dest="service", required=True)
    g = service.add_parser("gmail").add_subparsers(dest="command", required=True)

    p = g.add_parser("search")
    p.add_argument("query")
    p.add_argument("--max", type=int, default=10)

    p = g.add_parser("get")
    p.add_argument("message_id")

    p = g.add_parser("send")
    p.add_argument("--to", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--cc", default="")
    p.add_argument("--bcc", default="")
    p.add_argument("--from", dest="from_header", default="", help='e.g. \'"Research Agent" <you@example.com>\'')
    p.add_argument("--html", action="store_true", help="Send the body as HTML")
    p.add_argument("--thread-id", default="")
    p.add_argument("--dry-run", action="store_true", help="Build the message and print its headers; send nothing")

    p = g.add_parser("reply")
    p.add_argument("message_id")
    p.add_argument("--body", required=True)
    p.add_argument("--all", action="store_true", help="Reply to everyone on the thread, not only the sender")
    p.add_argument("--from", dest="from_header", default="", help='e.g. \'"Support Bot" <you@example.com>\'')
    p.add_argument("--dry-run", action="store_true", help="Resolve recipients and print headers; send nothing")
    return parser


async def run(argv: Optional[List[str]] = None) -> Any:
    args = build_parser().parse_args(argv)
    credentials = google_client()
    gmail.configure(credential_provider=credentials)
    if args.command == "search":
        return await cmd_search(args)
    if args.command == "get":
        return await cmd_get(args)
    if args.command == "send":
        return await cmd_send(args)
    return await cmd_reply(args, Account(credentials))


def main(argv: Optional[List[str]] = None) -> int:
    try:
        result = asyncio.run(run(argv))
    except SkillError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    except Exception as e:  # Charter's errors are written to be read; pass them through
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
