# Google Workspace on Charter: a Hermes Agent skill

Gmail for [Hermes Agent](https://github.com/NousResearch/hermes-agent), built on
[Charter](https://github.com/r28ai/charter). Charter declares the Gmail API and builds
its wire format at runtime, so this skill assembles no MIME by hand:

- a reply to a message you sent goes to its recipients, not back to you
- non-ASCII display names are encoded on the name only, so Gmail accepts them
- an Outlook `RE:` subject is not prefixed twice
- a line break in a header field is refused instead of becoming an extra header
- `--dry-run` prints exactly who a message would go to, and sends nothing

It reuses the OAuth token Hermes's bundled `google-workspace` skill creates, so there
is nothing new to authorize. Version 0.1 covers Gmail; Calendar and Sheets come next.

## Install

```bash
hermes skills install https://raw.githubusercontent.com/r28ai/hermes-charter-google-workspace/main/SKILL.md
python -m pip install 'charter-ai>=0.2.5'
```

Usage is in [SKILL.md](SKILL.md).

## Tests

Offline, against a mocked Gmail:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

## License

Apache-2.0
