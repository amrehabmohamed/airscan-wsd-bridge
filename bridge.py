"""AirScan (eSCL) front end for a WSD scanner.

macOS scans natively only from AirScan devices. This serves the eSCL
endpoints on this Mac, advertises them over Bonjour, and turns each scan
request into WS-Scan calls to the real device.
"""
import re
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import wsd

FORMATS = ("image/jpeg", "application/pdf")
GRAY_PROFILE = "/System/Library/ColorSync/Profiles/Generic Gray Gamma 2.2 Profile.icc"
XMLNS = 'xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"'
MIN_SIZE = 16  # eSCL units (1/300 inch)


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def sips(*args):
    subprocess.run(["sips", *args], check=True, capture_output=True)


def convert(jpeg, dpi, x, y, w, h, full_w, full_h, fmt="image/jpeg", gray=False):
    """Crop a full-flatbed scan to the requested region (1/300 inch units) and format."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "scan.jpg"
        src.write_bytes(jpeg)
        left, top, width, height = (round(v * dpi / 300) for v in (x, y, w, h))
        if (width, height) != (round(full_w * dpi / 300), round(full_h * dpi / 300)):
            sips("-c", str(height), str(width), "--cropOffset", str(top), str(left), str(src))
        if gray:
            sips("--matchTo", GRAY_PROFILE, str(src))
        if fmt == "application/pdf":
            out = Path(tmp) / "scan.pdf"
            sips("-s", "format", "pdf", str(src), "--out", str(out))
            return out.read_bytes()
        return src.read_bytes()


class Job:
    def __init__(self, settings):
        self.id = str(uuid.uuid4())
        self.created = time.time()
        self.settings = settings
        self.state = "Processing"
        self.result = None
        self.served = False
        self.device_job = None
        self.done = threading.Event()


class Bridge:
    def __init__(self, scanner, caps, port, on_moved=None):
        self.scanner = scanner
        self.caps = caps
        self.port = port
        self.on_moved = on_moved
        self.uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, scanner.endpoint))
        # eSCL sizes are in 1/300 inch, WSD in 1/1000 inch.
        self.max_w = caps["max_width"] * 300 // 1000
        self.max_h = caps["max_height"] * 300 // 1000
        device_modes = caps["color_modes"]
        self.color_modes = [m for m in ("RGB24", "Grayscale8") if m in device_modes]
        if "RGB24" in device_modes and "Grayscale8" not in device_modes:
            # Colour-only device: scan in colour and convert on the Mac.
            self.color_modes.append("Grayscale8")
        self.jobs = {}
        self.lock = threading.Lock()
        self._status = (0, True)
        self._last_search = 0

    def device(self, call):
        """Run a call against the scanner, finding it again if its address changed."""
        try:
            return call(self.scanner)
        except wsd.Unreachable:
            if time.time() - self._last_search < 30:
                raise
            self._last_search = time.time()
            found = wsd.locate(self.scanner.endpoint)
            if not found:
                raise
            log("scanner moved to", found.url)
            self.scanner.url = found.url
            if self.on_moved:
                self.on_moved(found.url)
            return call(self.scanner)

    def capabilities_xml(self):
        formats = "".join(f"<pwg:DocumentFormat>{f}</pwg:DocumentFormat><scan:DocumentFormatExt>{f}</scan:DocumentFormatExt>" for f in FORMATS)
        resolutions = "".join(f"<scan:DiscreteResolution><scan:XResolution>{r}</scan:XResolution><scan:YResolution>{r}</scan:YResolution></scan:DiscreteResolution>"
                              for r in self.caps["resolutions"])
        modes = "".join(f"<scan:ColorMode>{m}</scan:ColorMode>" for m in self.color_modes)
        top = max(self.caps["resolutions"])
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities {XMLNS}>
<pwg:Version>2.0</pwg:Version>
<pwg:MakeAndModel>{self.scanner.name}</pwg:MakeAndModel>
<scan:UUID>{self.uuid}</scan:UUID>
<scan:Platen><scan:PlatenInputCaps>
<scan:MinWidth>{MIN_SIZE}</scan:MinWidth><scan:MaxWidth>{self.max_w}</scan:MaxWidth>
<scan:MinHeight>{MIN_SIZE}</scan:MinHeight><scan:MaxHeight>{self.max_h}</scan:MaxHeight>
<scan:MaxScanRegions>1</scan:MaxScanRegions>
<scan:SettingProfiles><scan:SettingProfile>
<scan:ColorModes>{modes}</scan:ColorModes>
<scan:DocumentFormats>{formats}</scan:DocumentFormats>
<scan:SupportedResolutions><scan:DiscreteResolutions>{resolutions}</scan:DiscreteResolutions></scan:SupportedResolutions>
</scan:SettingProfile></scan:SettingProfiles>
<scan:SupportedIntents><scan:Intent>Document</scan:Intent><scan:Intent>TextAndGraphic</scan:Intent><scan:Intent>Photo</scan:Intent><scan:Intent>Preview</scan:Intent></scan:SupportedIntents>
<scan:MaxOpticalXResolution>{top}</scan:MaxOpticalXResolution><scan:MaxOpticalYResolution>{top}</scan:MaxOpticalYResolution>
</scan:PlatenInputCaps></scan:Platen>
</scan:ScannerCapabilities>"""

    def busy(self):
        with self.lock:
            if any(j.state == "Processing" for j in self.jobs.values()):
                return True
        # macOS polls every few seconds while a scan window is open; don't pass every poll on.
        checked, busy = self._status
        if time.time() - checked > 2:
            try:
                busy = self.device(lambda s: s.state()) != "Idle"
            except wsd.WSDError:
                busy = True
            self._status = (time.time(), busy)
        return busy

    def status_xml(self):
        busy = self.busy()
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: -j.created)
        infos = ""
        for j in jobs:
            reason = {"Completed": "JobCompletedSuccessfully", "Processing": "JobScanning"}.get(j.state, "AbortedBySystem")
            infos += (f"<scan:JobInfo><pwg:JobUri>/eSCL/ScanJobs/{j.id}</pwg:JobUri><pwg:JobUuid>urn:uuid:{j.id}</pwg:JobUuid>"
                      f"<scan:Age>{int(time.time() - j.created)}</scan:Age><pwg:ImagesCompleted>{1 if j.state == 'Completed' else 0}</pwg:ImagesCompleted>"
                      f"<pwg:ImagesToTransfer>1</pwg:ImagesToTransfer><pwg:JobState>{j.state}</pwg:JobState>"
                      f"<pwg:JobStateReasons><pwg:JobStateReason>{reason}</pwg:JobStateReason></pwg:JobStateReasons></scan:JobInfo>")
        return (f'<?xml version="1.0" encoding="UTF-8"?><scan:ScannerStatus {XMLNS}><pwg:Version>2.0</pwg:Version>'
                f'<pwg:State>{"Processing" if busy else "Idle"}</pwg:State><scan:Jobs>{infos}</scan:Jobs></scan:ScannerStatus>')

    def parse_settings(self, body):
        def num(tag, default):
            found = re.search(rf"<(?:\w+:)?{tag}>\s*(\d+)\s*<", body)
            return int(found.group(1)) if found else default

        wanted = num("XResolution", 300)
        dpi = min(self.caps["resolutions"], key=lambda r: abs(r - wanted))
        fmt = re.search(r"DocumentFormat(?:Ext)?>\s*([^<\s]+)", body)
        fmt = fmt.group(1) if fmt and fmt.group(1) in FORMATS else "image/jpeg"
        mode = re.search(r"ColorMode>\s*(\w+)", body)
        mode = mode.group(1) if mode and mode.group(1) in self.color_modes else self.color_modes[0]
        x, y = min(num("XOffset", 0), self.max_w - MIN_SIZE), min(num("YOffset", 0), self.max_h - MIN_SIZE)
        w, h = min(num("Width", self.max_w), self.max_w - x), min(num("Height", self.max_h), self.max_h - y)
        return dict(dpi=dpi, x=x, y=y, w=w, h=h, fmt=fmt, mode=mode)

    def start(self, body):
        """Start a scan on the device; the page is fetched in the background."""
        job = Job(self.parse_settings(body))
        log("scan request", job.settings)
        native = job.settings["mode"] if job.settings["mode"] in self.caps["color_modes"] else "RGB24"
        job.device_job, token = self.device(lambda s: s.create_job(self.caps, job.settings["dpi"], native))
        with self.lock:
            # Keep the last few jobs: macOS asks for their state after fetching the page.
            for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-4]:
                self.jobs.pop(old.id, None)
            self.jobs[job.id] = job
        threading.Thread(target=self._finish, args=(job, token, native), daemon=True).start()
        return job

    def _finish(self, job, token, native):
        s = job.settings
        try:
            jpeg = self.scanner.retrieve(job.device_job, token)
            job.result = convert(jpeg, s["dpi"], s["x"], s["y"], s["w"], s["h"], self.max_w, self.max_h,
                                 s["fmt"], gray=s["mode"] == "Grayscale8" and native != "Grayscale8")
            job.state = "Completed"
        except Exception as e:
            log("scan failed:", e)
            job.state = "Aborted"
        job.done.set()

    def cancel(self, job):
        if job.state == "Processing":
            self.scanner.cancel(job.device_job)
            job.state = "Canceled"
        with self.lock:
            self.jobs.pop(job.id, None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Status polls arrive every few seconds; logging them would bury everything else.
        if "ScannerStatus" not in (args[0] if args else ""):
            log(self.client_address[0], fmt % args)

    def reply(self, code, body=b"", ctype="text/xml", headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def allowed(self):
        # Bonjour advertises on the whole network, but only this Mac may scan through the bridge.
        client = self.client_address[0]
        if client in ("127.0.0.1", "::1") or client == self.connection.getsockname()[0]:
            return True
        self.reply(403)
        return False

    def route(self):
        return re.sub(r"^https?://[^/]+", "", self.path).split("?")[0].rstrip("/")

    def job(self, path):
        found = re.match(r"/eSCL/ScanJobs/([0-9a-f-]+)", path)
        bridge = self.server.bridge
        with bridge.lock:
            return bridge.jobs.get(found.group(1)) if found else None

    def do_GET(self):
        if not self.allowed():
            return
        bridge, path = self.server.bridge, self.route()
        if path == "/eSCL/ScannerCapabilities":
            return self.reply(200, bridge.capabilities_xml())
        if path == "/eSCL/ScannerStatus":
            return self.reply(200, bridge.status_xml())
        job = self.job(path)
        if job and path.endswith("/NextDocument"):
            if job.served:
                return self.reply(404)
            job.done.wait()
            if job.state != "Completed":
                return self.reply(503)
            job.served = True
            return self.reply(200, job.result, job.settings["fmt"], [("Content-Location", f"/eSCL/ScanJobs/{job.id}/1")])
        if job and path.endswith("/ScanImageInfo"):
            s = job.settings
            w, h = round(s["w"] * s["dpi"] / 300), round(s["h"] * s["dpi"] / 300)
            return self.reply(200, f'<?xml version="1.0" encoding="UTF-8"?><scan:ScanImageInfo {XMLNS}>'
                              f"<pwg:JobUri>/eSCL/ScanJobs/{job.id}</pwg:JobUri><pwg:JobUuid>urn:uuid:{job.id}</pwg:JobUuid>"
                              f"<scan:ActualWidth>{w}</scan:ActualWidth><scan:ActualHeight>{h}</scan:ActualHeight>"
                              f"<scan:ActualBytesPerLine>{w * (1 if s['mode'] == 'Grayscale8' else 3)}</scan:ActualBytesPerLine></scan:ScanImageInfo>")
        self.reply(404)

    do_HEAD = do_GET

    def do_POST(self):
        if not self.allowed():
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf8", "replace")
        if self.route() != "/eSCL/ScanJobs":
            return self.reply(404)
        try:
            job = self.server.bridge.start(body)
        except wsd.WSDError as e:
            log("refused:", e)
            return self.reply(503)
        host = self.headers.get("Host") or f"localhost:{self.server.bridge.port}"
        self.reply(201, headers=[("Location", f"http://{host}/eSCL/ScanJobs/{job.id}")])

    def do_DELETE(self):
        if not self.allowed():
            return
        job = self.job(self.route())
        if not job:
            return self.reply(404)
        self.server.bridge.cancel(job)
        self.reply(200)


def advertise(bridge):
    modes = ",".join({"RGB24": "color", "Grayscale8": "grayscale"}[m] for m in bridge.color_modes)
    txt = ["txtvers=1", "vers=2.0", f"ty={bridge.scanner.name}", "rs=eSCL", f"pdl={','.join(FORMATS)}",
           f"cs={modes}", "is=platen", "duplex=F", f"uuid={bridge.uuid}", "note=airscan-wsd-bridge"]
    return subprocess.Popen(["dns-sd", "-R", bridge.scanner.name, "_uscan._tcp", "local", str(bridge.port), *txt],
                            stdout=subprocess.DEVNULL)


def serve(scanner, caps, port, on_moved=None):
    server = ThreadingHTTPServer(("", port), Handler)
    server.bridge = Bridge(scanner, caps, port, on_moved)
    bonjour = advertise(server.bridge)
    log(f"AirScan bridge for {scanner.name} on port {port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bonjour.terminate()
