# coding=utf-8
import io
import json
import os
import threading
import time

import octoprint.plugin

from .core import DEFAULT_SETTINGS, FAILED, PRINTING, RUNNING, LibraryNode, QueueItem, QueueRunner, parse_help_line

MARKER = "autoprintqueue_done"
STATE_FILE = "queue.json"
MACROS_FILE = "macros.json"
HELP_CAPTURE_SECONDS = 8


def _can(permission_name):
    """True when the current request's user holds the permission (OctoPrint >= 1.4)."""
    try:
        from octoprint.access.permissions import Permissions

        return getattr(Permissions, permission_name).can()
    except Exception:
        return True


class AutoPrintQueuePlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def __init__(self):
        self._runner = None
        self._timer = None
        self._save_lock = threading.Lock()
        self._macros = []
        self._help_until = 0
        self._help_found = {}

    # ---------------------------------------------------------------- startup

    def on_after_startup(self):
        state = self._load_state()
        items = []
        interrupted = False
        for raw in state.get("items", []):
            try:
                item = QueueItem.from_dict(raw)
            except Exception:
                self._logger.warning("Dropping unreadable queue entry: %r", raw)
                continue
            if item.status in (PRINTING, RUNNING):
                # OctoPrint went down mid-print; that print is gone.
                item.status = FAILED
                item.result = "Interrupted (OctoPrint restarted)"
                interrupted = True
            items.append(item)

        self._runner = QueueRunner(
            self,
            get_settings=self._current_settings,
            items=items,
            library=[LibraryNode.from_dict(n) for n in state.get("library", [])],
            running=bool(state.get("running")) and not interrupted,
            needs_clear=bool(state.get("needs_clear")) or interrupted,
        )
        if interrupted:
            self._runner.message = "Queue stopped: a queued print was interrupted by a restart"
        self._save_state()
        self._macros = self._load_json(MACROS_FILE, [])

        from octoprint.util import RepeatedTimer

        self._timer = RepeatedTimer(2.0, self._safe_tick, daemon=True)
        self._timer.start()
        self._logger.info("Auto Print Queue ready with %d item(s)", len(items))

    def on_shutdown(self):
        if self._timer is not None:
            self._timer.cancel()
        if self._runner is not None:
            self._save_state()

    def _safe_tick(self):
        try:
            self._runner.tick()
        except Exception:
            self._logger.exception("Queue tick failed")

    # ------------------------------------------------------------ persistence

    def _load_state(self):
        return self._load_json(STATE_FILE, {})

    def _save_state(self):
        self._write_json(STATE_FILE, self._runner.persistent_state())

    def _load_json(self, name, default):
        path = os.path.join(self.get_plugin_data_folder(), name)
        if not os.path.exists(path):
            return default
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            self._logger.exception("Could not read %s", path)
            return default

    def _write_json(self, name, data):
        path = os.path.join(self.get_plugin_data_folder(), name)
        tmp = path + ".tmp"
        with self._save_lock:
            with io.open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            os.replace(tmp, path)

    # -------------------------------------------------- host interface (core)

    def now(self):
        return time.time()

    def printer_idle(self):
        try:
            return self._printer.get_state_id() == "OPERATIONAL"
        except Exception:
            return False

    def file_exists(self, origin, path):
        if origin == "sdcard":
            return True  # the SD listing is only known to the printer; let select_file decide
        return self._file_manager.file_exists("local", path)

    def start_print(self, origin, path):
        sd = origin == "sdcard"
        target = path if sd else self._file_manager.path_on_disk("local", path)
        self._logger.info("Starting queued print %s:%s", origin, path)
        self._printer.select_file(target, sd, printAfterSelect=True)

    def send_script(self, lines, token):
        commands = list(lines)
        if token:
            # M400 waits for the moves to finish; OctoPrint only processes the
            # @-command once the printer has acknowledged everything before it.
            commands += ["M400", "@{} {}".format(MARKER, token)]
        self._logger.info("Sending %d queue G-code line(s)", len(lines))
        self._printer.commands(commands, tags={"trigger:autoprintqueue"})

    def changed(self):
        try:
            self._save_state()
        except Exception:
            self._logger.exception("Could not save the queue")
        self._push_state()

    def notify(self, level, message, **extra):
        payload = {"type": "notify", "level": level, "message": message}
        payload.update(extra)
        self._plugin_manager.send_plugin_message(self._identifier, payload)

    def _snapshot(self):
        data = self._runner.snapshot()
        data["macros"] = self._macros
        data["macros_loading"] = time.time() < self._help_until
        return data

    def _push_state(self):
        payload = {"type": "state"}
        payload.update(self._snapshot())
        self._plugin_manager.send_plugin_message(self._identifier, payload)

    # ------------------------------------------------ printer macro list

    def refresh_macros(self, quiet=False):
        """Ask Klipper for its command list; the reply is read by on_gcode_received."""
        if not self._printer.is_operational():
            raise ValueError("Connect to the printer first")
        self._help_quiet = quiet
        self._help_found = {}
        self._help_until = time.time() + HELP_CAPTURE_SECONDS
        self._printer.commands(["HELP"], tags={"trigger:autoprintqueue"})
        timer = threading.Timer(HELP_CAPTURE_SECONDS + 0.5, self._finish_macro_capture)
        timer.daemon = True
        timer.start()

    def _finish_macro_capture(self):
        self._help_until = 0
        found = self._help_found
        self._help_found = {}
        if found:
            self._macros = [{"name": k, "description": found[k]} for k in sorted(found)]
            self._write_json(MACROS_FILE, self._macros)
            self._logger.info("Loaded %d printer macros/commands", len(self._macros))
        elif not getattr(self, "_help_quiet", False):
            self.notify("error", "The printer did not answer HELP with a command list (this needs Klipper)")
        self._push_state()

    def on_gcode_received(self, comm, line, *args, **kwargs):
        if self._help_until and time.time() < self._help_until:
            parsed = parse_help_line(line)
            if parsed:
                self._help_found[parsed[0]] = parsed[1]
        return line

    def _current_settings(self):
        return {key: self._settings.get([key]) for key in DEFAULT_SETTINGS}

    # ----------------------------------------------------------------- hooks

    def on_atcommand_sending(self, comm, phase, command, parameters, tags=None, *args, **kwargs):
        if command != MARKER or self._runner is None:
            return
        # Runs on the serial send thread; don't start a print from there.
        token = (parameters or "").strip()
        threading.Thread(target=self._runner.on_marker, args=(token,), daemon=True).start()

    def on_event(self, event, payload):
        if self._runner is None:
            return
        payload = payload or {}
        origin = payload.get("origin", "local")
        path = payload.get("path")
        if event == "PrintStarted":
            self._runner.on_print_started(origin, path)
        elif event == "PrintDone":
            self._runner.on_print_ended(origin, path, "done")
        elif event == "PrintFailed":
            # A cancel fires PrintCancelled and then PrintFailed(reason=cancelled).
            reason = payload.get("reason") or "error"
            outcome = "cancelled" if reason == "cancelled" else "failed"
            self._runner.on_print_ended(origin, path, outcome, reason=reason)
        elif event == "ClientOpened":
            self._push_state()
        elif event == "Connected" and not self._macros:
            # fill the macro list once, so node editors can autocomplete
            def later():
                try:
                    self.refresh_macros(quiet=True)
                except Exception:
                    pass

            timer = threading.Timer(5.0, later)
            timer.daemon = True
            timer.start()

    # -------------------------------------------------------------- settings

    def get_settings_defaults(self):
        return dict(DEFAULT_SETTINGS, file_button=True)

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        if self._runner is not None:
            self._push_state()

    # ---------------------------------------------------------- assets/views

    def get_assets(self):
        return {"js": ["js/autoprintqueue.js"], "css": ["css/autoprintqueue.css"]}

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [
            {"type": "tab", "name": "Print Queue", "custom_bindings": True},
            {
                "type": "sidebar",
                "name": "Print Queue",
                "icon": "list-ol",
                "custom_bindings": True,
                "template": "autoprintqueue_sidebar.jinja2",
            },
            {"type": "settings", "name": "Auto Print Queue", "custom_bindings": False},
        ]

    # ------------------------------------------------------------------- API

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {
            "add": ["path"],
            "add_node": [],
            "remove": ["id"],
            "update": ["id"],
            "move": ["id", "index"],
            "requeue": ["id"],
            "print_now": ["id"],
            "clear_finished": [],
            "start": [],
            "stop": [],
            "approve": [],
            "set_approval": ["enabled"],
            "library_add": [],
            "library_update": ["id"],
            "library_remove": ["id"],
            "library_move": ["id", "index"],
            "refresh_macros": [],
        }

    def on_api_get(self, request):
        import flask

        if not _can("STATUS"):
            flask.abort(403)
        return flask.jsonify(self._snapshot())

    def on_api_command(self, command, data):
        import flask

        if not _can("PRINT"):
            flask.abort(403)
        r = self._runner
        try:
            if command == "add":
                origin = data.get("origin") or "local"
                path = data["path"]
                if origin == "local" and not self._file_manager.file_exists("local", path):
                    return flask.make_response("File not found: " + path, 404)
                r.add(
                    path,
                    origin=origin,
                    name=data.get("name"),
                    scheduled_at=data.get("scheduled_at"),
                    copies=data.get("copies", 1),
                    index=data.get("index"),
                    auto_nodes=data.get("auto_nodes", True),
                )
            elif command == "add_node":
                r.add_node(
                    library_id=data.get("library_id"),
                    name=data.get("name"),
                    gcode=data.get("gcode"),
                    index=data.get("index"),
                    after_id=data.get("after_id"),
                )
            elif command == "remove":
                r.remove(data["id"])
            elif command == "update":
                fields = {k: data[k] for k in ("enabled", "scheduled_at", "copies", "name", "gcode") if k in data}
                if r.update(data["id"], **fields) is None:
                    return flask.make_response("Unknown queue item", 404)
            elif command == "move":
                r.move(data["id"], data["index"])
            elif command == "requeue":
                r.requeue(data["id"])
            elif command == "print_now":
                r.print_now(data["id"])
            elif command == "clear_finished":
                r.clear_finished()
            elif command == "start":
                r.start_queue()
            elif command == "stop":
                r.stop_queue()
            elif command == "approve":
                r.approve()
            elif command == "set_approval":
                self._settings.set_boolean(["require_approval"], bool(data["enabled"]))
                self._settings.save()
                r.tick()
            elif command == "library_add":
                r.library_add(
                    name=data.get("name", ""), gcode=data.get("gcode", ""), auto_add=data.get("auto_add", False)
                )
            elif command == "library_update":
                fields = {k: data[k] for k in ("name", "gcode", "auto_add") if k in data}
                if r.library_update(data["id"], **fields) is None:
                    return flask.make_response("Unknown library node", 404)
            elif command == "library_remove":
                r.library_remove(data["id"])
            elif command == "library_move":
                r.library_move(data["id"], data["index"])
            elif command == "refresh_macros":
                self.refresh_macros()
        except ValueError as exc:
            return flask.make_response(str(exc), 409)
        self._push_state()
        return flask.jsonify(self._snapshot())

    # --------------------------------------------------------- update hook

    def get_update_information(self):
        return {
            "autoprintqueue": {
                "displayName": "Auto Print Queue",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "Luix333",
                "repo": "OctoPrint-AutoPrintQueue",
                "current": self._plugin_version,
                "pip": "https://github.com/Luix333/OctoPrint-AutoPrintQueue/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "Auto Print Queue"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = AutoPrintQueuePlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.atcommand.sending": __plugin_implementation__.on_atcommand_sending,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.on_gcode_received,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }
