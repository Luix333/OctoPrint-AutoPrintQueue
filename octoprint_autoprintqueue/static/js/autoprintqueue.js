/*
 * Auto Print Queue - tab view model and the "Add to queue" button in the Files panel.
 */
$(function () {
    var PLUGIN = "autoprintqueue";

    // Add our button to every G-code entry of the Files panel. This has to run
    // before Knockout binds the files list, i.e. in a view model constructor.
    function injectFileButton() {
        var template = $("#files_template_machinecode");
        if (!template.length) {
            console.warn("Auto Print Queue: files template not found, no Add-to-queue button");
            return;
        }
        var html = template.html();
        if (html.indexOf("apq-files-add") !== -1) return;
        var button =
            '<div class="btn btn-mini apq-files-add" ' +
            'data-bind="visible: $root.apqShowButton() && $root.loginState.hasPermissionKo($root.access.permissions.PRINT), ' +
            'click: function() { $root.apqAddToQueue($data); }" ' +
            'title="Add to print queue" aria-label="Add to print queue" role="link"><i class="fas fa-list-ol"></i></div>';
        var marker = /(<div class="btn-group action-buttons">)/;
        if (!marker.test(html)) {
            console.warn("Auto Print Queue: files template layout changed, no Add-to-queue button");
            return;
        }
        template.html(html.replace(marker, "$1" + button));
    }

    function pad(n) {
        return (n < 10 ? "0" : "") + n;
    }

    function toInputValue(epoch) {
        var d = epoch ? new Date(epoch * 1000) : new Date(Date.now() + 3600 * 1000);
        return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
            "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
    }

    function formatWhen(epoch) {
        var d = new Date(epoch * 1000);
        var today = new Date();
        var sameDay = d.toDateString() === today.toDateString();
        var time = pad(d.getHours()) + ":" + pad(d.getMinutes());
        return sameDay ? "today " + time : d.toLocaleDateString() + " " + time;
    }

    function formatDuration(seconds) {
        seconds = Math.max(0, Math.round(seconds));
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        var s = seconds % 60;
        if (h >= 48) return Math.floor(h / 24) + "d " + (h % 24) + "h";
        if (h) return h + "h " + pad(m) + "m";
        if (m) return m + "m " + pad(s) + "s";
        return s + "s";
    }

    var STATUS_TEXT = {
        pending: "Queued",
        printing: "Printing",
        done: "Done",
        failed: "Failed",
        cancelled: "Cancelled"
    };

    function QueueItemViewModel(parent, data) {
        var self = this;
        self.id = data.id;
        self.path = data.path;
        self.origin = data.origin;
        self.name = data.name;

        self.enabled = ko.observable(data.enabled);
        self.copies = ko.observable(data.copies);
        self.completed = ko.observable(data.completed);
        self.status = ko.observable(data.status);
        self.result = ko.observable(data.result);
        self.scheduledAt = ko.observable(data.scheduled_at);
        self.editingSchedule = ko.observable(false);
        self.scheduleInput = ko.observable("");
        self._syncing = false;

        self.update = function (d) {
            self._syncing = true;
            self.enabled(d.enabled);
            self.copies(d.copies);
            self.completed(d.completed);
            self.status(d.status);
            self.result(d.result);
            self.scheduledAt(d.scheduled_at);
            self._syncing = false;
        };

        self.enabled.subscribe(function (v) {
            if (!self._syncing) parent.send("update", {id: self.id, enabled: v});
        });
        self.copies.subscribe(function (v) {
            if (self._syncing) return;
            var n = parseInt(v, 10);
            if (!(n >= 1)) {
                self._syncing = true;
                self.copies(1);
                self._syncing = false;
                n = 1;
            }
            parent.send("update", {id: self.id, copies: n});
        });

        self.isNext = ko.pureComputed(function () {
            return parent.nextId() === self.id && self.status() === "pending";
        });

        self.scheduleText = ko.pureComputed(function () {
            var at = self.scheduledAt();
            return at ? formatWhen(at) : "Immediately";
        });

        self.scheduleCountdown = ko.pureComputed(function () {
            var at = self.scheduledAt();
            if (!at || self.status() !== "pending") return "";
            parent.clock();
            var left = at - parent.serverNow();
            return left > 0 ? "in " + formatDuration(left) : "due";
        });

        self.statusText = ko.pureComputed(function () {
            var text = STATUS_TEXT[self.status()] || self.status();
            if (self.status() === "pending" && !self.enabled()) text = "Disabled";
            if (self.copies() > 1 && self.status() !== "done") {
                text += " (" + (self.completed() + (self.status() === "printing" ? 1 : 0)) + "/" + self.copies() + ")";
            }
            if (self.result() && (self.status() === "failed" || self.status() === "cancelled")) {
                text += ": " + self.result();
            }
            return text;
        });

        self.rowClass = ko.pureComputed(function () {
            return {
                "apq-row-printing": self.status() === "printing",
                "apq-row-done": self.status() === "done",
                "apq-row-failed": self.status() === "failed" || self.status() === "cancelled",
                "apq-row-disabled": !self.enabled()
            };
        });

        self.editSchedule = function () {
            if (!parent.canControl()) return;
            self.scheduleInput(toInputValue(self.scheduledAt()));
            self.editingSchedule(true);
        };
        self.saveSchedule = function () {
            var value = self.scheduleInput();
            var when = value ? new Date(value).getTime() / 1000 : null;
            if (value && isNaN(when)) return;
            self.editingSchedule(false);
            parent.send("update", {id: self.id, scheduled_at: when});
        };
        self.clearSchedule = function () {
            self.editingSchedule(false);
            parent.send("update", {id: self.id, scheduled_at: null});
        };
        self.cancelSchedule = function () {
            self.editingSchedule(false);
        };
    }

    function AutoPrintQueueViewModel(parameters) {
        var self = this;
        self.loginState = parameters[0];
        self.settingsViewModel = parameters[1];
        self.files = parameters[2];
        self.access = parameters[3];

        self.items = ko.observableArray([]);
        self.running = ko.observable(false);
        self.state = ko.observable("idle");
        self.needsClear = ko.observable(false);
        self.nextId = ko.observable(null);
        self.approvalId = ko.observable(null);
        self.nextScheduled = ko.observable(null);
        self.deadline = ko.observable(null);
        self.message = ko.observable("");
        self.requireApproval = ko.observable(true);
        self.clock = ko.observable(Date.now());
        self.serverOffset = 0;

        self.availableFiles = ko.observableArray([]);
        self.fileToAdd = ko.observable();

        self.serverNow = function () {
            return (self.clock() + self.serverOffset) / 1000;
        };

        self.canControl = ko.pureComputed(function () {
            return self.loginState.hasPermission(self.access.permissions.PRINT);
        });

        self.canPrintNow = ko.pureComputed(function () {
            return self.canControl() && ["starting", "printing", "macro"].indexOf(self.state()) === -1;
        });

        self.hasPending = ko.pureComputed(function () {
            return self.items().some(function (i) {
                return i.status() === "pending" && i.enabled();
            });
        });

        self.hasFinished = ko.pureComputed(function () {
            return self.items().some(function (i) {
                return ["done", "failed", "cancelled"].indexOf(i.status()) !== -1;
            });
        });

        self.itemName = function (id) {
            var item = ko.utils.arrayFirst(self.items(), function (i) {
                return i.id === id;
            });
            return item ? item.name : "";
        };

        self.approvalName = ko.pureComputed(function () {
            return self.itemName(self.approvalId());
        });

        self.statusText = ko.pureComputed(function () {
            switch (self.state()) {
                case "printing":
                    return "Printing";
                case "starting":
                    return "Starting print…";
                case "macro":
                    return "Running between-prints G-code";
                case "delay":
                    return "Waiting before the next print";
                case "approval":
                    return "Waiting for approval";
                case "waiting":
                    return self.nextScheduled() ? "Waiting for scheduled print" : "Waiting for the printer";
                default:
                    return self.running() ? "Running" : "Stopped";
            }
        });

        self.statusDetail = ko.pureComputed(function () {
            self.clock();
            var now = self.serverNow();
            if (self.state() === "delay" && self.deadline()) {
                return formatDuration(self.deadline() - now) + " left";
            }
            if (self.state() === "waiting" && self.nextScheduled()) {
                return "next at " + formatWhen(self.nextScheduled()) + " (in " + formatDuration(self.nextScheduled() - now) + ")";
            }
            if (self.state() === "waiting" && self.nextId()) {
                return "next: " + self.itemName(self.nextId());
            }
            if (!self.running() && self.state() === "printing") {
                return "queue stopped, no further prints will start";
            }
            return "";
        });

        self.statusClass = ko.pureComputed(function () {
            if (self.state() === "approval") return "apq-state-approval";
            if (self.running() || self.state() !== "idle") return "apq-state-running";
            return "apq-state-stopped";
        });

        // ------------------------------------------------------------ state

        self.fromState = function (data) {
            self.serverOffset = data.server_time * 1000 - Date.now();
            self.running(data.running);
            self.state(data.state);
            self.needsClear(data.needs_clear);
            self.nextId(data.next_id);
            self.approvalId(data.approval_id);
            self.nextScheduled(data.next_scheduled);
            self.deadline(data.deadline);
            self.message(data.message || "");

            var existing = {};
            _.each(self.items(), function (i) {
                existing[i.id] = i;
            });
            var list = _.map(data.items, function (d) {
                var vm = existing[d.id];
                if (vm) {
                    vm.update(d);
                    return vm;
                }
                return new QueueItemViewModel(self, d);
            });
            self.items(list);
            self.updateApprovalPopup();
        };

        self.requestState = function () {
            if (!self.loginState.hasPermission(self.access.permissions.STATUS)) return;
            OctoPrint.simpleApiGet(PLUGIN).done(self.fromState);
        };

        self.send = function (command, payload) {
            return OctoPrint.simpleApiCommand(PLUGIN, command, payload || {})
                .done(self.fromState)
                .fail(function (xhr) {
                    new PNotify({
                        title: "Print queue",
                        text: xhr.responseText || "Request failed",
                        type: "error"
                    });
                    self.requestState();
                });
        };

        // --------------------------------------------------------- actions

        self.start = function () {
            self.send("start");
        };
        self.stop = function () {
            self.send("stop");
        };
        self.approve = function () {
            self.send("approve");
        };
        self.skipDelay = function () {
            self.send("skip_delay");
        };
        self.clearFinished = function () {
            self.send("clear_finished");
        };
        self.remove = function (item) {
            self.send("remove", {id: item.id});
        };
        self.requeue = function (item) {
            self.send("requeue", {id: item.id});
        };
        self.printNow = function (item) {
            showConfirmationDialog({
                title: "Print now?",
                message: "Start <strong>" + _.escape(item.name) + "</strong> right away, ignoring its schedule and the approval step?",
                proceed: "Print",
                onproceed: function () {
                    self.send("print_now", {id: item.id});
                }
            });
        };
        self.moveUp = function (item) {
            var i = self.items.indexOf(item);
            if (i > 0) self.send("move", {id: item.id, index: i - 1});
        };
        self.moveDown = function (item) {
            var i = self.items.indexOf(item);
            if (i < self.items().length - 1) self.send("move", {id: item.id, index: i + 1});
        };

        self.addFile = function (origin, path, name) {
            return self.send("add", {origin: origin, path: path, name: name}).done(function () {
                new PNotify({
                    title: "Added to print queue",
                    text: _.escape(name || path),
                    type: "success",
                    delay: 2500
                });
            });
        };

        self.addSelected = function () {
            var key = self.fileToAdd();
            var entry = ko.utils.arrayFirst(self.availableFiles(), function (f) {
                return f.key === key;
            });
            if (!entry) return;
            self.addFile(entry.origin, entry.path, entry.name);
            self.fileToAdd(undefined);
        };

        self.refreshFiles = function () {
            if (!self.loginState.hasPermission(self.access.permissions.FILES_LIST)) return;
            OctoPrint.files.list(true).done(function (response) {
                var out = [];
                (function walk(entries) {
                    _.each(entries, function (e) {
                        if (e.type === "folder") {
                            walk(e.children);
                        } else if (e.type === "machinecode") {
                            out.push({
                                key: e.origin + ":" + e.path,
                                origin: e.origin,
                                path: e.path,
                                name: e.display || e.name,
                                label: (e.origin === "sdcard" ? "[SD] " : "") + e.path
                            });
                        }
                    });
                })(response.files);
                out.sort(function (a, b) {
                    return a.label.localeCompare(b.label);
                });
                self.availableFiles(out);
            });
        };

        // Called from the button injected into the Files panel.
        self.files.apqShowButton = ko.observable(true);
        self.files.apqAddToQueue = function (data) {
            self.addFile(data.origin, data.path, data.display || data.name);
        };

        // Keep the quick toggle on the tab in sync with the plugin setting.
        self.requireApproval.subscribe(function (v) {
            var s = self.pluginSettings();
            if (!s || s.require_approval() === v) return;
            self.send("set_approval", {enabled: v}).done(function () {
                s.require_approval(v);
            });
        });

        self.pluginSettings = function () {
            var p = self.settingsViewModel.settings && self.settingsViewModel.settings.plugins;
            return p && p.autoprintqueue;
        };

        self.syncSettings = function () {
            var s = self.pluginSettings();
            if (!s) return;
            self.requireApproval(!!s.require_approval());
            self.files.apqShowButton(!!s.file_button());
        };

        // ------------------------------------------------ approval popup

        self.approvalPopup = undefined;
        self.updateApprovalPopup = function () {
            var waiting = self.state() === "approval" && self.canControl();
            if (!waiting) {
                if (self.approvalPopup) {
                    self.approvalPopup.remove();
                    self.approvalPopup = undefined;
                }
                return;
            }
            if (self.approvalPopup) return;
            self.approvalPopup = new PNotify({
                title: "Print queue: clear the bed",
                text: "The previous print finished. Start <strong>" + _.escape(self.approvalName()) + "</strong> once the bed is clear.",
                type: "info",
                hide: false,
                confirm: {
                    confirm: true,
                    buttons: [
                        {
                            text: "Bed is clear, start",
                            addClass: "btn-success",
                            click: function (notice) {
                                self.approve();
                                notice.remove();
                            }
                        },
                        {
                            text: "Later",
                            click: function (notice) {
                                notice.remove();
                            }
                        }
                    ]
                },
                buttons: {closer: false, sticker: false},
                history: {history: false}
            });
        };

        // ------------------------------------------------ OctoPrint hooks

        self.onBeforeBinding = function () {
            self.syncSettings();
        };

        self.onSettingsSaved = function () {
            self.syncSettings();
        };

        self.onUserLoggedIn = self.onUserLoggedOut = function () {
            self.requestState();
            self.refreshFiles();
        };

        self.onStartupComplete = function () {
            self.requestState();
            self.refreshFiles();
            setInterval(function () {
                self.clock(Date.now());
            }, 1000);
        };

        self.onEventUpdatedFiles = function () {
            self.refreshFiles();
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== PLUGIN) return;
            if (data.type === "state") {
                self.fromState(data);
            } else if (data.type === "notify" && data.level !== "approval") {
                new PNotify({
                    title: "Print queue",
                    text: _.escape(data.message),
                    type: data.level === "error" ? "error" : "success",
                    hide: data.level !== "error"
                });
            }
        };

        injectFileButton();
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: AutoPrintQueueViewModel,
        dependencies: ["loginStateViewModel", "settingsViewModel", "filesViewModel", "accessViewModel"],
        elements: ["#apq"]
    });
});
