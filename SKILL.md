---
name: google-workspace-charter
description: "Gmail search, read, send and reply on Charter: replies reach the right person, non-ASCII names are accepted, --dry-run shows recipients before sending."
version: 0.1.0
author: Nathan Queme (nathanqueme)
license: Apache-2.0
platforms: [linux, macos, windows]
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 token, the one the google-workspace skill's setup creates
metadata:
  hermes:
    tags: [Google, Gmail, Email, OAuth, Charter]
    homepage: https://github.com/r28ai/charter
    related_skills: [google-workspace]
    requires_toolsets: [terminal]
---

# Google Workspace on Charter

Gmail through [Charter](https://github.com/r28ai/charter), an open-source library
that declares the Gmail API and builds its wire format at runtime. Nothing in this
skill assembles MIME by hand, so the things that break in hand-built email are
handled in one tested place:

- A reply to a message **you** sent, from your address or any send-as alias, goes to
  that message's recipients, not back to you.
- Non-ASCII display names (`"Jörg Müller" <jorg@example.de>`) are encoded on the
  name only, so Gmail accepts them in To, Cc, Bcc, From and Reply-To.
- An Outlook `RE:` subject is not prefixed a second time.
- A line break in a header field is refused, so it can never become an extra header.
- `--dry-run` prints exactly who a message would go to, and sends nothing.

Version 0.1 covers Gmail. Calendar and Sheets come next.

## Setup

1. This skill reuses the token the bundled `google-workspace` skill creates
   (`google_token.json` in your Hermes home). If that skill isn't set up yet, run
   its setup first. Nothing else to authorize.
2. Install Charter into the Python that runs Hermes skills:

```bash
python -m pip install 'charter-ai>=0.2.7'
```

## Scripts

- `scripts/gws_charter.py` — the CLI below. Prints JSON; errors are JSON on stderr with exit code 1.

```bash
CG="python ${HERMES_SKILL_DIR}/scripts/gws_charter.py"
```

If the line above still contains `HERMES_SKILL_DIR`, template substitution is off:
use the `[Skill directory: ...]` path shown when this skill loaded.

## Gmail

```bash
# Find mail (Gmail search syntax)
$CG gmail search "is:unread newer_than:2d" --max 10

# Read one message: headers, text body, attachment names
$CG gmail get MESSAGE_ID

# Send
$CG gmail send --to user@example.com --subject "Hello" --body "Message text"
$CG gmail send --to '"Jörg Müller" <jorg@example.de>' --cc team@example.com --subject "Hallo" --body "..."
$CG gmail send --to user@example.com --subject "Report" --body "<h1>Q4</h1>" --html
$CG gmail send --to user@example.com --subject "Hello" --from '"Research Agent" <you@example.com>' --body "..."

# Reply: threads it (In-Reply-To, References, thread ID). Goes to Reply-To,
# then From; on your own sent message, to that message's recipients.
$CG gmail reply MESSAGE_ID --body "Thanks, that works for me."
$CG gmail reply MESSAGE_ID --all --body "Looping everyone in."   # everyone on the thread except you

# Check before sending: resolve recipients and print the headers, send nothing
$CG gmail reply MESSAGE_ID --body "..." --dry-run
$CG gmail send --to user@example.com --subject "Hi" --body "..." --dry-run
```

When the user hasn't said who a reply should go to and the thread has several
people on it, run the reply with `--dry-run` first and confirm the recipients.

## Output

- `search`: `[{id, threadId, from, to, subject, date, snippet}]`
- `get`: `{id, threadId, from, to, cc, subject, date, labels, body, attachments}`
- `send` / `reply`: `{status: "sent", id, threadId}`. With `--dry-run`: `{status: "dry-run", threadId, headers}`.

## Notes

- `--from` with an address other than the account's own needs that address set up
  as a Send As alias in Gmail settings.
- `get` reads text from every nested part, so a message with attachments still
  has its body. When the plain-text part is a stub ("View this email in your
  browser") beside a longer HTML version, `body` is the HTML's text instead.
  A message forwarded as an attachment (`message/rfc822`) is not read inside;
  open that one in Gmail.
