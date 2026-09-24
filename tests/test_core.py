import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "octoprint_autoprintqueue"))

import core  # noqa: E402  (imported without the OctoPrint package __init__)
from core import QueueRunner, parse_help_line, parse_script  # noqa: E402


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
        base = {"require_approval": False}
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
        """Let the printer finish the node G-code that was sent last."""
        lines, token = self.host.scripts[-1]
        self.r.on_marker(token)
        return lines

    def events(self):
        """Prints started and node scripts sent, in order."""
        return list(self.host.started)

    def statuses(self):
        return [(i.title, i.status) for i in self.r.items]


class ParserTests(unittest.TestCase):
    def test_parse_script_drops_comments_and_blanks(self):
        text = "; park\nG28 ; home\n\n  PARK_TOOLHEAD  \n;only comment\nM400"
        self.assertEqual(parse_script(text), ["G28", "PARK_TOOLHEAD", "M400"])

    def test_empty(self):
        self.assertEqual(parse_script(None), [])

    def test_klipper_help_lines(self):
        self.assertEqual(parse_help_line("// PARK: G-Code macro"), ("PARK", "G-Code macro"))
        self.assertEqual(
            parse_help_line("// BED_MESH_CALIBRATE : Perform Mesh Bed Leveling"),
            ("BED_MESH_CALIBRATE", "Perform Mesh Bed Leveling"),
        )
        self.assertEqual(parse_help_line("//   g32: lower case"), ("G32", "lower case"))
        self.assertIsNone(parse_help_line("// Available extended commands:"))
        self.assertIsNone(parse_help_line("ok"))
        self.assertIsNone(parse_help_line("echo: PARK: x"))


class QueueFlowTests(Base):
    def test_nodes_run_between_prints_in_order(self):
        r = self.make(finished_enabled=True, finished_gcode="M84")
        r.add("a.gcode")
        r.add_node(gcode="PARK")
        r.add_node(gcode="G4 S5\nCLEAN_NOZZLE")
        r.add("b.gcode")
        self.assertEqual(self.host.started, [])  # nothing runs until the queue is started
        r.start_queue()
        self.assertEqual(self.host.started, ["a.gcode"])

        self.finish("a.gcode")
        self.assertEqual(r.state, core.MACRO)
        self.assertEqual(self.run_marker(), ["PARK"])
        self.assertEqual(r.state, core.MACRO)
        self.assertEqual(self.run_marker(), ["G4 S5", "CLEAN_NOZZLE"])
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

        self.finish("b.gcode")
        self.assertFalse(r.running)
        self.assertEqual(self.host.scripts[-1], (["M84"], None))
        self.assertEqual([s for _, s in self.statuses()], ["done"] * 4)

    def test_node_waits_for_marker_before_next_print(self):
        r = self.make()
        r.add("a.gcode")
        n = r.add_node(gcode="PARK")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(n.status, core.RUNNING)
        r.on_marker("not-the-token")
        r.tick()
        self.assertEqual(self.host.started, ["a.gcode"])
        self.run_marker()
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

    def test_disabled_node_is_skipped(self):
        r = self.make()
        r.add("a.gcode")
        n = r.add_node(gcode="PARK")
        r.add("b.gcode")
        r.update(n.id, enabled=False)
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.host.scripts, [])
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])
        self.assertEqual(n.status, core.PENDING)

    def test_empty_node_passes_straight_through(self):
        r = self.make()
        r.add("a.gcode")
        r.add_node(gcode="; just a comment")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.host.started, ["a.gcode", "b.gcode"])

    def test_leading_node_runs_before_first_print(self):
        r = self.make()
        r.add_node(gcode="PREHEAT")
        r.add("a.gcode")
        r.start_queue()
        self.assertEqual(self.host.started, [])
        self.assertEqual(self.run_marker(), ["PREHEAT"])
        self.assertEqual(self.host.started, ["a.gcode"])

    def test_trailing_node_runs_after_last_print(self):
        r = self.make()
        r.add("a.gcode")
        r.add_node(gcode="COOLDOWN")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.run_marker(), ["COOLDOWN"])
        self.assertFalse(r.running)

    def test_node_waits_for_idle_printer(self):
        r = self.make()
        r.add_node(gcode="PARK")
        self.host.idle = False
        r.start_queue()
        self.assertEqual(self.host.scripts, [])
        self.host.idle = True
        r.tick()
        self.assertEqual(self.host.scripts[-1][0], ["PARK"])

    def test_disabled_prints_are_skipped(self):
        r = self.make()
        a = r.add("a.gcode")
        r.add("b.gcode")
        r.update(a.id, enabled=False)
        r.start_queue()
        self.assertEqual(self.host.started, ["b.gcode"])
        self.finish("b.gcode")
        self.assertFalse(r.running)
        self.assertEqual(a.status, "pending")

    def test_copies_repeat_the_nodes_after_the_print(self):
        r = self.make()
        a = r.add("a.gcode", copies=3)
        n = r.add_node(gcode="EJECT")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.run_marker(), ["EJECT"])
        self.assertEqual(n.status, core.PENDING)  # still owed after the last copy
        self.assertEqual(self.host.started, ["a.gcode", "a.gcode"])
        self.finish("a.gcode")
        self.run_marker()
        self.finish("a.gcode")
        self.assertEqual(a.status, core.DONE)
        self.assertEqual(self.run_marker(), ["EJECT"])
        self.assertEqual(n.status, core.DONE)
        self.assertEqual(self.host.started, ["a.gcode"] * 3 + ["b.gcode"])
        self.assertEqual(len(self.host.scripts), 3)

    def test_node_edit_changes_what_is_sent(self):
        r = self.make()
        r.add("a.gcode")
        n = r.add_node(gcode="OLD")
        r.add("b.gcode")
        r.update(n.id, gcode="NEW_MACRO", name="Swap")
        self.assertEqual(n.title, "Swap")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.run_marker(), ["NEW_MACRO"])

    def test_insert_node_after_entry(self):
        r = self.make()
        a = r.add("a.gcode")
        r.add("b.gcode")
        n = r.add_node(gcode="PARK", after_id=a.id)
        self.assertEqual([i.id for i in r.items][1], n.id)

    def test_run_node_now(self):
        r = self.make()
        n = r.add_node(gcode="PARK")
        r.print_now(n.id)
        self.assertEqual(self.run_marker(), ["PARK"])
        self.assertEqual(n.status, core.DONE)
        self.assertEqual(r.state, core.IDLE)


class LibraryTests(Base):
    def test_auto_add_nodes_go_between_prints(self):
        r = self.make()
        r.library_add(name="Eject", gcode="EJECT", auto_add=True)
        r.library_add(name="Unused", gcode="X", auto_add=False)
        r.library_add(name="Wipe", gcode="WIPE", auto_add=True)
        r.add("a.gcode")  # first print: nothing to separate yet
        r.add("b.gcode")
        r.add("c.gcode")
        self.assertEqual(
            [i.title for i in r.items],
            ["a.gcode", "Eject", "Wipe", "b.gcode", "Eject", "Wipe", "c.gcode"],
        )
        self.assertEqual(r.items[1].library_id, r.library[0].id)

    def test_add_without_auto_nodes(self):
        r = self.make()
        r.library_add(name="Eject", gcode="EJECT", auto_add=True)
        r.add("a.gcode")
        r.add("b.gcode", auto_nodes=False)
        self.assertEqual([i.title for i in r.items], ["a.gcode", "b.gcode"])

    def test_library_node_into_queue_is_a_copy(self):
        r = self.make()
        lib = r.library_add(name="Park", gcode="PARK")
        entry = r.add_node(library_id=lib.id)
        r.library_update(lib.id, gcode="PARK_V2")
        self.assertEqual(entry.gcode, "PARK")
        self.assertEqual(entry.title, "Park")

    def test_blank_node_title_comes_from_its_gcode(self):
        r = self.make()
        entry = r.add_node()
        self.assertEqual(entry.title, "Empty node")
        r.update(entry.id, gcode="BLOBIFIER_CLEAN\nPARK")
        self.assertEqual(entry.title, "BLOBIFIER_CLEAN …")

    def test_library_crud(self):
        r = self.make()
        a = r.library_add(name="A")
        b = r.library_add(name="B")
        r.library_move(b.id, 0)
        self.assertEqual([n.name for n in r.library], ["B", "A"])
        r.library_update(a.id, auto_add=True)
        self.assertTrue(a.auto_add)
        r.library_remove(b.id)
        self.assertEqual([n.name for n in r.library], ["A"])
        with self.assertRaises(ValueError):
            r.add_node(library_id="missing")


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

    def test_nodes_run_before_approval(self):
        r = self.make(require_approval=True)
        r.add("a.gcode")
        r.add_node(gcode="PARK")
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

    def test_node_moved_ahead_runs_before_approval(self):
        r = self.make(require_approval=True)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(r.state, core.APPROVAL)
        r.add_node(gcode="PARK", index=1)
        self.assertEqual(r.state, core.MACRO)

    def test_turning_approval_off_while_waiting_starts_next(self):
        r = self.make(require_approval=True)
        r.add("a.gcode")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.settings["require_approval"] = False
        r.tick()
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

    def test_queue_runs_in_order_so_a_scheduled_print_holds_the_rest(self):
        r = self.make()
        r.add("later.gcode", scheduled_at=self.host.t + 3600)
        r.add("after.gcode")
        r.start_queue()
        self.assertEqual(self.host.started, [])
        self.host.t += 3601
        r.tick()
        self.finish("later.gcode")
        self.assertEqual(self.host.started, ["later.gcode", "after.gcode"])

    def test_nodes_before_a_scheduled_print_run_right_away(self):
        r = self.make()
        r.add("a.gcode")
        r.add_node(gcode="COOLDOWN")
        r.add("b.gcode", scheduled_at=self.host.t + 3600)
        r.start_queue()
        self.finish("a.gcode")
        self.assertEqual(self.run_marker(), ["COOLDOWN"])
        self.assertEqual(r.state, core.WAITING)
        self.assertEqual(self.host.started, ["a.gcode"])

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
        r.add_node(gcode="PARK")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode", "failed", "error")
        self.assertFalse(r.running)
        self.assertEqual(r.items[0].status, "failed")
        self.assertEqual(self.host.scripts, [])

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

    def test_node_timeout_pauses(self):
        r = self.make(macro_timeout=60)
        r.add("a.gcode")
        n = r.add_node(gcode="WAIT_FOREVER")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        self.host.t += 61
        r.tick()
        self.assertFalse(r.running)
        self.assertEqual(n.status, core.FAILED)
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
        r.add_node(gcode="PARK")
        r.add("b.gcode")
        r.start_queue()
        r.stop_queue()
        self.finish("a.gcode")
        self.assertEqual(r.items[0].status, "done")
        self.assertEqual(self.host.scripts, [])
        self.assertEqual(r.state, core.IDLE)

    def test_stop_during_node_lets_it_finish(self):
        r = self.make()
        r.add("a.gcode")
        n = r.add_node(gcode="PARK")
        r.add("b.gcode")
        r.start_queue()
        self.finish("a.gcode")
        r.stop_queue()
        self.run_marker()
        self.assertEqual(n.status, core.DONE)
        self.assertEqual(r.state, core.IDLE)
        self.assertEqual(self.host.started, ["a.gcode"])

    def test_cannot_remove_running_entries(self):
        r = self.make()
        a = r.add("a.gcode")
        n = r.add_node(gcode="PARK")
        r.add("b.gcode")
        r.start_queue()
        with self.assertRaises(ValueError):
            r.remove(a.id)
        self.finish("a.gcode")
        with self.assertRaises(ValueError):
            r.remove(n.id)

    def test_persistent_state_round_trip(self):
        r = self.make()
        r.add("a.gcode", scheduled_at=5000, copies=3)
        r.add_node(name="Park", gcode="PARK")
        r.library_add(name="Eject", gcode="EJECT", auto_add=True)
        data = r.persistent_state()
        items = [core.QueueItem.from_dict(d) for d in data["items"]]
        self.assertEqual([i.to_dict() for i in items], [i.to_dict() for i in r.items])
        lib = [core.LibraryNode.from_dict(d) for d in data["library"]]
        self.assertEqual(lib[0].to_dict(), r.library[0].to_dict())

    def test_v1_state_without_type_loads_as_print(self):
        item = core.QueueItem.from_dict({"id": "x", "path": "a.gcode", "status": "pending"})
        self.assertFalse(item.is_node)
        self.assertEqual(item.title, "a.gcode")


if __name__ == "__main__":
    unittest.main()
