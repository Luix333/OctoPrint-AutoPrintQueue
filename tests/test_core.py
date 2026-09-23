import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "octoprint_autoprintqueue"))

import core  # noqa: E402  (imported without the OctoPrint package __init__)
from core import QueueRunner, parse_script  # noqa: E402


class FakeHost(object):
    def __init__(self):
        self.t = 1000.0
        self.idle = True
        self.missing = set()
        self.started = []
        self.scripts = []
        self.notes = []
        self.changes = 0
        self.fail_start = None

    def now(self):
        return self.t

    def printer_idle(self):
        return self.idle

    def file_exists(self, origin, path):
        return path not in self.missing

    def start_print(self, origin, path):
        if self.fail_start:
            raise ValueError(self.fail_start)
        self.started.append(path)
        self.idle = False

    def send_script(self, lines, token):
        self.scripts.append((list(lines), token))

    def changed(self):
        self.changes += 1

    def notify(self, level, message, **kw):
        self.notes.append((level, message))


class Base(unittest.TestCase):
    def make(self, **settings):
        self.host = FakeHost()
        base = {"require_approval": False, "between_gcode": ""}
        base.update(settings)
        self.settings = base
        self.r = QueueRunner(self.host, lambda: self.settings)
        return self.r

    def finish(self, path, outcome="done", reason=None):
        """Simulate the printer running `path` to the end."""
        self.r.on_print_started("local", path)
        self.host.idle = True
        self.r.on_print_ended("local", path, outcome, reason)

    def run_marker(self):
        lines, token = self.host.scripts[-1]
        self.r.on_marker(token)
        return lines


class ScriptTests(unittest.TestCase):
    def test_parse_script_drops_comments_and_blanks(self):
        text = "; park\nG28 ; home\n\n  PARK_TOOLHEAD  \n;only comment\nM400"
        self.assertEqual(parse_script(text), ["G28", "PARK_TOOLHEAD", "M400"])

    def test_empty(self):
        self.assertEqual(parse_script(None), [])


class QueueFlowTests(Base):
    def test_runs_queue_back_to_back_with_macro_between(self):
        r = self.make(between_gcode="G28\nPARK", finished_enabled=True, finished_gcode="M84")
        r.add("a.gcode")
        r.add("b.gcode")
        self.assertEqual(self.host.started, [])  # nothing starts until the queue runs
        r.start_queue()
        self.assertEqual(self.host.started, ["a.gcode"])
        self.assertEqual(r.state, core.STARTING)

        self.finish("a.gcode")
        self.assertEqual(r.state, core.MACRO)
        self.assertEqual(self.host.started, ["a.gcode"])  # waits for the macro
        self.assertEqual(self.run_marker(), ["G28", "PARK"])
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

        self.finish("b.gcode")
        self.assertFalse(r.running)
        self.assertEqual(r.state, core.IDLE)
        self.assertEqual(self.host.scripts[-1], (["M84"], None))
        self.assertEqual([i.status for i in r.items], ["done", "done"])

    def test_wrong_marker_is_ignored(self):
        r = self.make(between_gcode="G28")
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        r.on_marker("not-the-token")
        self.assertEqual(r.state, core.MACRO)

    def test_between_macro_can_be_disabled(self):
        r = self.make(between_gcode="G28", between_enabled=False)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.host.scripts, [])
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

    def test_disabled_items_are_skipped(self):
        r = self.make()
        a = r.add("a.gcode")
        r.add("b.gcode")
        r.update(a.id, enabled=False)
        r.start_queue()
        self.assertEqual(self.host.started, ["b.gcode"])
        self.finish("b.gcode")
        self.assertFalse(r.running)  # only a disabled item left
        self.assertEqual(a.status, "pending")

    def test_waits_for_printer_to_be_idle(self):
        r = self.make()
        r.add("a.gcode")
        self.host.idle = False
        r.start_queue()
        self.assertEqual(self.host.started, [])
        self.host.idle = True
        r.tick()
        self.assertEqual(self.host.started, ["a.gcode"])

    def test_copies(self):
        r = self.make()
        r.add("a.gcode", copies=2)
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.host.started, ["a.gcode", "a.gcode"])
        self.finish("a.gcode")
        self.assertEqual(r.items[0].status, "done")
        self.assertEqual(r.items[0].completed, 2)

    def test_delay_between_prints(self):
        r = self.make(delay_seconds=30)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(r.state, core.DELAY)
        self.host.t += 10
        r.tick()
        self.assertEqual(self.host.started, ["a.gcode"])
        self.host.t += 25
        r.tick()
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

    def test_skip_delay(self):
        r = self.make(delay_seconds=300)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        r.skip_delay()
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])


class ApprovalTests(Base):
    def test_waits_for_approval_after_a_print(self):
        r = self.make(require_approval=True)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()  # pressing start is the approval for the first print
        self.assertEqual(self.host.started, ["a.gcode"])
        self.finish("a.gcode")
        self.assertEqual(r.state, core.APPROVAL)
        self.assertEqual(r.approval_id, r.items[1].id)
        self.assertIn("approval", [n[0] for n in self.host.notes])
        r.tick()
        self.assertEqual(self.host.started, ["a.gcode"])
        r.approve()
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

    def test_macro_runs_before_approval(self):
        r = self.make(require_approval=True, between_gcode="PARK")
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(r.state, core.MACRO)
        self.run_marker()
        self.assertEqual(r.state, core.APPROVAL)

    def test_approval_retargets_when_queue_is_reordered(self):
        r = self.make(require_approval=True)
        r.add("a.gcode")
        b = r.add("b.gcode")
        c = r.add("c.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(r.approval_id, b.id)
        r.move(c.id, 0)
        self.assertEqual(r.approval_id, c.id)
        r.approve()
        self.assertEqual(self.host.started[-1], "c.gcode")

    def test_turning_approval_off_while_waiting_starts_next(self):
        r = self.make(require_approval=True)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.settings["require_approval"] = False
        r.tick()  # still waiting: bed flagged as needing a clear, re-check picks it up
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

    def test_restart_after_stop_needs_no_approval(self):
        r = self.make(require_approval=True)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        r.stop_queue()
        self.assertEqual(r.state, core.IDLE)
        r.start_queue()
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])


class ScheduleTests(Base):
    def test_scheduled_item_waits_for_its_time(self):
        r = self.make()
        a = r.add("a.gcode", scheduled_at=self.host.t + 3600)
        r.start_queue()
        self.assertEqual(self.host.started, [])
        self.assertEqual(r.state, core.WAITING)
        self.assertEqual(r.snapshot()["next_scheduled"], a.scheduled_at)
        self.host.t += 3601
        r.tick()
        self.assertEqual(self.host.started, ["a.gcode"])

    def test_due_items_run_before_a_future_one(self):
        r = self.make()
        r.add("later.gcode", scheduled_at=self.host.t + 3600)
        r.add("now.gcode")
        r.start_queue()
        self.assertEqual(self.host.started, ["now.gcode"])
        self.finish("now.gcode")
        self.assertTrue(r.running)  # still waiting for the scheduled one
        self.host.t += 4000
        r.tick()
        self.assertEqual(self.host.started, ["now.gcode", "later.gcode"])

    def test_clearing_schedule_means_immediately(self):
        r = self.make()
        a = r.add("a.gcode", scheduled_at=self.host.t + 3600)
        r.start_queue()
        r.update(a.id, scheduled_at=None)
        self.assertEqual(self.host.started, ["a.gcode"])

    def test_print_now_ignores_schedule(self):
        r = self.make()
        a = r.add("a.gcode", scheduled_at=self.host.t + 3600)
        r.print_now(a.id)
        self.assertEqual(self.host.started, ["a.gcode"])
        self.assertFalse(r.running)


class FailureTests(Base):
    def test_failure_pauses_queue(self):
        r = self.make()
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode", "failed", "error")
        self.assertFalse(r.running)
        self.assertEqual(r.items[0].status, "failed")
        self.assertEqual(self.host.started, ["a.gcode"])

    def test_cancel_can_continue(self):
        r = self.make(on_failure="continue")
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode", "cancelled", "cancelled")
        self.assertEqual(r.items[0].status, "cancelled")
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

    def test_missing_file_is_skipped(self):
        r = self.make()
        r.add("gone.gcode")
        r.add("b.gcode")
        self.host.missing.add("gone.gcode")
        r.start_queue()
        r.tick()
        self.assertEqual(r.items[0].status, "failed")
        self.assertEqual(r.items[0].result, "File not found")
        self.assertEqual(self.host.started, ["b.gcode"])

    def test_start_rejected_pauses(self):
        r = self.make()
        r.add("a.gcode")
        self.host.fail_start = "Invalid file"
        r.start_queue()
        self.assertFalse(r.running)
        self.assertEqual(r.items[0].result, "Invalid file")

    def test_print_that_never_starts_times_out(self):
        r = self.make(start_timeout=30)
        r.add("a.gcode")
        r.start_queue()
        self.host.idle = True  # select_file silently did nothing
        self.host.t += 31
        r.tick()
        self.assertFalse(r.running)
        self.assertEqual(r.items[0].status, "failed")

    def test_macro_timeout_pauses(self):
        r = self.make(between_gcode="WAIT_FOREVER", macro_timeout=60)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.host.t += 61
        r.tick()
        self.assertFalse(r.running)
        self.assertEqual(self.host.started, ["a.gcode"])

    def test_requeue(self):
        r = self.make()
        r.add("a.gcode")
        r.start_queue()
        self.finish("a.gcode", "failed")
        r.requeue(r.items[0].id)
        self.assertEqual(r.items[0].status, "pending")
        r.start_queue()
        self.assertEqual(self.host.started, ["a.gcode", "a.gcode"])


class ManualPrintTests(Base):
    def test_queue_continues_after_a_manual_print(self):
        r = self.make(require_approval=True)
        r.add("q.gcode")
        self.host.idle = False  # user is already printing something
        r.start_queue()
        r.on_print_started("local", "manual.gcode")
        self.host.idle = True
        r.on_print_ended("local", "manual.gcode", "done")
        self.assertEqual(r.state, core.APPROVAL)
        self.assertEqual(r.items[0].status, "pending")  # manual print didn't count
        r.approve()
        self.assertEqual(self.host.started, ["q.gcode"])

    def test_stop_lets_current_print_finish_but_starts_nothing(self):
        r = self.make()
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        r.stop_queue()
        self.finish("a.gcode")
        self.assertEqual(r.items[0].status, "done")
        self.assertEqual(self.host.started, ["a.gcode"])
        self.assertEqual(r.state, core.IDLE)

    def test_cannot_remove_printing_item(self):
        r = self.make()
        a = r.add("a.gcode")
        r.start_queue()
        with self.assertRaises(ValueError):
            r.remove(a.id)

    def test_persistent_state_round_trip(self):
        r = self.make()
        r.add("a.gcode", scheduled_at=5000, copies=3)
        data = r.persistent_state()
        items = [core.QueueItem.from_dict(d) for d in data["items"]]
        self.assertEqual(items[0].to_dict(), r.items[0].to_dict())


if __name__ == "__main__":
    unittest.main()
