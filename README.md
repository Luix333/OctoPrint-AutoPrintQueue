# OctoPrint-AutoPrintQueue

A print queue for OctoPrint. When the current print finishes, the next one in the queue starts
on its own. Between prints you can place **nodes** that run your G-code or Klipper macros. You
can also schedule prints for a set date and time, turn any entry on or off, and require a
"bed is clear" confirmation before each new print.

## Features

- **Runs the queue for you.** The queue is one ordered list of prints and nodes, and it runs
  from top to bottom. When a print finishes, the queue moves on to the next entry. This also
  applies after a print you started by hand while the queue was running.
- **Nodes between prints.** A node is a queue entry that sends one or more macros or G-code
  lines, for example `PARK`, `BLOBIFIER_CLEAN` or `G4 S60`. The queue waits until the printer
  has actually finished a node before moving on (see
  [How the node wait works](#how-the-node-wait-works)). Add a node anywhere in the queue. Use a
  blank node to type macro names, with autocomplete from your printer's own command list, or
  pick a node from your library.
- **Node library.** Save the nodes you use often. Tick **Auto** on a library node and it is
  inserted automatically between prints each time you add a print, whether from the tab or
  from the Files panel.
- **Turn entries on and off.** Every print and node has a checkbox. Unchecked entries stay in
  the list but are skipped.
- **Schedule a start time.** Prints start "Immediately" by default. Click that to choose a date
  and time. The queue runs in order, so it holds at a scheduled print until its time comes.
  Nodes before that print still run straight away.
- **Optional approval between prints.** When this is on, the queue stops before the next print,
  after the nodes in between have run. It shows a "Bed is clear, start next print" button in the
  tab, the sidebar and a popup. Pressing **Start queue** counts as approval too.
- **Sidebar panel.** A compact "Print Queue" panel shows the live queue with start, stop and
  approve buttons and the on/off checkboxes. It is a normal OctoPrint sidebar panel, so the
  **UI Customizer** plugin can move it to any column.
- **Add files from the Files panel.** Every G-code file gets a <kbd>☰</kbd> (list) button that
  adds it to the queue. The tab also has a picker covering every file, including subfolders.
- Set **copies** per print. The nodes directly after a print run again between its copies.
  You can also reorder entries, **print/run now**, re-queue finished entries, save a queue node
  to the library, and clear finished entries.
- **Stops safely.** By default the queue stops when a print fails or is cancelled, a file is
  missing, a print never starts, or a node times out. The queue and library are saved, so
  scheduled prints still happen after an OctoPrint restart. If a queued print was interrupted by
  the restart, the queue stays stopped.

## Install

In OctoPrint go to **Settings → Plugin Manager → Get More → … from URL** and enter:

```
https://github.com/Luix333/OctoPrint-AutoPrintQueue/archive/main.zip
```

Or from the OctoPrint virtualenv:

```bash
pip install https://github.com/Luix333/OctoPrint-AutoPrintQueue/archive/main.zip
```

Restart OctoPrint afterwards. Requires OctoPrint 1.4 or newer and Python 3.7 or newer. Tested
on OctoPrint 1.11.8.

## Usage

1. Open the **Node library** on the **Print Queue** tab and create the nodes you use between
   prints, for example "Eject part" = `PARK` + `OPEN_HOOD`. Tick **Auto** on the ones that
   should go between every pair of prints. Press **Load from printer** once so the editor can
   autocomplete your macro names. This works with Klipper, and happens automatically on the
   first connection.
2. Add prints with the list button in the **Files** panel, or with the picker on the tab. The
   Auto nodes are placed between each new print and the one before it.
3. Adjust as needed. Add more nodes with **Add node**; the <i>↳</i> button on a row puts the next
   node right after that row. Click a node to edit it. You can also untick entries, set copies,
   or schedule a print.
4. Press **Start queue**. If the printer is idle, the first entry runs right away. Otherwise the
   queue waits for the current print to finish.
5. The queue stops by itself when nothing is left, and runs the "queue finished" G-code if you
   set one in Settings.

**Stop queue** doesn't touch a print or node that is already running. It only prevents the next
entry from starting.

### Example nodes (Klipper)

| Node | G-code |
|---|---|
| Eject part | `PARK` then `OPEN_HOOD` |
| Cool bed | `TEMPERATURE_WAIT SENSOR=heater_bed MAXIMUM=35` |
| Wait a minute | `G4 S60` |
| Clean nozzle | `BLOBIFIER_CLEAN` |

If a node waits a long time (like `TEMPERATURE_WAIT`), also add that command under **Settings →
Serial Connection → Behaviour → Long running commands** so OctoPrint doesn't log communication
timeouts. The plugin's own **Node timeout** defaults to one hour.

## Settings

| Setting | Default | |
|---|---|---|
| Wait for my approval before starting the next print | on | Stop before each print that follows another until you confirm the bed is clear |
| Run G-code when the last queued print finishes | off | e.g. `M84`, turn off lights |
| If a print fails or is cancelled | Stop the queue | Or keep going with the next entry |
| Node timeout | 3600 s | Stop the queue if a node hasn't finished by then |
| Show the Add-to-queue button in the Files panel | on | |

Nodes and the node library are managed on the Print Queue tab, not in Settings.

## How the node wait works

A node's lines are sent with `M400` and an OctoPrint `@autoprintqueue_done <token>` command added
at the end. The printer never sees `@` commands. OctoPrint handles one only when it reaches that
point in its send queue, which is after the printer has acknowledged everything before it.
Klipper doesn't acknowledge `M400` or a blocking macro until the work is done. So when the plugin
sees its token, the node has really finished.

**Load from printer** sends Klipper's `HELP` command and reads the `// NAME: description` lines
that come back.

## API

`GET /api/plugin/autoprintqueue` returns the queue, the library, the known printer macros and
the runner state. `POST` accepts these commands:

- Queue: `add` (`path`, optional `origin`, `scheduled_at` as epoch seconds, `copies`, `index`,
  `auto_nodes`), `add_node` (`library_id`, or `name` + `gcode`; optional `index` or `after_id`),
  `remove`, `update` (`id` plus `enabled` / `scheduled_at` / `copies` / `name` / `gcode`),
  `move` (`id`, `index`), `requeue`, `print_now`, `clear_finished`
- Control: `start`, `stop`, `approve`, `set_approval` (`enabled`)
- Library: `library_add` (`name`, `gcode`, `auto_add`), `library_update`, `library_remove`,
  `library_move`
- `refresh_macros`

Reading requires the STATUS permission, and every command requires PRINT.

## Development

The queue logic in `octoprint_autoprintqueue/core.py` doesn't import OctoPrint, so its tests run anywhere:

```bash
python -m unittest discover -s tests
```

## License

AGPL-3.0-or-later
