# Guide for AI assistants

This file is for Claude, Claude Code and other AI tools helping someone set up or work on this project. Humans: see [README.md](README.md).

## What this is

A macOS tool that makes network all-in-one printers work natively when macOS ignores them: it adds the printer as a driverless IPP queue, and runs a local service that exposes the device's WSD scanner as an AirScan (eSCL) scanner. Python 3.9 standard library only.

## Helping a user set it up

Run these from the repository folder. Start read-only, then set up.

| Goal | Command | Changes anything? |
| --- | --- | --- |
| See what is on the network | `python3 airscan_wsd.py discover` | No |
| Check current state | `python3 airscan_wsd.py status` | No |
| Set up without prompts | `python3 airscan_wsd.py setup --yes --no-test` | Adds a printer queue if missing, installs a login service |
| Set up with a test scan | `python3 airscan_wsd.py setup --yes` | As above, plus one scan (moves the scan head) |
| Scan from the command line | `python3 airscan_wsd.py scan --no-open` | Runs one scan, writes a file to `~/Documents/Scans` |
| Remove everything | `python3 airscan_wsd.py uninstall` | Removes the service, settings, and the printer if setup added it |

Notes for driving it non-interactively:

- Plain `setup` asks questions on stdin. Use `--yes` when you cannot answer prompts. `--yes` never prints a test page and does not open the test scan.
- With `--yes`, if several printers or scanners are found, setup skips that device instead of guessing. Ask the user which one, then have them run `setup` interactively.
- Discovery takes about 10 seconds in total. An empty result usually means the printer is off, on another network, or the terminal lacks Local Network permission.
- The service appears in macOS Login Items as "AirScan WSD Bridge". The log is `~/Library/Logs/airscan-wsd-bridge.log`; scan requests and refusals are recorded there.

## Things to ask the user before doing

- **Printing a test page.** It uses paper and ink.
- **Scanning.** Confirm a page is on the glass and the printer is not printing.
- **Running `setup` or `uninstall`.** They add or remove a printer queue and a login service on the user's Mac.

## Safety rules for the device

- **Never send a scan while the device is busy.** Check `status` shows `state : Idle` first. A scan request sent to a printing or scanning device has left one stuck until it was power-cycled.
- **If the scanner hangs** (light bar stopped mid-glass, state stays `Processing`, print jobs do not start): it cannot be cancelled remotely. Tell the user to press Stop on the printer, or switch it off and on. Do not keep retrying.
- **Do not hand-write scan tickets.** `wsd.build_ticket` derives them from the device's own default ticket on purpose; tickets written from scratch get rejected.
- Do not loop scans or prints to "test"; each one moves hardware or uses paper.

## Diagnosing problems

1. `python3 airscan_wsd.py status`: is the scanner reachable, is the service answering?
2. Read the end of `~/Library/Logs/airscan-wsd-bridge.log`.
3. `python3 airscan_wsd.py discover`: does the device still answer, and with which features?
4. Bridge endpoints, for checking by hand: `http://localhost:8290/eSCL/ScannerCapabilities` and `/eSCL/ScannerStatus` (the port is in `~/Library/Application Support/airscan-wsd-bridge/config.json`).

Messages such as "offers no JPEG output" or "reports no flatbed" mean the device is found but not supported yet. That is a device report, not a setup mistake.

## Working on the code

| File | Role |
| --- | --- |
| `wsd.py` | WS-Discovery and the WS-Scan client |
| `bridge.py` | eSCL server, Bonjour advertisement, image crop/convert |
| `ipp.py` | Printer discovery and queue management through `ippfind`, `ipptool`, `lpadmin` |
| `airscan_wsd.py` | Command line, configuration, LaunchAgent install |
| `tests/` | Offline tests: `python3 -m unittest discover tests` |

Rules (the full list is in [CONTRIBUTING.md](CONTRIBUTING.md)):

- Standard library only, Python 3.9 compatible.
- Nothing device-specific hardcoded: no addresses, serials, UUIDs or model names. Key special handling on what the device reports.
- The service runs from a copy in `~/Library/Application Support/airscan-wsd-bridge`. After editing code, rerun `setup` to refresh that copy.
- The bridge must only serve the Mac it runs on. Do not add anything that listens to other machines, downloads or executes code, or sends data anywhere but the scanner.
- Never commit a user's device identifiers. Redact IP addresses, serial numbers, MAC addresses and `urn:uuid:` values in issues, logs and test fixtures.
- Changes go through pull requests; only the maintainer merges.
