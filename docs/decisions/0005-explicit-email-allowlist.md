# 0005 — An explicit email allowlist alongside domain matching

**Status:** accepted

## Context

The original allowlist (`ALLOWED_EMAIL_DOMAINS`) checks only the domain half of
an email address. That is the right shape when every legitimate user is on a
domain you control — "anyone @yourcompany.com" is a sound policy because you
also control who gets an @yourcompany.com address.

It breaks down the moment legitimate users are on a domain you do *not*
control. Personal addresses — Gmail, iCloud, whatever — share a domain with
millions of strangers. `ALLOWED_EMAIL_DOMAINS=gmail.com` does not mean "these
two people"; it means "anyone who has ever made a Gmail account," which is not
an allowlist at all.

## Decision

Add a second allowlist, `ALLOWED_EMAILS`, matched on the full address rather
than the domain. The two lists are OR'd: a caller passes if they match either
one. This lets a deployment express both "anyone at this company" and "these
specific people," together or separately, without one policy shape being
misused to approximate the other.

## Two layers, on purpose

The email check in `app/auth.py` is a second gate, not the only one. The
primary gate is the Cloudflare Access **policy** itself — configured in the
Zero Trust dashboard as an Include rule on specific emails, which is where
Cloudflare actually stops an unauthorized login before a JWT is ever issued.

Both layers should agree, but they fail differently, which is why both are
worth keeping:

If the Access policy is ever loosened by mistake — someone widens it while
debugging and forgets to narrow it back — the application-level check still
rejects the request, because it does not trust Access's decision alone; it
re-verifies the JWT signature and then re-checks the claimed identity against
its own list (see `app/auth.py`'s existing comment on why header presence is
not authentication). Conversely, if the application-level allowlist is ever
left empty by a misconfigured deploy, the Access policy is still the thing
standing at the door.

Neither layer is a substitute for the other. Set both.

## Consequences

A deployment for a small, specifically-known set of people — personal
addresses, a handful of collaborators, anyone not on a shared corporate domain
— should leave `ALLOWED_EMAIL_DOMAINS` empty and list exact addresses in
`ALLOWED_EMAILS`, matching the same list configured as the Access policy's
Include rule. `.env.example` ships with placeholder addresses, not real ones:
this file is meant to be committed and shared, and real allowlisted emails are
a deployment-specific secret, not template content.
