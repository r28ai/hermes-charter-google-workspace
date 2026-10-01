"""Offline tests: Gmail is mocked with respx, nothing reaches the network."""

import base64
import email
import importlib.util
import json
from email import policy
from pathlib import Path

import httpx
import pytest
import respx

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gws_charter.py"
GMAIL = "gmail.googleapis.com"
OWN = "me@example.com"


@pytest.fixture
def skill(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "google_token.json").write_text(json.dumps({
        "type": "authorized_user", "token": "ya29.old", "refresh_token": "1//refresh",
        "client_id": "client.apps.googleusercontent.com", "client_secret": "secret",
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = importlib.util.spec_from_file_location("gws_charter_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def google():
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(return_value=httpx.Response(
            200, json={"access_token": "ya29.fresh", "expires_in": 3600, "token_type": "Bearer"}))
        mock.get(host=GMAIL, path="/gmail/v1/users/me/profile").mock(
            return_value=httpx.Response(200, json={"emailAddress": OWN}))
        sent = []

        def send(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "s1", "threadId": sent[-1].get("threadId", "t-new")})

        mock.post(host=GMAIL, path="/gmail/v1/users/me/messages/send").mock(side_effect=send)
        mock.sent = sent
        yield mock


def original(mock, headers, message_id="m1", thread_id="t1"):
    mock.get(host=GMAIL, path=f"/gmail/v1/users/me/messages/{message_id}").mock(return_value=httpx.Response(
        200, json={"id": message_id, "threadId": thread_id,
                   "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]}}))


def wire(body):
    raw = body["raw"]
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return data, email.message_from_bytes(data, policy=policy.default)


def run(skill, capsys, *argv):
    code = skill.main(list(argv))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out else None), captured.err


def test_reply_to_own_sent_message_goes_to_its_recipients(skill, google, capsys):
    original(google, {"From": f"Me <{OWN}>", "To": "Support Desk <support@example.com>",
                      "Subject": "Order 1234", "Message-ID": "<a@x>"})
    code, out, _ = run(skill, capsys, "gmail", "reply", "m1", "--body", "ping")
    assert code == 0 and out["status"] == "sent"
    _, msg = wire(google.sent[0])
    assert [a.addr_spec for a in msg["To"].addresses] == ["support@example.com"]


def test_reply_to_non_ascii_sender_encodes_the_display_name_only(skill, google, capsys):
    original(google, {"From": '"Jörg Müller" <jorg@example.de>', "To": OWN,
                      "Subject": "Angebot", "Message-ID": "<b@x>"})
    code, _, _ = run(skill, capsys, "gmail", "reply", "m1", "--body", "Danke")
    assert code == 0
    data, msg = wire(google.sent[0])
    to_line = next(line for line in data.replace(b"\r\n ", b" ").split(b"\r\n") if line.startswith(b"To:"))
    assert b"<jorg@example.de>" in to_line
    assert [(a.display_name, a.addr_spec) for a in msg["To"].addresses] == [("Jörg Müller", "jorg@example.de")]


@pytest.mark.parametrize("subject", ["RE: Order 1234", "re: Order 1234", "Re: Order 1234"])
def test_reply_keeps_an_existing_re_prefix_in_any_case(skill, google, capsys, subject):
    original(google, {"From": "Shop <shop@example.com>", "To": OWN, "Subject": subject, "Message-ID": "<c@x>"})
    run(skill, capsys, "gmail", "reply", "m1", "--body", "ok")
    _, msg = wire(google.sent[0])
    assert str(msg["Subject"]) == subject


def test_reply_threads_with_in_reply_to_and_the_references_chain(skill, google, capsys):
    original(google, {"From": "Shop <shop@example.com>", "To": OWN, "Subject": "Order",
                      "Message-ID": "<c@x>", "References": "<a@x> <b@x>"}, thread_id="t9")
    run(skill, capsys, "gmail", "reply", "m1", "--body", "ok")
    body = google.sent[0]
    _, msg = wire(body)
    assert body["threadId"] == "t9"
    assert str(msg["In-Reply-To"]) == "<c@x>"
    assert str(msg["References"]) == "<a@x> <b@x> <c@x>"
    assert str(msg["Subject"]) == "Re: Order"


def test_reply_all_keeps_everyone_but_you(skill, google, capsys):
    original(google, {"From": "Ann <ann@example.com>", "To": f"Me <{OWN}>, Bob <bob@example.com>",
                      "Cc": "Cy <cy@example.com>, ann@example.com", "Subject": "Plan", "Message-ID": "<d@x>"})
    run(skill, capsys, "gmail", "reply", "m1", "--body", "ok", "--all")
    _, msg = wire(google.sent[0])
    assert [a.addr_spec for a in msg["To"].addresses] == ["ann@example.com", "bob@example.com"]
    assert [a.addr_spec for a in msg["Cc"].addresses] == ["cy@example.com"]


def test_reply_prefers_reply_to(skill, google, capsys):
    original(google, {"From": "Noreply <noreply@example.com>", "Reply-To": "Desk <desk@example.com>",
                      "To": OWN, "Subject": "Ticket", "Message-ID": "<e@x>"})
    run(skill, capsys, "gmail", "reply", "m1", "--body", "ok")
    _, msg = wire(google.sent[0])
    assert [a.addr_spec for a in msg["To"].addresses] == ["desk@example.com"]


def test_send_with_non_ascii_names_and_html(skill, google, capsys):
    code, out, _ = run(skill, capsys, "gmail", "send", "--to", '"Jörg Müller" <jorg@example.de>',
                       "--cc", "Bo Ärger <bo@example.de>", "--from", '"Agent Ünicode" <me@example.com>',
                       "--subject", "Grüße", "--body", "<p>Hallo</p>", "--html")
    assert code == 0 and out == {"status": "sent", "id": "s1", "threadId": "t-new"}
    data, msg = wire(google.sent[0])
    data.decode("ascii")
    assert msg.get_content_type() == "text/html"
    assert msg["To"].addresses[0].display_name == "Jörg Müller"
    assert msg["Cc"].addresses[0].addr_spec == "bo@example.de"
    assert msg["From"].addresses[0].display_name == "Agent Ünicode"


def test_dry_run_reply_resolves_and_sends_nothing(skill, google, capsys):
    original(google, {"From": f"Me <{OWN}>", "To": "Shop <shop@example.com>",
                      "Subject": "Order 1234", "Message-ID": "<x@example.com>"})
    code, out, _ = run(skill, capsys, "gmail", "reply", "m1", "--body", "ping", "--dry-run")
    assert code == 0 and google.sent == []
    assert out["status"] == "dry-run" and out["threadId"] == "t1"
    assert out["headers"]["To"] == "Shop <shop@example.com>"
    assert out["headers"]["In-Reply-To"] == "<x@example.com>"


def test_dry_run_send_sends_nothing(skill, google, capsys):
    code, out, _ = run(skill, capsys, "gmail", "send", "--to", "a@example.com", "--subject", "S",
                       "--body", "B", "--dry-run")
    assert code == 0 and google.sent == []
    assert out["headers"]["To"] == "a@example.com"


def test_a_line_break_in_a_header_is_refused_and_nothing_is_sent(skill, google, capsys):
    code, out, err = run(skill, capsys, "gmail", "send", "--to", "a@example.com",
                         "--subject", "Hello\r\nBcc: someone@evil.example", "--body", "B")
    assert code == 1 and out is None and google.sent == []
    assert "single line" in json.loads(err)["error"]


def test_search_returns_headers_for_each_match(skill, google, capsys):
    google.get(host=GMAIL, path="/gmail/v1/users/me/messages").mock(return_value=httpx.Response(
        200, json={"messages": [{"id": "m1", "threadId": "t1"}]}))
    original(google, {"From": "Ann <ann@example.com>", "To": OWN, "Subject": "Hi", "Date": "Thu, 1 Oct 2026"})
    code, out, _ = run(skill, capsys, "gmail", "search", "is:unread", "--max", "5")
    assert code == 0
    assert out == [{"id": "m1", "threadId": "t1", "from": "Ann <ann@example.com>", "to": OWN,
                    "subject": "Hi", "date": "Thu, 1 Oct 2026", "snippet": ""}]


def test_missing_token_is_a_readable_error(skill, google, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))
    code, out, err = run(skill, capsys, "gmail", "send", "--to", "a@example.com", "--subject", "S", "--body", "B")
    assert code == 1 and out is None
    assert "google-workspace skill's setup" in json.loads(err)["error"]
