#!/usr/bin/env python3
"""Make a network printer/scanner that macOS ignores print and scan natively.

Commands:
  setup      find your printer and scanner, add them to macOS, and test them
  discover   list the printers and scanners on your network and what they can do
  scan       scan one page from the command line
  status     show whether the printer, the scanner and the service are responding
  uninstall  remove the background service, its settings and the printer it added
  serve      run the bridge in the foreground (what the service runs)
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import bridge
import ipp
import wsd

NAME = "airscan-wsd-bridge"
LABEL = f"local.{NAME}"
# The service runs from a copy here: launchd services cannot read Desktop, Documents or Downloads.
APP_DIR = Path.home() / "Library" / "Application Support" / NAME
CONFIG = APP_DIR / "config.json"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / f"{NAME}.log"
SCAN_DIR = Path.home() / "Documents" / "Scans"
DEFAULT_PORT = 8290
# macOS names a background item after the file launchd runs, so run a launcher with a readable name
# instead of "python3".
LAUNCHER = "AirScan WSD Bridge"
SOURCES = ("airscan_wsd.py", "bridge.py", "ipp.py", "wsd.py")


def read_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def load_config():
    config = read_config()
    if "endpoint" not in config:
        sys.exit(f"No scanner is set up yet. Run: python3 {Path(__file__).name} setup")
    return config


def save_config(config):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2))


def scanner_from(config):
    return wsd.Scanner(config["endpoint"], config["url"], config["model"], config["manufacturer"])


def moved(config):
    def update(url):
        config["url"] = url
        save_config(config)
    return update


def describe(caps):
    inches = f'{caps["max_width"] / 1000:g} x {caps["max_height"] / 1000:g} in'
    mm = f'{caps["max_width"] * 0.0254:.0f} x {caps["max_height"] * 0.0254:.0f} mm'
    modes = {"RGB24": "colour", "Grayscale8": "greyscale", "BlackAndWhite1": "black and white"}
    lines = [
        f'  Resolutions : {", ".join(str(r) for r in caps["resolutions"])} dpi',
        f'  Colour modes: {", ".join(modes.get(m, m) for m in caps["color_modes"])}',
        f"  Flatbed size: {inches} ({mm})",
    ]
    if caps["has_feeder"]:
        lines.append("  Note        : this device has a document feeder; only the flatbed is supported")
    return "\n".join(lines)


def find_scanners():
    """Discover scanners and read their capabilities; return [(scanner, caps or error text)]."""
    print("Searching the network for WSD scanners (about 5 seconds)...")
    try:
        scanners = wsd.discover()
    except wsd.WSDError as e:
        sys.exit(f"Could not search the network: {e}")
    found = []
    for scanner in scanners:
        try:
            found.append((scanner, scanner.capabilities()))
        except (wsd.WSDError, ValueError, TypeError) as e:
            found.append((scanner, str(e)))
    return found


def find_printers():
    """Discover IPP printers and read their attributes; return [(uri, name, attrs or error text)]."""
    print("Searching the network for printers (about 5 seconds)...")
    found = []
    try:
        for uri, name in ipp.discover():
            try:
                found.append((uri, name, ipp.attributes(uri)))
            except ipp.PrintError as e:
                found.append((uri, name, str(e)))
    except ipp.PrintError as e:
        print(f"Could not search for printers: {e}")
    return found


NOT_FOUND = """No printer or scanner answered. Things to check:
  - the printer is switched on and has finished starting up
  - the printer and this Mac are on the same Wi-Fi network (not a guest network)
  - no VPN is active on this Mac
  - if macOS asked whether Terminal (or python3) may find devices on your
    local network, it must be allowed: System Settings > Privacy & Security
    > Local Network
Then run this command again."""


def cmd_discover(args):
    printers, scanners = find_printers(), find_scanners()
    if not printers and not scanners:
        sys.exit(NOT_FOUND)
    for uri, name, attrs in printers:
        print(f"\nPrinter: {name}\n  Address     : {uri}")
        if not isinstance(attrs, dict):
            print(f"  Not usable  : {attrs}")
            continue
        print(ipp.describe(attrs))
        queue = ipp.existing_queue(uri, name)
        print(f"  In macOS    : {'yes, as ' + queue if queue else 'not added yet'}")
        if not ipp.driverless(attrs):
            print("  Note        : needs the manufacturer's driver; cannot be added automatically")
    for scanner, caps in scanners:
        print(f"\nScanner: {scanner.name}\n  Address     : {scanner.url}")
        print(describe(caps) if isinstance(caps, dict) else f"  Not usable  : {caps}")


def choose(label, names, auto):
    """Pick one of several devices; returns an index, or None to skip."""
    if len(names) == 1:
        return 0
    if auto:
        # Several candidates and nobody to ask: don't guess.
        print(f"Found {len(names)} {label}s; run setup without --yes to choose one.")
        return None
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    answer = ask(f"Which {label}? [1-{len(names)}, or s to skip] ", "1", auto)
    if answer.isdigit() and 1 <= int(answer) <= len(names):
        return int(answer) - 1
    return None


def ask(prompt, default, auto):
    if auto:
        return default
    answer = input(prompt).strip().lower()
    return answer or default


def free_port(start):
    for port in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    sys.exit(f"No free port between {start} and {start + 19}.")


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def service_target():
    return f"gui/{os.getuid()}/{LABEL}"


def install_service():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    if here != APP_DIR:
        for name in SOURCES:
            shutil.copy(here / name, APP_DIR / name)
    launcher = APP_DIR / LAUNCHER
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" -u "{APP_DIR / "airscan_wsd.py"}" serve\n')
    launcher.chmod(0o755)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array><string>{launcher}</string></array>
<key>WorkingDirectory</key><string>{APP_DIR}</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{LOG}</string>
<key>StandardErrorPath</key><string>{LOG}</string>
</dict></plist>
""")
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
    if result.returncode:
        sys.exit(f"Could not start the service: {result.stderr.strip()}")


def service_answers(port, wait=10):
    deadline = time.time() + wait
    while True:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/eSCL/ScannerCapabilities", timeout=3) as f:
                return f.status == 200
        except OSError:
            if time.time() > deadline:
                return False
            time.sleep(0.5)


def setup_printer(printers, config, args):
    usable = [(uri, name, attrs) for uri, name, attrs in printers if isinstance(attrs, dict)]
    if not usable:
        print("No printer answered. Skipping.")
        return
    picked = choose("printer", [name for _, name, _ in usable], args.yes)
    if picked is None:
        print("Skipped.")
        return
    uri, name, attrs = usable[picked]
    print(f"Found: {name}\n{ipp.describe(attrs)}\n")
    queue = ipp.existing_queue(uri, name)
    if queue:
        print(f'macOS already has this printer as "{queue}". Nothing to add.')
    elif not ipp.driverless(attrs):
        print("This printer needs the manufacturer's driver, so it cannot be added automatically.")
        return
    else:
        queue = ipp.queue_name(attrs.get("printer-make-and-model", [name])[0])
        try:
            ipp.add(uri, queue)
        except ipp.PrintError as e:
            print(f"Could not add the printer: {e}\n(Adding printers needs an administrator account.)")
            return
        config["print_queue"] = queue
        save_config(config)
        print(f'Added to macOS as "{queue}". It now appears in every app\'s print dialog (Cmd+P).')
    if not args.no_test and ask("Print a test page? It uses one sheet of paper. [y/N] ", "n", args.yes) == "y":
        APP_DIR.mkdir(parents=True, exist_ok=True)
        page = APP_DIR / "test-page.txt"
        page.write_text(f"{NAME} test page\n\nPrinter: {name}\nPrinted from macOS without a vendor driver.\n"
                        f"{datetime.now():%Y-%m-%d %H:%M}\n\nThe quick brown fox jumps over the lazy dog.\n0123456789\n")
        try:
            print(ipp.print_file(queue, page))
            print("Sent. Wait for the page to come out before the scan test below.")
            if not args.yes:
                input("Press Enter when the page has finished printing: ")
        except ipp.PrintError as e:
            print(f"Could not print: {e}")


def cmd_setup(args):
    auto = args.yes
    config = read_config()
    print("Step 1 of 5: find your devices\n")
    printers, found = find_printers(), find_scanners()
    if not printers and not found:
        sys.exit(NOT_FOUND)
    print(f"Found {len(printers)} printer(s) and {len(found)} scanner(s).")

    print("\nStep 2 of 5: add the printer to macOS\n")
    setup_printer(printers, config, args)

    print("\nStep 3 of 5: test the scanner\n")
    usable = [(s, c) for s, c in found if isinstance(c, dict) and ({"RGB24", "Grayscale8"} & set(c["color_modes"]))]
    for scanner, problem in found:
        if not isinstance(problem, dict):
            print(f"Skipping {scanner.name}: {problem}")
    picked = choose("scanner", [f"{s.name}  ({s.url})" for s, _ in usable], auto) if usable else None
    if picked is None:
        print("No scanner to set up. Printing setup above is complete; steps 4 and 5 do not apply.")
        return
    scanner, caps = usable[picked]
    print(f"Found: {scanner.name}\n{describe(caps)}\n")
    print("Put a page face down on the glass and close the lid. Make sure the printer\n"
          "is not printing: a scan sent to a busy device can leave it stuck until you\n"
          "switch it off and on.")
    if not args.no_test and ask("Press Enter to scan, or type s to skip: ", "", auto) != "s":
        dpi = min(caps["resolutions"])
        mode = "RGB24" if "RGB24" in caps["color_modes"] else "Grayscale8"
        print(f"Scanning at {dpi} dpi. The scan head should start moving within a few seconds...")
        try:
            jpeg = scanner.scan(caps, dpi, mode)
        except wsd.WSDError as e:
            sys.exit(f"The test scan failed: {e}\nThe scan service was not installed.")
        APP_DIR.mkdir(parents=True, exist_ok=True)
        test = APP_DIR / "test-scan.jpg"
        test.write_bytes(jpeg)
        print(f"Saved {test} ({len(jpeg) // 1024} KB).")
        if not auto:
            subprocess.run(["open", str(test)])
            if ask("Does the scan look right? [Y/n] ", "y", auto) != "y":
                sys.exit("The scan service was not installed. Please open an issue with your printer model and what you saw.")
    else:
        print("Skipped.")

    print("\nStep 4 of 5: install the scan service\n")
    launchctl("bootout", service_target())
    time.sleep(1)
    port = free_port(DEFAULT_PORT)
    config.update({"endpoint": scanner.endpoint, "url": scanner.url, "model": scanner.model,
                   "manufacturer": scanner.manufacturer, "port": port, "caps": caps})
    save_config(config)
    install_service()
    if not service_answers(port):
        sys.exit(f"The service was installed but is not answering. Check the log: {LOG}")
    print(f"The bridge is running on this Mac (port {port}) and starts automatically at login.")

    print(f"""
Step 5 of 5: use it from macOS

Print: press Cmd+P in any app and choose the printer.

Scan: open Image Capture (or Preview > File > Import from Scanner, or System
Settings > Printers & Scanners). "{scanner.name}" appears under Shared.

What to expect when scanning:
  - Every scan sweeps the whole glass, including the overview. Cropping to
    your selection happens on the Mac afterwards.
  - macOS shows "Scanner is warming up" for the whole scan. That is normal:
    the page arrives in one piece at the end.
  - Higher resolutions are slow and large. Use the middle setting for
    documents and the highest only for photos.
  - Do not start a scan while the printer is printing.
  - Only this Mac can use the bridge. Other devices may see the scanner
    name on the network but will be refused.

Log file: {LOG}
To remove everything: python3 {Path(__file__).name} uninstall""")


def cmd_scan(args):
    config = load_config()
    scanner, caps = scanner_from(config), config["caps"]
    dpi = args.dpi or min(caps["resolutions"], key=lambda r: abs(r - 300))
    if dpi not in caps["resolutions"]:
        sys.exit(f"This scanner supports {', '.join(str(r) for r in caps['resolutions'])} dpi.")
    native = "RGB24" if "RGB24" in caps["color_modes"] else "Grayscale8"
    if args.gray and "Grayscale8" in caps["color_modes"]:
        native = "Grayscale8"
    out = args.out or SCAN_DIR / f"scan-{datetime.now():%Y%m%d-%H%M%S}"
    out = out.expanduser().with_suffix(".pdf" if args.pdf else ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"Scanning at {dpi} dpi...")
    try:
        try:
            jpeg = scanner.scan(caps, dpi, native)
        except wsd.Unreachable:
            print("The scanner is not at its last address; searching for it...")
            found = wsd.locate(scanner.endpoint)
            if not found:
                raise
            moved(config)(found.url)
            jpeg = found.scan(caps, dpi, native)
    except wsd.WSDError as e:
        sys.exit(f"Scan failed: {e}")
    full_w, full_h = caps["max_width"] * 300 // 1000, caps["max_height"] * 300 // 1000
    out.write_bytes(bridge.convert(jpeg, dpi, 0, 0, full_w, full_h, full_w, full_h,
                                   "application/pdf" if args.pdf else "image/jpeg",
                                   gray=args.gray and native != "Grayscale8"))
    print(out)
    if not args.no_open:
        subprocess.run(["open", str(out)])


def cmd_status(args):
    config = read_config()
    queues = ipp.parse_queues(subprocess.run(["lpstat", "-v"], capture_output=True, text=True).stdout)
    if queues:
        for queue, uri in queues.items():
            print(f"Printer : {queue} at {uri}")
    else:
        print("Printer : none added to macOS")
    if "endpoint" not in config:
        print(f"Scanner : not set up. Run: python3 {Path(__file__).name} setup")
        return
    scanner = scanner_from(config)
    print(f"Scanner : {scanner.name} at {scanner.url}")
    try:
        print(f"  state : {scanner.state()}")
    except wsd.WSDError as e:
        print(f"  state : not reachable ({e})")
    running = launchctl("print", service_target()).returncode == 0
    print(f"Service : {'installed' if running else 'not installed'}")
    if running:
        print(f"  bridge: {'answering' if service_answers(config['port'], wait=0) else 'NOT answering'} on port {config['port']}")
    print(f"Log     : {LOG}")


def cmd_uninstall(args):
    # Only remove a printer this tool added; one that was already in macOS is left alone.
    queue = read_config().get("print_queue")
    if queue:
        ipp.remove(queue)
        print(f'Removed the printer "{queue}" from macOS.')
    launchctl("bootout", service_target())
    PLIST.unlink(missing_ok=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    print(f"Removed the scan service and its settings. The log is still at {LOG}")


def cmd_serve(args):
    config = load_config()
    bridge.serve(scanner_from(config), config["caps"], config["port"], moved(config))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    p = sub.add_parser("setup")
    p.add_argument("--yes", action="store_true", help="don't ask questions (no test page is printed)")
    p.add_argument("--no-test", action="store_true", help="skip the test page and the test scan")
    p.set_defaults(run=cmd_setup)
    sub.add_parser("discover").set_defaults(run=cmd_discover)
    p = sub.add_parser("scan")
    p.add_argument("--dpi", type=int, help="resolution (default: the one closest to 300)")
    p.add_argument("--gray", action="store_true", help="greyscale instead of colour")
    p.add_argument("--pdf", action="store_true", help="save as PDF instead of JPEG")
    p.add_argument("--out", type=Path, help=f"output file (default: {SCAN_DIR}/scan-<timestamp>)")
    p.add_argument("--no-open", action="store_true", help="don't open the file afterwards")
    p.set_defaults(run=cmd_scan)
    sub.add_parser("status").set_defaults(run=cmd_status)
    sub.add_parser("uninstall").set_defaults(run=cmd_uninstall)
    sub.add_parser("serve").set_defaults(run=cmd_serve)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
