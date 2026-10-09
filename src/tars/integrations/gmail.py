"""Gmail: summarise unread mail, read messages, archive/label, draft and send.

Mail content reaches the model only as tool results, stripped of HTML and hidden
text and fenced as untrusted data. Sending always goes through the gate.
"""

from __future__ import annotations

import asyncio
import base64
from email.message import EmailMessage
from typing import Any

from ..actions import ActionResult, Tier, Tool, ToolError, schema
from ..sanitize import clean_text, html_to_text, wrap_untrusted
from .google_auth import GoogleSession

API = "https://gmail.googleapis.com/gmail/v1/users/me"


def _header(msg: dict[str, Any], name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to visible text from text/html."""
    plain, html = [], []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return clean_text("\n".join(plain))
    return html_to_text("\n".join(html))


def reply_subject(subject: str | None, original: str, is_reply: bool) -> str:
    """The subject actually used: the given one, or "Re: <original>" for a reply."""
    if subject:
        return subject
    if not is_reply:
        return ""
    return original if original.lower().startswith("re:") else f"Re: {original}"


def build_raw(to: list[str], subject: str, body: str, headers: dict[str, str] | None = None) -> str:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class Gmail:
    def __init__(self, session: GoogleSession):
        self.s = session

    async def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        listing = await self.s.request("GET", f"{API}/messages", params={"q": query, "maxResults": limit})
        # Fetched together: one at a time adds a round trip per message to the reply time.
        return list(await asyncio.gather(*(
            self.s.request("GET", f"{API}/messages/{ref['id']}",
                           params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
            for ref in listing.get("messages", [])
        )))

    async def get(self, message_id: str) -> dict[str, Any]:
        return await self.s.request("GET", f"{API}/messages/{message_id}", params={"format": "full"})

    async def modify(self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None) -> None:
        await self.s.request("POST", f"{API}/messages/batchModify", json={
            "ids": ids, "addLabelIds": add or [], "removeLabelIds": remove or [],
        })

    async def label_id(self, name: str) -> str:
        labels = (await self.s.request("GET", f"{API}/labels")).get("labels", [])
        for label in labels:
            if label["name"].lower() == name.lower():
                return label["id"]
        created = await self.s.request("POST", f"{API}/labels", json={"name": name})
        return created["id"]

    async def create_draft(self, raw: str, thread_id: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        return await self.s.request("POST", f"{API}/drafts", json={"message": message})

    async def delete_draft(self, draft_id: str) -> None:
        await self.s.request("DELETE", f"{API}/drafts/{draft_id}")

    async def send(self, raw: str, thread_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return await self.s.request("POST", f"{API}/messages/send", json=body)

    async def reply_context(self, message_id: str) -> tuple[str, dict[str, str], str]:
        """Thread id, threading headers and original subject for a reply."""
        orig = await self.s.request(
            "GET", f"{API}/messages/{message_id}",
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "References", "Subject"]},
        )
        mid = _header(orig, "Message-ID")
        refs = (_header(orig, "References") + " " + mid).strip()
        headers = {"In-Reply-To": mid, "References": refs} if mid else {}
        return orig["threadId"], headers, _header(orig, "Subject")


def build_tools(gmail: Gmail) -> list[Tool]:
    async def list_mail(args: dict[str, Any]) -> ActionResult:
        query = args.get("query") or "is:unread in:inbox"
        msgs = await gmail.search(query, min(args.get("limit") or 10, 25))
        if not msgs:
            return ActionResult("No matching mail.")
        rows = [
            f"[id {m['id']}] From: {_header(m, 'From')} | Subject: {_header(m, 'Subject')} | "
            f"{_header(m, 'Date')}\n  {clean_text(m.get('snippet', ''))}"
            for m in msgs
        ]
        return ActionResult(wrap_untrusted("gmail", "\n".join(rows)), untrusted=True)

    async def read_mail(args: dict[str, Any]) -> ActionResult:
        msg = await gmail.get(args["message_id"])
        head = f"From: {_header(msg, 'From')}\nTo: {_header(msg, 'To')}\nSubject: {_header(msg, 'Subject')}\n"
        return ActionResult(wrap_untrusted("gmail", head + "\n" + extract_body(msg["payload"])), untrusted=True)

    async def archive(args: dict[str, Any]) -> ActionResult:
        ids = args["message_ids"]
        await gmail.modify(ids, remove=["INBOX"])

        async def undo() -> str:
            await gmail.modify(ids, add=["INBOX"])
            return "Moved it back to the inbox."

        return ActionResult(f"Archived {len(ids)} message(s).", undo=undo, undo_label="archive mail")

    async def label(args: dict[str, Any]) -> ActionResult:
        ids = args["message_ids"]
        label_id = await gmail.label_id(args["label"])
        await gmail.modify(ids, add=[label_id])

        async def undo() -> str:
            await gmail.modify(ids, remove=[label_id])
            return "Took the label off."

        return ActionResult(f"Labelled {len(ids)} message(s) {args['label']}.", undo=undo, undo_label="label mail")

    async def _draft(args: dict[str, Any]) -> ActionResult:
        args = {k: v for k, v in args.items() if k != "draft_id"}
        thread_id, headers, orig_subject = None, {}, ""
        if args.get("reply_to_message_id"):
            thread_id, headers, orig_subject = await gmail.reply_context(args["reply_to_message_id"])
        subject = reply_subject(args.get("subject"), orig_subject, thread_id is not None)
        draft = await gmail.create_draft(build_raw(args["to"], subject, args["body"], headers), thread_id)

        async def undo() -> str:
            await gmail.delete_draft(draft["id"])
            return "Deleted that draft."

        return ActionResult(f"Draft saved to {', '.join(args['to'])}: '{subject}'. [draft {draft['id']}]",
                            undo=undo, undo_label="draft email")

    async def send(args: dict[str, Any]) -> str:
        thread_id, headers, orig_subject = None, {}, ""
        if args.get("reply_to_message_id"):
            thread_id, headers, orig_subject = await gmail.reply_context(args["reply_to_message_id"])
        subject = reply_subject(args.get("subject"), orig_subject, thread_id is not None)
        await gmail.send(build_raw(args["to"], subject, args["body"], headers), thread_id)
        if args.get("draft_id"):
            try:
                await gmail.delete_draft(args["draft_id"])
            except ToolError:
                pass  # sent either way; a leftover draft is harmless
        return f"Sent to {', '.join(args['to'])}."

    async def send_read_back(args: dict[str, Any]) -> str:
        reply, orig_subject = "", ""
        if args.get("reply_to_message_id"):
            _, _, orig_subject = await gmail.reply_context(args["reply_to_message_id"])
            reply = f" as a reply to '{orig_subject}'"
        subject = reply_subject(args.get("subject"), orig_subject, bool(reply))
        about = f", subject '{subject}'" if subject else ", with no subject"
        return f"Email to {', '.join(args['to'])}{reply}{about}, saying: \"{args['body']}\"."

    compose = {
        "to": {"type": "array", "items": {"type": "string"}, "description": "Email addresses"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "reply_to_message_id": {"type": "string"},
    }
    send_fields = {**compose, "draft_id": {"type": "string", "description": "The draft being sent, if any; "
                                                                        "it's removed after sending"}}
    ids = {"message_ids": {"type": "array", "items": {"type": "string"}}}
    return [
        Tool("list_email", "List email (default: unread in inbox) with sender, subject and snippet. "
             "`query` uses Gmail search syntax.",
             schema({"query": {"type": "string"}, "limit": {"type": "integer"}}),
             Tier.READ, list_mail, cue="Checking your email.", service="Gmail"),
        Tool("read_email", "Read one email in full by id.",
             schema({"message_id": {"type": "string"}}, ["message_id"]),
             Tier.READ, read_mail, cue="Opening that email.", service="Gmail"),
        Tool("archive_email", "Archive messages (remove from inbox).", schema(ids, ["message_ids"]),
             Tier.CREATE_FOR_YOU, archive, service="Gmail",
             read_back=lambda a: f"Archive {len(a['message_ids'])} message{'s' * (len(a['message_ids']) != 1)}."),
        Tool("label_email", "Add a label to messages, creating the label if needed.",
             schema({**ids, "label": {"type": "string"}}, ["message_ids", "label"]),
             Tier.CREATE_FOR_YOU, label, service="Gmail",
             read_back=lambda a: f"Label {len(a['message_ids'])} message{'s' * (len(a['message_ids']) != 1)} {a['label']}."),
        Tool("draft_email", "Save an email or reply as a Gmail draft without sending it.",
             schema(compose, ["to", "body"]), Tier.CREATE_FOR_YOU, _draft, service="Gmail",
             read_back=lambda a: f"Save a draft to {', '.join(a['to'])}: \"{a['body']}\"."),
        Tool("send_email", "Send an email or reply. The user must confirm the exact text first. "
             "To send a saved draft, pass its full text and its draft_id.",
             schema(send_fields, ["to", "body"]), Tier.AFFECTS_OTHERS, send,
             read_back=send_read_back, park=_draft, service="Gmail"),
    ]


__all__ = ["Gmail", "build_tools", "extract_body", "build_raw", "reply_subject", "ToolError"]
