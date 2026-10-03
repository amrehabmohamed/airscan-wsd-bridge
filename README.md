# airscan-wsd-bridge

Scan natively on macOS from network printers that macOS does not recognise as scanners.

Many inexpensive all-in-one printers (for example the Canon PIXMA G3010 / G3410 series) have a perfectly good Wi-Fi scanner, but they only speak **WSD** (Microsoft's "Web Services for Devices" scan protocol). macOS only scans from **AirScan (eSCL)** devices, so the scanner never appears in Image Capture, Preview or Printers & Scanners, and the vendor's phone app becomes the only way to scan.

This project is a small background service that sits on your Mac, presents itself to macOS as an AirScan scanner, and translates every request into WSD calls to the real device.

```
Image Capture / Preview  --eSCL-->  airscan-wsd-bridge (on your Mac)  --WSD-->  printer
```

- No vendor driver and no admin password.
- Python standard library only; nothing to `pip install`.
- Nothing about your printer is hardcoded: it is found on the network and asked what it can do.

## Requirements

- macOS with `python3` (3.9 or newer). It comes with the Xcode Command Line Tools; if `python3 --version` asks to install them, accept.
- A printer/scanner on the same network that offers a WSD scan service with a flatbed and JPEG output.

## Setup

```bash
git clone https://github.com/amrehabmohamed/airscan-wsd-bridge.git
cd airscan-wsd-bridge
python3 airscan_wsd.py setup
```

The setup walks you through four steps:

1. **Find your scanner.** It searches the network for about 5 seconds and prints each scanner's resolutions, colour modes and flatbed size. If there are several, you pick one.
2. **Test scan.** Put a page on the glass. It scans at the lowest resolution and opens the result so you can confirm the device really works before anything is installed. You can skip this.
3. **Install the background service.** A per-user LaunchAgent that starts at login. It runs from a copy in `~/Library/Application Support/airscan-wsd-bridge`, so you can move or delete the cloned folder afterwards.
4. **Scan from macOS.** Open Image Capture, Preview (File > Import from Scanner) or System Settings > Printers & Scanners. The scanner appears under *Shared*.

If macOS asks whether `python3` or your terminal may find devices on the local network, allow it. Discovery cannot work without it.

To just look, without installing anything:

```bash
python3 airscan_wsd.py discover
```

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
| `python3 airscan_wsd.py setup` | Guided setup (add `--yes` to skip the questions, `--no-test` to skip the test scan) |
| `python3 airscan_wsd.py discover` | List WSD scanners and their features |
| `python3 airscan_wsd.py scan` | Scan one page to `~/Documents/Scans` (`--dpi`, `--gray`, `--pdf`, `--out`, `--no-open`) |
| `python3 airscan_wsd.py status` | Is the service running, is the scanner reachable |
| `python3 airscan_wsd.py uninstall` | Remove the service and its settings |

Log file: `~/Library/Logs/airscan-wsd-bridge.log`

## Tested devices

| Device | macOS | Result | Reported by |
| --- | --- | --- | --- |
| Canon PIXMA G3010 series (sold as G3410 in some regions), firmware 1.130 | 27.0 | Works: colour and greyscale, 150/300/600 dpi, JPEG and PDF | @amrehabmohamed |

Everything else is untested. If you try another device, please open a device report issue, whether it worked or not. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Printing from the same printer

Printers like this usually print without a driver too; macOS just does not offer them automatically because they lack AirPrint. If yours supports IPP (most Mopria-certified printers do):

```bash
ippfind
```

That prints an address such as `ipp://XXXXXXXXXXXX.local:631/ipp/print`. Add the printer with it:

```bash
lpadmin -p My_Printer -E -v "ipp://XXXXXXXXXXXX.local:631/ipp/print" -m everywhere
```

It then appears in every app's print dialog.

## Troubleshooting

- **No scanner found.** Printer on and finished starting up; same Wi-Fi as the Mac (not a guest network); no VPN; Local Network permission allowed for your terminal (System Settings > Privacy & Security > Local Network).
- **"this scanner offers no JPEG output" or "reports no flatbed".** The device is discovered but not supported yet. Please report it.
- **The scanner is stuck with its light on.** Press Stop on the printer, or switch it off and on. Then run `status` and check it says `Idle` before scanning again.
- **The scanner shows in Image Capture but scans fail.** Look at the log file; it records each scan request and why it was refused.
- **Port 8290 is taken.** Setup picks the next free port automatically.

## Limitations

- Flatbed only. Document feeders (ADF) and duplex are not supported.
- One scanner per Mac.
- No live preview while scanning.
- The device's own JPEG compression setting is used as-is.
- WSD is plain, unauthenticated HTTP on your local network. That is how the printer works with or without this bridge; the bridge adds no new exposure beyond a local port that refuses other machines.

## How it works

- `wsd.py` finds devices with a WS-Discovery multicast probe, reads their metadata, and drives the WS-Scan service (`GetScannerElements`, `CreateScanJob`, `RetrieveImage`, `CancelJob`). Scan tickets are built from the device's own default ticket, because hand-written tickets get rejected.
- `bridge.py` serves the eSCL endpoints (`ScannerCapabilities`, `ScannerStatus`, `ScanJobs`, `NextDocument`), advertises `_uscan._tcp` through the built-in `dns-sd` tool, and crops/converts images with the built-in `sips` tool.
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
