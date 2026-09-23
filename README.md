# OctoPrint-AutoPrintQueue

A print queue for OctoPrint. When the current print finishes, the next one in the queue starts
on its own. You can run G-code or Klipper macros between prints, schedule prints for a set date
and time, turn queue entries on and off, and require a "bed is clear" confirmation before each
new print.

## Features

- **Runs the queue for you.** When a print finishes, the next enabled entry starts. This also
  applies after a print you started by hand while the queue was running.
- **Macros between prints.** Any G-code or Klipper macro, one per line. It runs after a print
  finishes and only when another print is waiting. The queue waits until the printer has
  actually executed every line before going on (see [How the macro wait works](#how-the-macro-wait-works)).
  You can add an extra delay after it, and set separate G-code for when the whole queue is done.
- **Turn entries on and off.** Disabled entries stay in the list but are skipped.
- **Schedule a start time.** Every entry starts "Immediately" by default. Click it to choose a
  date and time. A scheduled entry never starts before its time. Entries that are already due
  print first, and the scheduled one starts at its time, or when the printer frees up after that.
- **Optional approval between prints.** When this is on, the queue stops after each print with a
  "Bed is clear, start next print" button in the tab and in a popup. Pressing **Start queue**
  counts as approval too. You can toggle it from the tab or in Settings.
- **Add files from the Files panel.** Every G-code file gets a <kbd>☰</kbd> (list) button that adds it
  to the queue. The tab also has a picker covering every file, including ones in subfolders.
- Set **copies** per entry, reorder entries, **print now** (ignores the schedule and approval),
  re-queue finished or failed entries, and clear finished ones.
- **Stops safely.** By default a failed or cancelled print stops the queue, and so do a missing
  file, a print that never starts, or a macro that times out. The queue and its running state
  are saved, so scheduled prints still happen after an OctoPrint restart. If a queued print was
  interrupted by the restart, the queue stays stopped.

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

1. Add files with the list button in the **Files** panel, or with the picker on the **Print Queue** tab.
2. Optionally set copies, a start time, or disable entries.
3. Press **Start queue**. If the printer is idle, the first due entry starts right away.
   Otherwise the queue waits for the current print to finish.
4. After each print, the between-prints G-code runs, then the extra wait, then the approval
   prompt if you turned it on. After that the next print starts.
5. The queue stops by itself when nothing is left, and runs the "queue finished" G-code if you
   set one.

**Stop queue** doesn't touch a print that is already running. It only prevents the next one from starting.

### Example between-prints G-code (Klipper)

```
; lift and park, then wait for the bed to cool so parts release
G91
G1 Z10 F600
G90
G28 X Y
TEMPERATURE_WAIT SENSOR=heater_bed MAXIMUM=35
```

If a macro waits a long time (like `TEMPERATURE_WAIT` above), also add its name under
**Settings → Serial Connection → Behaviour → Long running commands** so OctoPrint doesn't log
communication timeouts. The plugin's own **G-code timeout** defaults to one hour.

## Settings

| Setting | Default | |
|---|---|---|
| Wait for my approval before starting the next print | on | Stop after each print until you confirm the bed is clear |
| Run G-code / macros between prints | on | Runs only when the box below has content |
| Between-prints G-code | empty | Sent after a print when another one is waiting |
| Extra wait | 0 s | Pause after that G-code, before the next print or approval prompt |
| Run G-code when the last queued print finishes | off | e.g. `M84`, turn off lights |
| If a print fails or is cancelled | Stop the queue | Or keep going with the next print |
| G-code timeout | 3600 s | Stop the queue if between-prints G-code hasn't finished by then |
| Show the Add-to-queue button in the Files panel | on | |

## How the macro wait works

The between-prints lines are sent with `M400` and an OctoPrint `@autoprintqueue_done <token>`
command added at the end. The printer never sees `@` commands. OctoPrint handles one only when it
reaches that point in its send queue, which is after the printer has acknowledged everything
before it. Klipper doesn't acknowledge `M400` or a blocking macro until the work is done. So when
the plugin sees its token, the macros have really finished.

## API

`GET /api/plugin/autoprintqueue` returns the queue and runner state. `POST` accepts these
commands: `add` (`path`, optional `origin`, `scheduled_at` as epoch seconds, `copies`, `index`),
`remove`, `update` (`id` plus `enabled` / `scheduled_at` / `copies`), `move` (`id`, `index`),
`requeue`, `print_now`, `clear_finished`, `start`, `stop`, `approve`, `skip_delay`,
`set_approval` (`enabled`). Reading requires the STATUS permission, and every command requires PRINT.

## Development

The queue logic in `octoprint_autoprintqueue/core.py` doesn't import OctoPrint, so its tests run anywhere:

```bash
python -m unittest discover -s tests
```

## License

AGPL-3.0-or-later
