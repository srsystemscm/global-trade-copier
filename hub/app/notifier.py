import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Dict

import httpx

from app.config import settings
from app.db import get_config_value

logger = logging.getLogger(__name__)


class Notifier:
    """Delivers notifications to whatever's configured in Settings ->
    Notifications (stored in the `config` KV table -- read fresh on every
    call so changes take effect immediately, no restart needed).

    Delivery failures are logged, never raised -- a broken webhook/SMTP
    server shouldn't take down the engine that triggered the notification.
    """

    async def notify(self, event_type: str, message: str, **details: Any) -> None:
        webhook_url = get_config_value("notify_webhook_url")
        email_to = get_config_value("notify_email")

        if not webhook_url and not email_to:
            logger.debug("no notification targets configured, skipping %s", event_type)
            return

        if webhook_url:
            await self._send_webhook(webhook_url, event_type, message, details)
        if email_to:
            await self._send_email(email_to, event_type, message, details)

    async def _send_webhook(self, url: str, event_type: str, message: str, details: Dict[str, Any]) -> None:
        payload = {"event": event_type, "message": message, **details}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    logger.error("webhook notification rejected by %s: %s", url, resp.status_code)
        except httpx.RequestError as exc:
            logger.error("webhook notification failed: %s: %s", url, exc)

    async def _send_email(self, to_addr: str, event_type: str, message: str, details: Dict[str, Any]) -> None:
        if not settings.smtp_host:
            logger.debug("SMTP not configured, skipping email notification for %s", event_type)
            return
        try:
            await asyncio.to_thread(self._send_email_sync, to_addr, event_type, message, details)
        except Exception as exc:
            logger.error("email notification failed: %s", exc)

    def _send_email_sync(self, to_addr: str, event_type: str, message: str, details: Dict[str, Any]) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[Trade Copier] {event_type}"
        msg["From"] = settings.smtp_from or "trade-copier@localhost"
        msg["To"] = to_addr
        body = message
        if details:
            body += "\n\n" + "\n".join(f"{k}: {v}" for k, v in details.items())
        msg.set_content(body)

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_user:
                smtp.starttls()
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
