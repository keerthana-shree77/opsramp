"""In-process job store for long-running comparison runs.

A firmware sweep over a large tenant takes minutes, so the request that starts
it returns immediately with a job id and the browser polls for progress.

Scope note: jobs live in this process's memory.  That is deliberate and
adequate for a single-worker internal deployment, but it means you must run the
app with **one** worker process (or replace this store with Redis/RQ) - see the
deployment section of the README.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable

from firmware.models import RecipeEntry
from opsramp.client import OpsRampError
from service import ComparisonResult, Tenant, run_many

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

_STAGE_WEIGHT = {
    "queued": 2,
    "authenticating": 5,
    "discovering": 15,
    "extracting": 80,
    "comparing": 95,
    "done": 100,
}


@dataclass
class Job:
    id: str
    tenant_id: str
    tenant_name: str
    category: str
    recipe_name: str
    owner: str = ""
    tenants: list = field(default_factory=list)      # list[service.Tenant]
    categories: list[str] = field(default_factory=list)
    status: str = STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    progress: dict = field(default_factory=lambda: {"stage": "queued", "percent": 0})
    result: ComparisonResult | None = None
    error: str = ""
    # A run the compliance matrix links to. Pinned jobs are exempt from
    # pruning: the matrix is long-lived and a pruned job turns one of its
    # cells into a dead link, which is worse than holding the result.
    pinned: bool = False

    def public_state(self) -> dict:
        data = {
            "id": self.id,
            "status": self.status,
            "tenant": self.tenant_name,
            "category": self.category,
            "recipe": self.recipe_name,
            "error": self.error,
        }
        data.update(self.progress)
        return data


class JobStore:
    def __init__(self, max_jobs: int = 25, retention_seconds: int = 21600) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self.max_jobs = max_jobs
        self.retention_seconds = retention_seconds

    def create(self, **kwargs) -> Job:
        job = Job(id=uuid.uuid4().hex, **kwargs)
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        return job

    def get(self, job_id: str, owner: str | None = None) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if owner is not None and job.owner and job.owner != owner:
            return None
        return job

    def completed(self, owner: str | None = None) -> list["Job"]:
        """Finished runs with a result, newest first."""
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(
            (
                job
                for job in jobs
                if job.status == STATUS_DONE
                and job.result is not None
                and (owner is None or not job.owner or job.owner == owner)
            ),
            key=lambda job: job.finished_at or job.created_at,
            reverse=True,
        )

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, job in self._jobs.items()
            if not job.pinned
            and job.status in (STATUS_DONE, STATUS_ERROR)
            and job.finished_at
            and now - job.finished_at > self.retention_seconds
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

        # Only unpinned jobs count towards the cap, and only they are evicted:
        # the matrix holds exactly accounts x recipes of them and releases the
        # previous set itself, so it is bounded without being pruned.
        loose = [job for job in self._jobs.values() if not job.pinned]
        if len(loose) > self.max_jobs:
            loose.sort(key=lambda j: j.created_at)
            for job in loose[: len(loose) - self.max_jobs]:
                if job.status in (STATUS_DONE, STATUS_ERROR):
                    self._jobs.pop(job.id, None)

    def start(
        self,
        job: Job,
        client_factory: Callable[[], object],
        entries: list[RecipeEntry],
    ) -> None:
        thread = threading.Thread(
            target=self._run, args=(job, client_factory, entries), daemon=True,
            name=f"compare-{job.id[:8]}",
        )
        thread.start()

    def adopt(self, job: Job, result) -> None:
        """Attach an already-computed result to a job.

        The compliance sweep reads each account once and then compares what it
        read against every recipe, so the comparisons arrive ready-made. They
        still need to be jobs, because that is what the matrix links to when a
        reader clicks a percentage to see the rows behind it.
        """
        job.result = result
        job.status = STATUS_DONE
        job.error = ""
        job.pinned = True
        job.finished_at = time.time()
        job.progress = {
            "stage": "done",
            "percent": 100,
            "message": "Comparison complete",
        }

    def release(self, job_ids) -> None:
        """Let go of matrix runs that have been superseded by a newer sweep."""
        with self._lock:
            for job_id in job_ids:
                self._jobs.pop(job_id, None)

    def run_blocking(self, job: Job, client_factory, entries) -> None:
        """Run a job on the calling thread.

        The compliance sweep needs tenants done one at a time, so that each
        one can be published as it finishes and the whole estate is not
        queried at once.
        """
        self._run(job, client_factory, entries)

    # ------------------------------------------------------------------ worker
    def _run(self, job: Job, client_factory, entries) -> None:
        job.status = STATUS_RUNNING
        self._set_progress(job, {"stage": "authenticating", "message": "Starting"})
        client = None
        try:
            client = client_factory()
            result = run_many(
                client,
                tenants=job.tenants or [Tenant(job.tenant_id, job.tenant_name)],
                categories=job.categories or [job.category],
                entries=entries,
                recipe_name=job.recipe_name,
                progress=lambda update: self._set_progress(job, update),
            )
            job.result = result
            job.status = STATUS_DONE
            self._set_progress(job, {"stage": "done", "message": "Comparison complete"})
            logger.info(
                "Job %s finished: %d comparison row(s) across %d asset(s)",
                job.id,
                len(result.rows),
                result.assets_matched,
            )
        except OpsRampError as exc:
            job.status = STATUS_ERROR
            job.error = str(exc)
            logger.error("Job %s failed: %s", job.id, exc)
        except Exception as exc:  # noqa: BLE001 - surfaced as a generic message
            job.status = STATUS_ERROR
            job.error = (
                "An unexpected error occurred while building the comparison. "
                "The details have been written to the application log."
            )
            logger.error(
                "Job %s crashed: %s\n%s", job.id, exc, traceback.format_exc()
            )
        finally:
            job.finished_at = time.time()
            if client is not None and hasattr(client, "close"):
                try:
                    client.close()
                except Exception:  # pragma: no cover - defensive
                    pass

    @staticmethod
    def _set_progress(job: Job, update: dict) -> None:
        progress = dict(job.progress)
        progress.update(update)
        stage = progress.get("stage", "queued")
        base = _STAGE_WEIGHT.get(stage, 0)
        if stage == "extracting":
            done = progress.get("assets_processed") or 0
            total = progress.get("assets_selected") or 0
            span = _STAGE_WEIGHT["extracting"] - _STAGE_WEIGHT["discovering"]
            fraction = (done / total) if total else 0.0
            base = _STAGE_WEIGHT["discovering"] + int(span * min(fraction, 1.0))
        progress["percent"] = max(0, min(100, base))
        job.progress = progress
