# airscan-wsd-bridge

Print and scan natively on macOS with network all-in-one printers that macOS does not set up by itself.

Many inexpensive all-in-one printers (for example the Canon PIXMA G3010 / G3410 series) have a perfectly good Wi-Fi scanner, but they only speak **WSD** (Microsoft's "Web Services for Devices" scan protocol). macOS only scans from **AirScan (eSCL)** devices, so the scanner never appears in Image Capture, Preview or Printers & Scanners, and the vendor's phone app becomes the only way to scan.

Printing has a similar gap: these printers usually support driverless printing (IPP), but without AirPrint macOS never offers them.

This project fixes both:

- **Scanning:** a small background service on your Mac presents itself to macOS as an AirScan scanner and translates every request into WSD calls to the real device.
- **Printing:** it finds the printer, shows what it can do, and adds it to macOS as a driverless printer.

```
Image Capture / Preview  --eSCL-->  airscan-wsd-bridge (on your Mac)  --WSD-->  printer
```

- No vendor driver and no admin password.
- Python standard library only; nothing to `pip install`.
- Nothing about your printer is hardcoded: it is found on the network and asked what it can do.

## Requirements

- macOS with `python3` (3.9 or newer). It comes with the Xcode Command Line Tools; if `python3 --version` asks to install them, accept.
- For scanning: a device on the same network that offers a WSD scan service with a flatbed and JPEG output.
- For printing: a device that supports driverless IPP printing (most Mopria-certified printers). Adding a printer needs an administrator account.

## Setup

```bash
git clone https://github.com/amrehabmohamed/airscan-wsd-bridge.git
cd airscan-wsd-bridge
python3 airscan_wsd.py setup
```

The setup walks you through five steps:

1. **Find your devices.** It searches the network for about 10 seconds and lists the printers and scanners that answered.
2. **Add the printer to macOS.** It shows the printer's colour support, paper sizes and resolution, then adds it as a driverless printer so it appears in every app's print dialog. If macOS already has it, nothing is changed. You can print a one-sheet test page.
3. **Test the scanner.** It prints the scanner's resolutions, colour modes and flatbed size. Put a page on the glass; it scans at the lowest resolution and opens the result so you can confirm the device really works before the service is installed. You can skip this.
4. **Install the scan service.** A per-user LaunchAgent that starts at login. It runs from a copy in `~/Library/Application Support/airscan-wsd-bridge`, so you can move or delete the cloned folder afterwards.
5. **Use it.** Print with Cmd+P in any app. Scan from Image Capture, Preview (File > Import from Scanner) or System Settings > Printers & Scanners; the scanner appears under *Shared*.

If only a printer or only a scanner is found, setup does that half and says so.

macOS will show a notification that **"AirScan WSD Bridge" can run in the background**. That is the scan service; it is listed under System Settings > General > Login Items & Extensions.

If macOS asks whether `python3` or your terminal may find devices on the local network, allow it. Discovery cannot work without it.

To just look, without installing anything:

```bash
python3 airscan_wsd.py discover
```

## Using it with Claude or another AI assistant

If you would rather have an AI tool do the setup, open this folder in it (for example Claude Code) and ask:

> Set up my printer and scanner with this tool.

[AGENTS.md](AGENTS.md) tells the assistant which commands are safe to run by itself, what to ask you first (printing a test page, starting a scan), and how to diagnose problems. Claude Code reads it automatically through `CLAUDE.md`.

## What to expect

- **Every scan sweeps the whole glass**, including the overview scan. Cropping to your selection is done on the Mac afterwards.
- **"Scanner is warming up" stays on screen for the whole scan.** The page reaches macOS in one piece at the end, so there is no live preview.
- **High resolutions are slow and large.** On the Canon G3010, a full page took about 7 seconds at 150 dpi and about a minute at 600 dpi (a 6 MB JPEG). Use the middle setting for documents.
- **Do not scan while the printer is printing.** The bridge refuses to start a job when the device reports busy, because a scan request sent to a busy device can leave it stuck until it is switched off and on.
- **Greyscale works even on colour-only devices**; the conversion is done on the Mac.
- **Only your Mac can use the bridge.** Other devices may see the scanner name on the network but are refused.
- If the router gives the printer a new address, the bridge finds it again by its device ID.

## Commands

| Command | What it does |
| --- | --- |
| `python3 airscan_wsd.py setup` | Guided setup (add `--yes` to skip the questions, `--no-test` to skip the test page and test scan) |
| `python3 airscan_wsd.py discover` | List printers and scanners and their features, without changing anything |
| `python3 airscan_wsd.py scan` | Scan one page to `~/Documents/Scans` (`--dpi`, `--gray`, `--pdf`, `--out`, `--no-open`) |
| `python3 airscan_wsd.py status` | Which printers macOS has, is the scan service running, is the scanner reachable |
| `python3 airscan_wsd.py uninstall` | Remove the scan service, its settings, and the printer if setup added it |

Log file: `~/Library/Logs/airscan-wsd-bridge.log`

## Tested devices

| Device | macOS | Printing | Scanning | Reported by |
| --- | --- | --- | --- | --- |
| Canon PIXMA G3010 series (sold as G3410 in some regions), firmware 1.130 | 27.0 | Works (driverless, colour, 600 dpi, one-sided) | Works: colour and greyscale, 150/300/600 dpi, JPEG and PDF | @amrehabmohamed |

Everything else is untested. If you try another device, please open a device report issue, whether it worked or not. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Troubleshooting

- **Nothing found.** Printer on and finished starting up; same Wi-Fi as the Mac (not a guest network); no VPN; Local Network permission allowed for your terminal (System Settings > Privacy & Security > Local Network).
- **"this scanner offers no JPEG output" or "reports no flatbed".** The device is discovered but not supported yet. Please report it.
- **The scanner is stuck with its light on.** Press Stop on the printer, or switch it off and on. Then run `status` and check it says `Idle` before scanning again.
- **The scanner shows in Image Capture but scans fail.** Look at the log file; it records each scan request and why it was refused.
- **"needs the manufacturer's driver".** The printer does not offer a driverless format (PWG Raster, URF or PDF), so it cannot be added automatically.
- **"Could not add the printer".** Adding printers needs an administrator account on the Mac.
- **Port 8290 is taken.** Setup picks the next free port automatically.

## Limitations

- Scanning is flatbed only. Document feeders (ADF) and duplex scanning are not supported.
- Printer features come from what the printer reports over IPP; vendor extras such as ink levels or head cleaning are not available.
- One scanner per Mac.
- No live preview while scanning.
- The device's own JPEG compression setting is used as-is.
- WSD is plain, unauthenticated HTTP on your local network. That is how the printer works with or without this bridge; the bridge adds no new exposure beyond a local port that refuses other machines.

## How it works

- `wsd.py` finds devices with a WS-Discovery multicast probe, reads their metadata, and drives the WS-Scan service (`GetScannerElements`, `CreateScanJob`, `RetrieveImage`, `CancelJob`). Scan tickets are built from the device's own default ticket, because hand-written tickets get rejected.
- `bridge.py` serves the eSCL endpoints (`ScannerCapabilities`, `ScannerStatus`, `ScanJobs`, `NextDocument`), advertises `_uscan._tcp` through the built-in `dns-sd` tool, and crops/converts images with the built-in `sips` tool.
- `ipp.py` finds IPP printers and adds them to macOS with the built-in `ippfind`, `ipptool` and `lpadmin` tools.
- `airscan_wsd.py` is the command line: setup, service installation and configuration.

Run the offline tests with:

```bash
python3 -m unittest discover tests
```

## Contributing

Reports for other devices and fixes that make more devices work are very welcome. All changes go through pull requests reviewed by the maintainer. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).

This project is not affiliated with Canon, Apple or Microsoft.
