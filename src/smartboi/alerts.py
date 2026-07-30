"""Optional webhook alerts: POSTs a small JSON payload whenever a signal
fires or a hypothetical paper trade opens/closes, so a headless deployment
(the Home Assistant add-on) can push a notification instead of relying on
someone happening to look at the dashboard.

Point ALERT_WEBHOOK_URL at a Home Assistant webhook trigger
(http://homeassistant.local:8123/api/webhook/<your-id>, from which an
automation can send a mobile notification) or any other HTTP endpoint.
Empty (the default) disables alerts entirely. Fire-and-forget: a failed or
slow webhook logs a warning and never blocks or breaks the engine."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from smartboi.news import redact_url

log = logging.getLogger(__name__)


class AlertSender:
    def __init__(self, webhook_url: str):
        self._url = webhook_url.strip()
        self._client = httpx.AsyncClient(timeout=10.0) if self._url else None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def send(self, event: str, title: str, message: str, data: dict | None = None) -> None:
        if self._client is None:
            return
        payload = {
            "event": event,  # "signal" | "paper_trade_opened" | "paper_trade_closed"
            "title": title,
            "message": message,
            "data": data or {},
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            response = await self._client.post(self._url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # The URL is scrubbed at the point of LOGGING, not left to the
            # reader downstream. raise_for_status() above produces
            # HTTPStatusError, whose str() is "Client error '404 Not Found'
            # for url 'http://.../api/webhook/<secret-id>'" -- and a webhook
            # id IS the credential: anyone holding it can fire the trigger.
            # That line went into logs/smartboi.log, and run_diagnostics
            # copies the last WARNING/ERROR lines verbatim into the bundle
            # an operator pastes into chat or an issue, under a heading that
            # promises credentials are omitted. HA restarting returns 404,
            # so this was the common case, not a corner one.
            log.warning("Alert webhook POST failed (%s): %s", event, redact_url(self._url, exc))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
