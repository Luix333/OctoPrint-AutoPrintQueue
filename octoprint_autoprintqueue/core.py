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

The queue is one ordered list of entries. An entry is either a print (a file)
or a node (G-code / macros to run between prints). The runner works through
the list top to bottom, skipping disabled entries.
"""

import re
import threading
import time
import uuid

# entry types
PRINT_ENTRY = "print"
NODE_ENTRY = "node"

# entry status
PENDING = "pending"
PRINTING = "printing"  # print entry is on the printer
RUNNING = "running"  # node entry's G-code is executing
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = (DONE, FAILED, CANCELLED)

# Runner activity. "idle" + running=False is shown as "Stopped".
IDLE = "idle"
WAITING = "waiting"  # queue running, looking at the next entry
APPROVAL = "approval"  # a print finished, waiting for the user to confirm the bed is clear
MACRO = "macro"  # a node's G-code is executing
STARTING = "starting"  # select_file sent, waiting for PrintStarted
PRINT = "printing"  # a print (queued or not) is running

DEFAULT_SETTINGS = {
    "require_approval": True,
    "finished_enabled": False,
    "finished_gcode": "",
    "on_failure": "pause",  # or "continue"
    "macro_timeout": 3600,
    "start_timeout": 60,
}

_HELP_LINE = re.compile(r"^//\s*([A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(.*)$")


def parse_script(text):
    """Turn a G-code textarea into a list of commands (comments and blanks dropped)."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.split(";", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def parse_help_line(line):
    """Parse one line of Klipper's HELP reply ("// NAME : description")."""
    match = _HELP_LINE.match((line or "").strip())
    if not match:
        return None
    return match.group(1).upper(), match.group(2).strip()


def _int(value, default, minimum=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _new_id():
    return uuid.uuid4().hex[:12]


def node_title(name, gcode):
    if name and name.strip():
        return name.strip()
    lines = parse_script(gcode)
    if not lines:
        return "Empty node"
    return lines[0] + (" …" if len(lines) > 1 else "")


class QueueItem(object):
    FIELDS = (
        "id",
        "type",
        "path",
        "origin",
        "name",
        "gcode",
        "library_id",
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
        path=None,
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
        type=PRINT_ENTRY,
        gcode=None,
        library_id=None,
    ):
        self.id = id or _new_id()
        self.type = NODE_ENTRY if type == NODE_ENTRY else PRINT_ENTRY
        self.path = path
        self.origin = origin or "local"
        self.gcode = gcode or ""
        self.library_id = library_id
        if self.is_node:
            self.name = (name or "").strip()
        else:
            self.name = name or (path or "").rsplit("/", 1)[-1]
        self.enabled = bool(enabled)
        self.scheduled_at = float(scheduled_at) if scheduled_at else None
        self.copies = _int(copies, 1, 1)
        self.completed = _int(completed, 0, 0)
        self.status = status or PENDING
        self.result = result
        self.added_at = added_at or time.time()
        self.finished_at = finished_at

    @property
    def is_node(self):
        return self.type == NODE_ENTRY

    @property
    def title(self):
        return node_title(self.name, self.gcode) if self.is_node else self.name

    def to_dict(self):
        data = {f: getattr(self, f) for f in self.FIELDS}
        data["title"] = self.title
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(**{f: data.get(f) for f in cls.FIELDS if f in data})

    def is_due(self, now):
        return self.is_node or self.scheduled_at is None or self.scheduled_at <= now

    def is_candidate(self):
        return self.enabled and self.status == PENDING


class LibraryNode(object):
    FIELDS = ("id", "name", "gcode", "auto_add")

    def __init__(self, name="", gcode="", auto_add=False, id=None):
        self.id = id or _new_id()
        self.name = (name or "").strip()
        self.gcode = gcode or ""
        self.auto_add = bool(auto_add)

    def to_dict(self):
        data = {f: getattr(self, f) for f in self.FIELDS}
        data["title"] = node_title(self.name, self.gcode)
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(**{f: data.get(f) for f in cls.FIELDS if f in data})

    def make_entry(self):
        return QueueItem(type=NODE_ENTRY, name=self.name, gcode=self.gcode, library_id=self.id)


class QueueRunner(object):
    def __init__(self, host, get_settings=None, items=None, running=False, needs_clear=False, library=None):
        self._host = host
        self._get_settings = get_settings or (lambda: {})
        self._lock = threading.RLock()
        self.items = list(items or [])
        self.library = list(library or [])
        self.running = bool(running)
        self.needs_clear = bool(needs_clear)
        self.state = WAITING if self.running else IDLE
        self.current_id = None  # the print on the printer, if we started it
        self.approval_id = None
        self.macro_id = None  # the node whose G-code is executing
        self.macro_token = None
        self.macro_counts = True  # False while repeating nodes between copies
        self.repeat = []  # node ids to run again before the next copy of a print
        self.printed = False  # a queued print finished since the queue was started
        self.deadline = None  # start or macro timeout
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

    def get_library(self, node_id):
        for node in self.library:
            if node.id == node_id:
                return node
        return None

    def next_entry(self):
        """The first enabled pending entry. The queue runs strictly in list order."""
        for item in self.items:
            if item.is_candidate():
                return item
        return None

    def has_pending(self):
        return self.next_entry() is not None

    def has_prints(self):
        return any(not i.is_node for i in self.items)

    def _changed(self):
        self._host.changed()

    def _index_after(self, after_id):
        if after_id is None:
            return None
        for n, item in enumerate(self.items):
            if item.id == after_id:
                return n + 1
        return None

    # ------------------------------------------------------------ public API

    def snapshot(self):
        with self._lock:
            now = self._host.now()
            nxt = self.get(self.approval_id) if self.state == APPROVAL else self.next_entry()
            waiting_until = None
            if nxt is not None and not nxt.is_due(now):
                waiting_until = nxt.scheduled_at
            return {
                "running": self.running,
                "state": self.state,
                "needs_clear": self.needs_clear,
                "current_id": self.current_id,
                "macro_id": self.macro_id,
                "approval_id": self.approval_id,
                "next_id": nxt.id if nxt else None,
                "next_scheduled": waiting_until,
                "deadline": self.deadline,
                "message": self.message,
                "server_time": now,
                "items": [i.to_dict() for i in self.items],
                "library": [n.to_dict() for n in self.library],
            }

    def persistent_state(self):
        with self._lock:
            return {
                "running": self.running,
                "needs_clear": self.needs_clear,
                "items": [i.to_dict() for i in self.items],
                "library": [n.to_dict() for n in self.library],
            }

    def add(
        self,
        path,
        origin="local",
        name=None,
        scheduled_at=None,
        copies=1,
        enabled=True,
        index=None,
        auto_nodes=True,
    ):
        """Add a print. The library's auto-add nodes go between it and the print before it."""
        with self._lock:
            entries = []
            if auto_nodes and self.has_prints():
                entries = [n.make_entry() for n in self.library if n.auto_add]
            item = QueueItem(
                path, origin=origin, name=name, scheduled_at=scheduled_at, copies=copies, enabled=enabled
            )
            entries.append(item)
            self._insert(entries, index)
            self._changed()
        self.tick()
        return item

    def add_node(self, library_id=None, name=None, gcode=None, index=None, after_id=None):
        with self._lock:
            if library_id:
                lib = self.get_library(library_id)
                if lib is None:
                    raise ValueError("Unknown library node")
                item = lib.make_entry()
            else:
                item = QueueItem(type=NODE_ENTRY, name=name, gcode=gcode)
            if after_id is not None:
                index = self._index_after(after_id)
            self._insert([item], index)
            self._changed()
        self.tick()
        return item

    def _insert(self, entries, index):
        if index is None or index >= len(self.items):
            self.items.extend(entries)
        else:
            index = max(0, int(index))
            self.items[index:index] = entries

    def remove(self, item_id):
        with self._lock:
            item = self.get(item_id)
            if item is None:
                return False
            if item.id in (self.current_id, self.macro_id):
                raise ValueError("Cannot remove an entry while it is running")
            self.items.remove(item)
            if self.approval_id == item_id:
                self._reset_to_waiting()
            self._changed()
        self.tick()
        return True

    def update(self, item_id, **fields):
        with self._lock:
            item = self.get(item_id)
            if item is None:
                return None
            if "enabled" in fields:
                item.enabled = bool(fields["enabled"])
            if "scheduled_at" in fields and not item.is_node:
                item.scheduled_at = float(fields["scheduled_at"]) if fields["scheduled_at"] else None
            if "copies" in fields and not item.is_node:
                item.copies = _int(fields["copies"], item.copies, 1)
                if item.status == DONE and item.completed < item.copies:
                    item.status = PENDING
            if item.is_node and item.id != self.macro_id:
                if "name" in fields:
                    item.name = (fields["name"] or "").strip()
                if "gcode" in fields:
                    item.gcode = fields["gcode"] or ""
            if self.state == APPROVAL:
                # the approval always targets whatever is next in line
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
                self._reset_to_waiting()
            self._changed()
        self.tick()
        return True

    def requeue(self, item_id):
        with self._lock:
            item = self.get(item_id)
            if item is None or item.id in (self.current_id, self.macro_id):
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
            self.items = [i for i in self.items if i.status not in FINISHED]
            if len(self.items) != before:
                self._changed()

    # ----------------------------------------------------------- library

    def library_add(self, name="", gcode="", auto_add=False):
        with self._lock:
            node = LibraryNode(name=name, gcode=gcode, auto_add=auto_add)
            self.library.append(node)
            self._changed()
            return node

    def library_update(self, node_id, **fields):
        with self._lock:
            node = self.get_library(node_id)
            if node is None:
                return None
            if "name" in fields:
                node.name = (fields["name"] or "").strip()
            if "gcode" in fields:
                node.gcode = fields["gcode"] or ""
            if "auto_add" in fields:
                node.auto_add = bool(fields["auto_add"])
            self._changed()
            return node

    def library_remove(self, node_id):
        with self._lock:
            node = self.get_library(node_id)
            if node is None:
                return False
            self.library.remove(node)
            self._changed()
            return True

    def library_move(self, node_id, index):
        with self._lock:
            node = self.get_library(node_id)
            if node is None:
                return False
            self.library.remove(node)
            self.library.insert(max(0, min(int(index), len(self.library))), node)
            self._changed()
            return True

    # ------------------------------------------------------- queue control

    def start_queue(self):
        """Pressing Start counts as confirming the bed is clear."""
        with self._lock:
            self.running = True
            self.needs_clear = False
            self.printed = False
            self.message = ""
            if self.state in (IDLE, APPROVAL):
                self.state = WAITING
            self._changed()
        self.tick()

    def stop_queue(self):
        """Stop starting new entries. A running print or node is left alone."""
        with self._lock:
            self.running = False
            self.approval_id = None
            self.repeat = []
            if self.state in (WAITING, APPROVAL):
                self.state = IDLE
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

    def print_now(self, item_id):
        """Start one entry right away, ignoring its schedule and the approval step."""
        with self._lock:
            item = self.get(item_id)
            if item is None:
                raise ValueError("Unknown queue entry")
            if self.state in (STARTING, PRINT, MACRO):
                raise ValueError("The printer is busy")
            if not self._host.printer_idle():
                raise ValueError("The printer is not ready")
            if item.status != PENDING:
                item.status = PENDING
                item.completed = 0
                item.result = None
            self.approval_id = None
            if item.is_node:
                self._run_node(item, counts=True)
                self._changed()
                return
            self.needs_clear = False
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
            self.repeat = []
            if item is not None:
                item.finished_at = now
                if outcome == "done":
                    item.completed += 1
                    item.result = None
                    if item.completed >= item.copies:
                        item.status = DONE
                    else:
                        item.status = PENDING
                        self.repeat = self._nodes_after(item)
                else:
                    item.status = CANCELLED if outcome == "cancelled" else FAILED
                    item.result = reason or outcome
                if self.running:
                    self.printed = True

            s = self.settings()
            if outcome != "done" and self.running and s["on_failure"] != "continue":
                self.running = False
                self.repeat = []
                self.state = IDLE
                self.message = "Queue paused: print {}".format(outcome)
                self._host.notify("error", self.message)
                self._changed()
                return

            if not self.running:
                self.state = IDLE
                self._changed()
                return

            if item is None:
                self.printed = True  # a manual print ran while the queue was waiting
            self.state = WAITING
            self._changed()
        self.tick()

    def on_marker(self, token):
        with self._lock:
            if self.state != MACRO or token != self.macro_token:
                return
            node = self.get(self.macro_id)
            if node is not None:
                if self.macro_counts:
                    node.status = DONE
                    node.finished_at = self._host.now()
                    node.result = None
                elif node.status == RUNNING:
                    node.status = PENDING
            if self.repeat and self.repeat[0] == self.macro_id:
                self.repeat.pop(0)
            self.macro_id = None
            self.macro_token = None
            self.deadline = None
            self.state = WAITING if self.running else IDLE
            self._changed()
        self.tick()

    # ------------------------------------------------------------- the loop

    def tick(self):
        with self._lock:
            now = self._host.now()

            if self.state == STARTING and self.deadline and now > self.deadline:
                item = self.get(self.current_id)
                if item is not None:
                    item.status = FAILED
                    item.result = "Print did not start"
                self.current_id = None
                self._pause("Queue paused: the printer did not start the print")
                return

            if self.state == MACRO and self.deadline and now > self.deadline:
                node = self.get(self.macro_id)
                if node is not None:
                    node.status = FAILED
                    node.result = "Timed out"
                self.macro_id = None
                self.macro_token = None
                self._pause("Queue paused: a node's G-code did not finish in time")
                return

            if self.state == APPROVAL:
                s = self.settings()
                item = self.get(self.approval_id)
                if not s["require_approval"] or item is None or item is not self.next_entry():
                    self._reset_to_waiting()
                    self._changed()

            # Empty nodes finish instantly, so keep going until something
            # actually has to wait.
            for _ in range(len(self.items) + len(self.repeat) + 1):
                if self.state != WAITING or not self.running:
                    return
                if not self._advance(now):
                    return

    def _advance(self, now):
        """Take one step. Returns True when another step may follow immediately."""
        if not self._host.printer_idle():
            return False

        while self.repeat:
            node = self.get(self.repeat[0])
            if node is not None and node.enabled:
                return self._run_node(node, counts=False)
            self.repeat.pop(0)

        entry = self.next_entry()
        if entry is None:
            self._finish_queue()
            self._changed()
            return False

        if entry.is_node:
            return self._run_node(entry, counts=True)

        if not entry.is_due(now):
            return False

        if self.settings()["require_approval"] and self.needs_clear:
            self.state = APPROVAL
            self.approval_id = entry.id
            self._host.notify(
                "approval",
                "Previous print finished. Confirm the bed is clear to start the next one.",
                item=entry.title,
            )
            self._changed()
            return False

        self._start(entry)
        return False

    # -------------------------------------------------------------- internals

    def _nodes_after(self, item):
        """Node ids directly after a print; they run again between its copies."""
        ids = []
        found = False
        for entry in self.items:
            if entry is item:
                found = True
                continue
            if not found:
                continue
            if not entry.is_node:
                break
            if entry.enabled:
                ids.append(entry.id)
        return ids

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

    def _run_node(self, node, counts):
        """Send a node's G-code. Returns True if it finished instantly (nothing to send)."""
        lines = parse_script(node.gcode)
        if not lines:
            if counts:
                node.status = DONE
                node.finished_at = self._host.now()
            elif self.repeat and self.repeat[0] == node.id:
                self.repeat.pop(0)
            self._changed()
            return True
        self.macro_id = node.id
        self.macro_counts = counts
        self.macro_token = uuid.uuid4().hex[:10]
        if counts:
            node.status = RUNNING
        self.state = MACRO
        self.deadline = self._host.now() + _int(self.settings()["macro_timeout"], 3600, 10)
        self._host.send_script(lines, self.macro_token)
        self._changed()
        return False

    def _finish_queue(self):
        self.running = False
        self.state = IDLE
        self.approval_id = None
        self.deadline = None
        s = self.settings()
        if self.printed:
            self.message = "Queue finished"
            script = parse_script(s["finished_gcode"]) if s["finished_enabled"] else []
            if script:
                self._host.send_script(script, None)
            self._host.notify("success", "Print queue finished")
        else:
            self.message = "Nothing left to run"
        self.printed = False

    def _reset_to_waiting(self):
        self.approval_id = None
        self.state = WAITING if self.running else IDLE

    def _pause(self, message):
        self.running = False
        self.state = IDLE
        self.deadline = None
        self.repeat = []
        self.message = message
        self._host.notify("error", message)
        self._changed()
