# Architecture & Design Decisions

A log of significant choices made during development — what we picked, what we rejected, and why.

---

## Template

```
## [YYYY-MM-DD] Decision Title

**Decision:** What we decided.

**Alternatives considered:** What else we looked at.

**Reason:** Why we chose this over the alternatives.

**Consequences:** Trade-offs or follow-on implications.
```

---

<!-- Add decisions below this line -->

## [2026-05-12] Email filtering: blocklist-only (no whitelist + regex)

**Decision:** Reject only emails whose domain appears in `DISPOSABLE_EMAIL_DOMAINS` frozenset in `security.py`. Any domain not in the list — including custom company domains like `@eksum.co.in` — is allowed through.

**Alternatives considered:**
- Whitelist of known-good providers (gmail, outlook, yahoo, etc.) — would block all company/custom domains by default, hostile to B2B users.
- Regex validation on domain structure — adds complexity with no real security benefit; determined bad actors can use real-looking domains anyway.
- Third-party API (e.g., Kickbox, ZeroBounce) for real-time disposable detection — adds latency, cost, and an external dependency at signup.

**Reason:** The goal is to reduce spam signups for the private beta, not to be a fortress. Blocking 65 known disposable providers catches the vast majority of throwaway accounts. Whitelisting would break legitimate users with non-standard domains before the product even launches.

**Consequences:** Motivated bad actors with a custom domain bypass the check. Acceptable for a 50-user private beta — revisit if spam becomes a real problem at scale.

---

## [2026-05-12] Transactional email: Resend over SendGrid / Mailgun

**Decision:** Use the Resend SDK (`resend==2.9.0`) for sending password reset emails.

**Alternatives considered:**
- **SendGrid** — industry standard but heavyweight SDK, requires API key with specific sender verification, free tier is 100 emails/day (enough) but UX is older.
- **Mailgun** — solid, but EU data-residency setup is awkward; free tier expires after trial.
- **SMTP directly (smtplib)** — zero dependencies but no deliverability infrastructure, likely to hit spam filters.
- **AWS SES** — cheapest at scale but setup involves domain verification, DKIM/SPF DNS records, and sandbox mode approval; overkill for 50 users.

**Reason:** Resend has the simplest Python SDK (`resend.Emails.send({...})`), modern developer experience, built-in domain verification, and a generous free tier (3,000 emails/month). API key is a single env var. Dev fallback (print to console when `RESEND_API_KEY` is empty) means local dev works with zero setup.

**Consequences:** Resend is a newer provider — less battle-tested than SendGrid at enterprise scale. Acceptable trade-off for a startup at beta stage.

---

## [2026-05-12] Password reset token expiry: 15 minutes

**Decision:** Password reset tokens expire 15 minutes after issuance. Tokens are single-use and hashed (SHA-256) in the database — raw token only ever exists in the email link.

**Alternatives considered:**
- **1 hour** — common default but gives a wider attack window if the email is compromised or forwarded.
- **24 hours** — maximises user convenience but unacceptable security posture for a credential reset action.
- **5 minutes** — tighter window, but users on slow email servers or who get distracted may find links expired before they act.

**Reason:** 15 minutes is the industry consensus for password reset links (NIST SP 800-63B spirit, OWASP recommendation). Short enough to limit exposure if an email is intercepted; long enough that a user who opens the email immediately has no trouble. Single-use invalidation means replaying a link after first use does nothing.

**Consequences:** Users who don't click within 15 minutes must request a new link. The `forgot-password` route invalidates previous unexpired tokens before creating a new one, so users can self-serve a fresh link immediately without confusion.
