/*
 * Auto Print Queue - tab + sidebar view model and the "Add to queue" button in the Files panel.
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

    // ------------------------------------------------- first-layer drawing

    // One colour per tool (T0..T11 on an MMU), readable on light and dark themes.
    var TOOL_COLORS = [
        "#e8632b", "#2f80ed", "#27ae60", "#d4a017", "#9b51e0", "#eb5757",
        "#00a3c4", "#f2994a", "#4caf50", "#c2185b", "#5c6bc0", "#8d6e63"
    ];

    function toolColor(tool) {
        return TOOL_COLORS[(parseInt(tool, 10) || 0) % TOOL_COLORS.length];
    }

    // "rgb(r, g, b)" / "rgba(r, g, b, a)" -> same colour with alpha `a`
    function withAlpha(color, a) {
        var m = /rgba?\(([^,]+),([^,]+),([^,)]+)/.exec(color || "");
        return m ? "rgba(" + m[1] + "," + m[2] + "," + m[3] + "," + a + ")" : "rgba(128,128,128," + a + ")";
    }

    // Draw the bed and a first layer (from GET ?first_layer=<id>) on a canvas
    // that fills its container's width. Returns false while the container is
    // hidden (collapsed panel, inactive tab); the ResizeObserver redraws later.
    function drawFirstLayer(canvas, data) {
        var wrap = canvas.parentNode;
        var cssW = wrap ? wrap.clientWidth : 0;
        if (!cssW) return false;
        var bed = (data && data.bed) || canvas._apqBed || {width: 200, depth: 200, origin: "lowerleft", form: "rectangular"};
        canvas._apqBed = bed; // keep the last bed so it stays visible while loading

        var pad = 4;
        var scale = (cssW - 2 * pad) / bed.width;
        var maxH = 520;
        if (bed.depth * scale + 2 * pad > maxH) scale = (maxH - 2 * pad) / bed.depth;
        var cssH = Math.round(bed.depth * scale + 2 * pad);
        var dpr = window.devicePixelRatio || 1;
        canvas.style.width = cssW + "px";
        canvas.style.height = cssH + "px";
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);

        var ctx = canvas.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, cssW, cssH);

        // machine coordinates -> canvas; the back of the bed (max Y) is at the top
        var left = (cssW - bed.width * scale) / 2;
        var x0 = bed.origin === "center" ? -bed.width / 2 : 0;
        var y0 = bed.origin === "center" ? -bed.depth / 2 : 0;
        function px(x) {
            return left + (x - x0) * scale;
        }
        function py(y) {
            return pad + (bed.depth - (y - y0)) * scale;
        }

        var fg = window.getComputedStyle(canvas).color;
        ctx.save();
        ctx.beginPath();
        if (bed.form === "circular") {
            ctx.arc(px(x0 + bed.width / 2), py(y0 + bed.depth / 2), (bed.width * scale) / 2, 0, 2 * Math.PI);
        } else {
            ctx.rect(px(x0), py(y0 + bed.depth), bed.width * scale, bed.depth * scale);
        }
        ctx.fillStyle = withAlpha(fg, 0.08);
        ctx.fill();
        ctx.lineWidth = 1;
        ctx.strokeStyle = withAlpha(fg, 0.45);
        ctx.stroke();
        ctx.clip();

        // 50 mm grid
        ctx.beginPath();
        var g;
        for (g = Math.ceil(x0 / 50) * 50; g <= x0 + bed.width; g += 50) {
            ctx.moveTo(Math.round(px(g)) + 0.5, py(y0 + bed.depth));
            ctx.lineTo(Math.round(px(g)) + 0.5, py(y0));
        }
        for (g = Math.ceil(y0 / 50) * 50; g <= y0 + bed.depth; g += 50) {
            ctx.moveTo(px(x0), Math.round(py(g)) + 0.5);
            ctx.lineTo(px(x0 + bed.width), Math.round(py(g)) + 0.5);
        }
        ctx.strokeStyle = withAlpha(fg, 0.15);
        ctx.stroke();
        ctx.restore();

        ctx.fillStyle = withAlpha(fg, 0.45);
        ctx.font = "9px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("front", cssW / 2, cssH - pad - 2);

        if (!data || !data.tools) return true;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.lineWidth = Math.max(1, 0.45 * scale); // ~one extrusion width, at least a pixel
        _.each(data.tools, function (polylines, tool) {
            ctx.strokeStyle = toolColor(tool);
            ctx.beginPath();
            _.each(polylines, function (pl) {
                ctx.moveTo(px(pl[0]), py(pl[1]));
                for (var i = 2; i < pl.length; i += 2) ctx.lineTo(px(pl[i]), py(pl[i + 1]));
            });
            ctx.stroke();
        });
        return true;
    }

    // <canvas data-bind="apqFirstLayer: someObservable">: redraws when the data
    // changes and whenever its container is resized or shown again (collapsed
    // panels, tab switches and UI Customizer layouts all change the width).
    ko.bindingHandlers.apqFirstLayer = {
        init: function (element, valueAccessor) {
            var redraw = _.debounce(function () {
                drawFirstLayer(element, ko.unwrap(valueAccessor()));
            }, 60);
            if (window.ResizeObserver) {
                new ResizeObserver(redraw).observe(element.parentNode);
            } else {
                $(window).on("resize", redraw);
            }
        },
        update: function (element, valueAccessor) {
            drawFirstLayer(element, ko.unwrap(valueAccessor()));
        }
    };

    function thumbnailUrl(thumbnail) {
        if (!thumbnail) return null;
        if (/^(https?:)?\/\//.test(thumbnail) || thumbnail.charAt(0) === "/") return thumbnail;
        return BASEURL + thumbnail; // plugins store it relative to OctoPrint's base URL
    }

    // The G-code lines of a node, without comments, for one-line summaries.
    function gcodeSummary(text) {
        return _.filter(
            _.map((text || "").split("\n"), function (l) {
                return l.split(";")[0].trim();
            }),
            function (l) {
                return l.length;
            }
        ).join(" · ");
    }

    var STATUS_TEXT = {
        pending: "Queued",
        printing: "Printing",
        running: "Running",
        done: "Done",
        failed: "Failed",
        cancelled: "Cancelled"
    };

    // Editing form shared by queue nodes and library nodes: a name, the
    // G-code, and a macro box that autocompletes from the printer's commands.
    function NodeEditor(name, gcode, onSave, onCancel) {
        var self = this;
        self.name = ko.observable(name || "");
        self.gcode = ko.observable(gcode || "");
        self.macro = ko.observable("");
        self.insertMacro = function () {
            var m = (self.macro() || "").trim();
            if (!m) return;
            var text = self.gcode().replace(/\s+$/, "");
            self.gcode(text ? text + "\n" + m : m);
            self.macro("");
        };
        self.macroKey = function (data, event) {
            if (event.keyCode === 13) {
                self.insertMacro();
                return false;
            }
            return true;
        };
        self.save = function () {
            self.insertMacro(); // a macro typed but not inserted yet still counts
            onSave(self.name().trim(), self.gcode());
        };
        self.cancel = function () {
            onCancel();
        };
    }

    function QueueItemViewModel(parent, data) {
        var self = this;
        self.id = data.id;
        self.type = data.type || "print";
        self.isNode = self.type === "node";
        self.path = data.path;
        self.origin = data.origin;

        self.name = ko.observable(data.name);
        self.title = ko.observable(data.title);
        self.gcode = ko.observable(data.gcode);
        self.enabled = ko.observable(data.enabled);
        self.copies = ko.observable(data.copies);
        self.completed = ko.observable(data.completed);
        self.status = ko.observable(data.status);
        self.result = ko.observable(data.result);
        self.scheduledAt = ko.observable(data.scheduled_at);
        self.thumbnail = ko.observable(data.thumbnail || null);
        self.fileDate = ko.observable(data.file_date || null);
        self.thumbBroken = ko.observable(false);
        self.editingSchedule = ko.observable(false);
        self.scheduleInput = ko.observable("");
        self.editor = ko.observable(null);
        self._syncing = false;

        self.update = function (d) {
            self._syncing = true;
            self.name(d.name);
            self.title(d.title);
            self.gcode(d.gcode);
            self.enabled(d.enabled);
            self.copies(d.copies);
            self.completed(d.completed);
            self.status(d.status);
            self.result(d.result);
            self.scheduledAt(d.scheduled_at);
            if (d.thumbnail !== self.thumbnail()) self.thumbBroken(false);
            self.thumbnail(d.thumbnail || null);
            self.fileDate(d.file_date || null);
            self._syncing = false;
        };

        // Slicer thumbnail in place of the print icon; falls back to the icon
        // when there is none or the image fails to load.
        self.thumbUrl = ko.pureComputed(function () {
            return self.isNode || self.thumbBroken() ? null : thumbnailUrl(self.thumbnail());
        });
        self.thumbError = function () {
            self.thumbBroken(true);
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

        self.isActive = ko.pureComputed(function () {
            return self.status() === "printing" || self.status() === "running" || parent.macroId() === self.id;
        });

        self.isNext = ko.pureComputed(function () {
            return parent.nextId() === self.id && self.status() === "pending";
        });

        self.summary = ko.pureComputed(function () {
            if (!self.isNode) return "";
            var s = gcodeSummary(self.gcode());
            // don't repeat the title when the node has no name of its own
            return self.name() ? s : "";
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
            if (self.status() === "pending" && !self.enabled()) text = self.isNode ? "Skipped" : "Disabled";
            if (!self.isNode && self.copies() > 1 && self.status() !== "done") {
                text += " (" + (self.completed() + (self.status() === "printing" ? 1 : 0)) + "/" + self.copies() + ")";
            }
            if (self.result() && (self.status() === "failed" || self.status() === "cancelled")) {
                text += ": " + self.result();
            }
            return text;
        });

        // Short text for the sidebar: the schedule while waiting, otherwise the status.
        self.sideText = ko.pureComputed(function () {
            if (self.status() === "pending" && self.enabled() && self.scheduledAt() && self.scheduleCountdown()) {
                return self.scheduleCountdown();
            }
            return self.statusText();
        });

        self.rowClass = ko.pureComputed(function () {
            return {
                "apq-row-node": self.isNode,
                "apq-row-active": self.isActive(),
                "apq-row-next": self.isNext(),
                "apq-row-done": self.status() === "done",
                "apq-row-failed": self.status() === "failed" || self.status() === "cancelled",
                "apq-row-disabled": !self.enabled(),
                "apq-row-previewed": !self.isNode && parent.previewItem() === self
            };
        });

        self.editNode = function () {
            if (!parent.canControl() || self.isActive()) return;
            self.editor(
                new NodeEditor(
                    self.name(),
                    self.gcode(),
                    function (name, gcode) {
                        self.editor(null);
                        parent.send("update", {id: self.id, name: name, gcode: gcode});
                    },
                    function () {
                        self.editor(null);
                    }
                )
            );
        };

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

    function LibraryNodeViewModel(parent, data) {
        var self = this;
        self.id = data.id;
        self.name = ko.observable(data.name);
        self.title = ko.observable(data.title);
        self.gcode = ko.observable(data.gcode);
        self.autoAdd = ko.observable(data.auto_add);
        self.editor = ko.observable(null);
        self._syncing = false;

        self.update = function (d) {
            self._syncing = true;
            self.name(d.name);
            self.title(d.title);
            self.gcode(d.gcode);
            self.autoAdd(d.auto_add);
            self._syncing = false;
        };

        self.autoAdd.subscribe(function (v) {
            if (!self._syncing) parent.send("library_update", {id: self.id, auto_add: v});
        });

        self.summary = ko.pureComputed(function () {
            return gcodeSummary(self.gcode()) || "(empty)";
        });

        self.edit = function () {
            if (!parent.canControl()) return;
            self.editor(
                new NodeEditor(
                    self.name(),
                    self.gcode(),
                    function (name, gcode) {
                        self.editor(null);
                        parent.send("library_update", {id: self.id, name: name, gcode: gcode});
                    },
                    function () {
                        self.editor(null);
                    }
                )
            );
        };
    }

    function reconcile(observableArray, rows, make) {
        var existing = {};
        _.each(observableArray(), function (vm) {
            existing[vm.id] = vm;
        });
        observableArray(
            _.map(rows, function (d) {
                var vm = existing[d.id];
                if (vm) {
                    vm.update(d);
                    return vm;
                }
                return make(d);
            })
        );
    }

    function AutoPrintQueueViewModel(parameters) {
        var self = this;
        self.loginState = parameters[0];
        self.settingsViewModel = parameters[1];
        self.files = parameters[2];
        self.access = parameters[3];

        self.items = ko.observableArray([]);
        self.library = ko.observableArray([]);
        self.macros = ko.observableArray([]);
        self.macrosLoading = ko.observable(false);
        self.running = ko.observable(false);
        self.state = ko.observable("idle");
        self.needsClear = ko.observable(false);
        self.nextId = ko.observable(null);
        self.approvalId = ko.observable(null);
        self.macroId = ko.observable(null);
        self.nextScheduled = ko.observable(null);
        self.message = ko.observable("");
        self.requireApproval = ko.observable(true);
        self.clock = ko.observable(Date.now());
        self.serverOffset = 0;

        self.availableFiles = ko.observableArray([]);
        self.fileToAdd = ko.observable();
        self.nodeToAdd = ko.observable("blank");
        self.insertAfter = ko.observable(null); // queue entry the next node goes after
        self.libraryOpen = ko.observable(false);
        self.newLibraryEditor = ko.observable(null);

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

        self.autoAddCount = ko.pureComputed(function () {
            return _.filter(self.library(), function (n) {
                return n.autoAdd();
            }).length;
        });

        self.nodeChoices = ko.pureComputed(function () {
            var choices = [{value: "blank", label: "Blank node (type macros)"}];
            _.each(self.library(), function (n) {
                choices.push({value: n.id, label: "Library: " + n.title()});
            });
            return choices;
        });

        // ------------------------------------------- first-layer preview

        self.printItems = ko.pureComputed(function () {
            return _.filter(self.items(), function (i) {
                return !i.isNode;
            });
        });
        self.previewPinned = ko.observable(null); // item id the user clicked, or null = follow the queue

        // Pinned job if it still exists; otherwise what matters right now:
        // the print on the printer, the one waiting for approval, the next one.
        self.previewItem = ko.pureComputed(function () {
            var prints = self.printItems();
            var pick = function (test) {
                return _.find(prints, test) || null;
            };
            return (
                pick(function (i) {
                    return i.id === self.previewPinned();
                }) ||
                pick(function (i) {
                    return i.status() === "printing";
                }) ||
                pick(function (i) {
                    return i.id === self.approvalId();
                }) ||
                pick(function (i) {
                    return i.status() === "pending" && i.enabled();
                }) ||
                prints[0] ||
                null
            );
        });
        self.previewIndex = ko.pureComputed(function () {
            return self.printItems().indexOf(self.previewItem());
        });
        self.showFirstLayer = function (item) {
            if (item && !item.isNode) self.previewPinned(item.id);
        };
        self.previewPrev = function () {
            var i = self.previewIndex();
            if (i > 0) self.previewPinned(self.printItems()[i - 1].id);
        };
        self.previewNext = function () {
            var i = self.previewIndex();
            if (i >= 0 && i < self.printItems().length - 1) self.previewPinned(self.printItems()[i + 1].id);
        };
        self.previewFollow = function () {
            self.previewPinned(null);
        };

        // The preview sits under the queue in the sidebar panel and can be
        // folded away; remembered per browser.
        var PREVIEW_OPEN_KEY = "autoprintqueue.previewOpen";
        var storedOpen = null;
        try {
            storedOpen = window.localStorage.getItem(PREVIEW_OPEN_KEY);
        } catch (e) {}
        self.previewOpen = ko.observable(storedOpen !== "0");
        self.togglePreview = function () {
            self.previewOpen(!self.previewOpen());
            try {
                window.localStorage.setItem(PREVIEW_OPEN_KEY, self.previewOpen() ? "1" : "0");
            } catch (e) {}
        };

        self.previewData = ko.observable(null);
        self.previewState = ko.observable("none"); // none | loading | ready | error
        self.previewError = ko.observable("");
        self._previewCache = {}; // "id|file_date" -> parsed first layer
        self._previewKey = null;

        self.loadPreview = function () {
            var item = self.previewItem();
            var key = item ? item.id + "|" + item.fileDate() : null;
            if (key === self._previewKey) return;
            self._previewKey = key;
            if (!item) {
                self.previewData(null);
                self.previewState("none");
                return;
            }
            if (self._previewCache[key]) {
                self.previewData(self._previewCache[key]);
                self.previewState("ready");
                return;
            }
            self.previewState("loading");
            OctoPrint.simpleApiGet(PLUGIN, {data: {first_layer: item.id}})
                .done(function (data) {
                    var keys = _.keys(self._previewCache);
                    if (keys.length >= 12) delete self._previewCache[keys[0]];
                    self._previewCache[key] = data;
                    if (self._previewKey !== key) return; // the selection moved on meanwhile
                    self.previewData(data);
                    self.previewState("ready");
                })
                .fail(function (xhr) {
                    if (self._previewKey !== key) return;
                    self._previewKey = null; // allow a retry on the next change
                    self.previewData(null);
                    var msg = (xhr.responseJSON && xhr.responseJSON.error) || xhr.responseText || "Could not read the file";
                    self.previewError(xhr.status === 403 ? "No permission to read files" : msg);
                    self.previewState("error");
                });
        };
        ko.computed(function () {
            var item = self.previewItem();
            if (item) item.fileDate(); // re-fetch when the file is replaced
            _.defer(self.loadPreview); // outside this computed, so the request's observables aren't tracked
        });

        self.previewMessage = ko.pureComputed(function () {
            switch (self.previewState()) {
                case "none":
                    return "No prints in the queue";
                case "loading":
                    return "Reading the first layer…";
                case "error":
                    return self.previewError();
            }
            var d = self.previewData();
            if (d && d.method === "none") return "Nothing is printed on the first layer";
            return "";
        });

        self.previewSummary = ko.pureComputed(function () {
            var d = self.previewData();
            if (!d || !d.bounds) return "";
            var w = d.bounds[2] - d.bounds[0];
            var h = d.bounds[3] - d.bounds[1];
            return (
                "First layer" +
                (d.z !== null ? " at Z " + d.z.toFixed(2) : "") +
                " · " + Math.round(w) + " × " + Math.round(h) + " mm" +
                (d.truncated ? " (partial)" : "")
            );
        });

        self.previewTools = ko.pureComputed(function () {
            var d = self.previewData();
            if (!d || !d.tools) return [];
            var tools = _.keys(d.tools).sort(function (a, b) {
                return a - b;
            });
            if (tools.length === 1 && tools[0] === "0") return []; // single extruder: nothing to tell apart
            return _.map(tools, function (t) {
                return {tool: t, color: toolColor(t)};
            });
        });

        self.findItem = function (id) {
            return ko.utils.arrayFirst(self.items(), function (i) {
                return i.id === id;
            });
        };

        self.approvalName = ko.pureComputed(function () {
            var item = self.findItem(self.approvalId());
            return item ? item.title() : "";
        });

        self.activeItem = ko.pureComputed(function () {
            return ko.utils.arrayFirst(self.items(), function (i) {
                return i.isActive();
            });
        });

        self.statusText = ko.pureComputed(function () {
            switch (self.state()) {
                case "printing":
                    return "Printing";
                case "starting":
                    return "Starting print…";
                case "macro":
                    return "Running node";
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
            var active = self.activeItem();
            if ((self.state() === "macro" || self.state() === "printing" || self.state() === "starting") && active) {
                return active.title();
            }
            if (self.state() === "waiting" && self.nextScheduled()) {
                return "next at " + formatWhen(self.nextScheduled()) + " (in " + formatDuration(self.nextScheduled() - now) + ")";
            }
            if (self.state() === "waiting" && self.nextId()) {
                var next = self.findItem(self.nextId());
                return next ? "next: " + next.title() : "";
            }
            if (!self.running() && self.state() === "printing") {
                return "queue stopped, nothing else will start";
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
            self.macroId(data.macro_id);
            self.nextScheduled(data.next_scheduled);
            self.message(data.message || "");
            if (data.macros) self.macros(data.macros);
            self.macrosLoading(!!data.macros_loading);

            reconcile(self.items, data.items, function (d) {
                return new QueueItemViewModel(self, d);
            });
            reconcile(self.library, data.library || [], function (d) {
                return new LibraryNodeViewModel(self, d);
            });
            if (self.insertAfter() && !self.findItem(self.insertAfter().id)) self.insertAfter(null);
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
                        text: _.escape(xhr.responseText || "Request failed"),
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
                title: item.isNode ? "Run this node now?" : "Print now?",
                message: item.isNode
                    ? "Send <strong>" + _.escape(item.title()) + "</strong> to the printer right away?"
                    : "Start <strong>" + _.escape(item.title()) + "</strong> right away, ignoring its schedule and the approval step?",
                proceed: item.isNode ? "Run" : "Print",
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

        // "+" on a row: the next node added goes right after that row.
        self.chooseInsertAfter = function (item) {
            self.insertAfter(self.insertAfter() === item ? null : item);
        };
        self.clearInsertAfter = function () {
            self.insertAfter(null);
        };

        self.addNode = function () {
            var choice = self.nodeToAdd();
            var payload = choice === "blank" ? {name: "", gcode: ""} : {library_id: choice};
            if (self.insertAfter()) payload.after_id = self.insertAfter().id;
            var before = _.pluck(self.items(), "id");
            self.send("add_node", payload).done(function () {
                self.insertAfter(null);
                if (choice !== "blank") return;
                // open the editor on the new blank node straight away
                var added = ko.utils.arrayFirst(self.items(), function (i) {
                    return before.indexOf(i.id) === -1;
                });
                if (added) added.editNode();
            });
        };

        // ----------------------------------------------------------- library

        self.toggleLibrary = function () {
            self.libraryOpen(!self.libraryOpen());
        };
        self.newLibraryNode = function () {
            self.libraryOpen(true);
            self.newLibraryEditor(
                new NodeEditor(
                    "",
                    "",
                    function (name, gcode) {
                        self.newLibraryEditor(null);
                        self.send("library_add", {name: name, gcode: gcode, auto_add: false});
                    },
                    function () {
                        self.newLibraryEditor(null);
                    }
                )
            );
        };
        self.libraryToQueue = function (node) {
            var payload = {library_id: node.id};
            if (self.insertAfter()) payload.after_id = self.insertAfter().id;
            self.send("add_node", payload).done(function () {
                self.insertAfter(null);
            });
        };
        self.libraryRemove = function (node) {
            showConfirmationDialog({
                title: "Delete library node?",
                message: "Delete <strong>" + _.escape(node.title()) + "</strong> from the library? Nodes already in the queue stay.",
                proceed: "Delete",
                onproceed: function () {
                    self.send("library_remove", {id: node.id});
                }
            });
        };
        self.libraryUp = function (node) {
            var i = self.library.indexOf(node);
            if (i > 0) self.send("library_move", {id: node.id, index: i - 1});
        };
        self.libraryDown = function (node) {
            var i = self.library.indexOf(node);
            if (i < self.library().length - 1) self.send("library_move", {id: node.id, index: i + 1});
        };
        self.saveQueueNodeToLibrary = function (item) {
            self.send("library_add", {name: item.name() || item.title(), gcode: item.gcode(), auto_add: false}).done(function () {
                self.libraryOpen(true);
            });
        };

        self.loadMacros = function () {
            self.macrosLoading(true);
            self.send("refresh_macros");
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

        self.openTab = function () {
            $('#tabs a[href="#tab_plugin_autoprintqueue"]').tab("show");
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
        elements: ["#apq", "#apq_sidebar"]
    });
});
