import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class AkismetError(Exception):
    pass


@dataclass
class AkismetResult:
    is_spam: bool
    classification: str
    submit_hash: str
    score: float | None


def comments_configured() -> bool:
    if settings.DEBUG:
        return True
    return bool(settings.AKISMET_API_KEY and settings.TURNSTILE_SITE_KEY and settings.TURNSTILE_SECRET_KEY)


def verify_turnstile(token: str, remoteip: str | None = None) -> tuple[bool, list[str]]:
    if settings.DEBUG:
        return True, []
    if not settings.TURNSTILE_SECRET_KEY:
        return False, ["missing-input-secret"]
    payload = {
        "secret": settings.TURNSTILE_SECRET_KEY,
        "response": token,
    }
    if remoteip:
        payload["remoteip"] = remoteip
    try:
        response = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=8)
    except requests.RequestException:
        logger.exception("Turnstile verification failed")
        return False, ["turnstile-request-failed"]
    if response.status_code != 200:
        return False, ["turnstile-unexpected-status"]
    data = response.json()
    return bool(data.get("success")), data.get("error-codes") or []


def _akismet_url(path: str) -> str:
    if not settings.AKISMET_API_KEY:
        raise AkismetError("Missing Akismet API key")
    return f"https://{settings.AKISMET_API_KEY}.rest.akismet.com/1.1/{path}"


def _akismet_headers() -> dict[str, str]:
    return {
        "User-Agent": "webstead/1.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def check_comment(payload: dict[str, str]) -> AkismetResult:
    url = _akismet_url("comment-check")
    try:
        response = requests.post(url, data=payload, headers=_akismet_headers(), timeout=8)
    except requests.RequestException as exc:
        logger.exception("Akismet check failed")
        raise AkismetError("Akismet request failed") from exc
    if response.status_code != 200:
        raise AkismetError("Akismet returned an unexpected status")

    result = response.text.strip().lower() == "true"
    pro_tip = (response.headers.get("X-akismet-pro-tip") or "").lower()
    classification = "discard" if pro_tip == "discard" else "spam" if result else "ham"
    submit_hash = response.headers.get("X-akismet-guid", "")
    return AkismetResult(is_spam=result, classification=classification, submit_hash=submit_hash, score=None)


def submit_ham(payload: dict[str, str]) -> None:
    _submit_feedback("submit-ham", payload)


def submit_spam(payload: dict[str, str]) -> None:
    _submit_feedback("submit-spam", payload)


def _submit_feedback(endpoint: str, payload: dict[str, str]) -> None:
    url = _akismet_url(endpoint)
    try:
        response = requests.post(url, data=payload, headers=_akismet_headers(), timeout=8)
    except requests.RequestException as exc:
        logger.exception("Akismet feedback submission failed")
        raise AkismetError("Akismet feedback failed") from exc
    if response.status_code != 200:
        raise AkismetError("Akismet feedback returned an unexpected status")
