"""Background work (scans, syncs, downloads) that the web UI starts and then follows by polling."""
import threading
import time
from contextlib import closing

from . import db


class Jobs:
    """Named background jobs, one run at a time per name. Each job reports progress as (done, total, message)."""

    def __init__(self):
        self._states: dict[str, dict] = {}

    def start(self, name: str, fn, first_message: str, queue: bool = False) -> dict:
        """Run fn(progress) in the background. If job `name` is already running, return its state; with
        queue=True, run fn after it instead (e.g. downloads requested while one is in progress)."""
        state = self._states.setdefault(name, {"running": False})
        lock = state.setdefault("_lock", threading.Lock())
        waiting = not lock.acquire(blocking=False)
        if waiting and not queue:
            return self.status(name)
        if waiting:
            state["queued"] = state.get("queued", 0) + 1

        def progress(i, n, msg):
            state.update(done=i, total=n, current=msg)

        def begin():
            state.update(running=True, done=0, total=0, current=first_message, result=None, error=None,
                         started=time.time())

        def run():
            if waiting:
                lock.acquire()
                state["queued"] -= 1
                begin()
            try:
                state["result"] = fn(progress)
            except Exception as e:
                state["error"] = str(e)
            finally:
                state["running"] = bool(state.get("queued"))  # stays "running" while queued work follows
                lock.release()

        if not waiting:
            begin()
        threading.Thread(target=run, daemon=True).start()
        return {**self.status(name), "queued_behind": waiting}

    def status(self, name: str) -> dict:
        return {k: v for k, v in self._states.get(name, {"running": False}).items() if not k.startswith("_")}


class Downloads:
    """Download batches run as the "download" job, one after another. Tracks which purchases are queued or
    downloading (for progress on their buttons) and lets Cancel stop the running batch and queued ones."""

    def __init__(self, cfg, jobs: Jobs):
        self.cfg, self.jobs = cfg, jobs
        self.items: dict[int, str] = {}  # purchase id -> "queued" | "running"
        self._generation = 0  # Cancel bumps this; batches started before it stop

    def start(self, file_ids: list[int]) -> dict:
        from .itch import DownloadCancelled, download_many
        with closing(db.connect(self.cfg)) as c:
            item_ids = [r[0] for r in c.execute(
                f"SELECT DISTINCT item_id FROM store_files WHERE id IN ({db.placeholders(file_ids)})", file_ids)]
        for i in item_ids:
            self.items.setdefault(i, "queued")
        generation = self._generation

        def run(progress):
            def checked(*args):
                if self._generation != generation:
                    raise DownloadCancelled()
                progress(*args)
            self.items.update(dict.fromkeys(item_ids, "running"))
            try:
                if self._generation != generation:  # cancelled while queued
                    return {"itch": {"downloaded": 0, "failed": 0, "errors": [], "cancelled": True}}
                return {"itch": download_many(self.cfg, file_ids, checked)}
            finally:
                for i in item_ids:
                    self.items.pop(i, None)
        return self.jobs.start("download", run, f"Queued {len(file_ids)} files…", queue=True)

    def cancel(self) -> None:
        self._generation += 1
