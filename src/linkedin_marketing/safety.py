"""The safety layer every write goes through, in the library and the MCP alike.

A write is first *planned*: the exact ``Request`` it would send plus a
human-readable before/after. ``Guard.execute`` then decides:

- without ``confirm=True`` it returns the plan as a dry run and sends nothing;
- with ``confirm=True`` but ``read_only`` on, it refuses (``read_only_mode``);
- otherwise it appends ``attempted`` to the JSONL audit log, sends, and
  appends ``succeeded`` or ``failed``.

The audit record holds the action, target ids and a summary, never request
bodies: conversion and audience writes carry hashed personal data, which has
no business in a local log file.

The allow-lists apply to reads as well as writes, so a misconfigured agent
cannot even look at an account or page outside them.

There are deliberately no delete operations, no billing operations, and no
create-campaign or create-creative operations anywhere in this library.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from linkedin_marketing.config import Settings
from linkedin_marketing.errors import (
    LinkedInError,
    ModuleDisabledError,
    NotAllowedError,
    WriteRefusedError,
)
from linkedin_marketing.transport import Request, Response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WritePlan:
    """One write, described exactly as it would be sent."""

    action: str
    request: Request
    target: dict
    before: object = None
    after: object = None
    summary: dict = field(default_factory=dict)
    warning: str = ""

    @property
    def is_noop(self) -> bool:
        return self.before is not None and self.before == self.after


@dataclass(frozen=True)
class WriteResult:
    """What happened to a planned write. ``dry_run`` is True unless it was sent."""

    action: str
    dry_run: bool
    result: str
    request: dict
    target: dict
    before: object = None
    after: object = None
    summary: dict = field(default_factory=dict)
    warning: str = ""
    status_code: int = 0
    created_id: str = ""
    response: dict = field(default_factory=dict)
    audit_error: str = ""

    def to_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value not in ("", None)}


class Guard:
    """Module switches, allow-lists, read-only mode and the audited write path."""

    def __init__(self, settings: Settings, send: Callable[[Request], Response]):
        self.settings = settings
        self._send = send

    def require_module(self, module: str) -> None:
        if module not in self.settings.modules:
            raise ModuleDisabledError(module, self.settings.modules)

    def check_account(self, account_id: int | str) -> int:
        """The account id as an int, refused when outside the allow-list."""
        resolved = int(account_id)
        allowed = self.settings.allowed_ad_account_ids
        if allowed and resolved not in allowed:
            raise NotAllowedError(
                "Ad account", resolved, allowed, "LINKEDIN_ALLOWED_AD_ACCOUNT_IDS"
            )
        return resolved

    def check_organization(self, organization_id: int | str) -> int:
        """The organization id as an int, refused when outside the allow-list."""
        resolved = int(organization_id)
        allowed = self.settings.allowed_organization_ids
        if allowed and resolved not in allowed:
            raise NotAllowedError(
                "Organization", resolved, allowed, "LINKEDIN_ALLOWED_ORGANIZATION_IDS"
            )
        return resolved

    def execute(self, plan: WritePlan, confirm: bool) -> WriteResult:
        """Return the dry run, refuse in read-only mode, or send and audit."""
        if plan.is_noop:
            return self._result(plan, True, "no_change: already in the requested state")
        if not confirm:
            return self._result(plan, True, self._dry_run_message())
        if self.settings.read_only:
            raise WriteRefusedError(
                "read_only_mode",
                plan.action,
                "LINKEDIN_READ_ONLY is on (the default). Set it to false to allow "
                "confirmed writes.",
                **plan.target,
            )
        self._audit("attempted", plan)
        try:
            response = self._send(plan.request)
        except LinkedInError as exc:
            self._audit_quietly("failed", plan, error=type(exc).__name__, **exc.context)
            raise
        audit_error = self._audit_quietly("succeeded", plan, status_code=response.status_code)
        return self._result(
            plan,
            False,
            "applied",
            status_code=response.status_code,
            created_id=response.restli_id,
            response=response.body,
            audit_error=audit_error,
        )

    def _dry_run_message(self) -> str:
        if self.settings.read_only:
            return (
                "dry_run: nothing sent. Read-only mode is on, so confirm=True will be "
                "refused until LINKEDIN_READ_ONLY=false."
            )
        return "dry_run: nothing sent. Pass confirm=True to send this exact request."

    def _result(self, plan: WritePlan, dry_run: bool, result: str, **extra) -> WriteResult:
        return WriteResult(
            action=plan.action,
            dry_run=dry_run,
            result=result,
            request=plan.request.describe(self.settings.api_version),
            target=plan.target,
            before=plan.before,
            after=plan.after,
            summary=plan.summary,
            warning=plan.warning,
            **extra,
        )

    def _audit_quietly(self, outcome: str, plan: WritePlan, **extra) -> str:
        """Audit after the request already went out; report, never raise.

        Once LinkedIn has accepted (or rejected) the call, a failing log append
        must not turn the result into an exception the caller might retry.
        The failure is returned instead and surfaces as ``audit_error``.
        """
        try:
            self._audit(outcome, plan, **extra)
        except OSError as exc:
            logger.error("linkedin.write.audit_failed error=%s", type(exc).__name__)
            return type(exc).__name__
        return ""

    def _audit(self, outcome: str, plan: WritePlan, **extra) -> None:
        """Append one JSON line per attempt and outcome; mirror it to the log."""
        record = {
            "at": datetime.now(UTC).isoformat(),
            "event": f"linkedin.write.{outcome}",
            "action": plan.action,
            "method": plan.request.method,
            "path": plan.request.path,
            **plan.target,
            "before": plan.before,
            "after": plan.after,
            **plan.summary,
            **extra,
        }
        line = json.dumps(record, default=str)
        logger.warning(line)
        path = self.settings.audit_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
