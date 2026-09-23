# coding=utf-8
"""Queue model and state machine for Auto Print Queue.

Nothing in here imports OctoPrint, so the whole queue logic can be unit tested
on any machine. The plugin talks to the printer through a small "host" object:

    host.now()                        -> float, epoch seconds
    host.printer_idle()               -> bool, connected and not printing
    host.file_exists(origin, path)    -> bool
    host.start_print(origin, path)    -> None, raises on failure
    host.send_script(lines, token)    -> None; when token is set, the host must
                                         call runner.on_marker(token) once the
                                         printer has executed every line
    host.changed()                    -> None, persist + push state to the UI
    host.notify(level, message, **kw) -> None, popup in the UI
"""

import threading
import time
import uuid

PENDING = "pending"
PRINTING = "printing"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# Runner activity. "idle" + running=False is shown as "Stopped".
IDLE = "idle"
WAITING = "waiting"  # queue running, looking for the next due item
APPROVAL = "approval"  # a print finished, waiting for the user to confirm the bed is clear
MACRO = "macro"  # between-prints G-code is executing
DELAY = "delay"  # configured pause between prints
STARTING = "starting"  # select_file sent, waiting for PrintStarted
PRINT = "printing"  # a print (queued or not) is running

DEFAULT_SETTINGS = {
    "require_approval": True,
    "between_enabled": True,
    "between_gcode": "",
    "finished_enabled": False,
    "finished_gcode": "",
    "delay_seconds": 0,
    "on_failure": "pause",  # or "continue"
    "macro_timeout": 3600,
    "start_timeout": 60,
}


def parse_script(text):
    """Turn a G-code textarea into a list of commands (comments and blanks dropped)."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.split(";", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _int(value, default, minimum=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


class QueueItem(object):
    FIELDS = (
        "id",
        "path",
        "origin",
        "name",
        "enabled",
        "scheduled_at",
        "copies",
        "completed",
        "status",
        "result",
        "added_at",
        "finished_at",
    )

    def __init__(
        self,
        path,
        origin="local",
        name=None,
        enabled=True,
        scheduled_at=None,
        copies=1,
        completed=0,
        status=PENDING,
        result=None,
        added_at=None,
        finished_at=None,
        id=None,
    ):
        self.id = id or uuid.uuid4().hex[:12]
        self.path = path
        self.origin = origin or "local"
        self.name = name or path.rsplit("/", 1)[-1]
        self.enabled = bool(enabled)
        self.scheduled_at = float(scheduled_at) if scheduled_at else None
        self.copies = _int(copies, 1, 1)
        self.completed = _int(completed, 0, 0)
        self.status = status
        self.result = result
        self.added_at = added_at or time.time()
        self.finished_at = finished_at

    def to_dict(self):
        return {f: getattr(self, f) for f in self.FIELDS}

    @classmethod
    def from_dict(cls, data):
        return cls(**{f: data.get(f) for f in cls.FIELDS if f in data})

    def is_due(self, now):
        return self.scheduled_at is None or self.scheduled_at <= now

    def is_candidate(self):
        return self.enabled and self.status == PENDING


class QueueRunner(object):
    def __init__(self, host, get_settings=None, items=None, running=False, needs_clear=False):
        self._host = host
        self._get_settings = get_settings or (lambda: {})
        self._lock = threading.RLock()
        self.items = list(items or [])
        self.running = bool(running)
        self.needs_clear = bool(needs_clear)
        self.state = WAITING if self.running else IDLE
        self.current_id = None
        self.approval_id = None
        self.macro_token = None
        self.deadline = None  # start/macro timeout, or end of the between-prints delay
        self.message = ""

    # ---------------------------------------------------------------- helpers

    def settings(self):
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in (self._get_settings() or {}).items() if v is not None})
        return merged

    def get(self, item_id):
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def next_ready(self, now=None):
        now = self._host.now() if now is None else now
        for item in self.items:
            if item.is_candidate() and item.is_due(now):
                return item
        return None

    def next_scheduled(self):
        """Earliest future start among candidates, when nothing is due yet."""
        times = [i.scheduled_at for i in self.items if i.is_candidate() and i.scheduled_at]
        return min(times) if times else None

    def has_pending(self):
        return any(i.is_candidate() for i in self.items)

    def _changed(self):
        self._host.changed()

    # ------------------------------------------------------------ public API

    def snapshot(self):
        with self._lock:
            now = self._host.now()
            nxt = self.get(self.approval_id) if self.state == APPROVAL else self.next_ready(now)
            return {
                "running": self.running,
                "state": self.state,
                "needs_clear": self.needs_clear,
                "current_id": self.current_id,
                "approval_id": self.approval_id,
                "next_id": nxt.id if nxt else None,
                "next_scheduled": None if nxt else self.next_scheduled(),
                "deadline": self.deadline,
                "message": self.message,
                "server_time": now,
                "items": [i.to_dict() for i in self.items],
            }

    def persistent_state(self):
        with self._lock:
            return {
                "running": self.running,
                "needs_clear": self.needs_clear,
                "items": [i.to_dict() for i in self.items],
            }

    def add(self, path, origin="local", name=None, scheduled_at=None, copies=1, enabled=True, index=None):
        with self._lock:
            item = QueueItem(
                path, origin=origin, name=name, scheduled_at=scheduled_at, copies=copies, enabled=enabled
            )
            if index is None or index >= len(self.items):
                self.items.append(item)
            else:
                self.items.insert(max(0, int(index)), item)
            self._changed()
        self.tick()
        return item

    def remove(self, item_id):
        with self._lock:
            item = self.get(item_id)
            if item is None:
                return False
            if item.id == self.current_id:
                raise ValueError("Cannot remove the item that is printing")
            self.items.remove(item)
            if self.approval_id == item_id:
                self._reset_to_waiting()
            self._changed()
            return True

    def update(self, item_id, **fields):
        with self._lock:
            item = self.get(item_id)
            if item is None:
                return None
            if "enabled" in fields:
                item.enabled = bool(fields["enabled"])
            if "scheduled_at" in fields:
                item.scheduled_at = float(fields["scheduled_at"]) if fields["scheduled_at"] else None
            if "copies" in fields:
                item.copies = _int(fields["copies"], item.copies, 1)
                if item.status == DONE and item.completed < item.copies:
                    item.status = PENDING
            if self.approval_id == item_id and not (item.is_candidate() and item.is_due(self._host.now())):
                self._reset_to_waiting()
            self._changed()
        self.tick()
        return item

    def move(self, item_id, index):
        with self._lock:
            item = self.get(item_id)
            if item is None:
                return False
            self.items.remove(item)
            index = max(0, min(int(index), len(self.items)))
            self.items.insert(index, item)
            if self.state == APPROVAL:
                # re-pick: the approval always targets whatever is first in line
                self._reset_to_waiting()
            self._changed()
        self.tick()
        return True

    def requeue(self, item_id):
        with self._lock:
            item = self.get(item_id)
            if item is None or item.id == self.current_id:
                return False
            item.status = PENDING
            item.completed = 0
            item.result = None
            item.finished_at = None
            self._changed()
        self.tick()
        return True

    def clear_finished(self):
        with self._lock:
            before = len(self.items)
            self.items = [i for i in self.items if i.status not in (DONE, FAILED, CANCELLED)]
            if len(self.items) != before:
                self._changed()

    def start_queue(self):
        """Pressing Start counts as confirming the bed is clear."""
        with self._lock:
            self.running = True
            self.needs_clear = False
            self.message = ""
            if self.state in (IDLE, APPROVAL):
                self.state = WAITING
            self._changed()
        self.tick()

    def stop_queue(self):
        """Stop starting new prints. A running print or macro is left alone."""
        with self._lock:
            self.running = False
            self.approval_id = None
            if self.state in (WAITING, APPROVAL, DELAY):
                self.state = IDLE
                self.deadline = None
            self.message = "Queue stopped"
            self._changed()

    def approve(self):
        with self._lock:
            self.needs_clear = False
            if self.state == APPROVAL:
                self.approval_id = None
                self.state = WAITING
            self._changed()
        self.tick()

    def skip_delay(self):
        with self._lock:
            if self.state != DELAY:
                return False
            self.state = WAITING
            self.deadline = None
            self._changed()
        self.tick()
        return True

    def print_now(self, item_id):
        """Start one item right away, ignoring its schedule and the approval step."""
        with self._lock:
            item = self.get(item_id)
            if item is None:
                raise ValueError("Unknown queue item")
            if self.state in (STARTING, PRINT, MACRO):
                raise ValueError("The printer is busy")
            if not self._host.printer_idle():
                raise ValueError("The printer is not ready")
            if item.status != PENDING:
                item.status = PENDING
                item.completed = 0
                item.result = None
            self.needs_clear = False
            self.approval_id = None
            self._start(item)

    # ------------------------------------------------------ printer callbacks

    def on_print_started(self, origin, path):
        with self._lock:
            item = self.get(self.current_id) if self.current_id else None
            if item is not None and self.state == STARTING and item.path == path and item.origin == origin:
                item.status = PRINTING
            else:
                # Somebody started a print by hand. Take note so the queue
                # continues after it instead of fighting for the printer.
                if item is not None and item.status == PRINTING:
                    item.status = PENDING
                self.current_id = None
            self.approval_id = None
            self.state = PRINT
            self.deadline = None
            self._changed()

    def on_print_ended(self, origin, path, outcome, reason=None):
        """outcome: "done", "failed" or "cancelled"."""
        with self._lock:
            now = self._host.now()
            item = self.get(self.current_id) if self.current_id else None
            self.current_id = None
            self.deadline = None
            self.needs_clear = True
            if item is not None:
                item.finished_at = now
                if outcome == "done":
                    item.completed += 1
                    item.result = None
                    item.status = DONE if item.completed >= item.copies else PENDING
                else:
                    item.status = CANCELLED if outcome == "cancelled" else FAILED
                    item.result = reason or outcome

            s = self.settings()
            if outcome != "done" and self.running and s["on_failure"] != "continue":
                self.running = False
                self.state = IDLE
                self.message = "Queue paused: print {}".format(outcome)
                self._host.notify("error", self.message)
                self._changed()
                return

            if not self.running:
                self.state = IDLE
                self._changed()
                return

            if self.has_pending():
                script = parse_script(s["between_gcode"]) if s["between_enabled"] else []
                if script:
                    self._run_macro(script)
                    self._changed()
                    return
                self._after_macro()
            else:
                self._finish_queue(ran_prints=True)
            self._changed()
        self.tick()

    def on_marker(self, token):
        with self._lock:
            if self.state != MACRO or token != self.macro_token:
                return
            self.macro_token = None
            self._after_macro()
            self._changed()
        self.tick()

    # ------------------------------------------------------------- the loop

    def tick(self):
        with self._lock:
            now = self._host.now()
            s = self.settings()

            if self.state == STARTING and self.deadline and now > self.deadline:
                item = self.get(self.current_id)
                if item is not None:
                    item.status = FAILED
                    item.result = "Print did not start"
                self.current_id = None
                self._pause("Queue paused: the printer did not start the print")
                return

            if self.state == MACRO and self.deadline and now > self.deadline:
                self.macro_token = None
                self._pause("Queue paused: between-prints G-code did not finish in time")
                return

            if self.state == DELAY and self.deadline and now >= self.deadline:
                self.deadline = None
                self.state = WAITING if self.running else IDLE
                self._changed()

            if self.state == APPROVAL:
                item = self.get(self.approval_id)
                if (
                    not s["require_approval"]
                    or item is None
                    or not (item.is_candidate() and item.is_due(now))
                ):
                    self._reset_to_waiting()
                    self._changed()

            if self.state != WAITING or not self.running:
                return
            if not self._host.printer_idle():
                return

            item = self.next_ready(now)
            if item is None:
                if not self.has_pending():
                    self._finish_queue(ran_prints=False)
                    self._changed()
                return

            if s["require_approval"] and self.needs_clear:
                self.state = APPROVAL
                self.approval_id = item.id
                self._host.notify(
                    "approval", "Previous print finished. Confirm the bed is clear to start the next one.",
                    item=item.name,
                )
                self._changed()
                return

            self._start(item)

    # -------------------------------------------------------------- internals

    def _start(self, item):
        now = self._host.now()
        if not self._host.file_exists(item.origin, item.path):
            item.status = FAILED
            item.result = "File not found"
            item.finished_at = now
            self._host.notify("error", "Queue: {} no longer exists, skipped".format(item.name))
            self._changed()
            return
        try:
            self._host.start_print(item.origin, item.path)
        except Exception as exc:  # the printer refused the file
            item.status = FAILED
            item.result = str(exc) or "Could not start"
            item.finished_at = now
            self._pause("Queue paused: could not start {}".format(item.name))
            return
        self.current_id = item.id
        self.approval_id = None
        self.needs_clear = False
        item.status = PRINTING
        self.state = STARTING
        self.deadline = now + _int(self.settings()["start_timeout"], 60, 5)
        self.message = ""
        self._changed()

    def _run_macro(self, lines):
        self.macro_token = uuid.uuid4().hex[:10]
        self.state = MACRO
        self.deadline = self._host.now() + _int(self.settings()["macro_timeout"], 3600, 10)
        self._host.send_script(lines, self.macro_token)

    def _after_macro(self):
        self.deadline = None
        if not self.running:
            self.state = IDLE
            return
        delay = _int(self.settings()["delay_seconds"], 0, 0)
        if delay > 0:
            self.state = DELAY
            self.deadline = self._host.now() + delay
        else:
            self.state = WAITING

    def _finish_queue(self, ran_prints):
        self.running = False
        self.state = IDLE
        self.approval_id = None
        self.deadline = None
        s = self.settings()
        if ran_prints:
            self.message = "Queue finished"
            script = parse_script(s["finished_gcode"]) if s["finished_enabled"] else []
            if script:
                self._host.send_script(script, None)
            self._host.notify("success", "Print queue finished")
        else:
            self.message = "Nothing left to print"

    def _reset_to_waiting(self):
        self.approval_id = None
        self.state = WAITING if self.running else IDLE

    def _pause(self, message):
        self.running = False
        self.state = IDLE
        self.deadline = None
        self.message = message
        self._host.notify("error", message)
        self._changed()
