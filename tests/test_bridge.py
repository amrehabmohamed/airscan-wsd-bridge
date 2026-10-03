"""Offline tests: no scanner or network needed. Run with: python3 -m unittest discover tests"""
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge  # noqa: E402
import ipp  # noqa: E402
import wsd  # noqa: E402

W = wsd.WSCN

# Shape of a real GetScannerElements response from a Canon G3010 series.
ELEMENTS = f"""<soap:Envelope xmlns:soap="{wsd.SOAP}" xmlns:wscn="{W}"><soap:Body><wscn:GetScannerElementsResponse><wscn:ScannerElements>
<wscn:ElementData Name="wscn:ScannerConfiguration" Valid="true"><wscn:ScannerConfiguration>
<wscn:DeviceSettings>
<wscn:FormatsSupported><wscn:FormatValue>exif</wscn:FormatValue></wscn:FormatsSupported>
<wscn:ContentTypesSupported><wscn:ContentTypeValue>Text</wscn:ContentTypeValue><wscn:ContentTypeValue>Photo</wscn:ContentTypeValue></wscn:ContentTypesSupported>
</wscn:DeviceSettings>
<wscn:Platen>
<wscn:PlatenResolutions><wscn:Widths><wscn:Width>150</wscn:Width><wscn:Width>300</wscn:Width><wscn:Width>600</wscn:Width></wscn:Widths>
<wscn:Heights><wscn:Height>150</wscn:Height><wscn:Height>300</wscn:Height><wscn:Height>600</wscn:Height></wscn:Heights></wscn:PlatenResolutions>
<wscn:PlatenColor><wscn:ColorEntry>RGB24</wscn:ColorEntry></wscn:PlatenColor>
<wscn:PlatenMinimumSize><wscn:Width>20</wscn:Width><wscn:Height>20</wscn:Height></wscn:PlatenMinimumSize>
<wscn:PlatenMaximumSize><wscn:Width>8500</wscn:Width><wscn:Height>11680</wscn:Height></wscn:PlatenMaximumSize>
</wscn:Platen>
</wscn:ScannerConfiguration></wscn:ElementData>
<wscn:ElementData Name="wscn:DefaultScanTicket" Valid="true"><wscn:DefaultScanTicket>
<wscn:JobDescription><wscn:JobName>DefaultJobName</wscn:JobName><wscn:JobInformation>DefaultJobInformation</wscn:JobInformation></wscn:JobDescription>
<wscn:DocumentParameters><wscn:Format>exif</wscn:Format><wscn:ImagesToTransfer>1</wscn:ImagesToTransfer><wscn:InputSource>Platen</wscn:InputSource><wscn:ContentType>Text</wscn:ContentType>
<wscn:MediaSides><wscn:MediaFront><wscn:ColorProcessing>RGB24</wscn:ColorProcessing>
<wscn:Resolution><wscn:Width>150</wscn:Width><wscn:Height>150</wscn:Height></wscn:Resolution></wscn:MediaFront></wscn:MediaSides>
</wscn:DocumentParameters></wscn:DefaultScanTicket></wscn:ElementData>
</wscn:ScannerElements></wscn:GetScannerElementsResponse></soap:Body></soap:Envelope>"""


def caps():
    return wsd.parse_capabilities(ET.fromstring(ELEMENTS))


class Capabilities(unittest.TestCase):
    def test_reads_flatbed_features(self):
        c = caps()
        self.assertEqual(c["resolutions"], [150, 300, 600])
        self.assertEqual(c["color_modes"], ["RGB24"])
        self.assertEqual(c["formats"], ["exif"])
        self.assertEqual((c["max_width"], c["max_height"]), (8500, 11680))
        self.assertFalse(c["has_feeder"])

    def test_rejects_device_without_flatbed(self):
        root = ET.fromstring(ELEMENTS)
        config = root.find(f".//{{{W}}}ScannerConfiguration")
        config.remove(config.find(f"{{{W}}}Platen"))
        with self.assertRaises(wsd.WSDError):
            wsd.parse_capabilities(root)


class Ticket(unittest.TestCase):
    def test_changes_only_what_is_needed(self):
        default = ET.fromstring(ELEMENTS).find(f".//{{{W}}}DefaultScanTicket")
        ticket = wsd.build_ticket(default, caps(), 300, "RGB24")
        self.assertEqual(ticket.tag, f"{{{W}}}ScanTicket")
        front = ticket.find(f".//{{{W}}}MediaFront")
        self.assertEqual(front.findtext(f"{{{W}}}Resolution/{{{W}}}Width"), "300")
        self.assertEqual(front.findtext(f"{{{W}}}ScanRegion/{{{W}}}ScanRegionWidth"), "8500")
        self.assertEqual(ticket.findtext(f".//{{{W}}}ContentType"), "Photo")
        # Fields the device supplied are kept: it rejects tickets without them.
        self.assertEqual(ticket.findtext(f".//{{{W}}}JobInformation"), "DefaultJobInformation")
        # The default ticket itself is left untouched.
        self.assertEqual(default.findtext(f".//{{{W}}}Resolution/{{{W}}}Width"), "150")


class Jpeg(unittest.TestCase):
    def test_extracts_image_from_multipart_body(self):
        image = b"\xff\xd8\xff\xe1data\xff\xd9"
        self.assertEqual(wsd.extract_jpeg(b"--boundary\r\n<xml/>\r\n" + image + b"\r\n--boundary--"), image)

    def test_no_image(self):
        self.assertIsNone(wsd.extract_jpeg(b"<soap/>"))


class Escl(unittest.TestCase):
    def setUp(self):
        scanner = wsd.Scanner("urn:uuid:00000000-0000-0000-0000-000000000000", "http://192.0.2.1/scan", "G3010 series", "CANON INC.")
        self.bridge = bridge.Bridge(scanner, caps(), 8290)

    def test_name_combines_maker_and_model(self):
        self.assertEqual(self.bridge.scanner.name, "Canon G3010 series")

    def test_capabilities_document(self):
        text = self.bridge.capabilities_xml()
        ET.fromstring(text)
        self.assertIn("<scan:MaxWidth>2550</scan:MaxWidth>", text)
        self.assertIn("<scan:MaxHeight>3504</scan:MaxHeight>", text)
        # Colour-only device still offers greyscale, converted on the Mac.
        self.assertEqual(self.bridge.color_modes, ["RGB24", "Grayscale8"])

    def test_settings_are_clamped_to_the_device(self):
        body = ("<scan:ScanSettings><pwg:XOffset>100</pwg:XOffset><pwg:YOffset>50</pwg:YOffset>"
                "<pwg:Width>9999</pwg:Width><pwg:Height>600</pwg:Height><scan:ColorMode>Grayscale8</scan:ColorMode>"
                "<scan:XResolution>200</scan:XResolution><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat></scan:ScanSettings>")
        s = self.bridge.parse_settings(body)
        self.assertEqual(s, dict(dpi=150, x=100, y=50, w=2450, h=600, fmt="application/pdf", mode="Grayscale8"))

    def test_settings_defaults(self):
        s = self.bridge.parse_settings("<scan:ScanSettings/>")
        self.assertEqual(s, dict(dpi=300, x=0, y=0, w=2550, h=3504, fmt="image/jpeg", mode="RGB24"))


class Printer(unittest.TestCase):
    IPPTOOL = (
        "    get-printer-attributes                                    [PASS]\n"
        "        document-format-supported (1setOf mimeMediaType) = application/octet-stream,image/pwg-raster\n"
        "        color-supported (boolean) = true\n"
        "        sides-supported (keyword) = one-sided\n"
        "        media-supported (1setOf keyword) = iso_a4_210x297mm,na_letter_8.5x11in,jpn_hagaki_100x148mm,custom_min_0x0mm\n"
        "        printer-make-and-model (textWithoutLanguage) = Canon G3010 series\n")

    def test_attributes(self):
        attrs = ipp.parse_attributes(self.IPPTOOL)
        self.assertEqual(attrs["printer-make-and-model"], ["Canon G3010 series"])
        self.assertTrue(ipp.driverless(attrs))
        text = ipp.describe(attrs)
        self.assertIn("Colour      : yes", text)
        self.assertIn("Two-sided   : no", text)
        self.assertIn("A4, Letter and 1 more", text)

    def test_vendor_only_printer_is_not_driverless(self):
        self.assertFalse(ipp.driverless({"document-format-supported": ["application/octet-stream"]}))

    def test_queues(self):
        queues = ipp.parse_queues("device for Office: ipp://printer.local:631/ipp/print\n"
                                  "device for Other: dnssd://My%20Printer._ipp._tcp.local./?uuid=1\n")
        self.assertEqual(queues["Office"], "ipp://printer.local:631/ipp/print")
        self.assertEqual(len(queues), 2)
        self.assertEqual(ipp.parse_queues("lpstat: No destinations added.\n"), {})

    def test_queue_name(self):
        self.assertEqual(ipp.queue_name("Canon G3010 series"), "Canon_G3010_series")
        self.assertEqual(ipp.queue_name("!!"), "Printer")


if __name__ == "__main__":
    unittest.main()
