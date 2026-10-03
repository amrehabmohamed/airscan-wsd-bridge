"""WS-Discovery and WS-Scan client for network scanners (standard library only)."""
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
DISCO = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
DEVPROF = "http://schemas.xmlsoap.org/ws/2006/02/devprof"
WSCN = "http://schemas.microsoft.com/windows/2006/08/wdp/scan"
ANONYMOUS = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
MULTICAST = ("239.255.255.250", 3702)

# Devices echo these prefixes back and some parse them literally, so keep the conventional ones.
ET.register_namespace("wscn", WSCN)

JPEG_FORMATS = ("exif", "jfif")


class WSDError(Exception):
    """The scanner answered with an error."""


class Unreachable(WSDError):
    """The scanner did not answer at its last known address."""


def _envelope(action, to, body):
    return (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:wsa="{WSA}" xmlns:wsd="{DISCO}" xmlns:wscn="{WSCN}">'
            f"<soap:Header><wsa:To>{to}</wsa:To><wsa:Action>{action}</wsa:Action>"
            f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"<wsa:ReplyTo><wsa:Address>{ANONYMOUS}</wsa:Address></wsa:ReplyTo>"
            f"</soap:Header><soap:Body>{body}</soap:Body></soap:Envelope>").encode()


def _fault(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return data[-300:].decode("utf8", "replace")
    reason = root.findtext(f".//{{{SOAP}}}Text") or "unknown error"
    detail = (root.findtext(f".//{{{SOAP}}}Detail") or "").strip()
    return f"{reason} ({detail})" if detail else reason


def _post(url, action, body, to=None, timeout=30):
    """Send one SOAP request and return the raw response body."""
    req = urllib.request.Request(url, data=_envelope(action, to or url, body),
                                 headers={"Content-Type": "application/soap+xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.read()
    except urllib.error.HTTPError as e:
        raise WSDError(_fault(e.read()))
    except OSError as e:
        raise Unreachable(f"no answer from {url}: {e}")


def _probe(timeout):
    """Multicast a WS-Discovery probe; return {endpoint address: metadata URL or None}."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    probe = _envelope(f"{DISCO}/Probe", "urn:schemas-xmlsoap-org:ws:2005:04:discovery", "<wsd:Probe/>")
    found = {}
    try:
        deadline = time.time() + timeout
        sent = 0
        while time.time() < deadline:
            # UDP is lossy; repeat the probe a couple of times.
            if sent < 2:
                sock.sendto(probe, MULTICAST)
                sent += 1
            sock.settimeout(min(1.0, max(0.1, deadline - time.time())))
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            for match in root.iter(f"{{{DISCO}}}ProbeMatch"):
                endpoint = match.findtext(f"{{{WSA}}}EndpointReference/{{{WSA}}}Address")
                if not endpoint:
                    continue
                xaddrs = (match.findtext(f"{{{DISCO}}}XAddrs") or "").split()
                # Prefer an IPv4 address: link-local IPv6 URLs need a scope id urllib can't supply.
                ipv4 = [x for x in xaddrs if re.match(r"https?://\d+\.\d+\.\d+\.\d+", x)]
                url = (ipv4 or xaddrs or [None])[0]
                if url or endpoint not in found:
                    found[endpoint.strip()] = url
    except OSError as e:
        raise Unreachable(f"could not search the network: {e}")
    finally:
        sock.close()
    return found


def _metadata(endpoint, url):
    """Fetch device metadata; return a Scanner, or None if the device has no scan service."""
    root = ET.fromstring(_post(url, "http://schemas.xmlsoap.org/ws/2004/09/transfer/Get", "", to=endpoint, timeout=5))
    scan_url = None
    for hosted in root.iter(f"{{{DEVPROF}}}Hosted"):
        if "ScannerServiceType" in (hosted.findtext(f"{{{DEVPROF}}}Types") or ""):
            addresses = (hosted.findtext(f"{{{WSA}}}EndpointReference/{{{WSA}}}Address") or "").split()
            ipv4 = [a for a in addresses if re.match(r"https?://\d+\.\d+\.\d+\.\d+", a)]
            scan_url = (ipv4 or addresses or [None])[0]
    if not scan_url:
        return None

    def text(tag):
        return (root.findtext(f".//{{{DEVPROF}}}{tag}") or "").strip()

    return Scanner(endpoint=endpoint, url=scan_url, model=text("ModelName") or text("FriendlyName") or "Scanner",
                   manufacturer=text("Manufacturer"))


def discover(timeout=5):
    """Return every scanner on the local network that offers a WSD scan service."""
    scanners = []
    for endpoint, url in _probe(timeout).items():
        if not url:
            continue
        try:
            scanner = _metadata(endpoint, url)
        except (WSDError, ET.ParseError):
            continue
        if scanner:
            scanners.append(scanner)
    return sorted(scanners, key=lambda s: s.name)


def locate(endpoint, timeout=5):
    """Find one known scanner again (its address changes when the router hands out a new one)."""
    for scanner in discover(timeout):
        if scanner.endpoint == endpoint:
            return scanner
    return None


def extract_jpeg(data):
    """Pull the JPEG out of a multipart RetrieveImage response."""
    start, end = data.find(b"\xff\xd8\xff"), data.rfind(b"\xff\xd9")
    if start < 0 or end < start:
        return None
    return data[start:end + 2]


def _set(parent, path, value):
    """Set the text of a descendant, creating any missing elements along the way."""
    node = parent
    for name in path.split("/"):
        child = node.find(f"{{{WSCN}}}{name}")
        if child is None:
            child = ET.SubElement(node, f"{{{WSCN}}}{name}")
        node = child
    node.text = str(value)


def parse_capabilities(root):
    """Read what the flatbed can do from a GetScannerElements response."""
    platen = root.find(f".//{{{WSCN}}}Platen")
    if platen is None:
        raise WSDError("this scanner reports no flatbed (document feeders are not supported)")

    def values(node, *names):
        path = ".//" + "/".join(f"{{{WSCN}}}{n}" for n in names)
        return [e.text.strip() for e in node.findall(path) if e.text]

    widths = {int(v) for v in values(platen, "PlatenResolutions", "Widths", "Width")}
    heights = {int(v) for v in values(platen, "PlatenResolutions", "Heights", "Height")}
    return {
        "resolutions": sorted(widths & heights if heights else widths),
        "color_modes": values(platen, "PlatenColor", "ColorEntry"),
        "formats": values(root, "FormatsSupported", "FormatValue"),
        "content_types": values(root, "ContentTypesSupported", "ContentTypeValue"),
        # Thousandths of an inch.
        "max_width": int(platen.findtext(f"{{{WSCN}}}PlatenMaximumSize/{{{WSCN}}}Width")),
        "max_height": int(platen.findtext(f"{{{WSCN}}}PlatenMaximumSize/{{{WSCN}}}Height")),
        "has_feeder": root.find(f".//{{{WSCN}}}ADF") is not None,
    }


def build_ticket(default_ticket, caps, dpi, color):
    """Turn the device's own default ticket into a full-flatbed JPEG scan request.

    Tickets written from scratch get rejected, and on some devices a rejected
    ticket leaves the scanner stuck, so only the fields we need are changed.
    """
    ticket = ET.fromstring(ET.tostring(default_ticket))
    ticket.tag = f"{{{WSCN}}}ScanTicket"
    w, h = caps["max_width"], caps["max_height"]
    _set(ticket, "DocumentParameters/Format", next(f for f in JPEG_FORMATS if f in caps["formats"]))
    _set(ticket, "DocumentParameters/ImagesToTransfer", 1)
    _set(ticket, "DocumentParameters/InputSource", "Platen")
    if "Photo" in caps["content_types"]:
        _set(ticket, "DocumentParameters/ContentType", "Photo")
    _set(ticket, "DocumentParameters/InputSize/InputMediaSize/Width", w)
    _set(ticket, "DocumentParameters/InputSize/InputMediaSize/Height", h)
    front = "DocumentParameters/MediaSides/MediaFront"
    for name, value in (("ScanRegionXOffset", 0), ("ScanRegionYOffset", 0), ("ScanRegionWidth", w), ("ScanRegionHeight", h)):
        _set(ticket, f"{front}/ScanRegion/{name}", value)
    _set(ticket, f"{front}/ColorProcessing", color)
    _set(ticket, f"{front}/Resolution/Width", dpi)
    _set(ticket, f"{front}/Resolution/Height", dpi)
    return ticket


class Scanner:
    def __init__(self, endpoint, url, model, manufacturer=""):
        self.endpoint = endpoint  # stable identity, survives address changes
        self.url = url            # scan service address, may change
        self.model = model
        self.manufacturer = manufacturer

    @property
    def name(self):
        maker = self.manufacturer.replace("INC.", "").replace("Inc.", "").strip().title()
        if not maker or self.model.lower().startswith(maker.lower()):
            return self.model
        return f"{maker} {self.model}"

    def _call(self, action, body, timeout=30):
        return _post(self.url, f"{WSCN}/{action}", body, timeout=timeout)

    def _elements(self, *names):
        wanted = "".join(f"<wscn:Name>wscn:{n}</wscn:Name>" for n in names)
        return ET.fromstring(self._call("GetScannerElements",
                                        f"<wscn:GetScannerElementsRequest><wscn:RequestedElements>{wanted}"
                                        f"</wscn:RequestedElements></wscn:GetScannerElementsRequest>", timeout=10))

    def capabilities(self):
        caps = parse_capabilities(self._elements("ScannerConfiguration"))
        if not any(f in caps["formats"] for f in JPEG_FORMATS):
            raise WSDError(f"this scanner offers no JPEG output (only {', '.join(caps['formats']) or 'nothing'})")
        return caps

    def state(self):
        return self._elements("ScannerStatus").findtext(f".//{{{WSCN}}}ScannerState") or "Unknown"

    def create_job(self, caps, dpi, color="RGB24"):
        """Start a full-flatbed scan; return (job id, job token)."""
        root = self._elements("ScannerStatus", "DefaultScanTicket")
        state = root.findtext(f".//{{{WSCN}}}ScannerState") or "Unknown"
        # A job sent while the device is printing or scanning can hang it until it is power-cycled.
        if state != "Idle":
            raise WSDError(f"the scanner is busy ({state}); wait for it to finish and try again")
        default = root.find(f".//{{{WSCN}}}DefaultScanTicket")
        if default is None:
            raise WSDError("the scanner did not report its default scan settings")
        ticket = ET.tostring(build_ticket(default, caps, dpi, color), encoding="unicode")
        root = ET.fromstring(self._call("CreateScanJob", f"<wscn:CreateScanJobRequest>{ticket}</wscn:CreateScanJobRequest>", timeout=60))
        job, token = root.findtext(f".//{{{WSCN}}}JobId"), root.findtext(f".//{{{WSCN}}}JobToken")
        if not job or not token:
            raise WSDError("the scanner accepted the job but returned no job id")
        return job, token

    def cancel(self, job):
        try:
            self._call("CancelJob", f"<wscn:CancelJobRequest><wscn:JobId>{job}</wscn:JobId></wscn:CancelJobRequest>", timeout=15)
        except WSDError:
            pass

    def retrieve(self, job, token):
        """Wait for the scan to finish and return it as JPEG bytes; cancels the job on failure."""
        try:
            data = self._call("RetrieveImage",
                              f"<wscn:RetrieveImageRequest><wscn:JobId>{job}</wscn:JobId><wscn:JobToken>{token}</wscn:JobToken>"
                              f"<wscn:DocumentDescription><wscn:DocumentName>scan</wscn:DocumentName></wscn:DocumentDescription>"
                              f"</wscn:RetrieveImageRequest>", timeout=600)
            jpeg = extract_jpeg(data)
            if not jpeg:
                raise WSDError("the scanner returned no image")
            return jpeg
        except BaseException:
            self.cancel(job)
            raise

    def scan(self, caps, dpi, color="RGB24"):
        return self.retrieve(*self.create_job(caps, dpi, color))
