# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, rather than opening a public issue.

This is a personal project maintained in spare time — expect a best-effort
response, not a guaranteed SLA.

## What this tool does with your credentials

**Your GitHub token is stored unencrypted** in the local SQLite database,
in the `settings` table. The Settings field stops displaying it after you
save, but that is a UI convenience, not protection: anyone who can read
that file can read the token.

- On Windows the database lives under your per-user `%APPDATA%`, protected
  by the account's ACL.
- On macOS/Linux the tool sets owner-only permissions (`0600` on the file,
  `0700` on its directory) when it creates the database.
- Use a token with the minimum possible scopes — a **classic** PAT needs
  **no scopes ticked at all**, a **fine-grained** PAT needs only
  "Public Repositories (read-only)".
- [Revoke the token](https://github.com/settings/tokens) if the machine is
  shared, lost, or compromised.

If you would rather not store a token at all, set `GITHUB_TOKEN` (or
`GH_TOKEN`) in the environment instead — it takes precedence over the
saved value and is never written to the database.

## Network and data boundaries

The tool talks to four hosts and nothing else:

| Host | Purpose | Token sent |
|---|---|---|
| `api.github.com` | REST + GraphQL queries | yes |
| `raw.githubusercontent.com` | raw file probe for orphan commits | yes |
| `github.com` | `git fetch` / `git clone` over HTTPS | no |
| `data.gharchive.org` | public hourly archive downloads | no |

There is no telemetry, no analytics and no crash reporting.

**Nothing is ever pushed.** Recovery clones and rescues are written to your
disk only; the tool never creates a repository or writes to GitHub on your
behalf.

## Data at rest

The monitored-repository database accumulates public metadata about
third-party repositories — names, owners, and the commit author/message of
any commit it probes. This stays on your machine; `data/` is gitignored so
it cannot be committed by accident.

## Hardening already in place

- Commit SHAs and owner/repository names are validated before they reach a
  `git` command line, and refs are passed after a `--` separator so a
  crafted value cannot be parsed as an option.
- Rescue and clone destinations are checked to resolve inside the intended
  destination root.
- Remote-derived text (commit messages, repository names) is rendered as
  plain text in the UI, so it cannot forge markup in the report.
- No `shell=True` anywhere; every subprocess call passes an argument list
  and a timeout.
