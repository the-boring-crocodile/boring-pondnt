"""Process-owned DMF submissions with bounded restart receipts."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boring_service_kit.outcomes import Failed, Outcome, Refused

from . import disk
from .d365data_client import DataClient, mask_error

RETAINED = 50
RECEIPT_BYTES = 4096
SUBMISSION_ID = re.compile(r"[0-9a-f]{32}")
CHECK_HISTORY = "Check D365 execution history before importing again."


@dataclass
class Submission:
    id: str
    package_bytes: int
    execute: bool
    done: threading.Event = field(default_factory=threading.Event)
    answer: dict[str, Any] = field(default_factory=dict)
    error: Outcome | None = None

    def pending(self) -> dict[str, Any]:
        return {"state": "submitting", "submission_id": self.id, "package_bytes": self.package_bytes,
                "note": f'Poll with dmf_status(submission_id="{self.id}").'}


class Submissions:
    """One active submission. The journal stores only ids and is never replayed."""

    def __init__(self, client: DataClient, path: Path) -> None:
        self.client, self.path = client, path
        self._lock = threading.Lock()
        self._records: dict[str, Submission | None] | None = None
        self._active: str | None = None

    def _load(self) -> dict[str, Submission | None]:
        if self._records is None:
            try:
                with self.path.open("rb") as stream:
                    raw = stream.read(RECEIPT_BYTES + 1)
                if len(raw) > RECEIPT_BYTES:
                    raise ValueError
                ids = json.loads(raw)
                if (not isinstance(ids, list) or len(ids) > RETAINED
                        or not all(isinstance(i, str) and SUBMISSION_ID.fullmatch(i) for i in ids)
                        or len(set(ids)) != len(ids)):
                    raise ValueError
            except FileNotFoundError:
                ids = []
            except (OSError, ValueError, RecursionError):
                raise Failed("submission_state", "cannot read the DMF submission receipts. " + CHECK_HISTORY) from None
            self._records = dict.fromkeys(ids)
        return self._records

    def start(self, definition_group: str, package_path: str, company: str, execute: bool,
              overwrite: bool, wait_seconds: float) -> dict[str, Any]:
        wait_until = time.monotonic() + wait_seconds
        with self._lock:
            self._refuse_if_busy()
        # Reading and checking up to 64 MiB happens outside the lock so status polls are not held up.
        data = self.client.prepare_import(definition_group, package_path, company, execute, overwrite)
        with self._lock:
            self._refuse_if_busy()  # another import may have started while this package was read
            records = self._load()
            entry = Submission(uuid.uuid4().hex, len(data), execute)
            recent = {key: records[key] for key in list(records)[-(RETAINED - 1):]}
            recent[entry.id] = entry
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                disk.replace_atomically(self.path, json.dumps(list(recent)).encode("utf-8"),
                                        create=not self.path.exists())
            except (OSError, Outcome):
                raise Failed("submission_state", "cannot record the DMF submission. Nothing was sent.") from None
            self._records = recent
            self._active = entry.id
            worker = threading.Thread(target=self._submit, args=(entry, definition_group, data, company, overwrite),
                                      name="d365data-import", daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._active = None
                entry.error = Failed("submission_start", "could not start the DMF submission. Nothing was sent.")
                entry.done.set()
        if not entry.done.wait(max(0.0, wait_until - time.monotonic())):
            return entry.pending()
        if entry.error is not None:
            raise type(entry.error)(entry.error.code, entry.error.message, **entry.error.details)
        return entry.answer

    def _refuse_if_busy(self) -> None:
        if self._active is not None:
            raise Refused("busy", f"DMF submission {self._active} is in progress", submission_id=self._active)

    def _submit(self, entry: Submission, definition_group: str, data: bytes, company: str, overwrite: bool) -> None:
        try:
            entry.answer = self.client.submit_import(definition_group, data, company, entry.execute, overwrite)
        except Outcome as error:
            entry.error = type(error)(error.code, mask_error(error.message, omit_all_urls=True),
                                      **mask_error(error.details, omit_all_urls=True))
        except Exception:
            entry.error = Failed("submission_failed", "DMF submission stopped unexpectedly.", note=CHECK_HISTORY)
        finally:
            with self._lock:
                self._active = None
                entry.done.set()

    def status(self, submission_id: str) -> tuple[dict[str, Any], bool]:
        if not SUBMISSION_ID.fullmatch(submission_id):
            raise Refused("invalid_request", "submission_id must be 32 lowercase hex characters")
        with self._lock:
            records = self._load()
            if submission_id not in records:
                raise Refused("not_found", "DMF submission id not found", submission_id=submission_id)
            entry = records[submission_id]
            if entry is None:
                return {"state": "unknown", "submission_id": submission_id,
                        "note": "The service restarted and the submission outcome is unknown. " + CHECK_HISTORY}, False
            if not entry.done.is_set():
                return entry.pending(), False
            if entry.error is not None:
                return {**entry.error.details, "state": "failed", "submission_id": submission_id,
                        "code": entry.error.code, "message": entry.error.message}, False
            return {**entry.answer, "state": "submitted", "submission_id": submission_id}, entry.execute
