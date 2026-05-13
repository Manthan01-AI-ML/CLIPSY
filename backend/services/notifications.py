"""
backend/services/notifications.py

Email delivery via Resend (https://resend.com).

Dev fallback: if RESEND_API_KEY is empty, reset links are printed to the
console instead of being sent. This lets the whole forgot-password flow be
tested locally without any API key.

Usage:
    from backend.services.notifications import send_password_reset_email
    send_password_reset_email(
        to_email="user@example.com",
        reset_url="https://app.clipsy.pro/reset-password?token=...",
        user_name="Alex",  # optional
    )
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.core.config import settings

logger = logging.getLogger(__name__)


def _build_reset_email_html(reset_url: str, user_name: Optional[str]) -> str:
    greeting = f"Hi {user_name}," if user_name else "Hi,"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reset your Clipsy password</title>
</head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;color:#f4f1ea;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:40px 20px;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#131311;border:1px solid #2a2a25;border-radius:8px;padding:40px 48px;max-width:560px;">
          <tr>
            <td style="padding-bottom:32px;border-bottom:1px solid #2a2a25;">
              <span style="font-size:24px;font-style:italic;color:#f4f1ea;letter-spacing:-0.02em;">Clip<em style="color:#ff5f3e;">sy</em></span>
            </td>
          </tr>
          <tr>
            <td style="padding:32px 0 24px;">
              <p style="margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:0.12em;color:#6b6761;font-family:'Courier New',monospace;">Password reset</p>
              <h1 style="margin:0 0 16px;font-size:32px;font-weight:400;letter-spacing:-0.03em;line-height:1.1;">{greeting}</h1>
              <p style="margin:0 0 24px;font-size:16px;line-height:1.6;color:#a09b8d;">
                We received a request to reset the password for your Clipsy account.
                Click the button below to choose a new password. This link expires in
                <strong style="color:#f4f1ea;">15 minutes</strong>.
              </p>
              <table cellpadding="0" cellspacing="0" style="margin:0 0 24px;">
                <tr>
                  <td style="background:#ff5f3e;border-radius:4px;">
                    <a href="{reset_url}"
                       style="display:inline-block;padding:14px 28px;color:#0a0a0a;font-size:15px;font-weight:500;text-decoration:none;letter-spacing:-0.01em;">
                      Reset my password
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin:0 0 8px;font-size:13px;color:#6b6761;line-height:1.5;">
                If the button doesn't work, copy and paste this URL into your browser:
              </p>
              <p style="margin:0 0 24px;font-size:12px;font-family:'Courier New',monospace;color:#ff5f3e;word-break:break-all;">
                {reset_url}
              </p>
              <p style="margin:0;font-size:13px;color:#6b6761;line-height:1.5;">
                If you didn't request a password reset, you can safely ignore this email.
                Your password won't change unless you click the link above.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding-top:24px;border-top:1px solid #2a2a25;">
              <p style="margin:0;font-size:11px;color:#6b6761;font-family:'Courier New',monospace;text-transform:uppercase;letter-spacing:0.1em;">
                Clipsy · AI-powered video repurposing
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_password_reset_email(
    to_email: str,
    reset_url: str,
    user_name: Optional[str] = None,
) -> None:
    """
    Send a password-reset email via Resend.

    If RESEND_API_KEY is empty (dev / CI), logs the reset URL to the console
    instead of sending a real email. This means the forgot-password flow is
    fully testable without any email credentials.

    Raises:
        Exception: if Resend returns an error (logged; caller decides whether
                   to surface to the user — auth.py swallows it and returns the
                   same generic 200 response regardless).
    """
    if not settings.RESEND_API_KEY:
        logger.warning(
            "[notifications] RESEND_API_KEY not set — printing reset link to console (dev mode)"
        )
        logger.info(f"[notifications] PASSWORD RESET LINK for {to_email}: {reset_url}")
        return

    try:
        import resend
        resend.api_key = settings.RESEND_API_KEY

        html_body = _build_reset_email_html(reset_url, user_name)

        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [to_email],
            "subject": "Reset your Clipsy password",
            "html": html_body,
        })
        logger.info(f"[notifications] password reset email sent to {to_email}")
    except Exception as e:
        logger.error(f"[notifications] failed to send reset email to {to_email}: {e}")
        raise
