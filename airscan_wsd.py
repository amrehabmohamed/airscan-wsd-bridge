#!/usr/bin/env python3
"""Make a WSD-only network scanner show up as a native scanner on macOS.

Commands:
  setup      find your scanner, test it, and install the background service
  discover   list the WSD scanners on your network and what they can do
  scan       scan one page from the command line
  status     show whether the service and the scanner are responding
  uninstall  remove the background service and its settings
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
SOURCES = ("airscan_wsd.py", "bridge.py", "wsd.py")


def load_config():
    if not CONFIG.exists():
        sys.exit(f"No scanner is set up yet. Run: python3 {Path(__file__).name} setup")
    return json.loads(CONFIG.read_text())


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


NOT_FOUND = """No WSD scanner answered. Things to check:
  - the printer is switched on and has finished starting up
  - the printer and this Mac are on the same Wi-Fi network (not a guest network)
  - no VPN is active on this Mac
  - if macOS asked whether Terminal (or python3) may find devices on your
    local network, it must be allowed: System Settings > Privacy & Security
    > Local Network
Then run this command again."""


def cmd_discover(args):
    found = find_scanners()
    if not found:
        sys.exit(NOT_FOUND)
    for scanner, caps in found:
        print(f"\n{scanner.name}\n  Address     : {scanner.url}")
        print(describe(caps) if isinstance(caps, dict) else f"  Not usable  : {caps}")


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
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array><string>{sys.executable}</string><string>-u</string><string>{APP_DIR / "airscan_wsd.py"}</string><string>serve</string></array>
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


def cmd_setup(args):
    auto = args.yes
    print("Step 1 of 4: find your scanner\n")
    found = find_scanners()
    if not found:
        sys.exit(NOT_FOUND)
    usable = [(s, c) for s, c in found if isinstance(c, dict) and ({"RGB24", "Grayscale8"} & set(c["color_modes"]))]
    for scanner, problem in found:
        if not isinstance(problem, dict):
            print(f"Skipping {scanner.name}: {problem}")
    if not usable:
        sys.exit("A scanner was found, but it cannot be used with this bridge (see above).")
    if len(usable) == 1 or auto:
        scanner, caps = usable[0]
    else:
        for i, (s, _) in enumerate(usable, 1):
            print(f"  {i}. {s.name}  ({s.url})")
        choice = ask(f"Which one? [1-{len(usable)}] ", "1", auto)
        if not choice.isdigit() or not 1 <= int(choice) <= len(usable):
            sys.exit("Not a valid choice.")
        scanner, caps = usable[int(choice) - 1]
    print(f"Found: {scanner.name}\n{describe(caps)}\n")

    print("Step 2 of 4: test scan\n")
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
            sys.exit(f"The test scan failed: {e}\nNothing was installed.")
        APP_DIR.mkdir(parents=True, exist_ok=True)
        test = APP_DIR / "test-scan.jpg"
        test.write_bytes(jpeg)
        print(f"Saved {test} ({len(jpeg) // 1024} KB).")
        if not auto:
            subprocess.run(["open", str(test)])
            if ask("Does the scan look right? [Y/n] ", "y", auto) != "y":
                sys.exit("Nothing was installed. Please open an issue with your printer model and what you saw.")
    else:
        print("Skipped.")

    print("\nStep 3 of 4: install the background service\n")
    launchctl("bootout", service_target())
    time.sleep(1)
    port = free_port(DEFAULT_PORT)
    save_config({"endpoint": scanner.endpoint, "url": scanner.url, "model": scanner.model,
                 "manufacturer": scanner.manufacturer, "port": port, "caps": caps})
    install_service()
    if not service_answers(port):
        sys.exit(f"The service was installed but is not answering. Check the log: {LOG}")
    print(f"The bridge is running on this Mac (port {port}) and starts automatically at login.")

    print(f"""
Step 4 of 4: scan from macOS

Open Image Capture (or Preview > File > Import from Scanner, or System
Settings > Printers & Scanners). "{scanner.name}" appears under Shared.

What to expect:
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
    config = load_config()
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
    launchctl("bootout", service_target())
    PLIST.unlink(missing_ok=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    print(f"Removed the service and its settings. The log is still at {LOG}")


def cmd_serve(args):
    config = load_config()
    bridge.serve(scanner_from(config), config["caps"], config["port"], moved(config))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    p = sub.add_parser("setup")
    p.add_argument("--yes", action="store_true", help="don't ask questions; use the first scanner found")
    p.add_argument("--no-test", action="store_true", help="skip the test scan")
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
