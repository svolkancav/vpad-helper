# Daemon probes

Three bugs were found in `vpad_daemon.py` / `vpad_helper.py` on 2026-08-16.
Each of these scripts fails against the code as it was and passes against the
fix, so they are the regression guard for changes that would quietly bring the
bugs back.

They are **probes, not unit tests**: they drive a real daemon over a real
socket and read the result back from the operating system rather than from our
own bookkeeping. That is deliberate — two of the three bugs were invisible from
inside the process.

| Script | Guards against |
|---|---|
| `pad_isolation.py` | a refused connection neutralising the pad of whoever is playing |
| `goodbye_on_quit.py` | quitting the tray leaving the Bonjour record advertised |
| `single_instance.py` | two helpers running, only one of them reachable |

## Running them

`pad_isolation.py` needs **Windows with ViGEmBus installed**, because it reads
button state back through XInput. The other two run anywhere.

```bash
# terminal 1 — a daemon to probe
python vpad_daemon.py --port 51555 --inject vigem --name probe

# terminal 2
python android-vpad-helper/host/probes/pad_isolation.py 51555
python android-vpad-helper/host/probes/goodbye_on_quit.py
python android-vpad-helper/host/probes/single_instance.py
```

On Turkish (and other non-UTF-8) Windows consoles, run them with
`PYTHONIOENCODING=utf-8` — otherwise a box-drawing character in a log line
raises `UnicodeEncodeError` and kills the thread printing it.
