---
name: public-repo-hygiene
description: Use before every commit, push, PR, issue, or public artifact in the Deadband repository - this repo is PUBLIC and holds a system touching real brokerage and exchange accounts. Covers what must never be published, what the automated hook cannot catch, and what to do after a leak.
---

# Public-repo hygiene — Deadband

**This repository is public. The system it describes touches real money.**

Assume anything committed is permanent. GitHub caches, forks, clones, and search
indexes all survive a `git rm`, a force-push, and repository deletion. There is no
undo — the only reliable response to a published credential is to **revoke it**.

## The layered defense, and where it ends

| Layer | Catches | Does not catch |
|---|---|---|
| `.gitignore` | Whole categories of file | Anything force-added, or pasted into a tracked file |
| `.githooks/pre-commit` | Forbidden paths, deny-listed strings, gitleaks findings, huge files | Judgment calls; anything committed with `--no-verify` |
| **This skill** | The judgment calls | Nothing else — you are the last layer |

The hook exists because instructions get forgotten. This skill exists because a regex
cannot recognize that a paragraph describing an unpatched network weakness is more
dangerous than any API key in the repo.

**Never bypass the hook with `--no-verify` on your own judgment.** If it fires, either
fix the content or ask Michael. A false positive is a reason to refine `.gitleaks.toml`
or the deny-list — not a reason to skip the check once.

## Setup — verify before trusting

The hook only runs if `core.hooksPath` points at `.githooks`. A fresh clone does **not**
configure this automatically:

```bash
git config core.hooksPath .githooks     # required once per clone
git config --get core.hooksPath          # must print: .githooks
command -v gitleaks || ls /root/bin/gitleaks
```

If gitleaks is missing the hook prints a warning and **continues** — deliberately, so a
missing tool never silently blocks work. But a warning means secret scanning did not run.
Do not commit through it without reading the diff yourself.

## Never commit

**Credentials and key material.** Broker and exchange API keys, even read-only ones.
Wallet private keys or seed phrases. Database passwords. SSH keys. Session tokens.
A read-only exchange key still discloses complete position and balance history.

**Personal financial data.** Real account numbers. Real balances, positions, or P&L.
Actual venue exports — CSV files from a broker contain account numbers, and full
holdings. Test fixtures must be **synthetic or anonymized**, and live under
`fixtures/`, which is the only place a `.csv` may be tracked.

**Infrastructure detail.** Hostnames, tailnet or LAN IPs, ports, absolute paths on the
deployment host, compose project layout. All of it belongs in `docs/ops/`, which is
gitignored. The public contract lives in §10 of the design spec — properties the
deployment must satisfy, with no description of any particular machine.

**Anything about other people.** The deployment host shares a network with several
other people's accounts and runs services other people depend on. Their usernames,
services, and machines must never appear here. They did not consent to being in a
public repo, and it is not Michael's disclosure to make.

**Descriptions of unpatched weaknesses.** This is the one no scanner will ever catch,
and the most damaging. A sentence like *"the database container is published on all
interfaces and reachable from a network shared with other people"* is a working
attack description attributed to a named individual. Even after it is fixed, publishing
it tells anyone reading exactly where to look. Such notes go in `docs/ops/`, always.

## Judgment calls the hook cannot make

Before committing, ask of anything new:

1. **Does this describe a specific machine?** If a reader could learn what the
   deployment looks like, genericize it and move the specifics to `docs/ops/`.
2. **Is this data real?** Synthetic values that look plausible are fine. Real values
   that look boring are not — a balance of `1000.00` is still a real balance.
3. **Would this help someone attack Michael?** Not just credentials: schedules, backup
   locations, which venues hold the most, when systems are unattended.
4. **Does this name a third party?** Remove it.
5. **Would Michael be comfortable with a stranger reading this?** If the answer needs
   thought, the answer is no.

## Writing docs and commit messages

Commit messages, PR titles, and issue text are as public as code and are **not scanned
as thoroughly**. Never paste an error message, log line, stack trace, or terminal
output into one without reading it first — those routinely carry hostnames, absolute
paths, and connection strings.

The same applies to anything rendered or published outward: artifacts, gists, pastebins.
When in doubt about a destination, ask before sending.

## If something leaks

Speed matters more than tidiness. In order:

1. **Revoke the credential first.** Before any git work. Rotate the key at the venue,
   change the password, disable the token. History rewriting takes minutes; a key is
   compromised the second it is pushed.
2. **Tell Michael immediately.** Do not attempt to quietly clean it up.
3. Only then consider history rewriting, and understand it is mitigation rather than
   a fix — forks and caches may already hold it.
4. If it was personal financial data rather than a credential, there is nothing to
   revoke. Report it and let Michael decide whether the repository should go private.

**Never** hide a leak, downplay it, or judge it minor on your own. That call is not
yours to make.
