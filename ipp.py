"""Find driverless (IPP) printers and add them to macOS, using the tools that ship with it."""
import re
import subprocess
import urllib.parse

# Formats macOS can print to without a vendor driver.
DRIVERLESS = ("image/pwg-raster", "image/urf", "application/pdf")
PAPER = {"iso_a4_210x297mm": "A4", "iso_a5_148x210mm": "A5", "na_letter_8.5x11in": "Letter",
         "na_legal_8.5x14in": "Legal", "na_index-4x6_4x6in": "4x6 in", "na_5x7_5x7in": "5x7 in"}


class PrintError(Exception):
    pass


def _run(*args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PrintError(f"{args[0]} failed: {e}")


def discover(timeout=5):
    """Return [(uri, name)] for every IPP printer advertised on the local network."""
    result = _run("ippfind", "-T", str(timeout), "_ipp._tcp", "-x", "echo", "{service_uri}|{service_name}", ";",
                  timeout=timeout + 10)
    printers = []
    for line in result.stdout.splitlines():
        uri, _, name = line.partition("|")
        if uri.startswith("ipp") and (uri, name) not in printers:
            printers.append((uri, name or uri))
    return printers


def parse_attributes(output):
    """Parse `ipptool -tv` output into {attribute: [values]}."""
    attrs = {}
    for line in output.splitlines():
        found = re.match(r"\s+([\w-]+) \([^)]*\) = (.*)$", line)
        if found:
            attrs[found.group(1)] = found.group(2).split(",")
    return attrs


def attributes(uri):
    result = _run("ipptool", "-tv", "-T", "10", uri, "get-printer-attributes.test")
    attrs = parse_attributes(result.stdout)
    if not attrs:
        raise PrintError("the printer did not answer")
    return attrs


def driverless(attrs):
    return any(f in attrs.get("document-format-supported", []) for f in DRIVERLESS)


def describe(attrs):
    colour = "true" in attrs.get("color-supported", []) or "color" in attrs.get("print-color-mode-supported", [])
    sides = attrs.get("sides-supported", [])
    media = attrs.get("media-supported", [])
    known = [PAPER[m] for m in media if m in PAPER]
    others = len([m for m in media if m not in PAPER and not m.startswith("custom_")])
    paper = ", ".join(known) + (f" and {others} more" if others else "")
    return "\n".join([
        f'  Colour      : {"yes" if colour else "no (black only)"}',
        f'  Two-sided   : {"yes" if any(s.startswith("two-sided") for s in sides) else "no"}',
        f'  Resolution  : {", ".join(attrs.get("printer-resolution-supported", ["unknown"]))}',
        f'  Paper       : {paper or "unknown"}',
        f'  State       : {", ".join(attrs.get("printer-state", ["unknown"]))}',
    ])


def parse_queues(output):
    """Parse `lpstat -v` output into {queue name: device uri}."""
    return dict(re.findall(r"^device for ([^:]+): (\S+)", output, re.M))


def existing_queue(uri, name):
    """Return the macOS print queue that already points at this printer, if any."""
    for queue, device in parse_queues(_run("lpstat", "-v").stdout).items():
        if device.rstrip("/").lower() == uri.rstrip("/").lower():
            return queue
        # Printers macOS added by itself are stored by their Bonjour name instead of an address.
        if device.startswith("dnssd://") and urllib.parse.unquote(device[8:]).split("._")[0] == name:
            return queue
    return None


def queue_name(model):
    return re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_") or "Printer"


def add(uri, queue):
    result = _run("lpadmin", "-p", queue, "-E", "-v", uri, "-m", "everywhere", timeout=60)
    if result.returncode:
        raise PrintError(result.stderr.strip() or "lpadmin failed")


def remove(queue):
    _run("lpadmin", "-x", queue)


def print_file(queue, path):
    result = _run("lp", "-d", queue, str(path))
    if result.returncode:
        raise PrintError(result.stderr.strip() or "lp failed")
    return result.stdout.strip()
