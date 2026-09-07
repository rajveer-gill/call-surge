"""
Optional appointment confirmation emails via Resend (preferred) or SMTP.

Env:
  RESEND_API_KEY — Resend API key (https://resend.com)
  APPOINTMENT_EMAIL_FROM — verified sender, e.g. appointments@yourdomain.com
  APPOINTMENT_EMAIL_REPLY_TO — where replies go. The sending domain has no inbox, so
    without this a salon owner hitting reply on a notification gets a bounce.
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD — fallback if Resend unset
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)


def _from_address() -> str:
    return (os.getenv("APPOINTMENT_EMAIL_FROM") or os.getenv("RESEND_FROM") or "").strip()


def _reply_to_address() -> str:
    """Where a reply should land.

    Mail is sent from a domain that exists only to send — there is no mailbox behind
    appointments@mail.call-surge.com, so a reply to it bounces. Anyone replying to "new
    appointment request" is a customer with a question, and bouncing them is the worst
    possible answer.
    """
    return (os.getenv("APPOINTMENT_EMAIL_REPLY_TO") or "").strip()


def config_status() -> dict:
    """Booleans describing whether transactional email is configured on this (backend) host.

    Reflects the exact env this module uses to send — a Resend key OR an SMTP host counts as a
    transport. Values are booleans only; secret values are never returned. The marketing contact
    form runs on the frontend (Netlify) and is not observable from here."""
    resend = bool((os.getenv("RESEND_API_KEY") or "").strip())
    smtp = bool((os.getenv("SMTP_HOST") or "").strip())
    from_addr = bool(_from_address())
    operator_alert_to = bool((os.getenv("OPERATOR_ALERT_EMAIL") or "").strip())
    can_send = (resend or smtp) and from_addr
    return {
        "resend_key": resend,
        "smtp_host": smtp,
        "from_addr": from_addr,
        "operator_alert_to": operator_alert_to,
        "can_send": can_send,
        # Feedback / operator alerts need a transport, a sender, AND a recipient.
        "feedback_alerts_ready": can_send and operator_alert_to,
    }


def send_appointment_email(
    to: str,
    *,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
) -> bool:
    """Send one transactional email. Returns True on success."""
    to_addr = (to or "").strip()
    if not to_addr or "@" not in to_addr:
        return False
    from_addr = _from_address()
    if not from_addr:
        logger.info("appointment_email_skipped reason=no_from_address to=%s", to_addr[:3] + "…")
        return False
    text = (text_body or "").strip() or _html_to_plain(html_body)
    reply_to = _reply_to_address()
    if _send_via_resend(to_addr, from_addr, subject, html_body, text, reply_to):
        return True
    return _send_via_smtp(to_addr, from_addr, subject, html_body, text, reply_to)


def send_operator_alert(subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send an operational alert to the operator (e.g. a tenant crossing its usage cap).

    Recipient is OPERATOR_ALERT_EMAIL. No-op (returns False) when unset, so this is safe
    to call best-effort from a hot path. Reuses the same Resend/SMTP transport."""
    to_addr = (os.getenv("OPERATOR_ALERT_EMAIL") or "").strip()
    if not to_addr or "@" not in to_addr:
        logger.info("operator_alert_skipped reason=no_recipient")
        return False
    from_addr = _from_address()
    if not from_addr:
        logger.info("operator_alert_skipped reason=no_from_address")
        return False
    text = (text_body or "").strip() or _html_to_plain(html_body)
    if _send_via_resend(to_addr, from_addr, subject, html_body, text):
        return True
    return _send_via_smtp(to_addr, from_addr, subject, html_body, text)


def _html_to_plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").replace("&nbsp;", " ").strip()


def _send_via_resend(
    to: str, from_addr: str, subject: str, html: str, text: str, reply_to: str = ""
) -> bool:
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not key:
        return False
    try:
        import httpx

        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "from": from_addr,
                "to": [to],
                "subject": subject,
                "html": html,
                "text": text,
                **({"reply_to": [reply_to]} if reply_to else {}),
            },
            timeout=20.0,
        )
        if r.status_code in (200, 201):
            logger.info("appointment_email_sent provider=resend to=%s", to.split("@")[0] + "@…")
            return True
        logger.warning("appointment_email_resend_failed status=%s body=%s", r.status_code, (r.text or "")[:200])
    except Exception as e:
        logger.warning("appointment_email_resend_error: %s", e, exc_info=True)
    return False


def _send_via_smtp(
    to: str, from_addr: str, subject: str, html: str, text: str, reply_to: str = ""
) -> bool:
    host = (os.getenv("SMTP_HOST") or "").strip()
    if not host:
        return False
    port = int(os.getenv("SMTP_PORT") or "587")
    user = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            if port != 25:
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to], msg.as_string())
        logger.info("appointment_email_sent provider=smtp to=%s", to.split("@")[0] + "@…")
        return True
    except Exception as e:
        logger.warning("appointment_email_smtp_error: %s", e, exc_info=True)
    return False


def format_appointment_email(
    *,
    kind: str,
    business_name: str,
    customer_name: str,
    date: str,
    time_ampm: str,
    service: str = "",
) -> tuple[str, str, str]:
    """Return (subject, html, text) for kind in submitted | confirmed."""
    name = (customer_name or "there").strip()
    biz = (business_name or "the business").strip()
    svc = f"<p><strong>Service:</strong> {service}</p>" if service and service != "—" else ""
    svc_t = f"\nService: {service}" if service and service != "—" else ""
    if kind == "confirmed":
        subject = f"Appointment confirmed — {biz}"
        html = f"""
        <p>Hi {name},</p>
        <p>Your appointment at <strong>{biz}</strong> is <strong>confirmed</strong>.</p>
        <p><strong>When:</strong> {date} at {time_ampm}</p>
        {svc}
        <p>Reply to the business text thread or call the shop if you need to change anything.</p>
        """
        text = (
            f"Hi {name},\n\nYour appointment at {biz} is confirmed.\n"
            f"When: {date} at {time_ampm}.{svc_t}\n\nReply by text or call if you need to change."
        )
    else:
        subject = f"We received your appointment request — {biz}"
        html = f"""
        <p>Hi {name},</p>
        <p>We received your appointment request at <strong>{biz}</strong> and sent it to the shop for approval.</p>
        <p><strong>Requested time:</strong> {date} at {time_ampm}</p>
        {svc}
        <p>We'll text you when they confirm.</p>
        """
        text = (
            f"Hi {name},\n\nWe received your request at {biz}.\n"
            f"Requested: {date} at {time_ampm}.{svc_t}\n\nWe'll text you when they confirm."
        )
    return subject, html.strip(), text.strip()


def format_new_request_email(
    *,
    business_name: str,
    customer_name: str,
    customer_phone: str,
    date: str,
    time_ampm: str,
    service: str = "",
    stylist: str = "",
    dashboard_url: str = "",
) -> tuple[str, str, str]:
    """(subject, html, text) telling the SHOP a request just came in.

    Everything the desk needs to act is in the subject line, because this gets read on a
    phone between clients: who, when, and that it needs a response. The body carries the
    number to ring, since the whole point of a request is that somebody calls back.
    """
    who = (customer_name or "A caller").strip() or "A caller"
    when = f"{date} at {time_ampm}".strip()
    biz = (business_name or "your salon").strip()
    subject = f"New appointment request — {who}, {when}"

    rows = [("Customer", who), ("Phone", (customer_phone or "").strip() or "Not provided")]
    if service and service != "—":
        rows.append(("Service", service))
    if stylist:
        rows.append(("Stylist", stylist))
    rows.append(("Requested", when))

    html_rows = "".join(
        f"<tr><td style='padding:4px 14px 4px 0;color:#666'>{k}</td>"
        f"<td style='padding:4px 0'><strong>{v}</strong></td></tr>"
        for k, v in rows
    )
    cta = (
        f"<p style='margin-top:18px'><a href='{dashboard_url}' "
        f"style='background:#111;color:#fff;padding:10px 16px;border-radius:6px;"
        f"text-decoration:none'>Accept or decline</a></p>"
        if dashboard_url
        else ""
    )
    html = f"""
    <p>{who} asked the receptionist for an appointment at <strong>{biz}</strong>.
    Nothing is booked yet — it needs a yes or no from you.</p>
    <table style="border-collapse:collapse;font-size:15px">{html_rows}</table>
    {cta}
    <p style="color:#666;font-size:13px">Reply to this email if you need us.</p>
    """
    text = (
        f"{who} asked the receptionist for an appointment at {biz}.\n"
        "Nothing is booked yet - it needs a yes or no from you.\n\n"
        + "\n".join(f"{k}: {v}" for k, v in rows)
        + (f"\n\nAccept or decline: {dashboard_url}" if dashboard_url else "")
    )
    return subject, html.strip(), text.strip()


def notify_store_of_request(
    *,
    to: str,
    business_name: str,
    customer_name: str,
    customer_phone: str,
    date: str,
    time_ampm: str,
    service: str = "",
    stylist: str = "",
    dashboard_url: str = "",
) -> bool:
    """Email the shop that a request is waiting. Returns False when nothing was sent.

    A store with no email in Settings, or a deployment with no mail transport, simply gets
    nothing — the request is still on the dashboard either way. This must never be the
    reason a booking fails, so every failure path here is a quiet False.
    """
    to_addr = (to or "").strip()
    if not to_addr or "@" not in to_addr:
        return False
    try:
        subject, html, text = format_new_request_email(
            business_name=business_name,
            customer_name=customer_name,
            customer_phone=customer_phone,
            date=date,
            time_ampm=time_ampm,
            service=service,
            stylist=stylist,
            dashboard_url=dashboard_url,
        )
        return send_appointment_email(to_addr, subject=subject, html_body=html, text_body=text)
    except Exception as e:
        logger.warning("new_request_email_failed: %s", e, exc_info=True)
        return False
