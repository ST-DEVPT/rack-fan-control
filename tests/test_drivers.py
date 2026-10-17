import os
import shutil
import subprocess
import tempfile
import unittest

from fanctl import drivers
from fanctl.drivers import DriverError, bare_host, parse_redfish, parse_sdr

SDR = """Fan1 RPM         | 30h | ok  |  7.1 | 3720 RPM
Fan2 RPM         | 31h | ok  |  7.1 | 3600 RPM
Fan Redundancy   | 75h | ok  |  7.1 | Fully Redundant
Inlet Temp       | 04h | ok  |  7.1 | 23 degrees C
Exhaust Temp     | 01h | ok  |  7.1 | 31 degrees C
Temp             | 0Eh | ok  |  3.1 | 44 degrees C
Temp             | 0Fh | ok  |  3.2 | 41 degrees C
Temp             | 10h | ns  |  3.3 | No Reading
Pwr Consumption  | 77h | ok  |  7.1 | 112 Watts
Current 1        | 6Ah | ok  | 10.1 | 0.40 Amps"""

ILO_THERMAL = {
    "Fans": [{"FanName": "Fan 1", "CurrentReading": 23, "Units": "Percent", "Status": {"Health": "OK", "State": "Enabled"}},
             {"FanName": "Fan 2", "CurrentReading": 25, "Units": "Percent", "Status": {"Health": "OK", "State": "Enabled"}},
             {"FanName": "Fan 7", "CurrentReading": 0, "Units": "Percent", "Status": {"State": "Absent"}}],
    "Temperatures": [
        {"Name": "01-Inlet Ambient", "ReadingCelsius": 21, "PhysicalContext": "Intake", "Status": {"Health": "OK", "State": "Enabled"}},
        {"Name": "02-CPU 1", "ReadingCelsius": 40, "PhysicalContext": "CPU", "Status": {"Health": "OK", "State": "Enabled"}},
        {"Name": "03-CPU 2", "ReadingCelsius": 44, "PhysicalContext": "CPU", "Status": {"Health": "OK", "State": "Enabled"}},
        {"Name": "04-P1 DIMM 1-6", "ReadingCelsius": 0, "Status": {"State": "Absent"}},
        {"Name": "32-PCI 1", "ReadingCelsius": 60, "PhysicalContext": "SystemBoard", "Status": {"Health": "OK", "State": "Enabled"}}]}
ILO_POWER = {"PowerControl": [{"PowerConsumedWatts": 118}]}


class Recording:
    """Records ipmitool calls instead of running them."""
    def __init__(self, *a, **kw):
        import tempfile
        super().__init__("10.0.0.1", "root", "x", data_dir=tempfile.mkdtemp(prefix="fanctl-drv-"))
        self.sent = []

    def ipmi(self, *args, timeout=20):
        self.sent.append(args)
        return self.answer(args)

    def answer(self, args):
        return ""


class FakeDell(Recording, drivers.DellDriver):
    """Refuses the 0xff selector and fan ids above 3, like some 11th-generation BMCs."""
    def answer(self, args):
        if args[:4] == ("raw", "0x30", "0x30", "0x02") and int(args[4], 16) > 3:
            raise DriverError("Unable to send RAW command (rsp=0xcc): Invalid data field in request")
        return ""


class DeadDell(Recording, drivers.DellDriver):
    def answer(self, args):
        if args[:4] == ("raw", "0x30", "0x30", "0x02"):
            raise DriverError("rsp=0xcc")
        return ""


class FakeSupermicro(Recording, drivers.SupermicroDriver):
    def answer(self, args):
        return " 04\n" if args[1:4] == ("0x30", "0x45", "0x00") else ""  # the user had HeavyIO


class FakeILO(drivers.ILO4UnlockedDriver):
    def __init__(self):
        super().__init__("10.0.0.3", "Administrator", "x")
        self.ssh_sent = []

    def get(self, path):
        return {"/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1/"}]},
                "/redfish/v1/Chassis/1/": {"Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal/"},
                                           "Power": {"@odata.id": "/redfish/v1/Chassis/1/Power/"}},
                "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1/"}]},
                "/redfish/v1/Systems/1/": {"Model": "ProLiant DL360 Gen9", "PowerState": "On"},
                "/redfish/v1/Chassis/1/Thermal/": ILO_THERMAL,
                "/redfish/v1/Chassis/1/Power/": ILO_POWER}[path]

    def ssh(self, command):
        self.ssh_sent.append(command)
        return ""


class Parsing(unittest.TestCase):
    def test_sdr_12th_generation(self):
        s = parse_sdr(SDR)
        self.assertEqual([f["name"] for f in s["fans"]], ["Fan1", "Fan2"])
        self.assertEqual([f["rpm"] for f in s["fans"]], [3720, 3600])
        self.assertEqual([t["name"] for t in s["temps"]], ["Inlet Temp", "Exhaust Temp", "CPU 1", "CPU 2"])
        self.assertEqual((s["inlet"], s["exhaust"], s["watts"]), (23, 31, 112))

    def test_sdr_11th_generation(self):
        s = parse_sdr("Ambient Temp     | 0Eh | ok  |  7.1 | 21 degrees C\n"
                      "FAN MOD 1A RPM   | 30h | ok  |  7.1 | 4200 RPM\n"
                      "Temp             | 01h | ok  |  3.1 | 40 degrees C")
        self.assertEqual((s["inlet"], s["exhaust"]), (21, None))
        self.assertEqual(s["fans"][0]["name"], "FAN MOD 1A")
        self.assertEqual(s["temps"][1]["name"], "CPU 1")

    def test_redfish_standard_names(self):
        _, fans, _ = parse_redfish({"Fans": [{"Name": "Fan1", "Reading": 5400, "ReadingUnits": "RPM"}]})
        self.assertEqual((fans[0]["rpm"], fans[0]["pct"]), (5400, None))

    def test_bare_host(self):
        self.assertEqual([bare_host(h) for h in ("10.0.0.5", "10.0.0.5:8443", "[fe80::1]:443", "[fe80::1]", "fe80::1")],
                         ["10.0.0.5", "10.0.0.5", "fe80::1", "fe80::1", "fe80::1"])


class Thresholds(unittest.TestCase):
    SENSOR = """Inlet Temp       | 23.000     | degrees C  | ok    | na        | -7.000    | 3.000     | 42.000    | 47.000    | na
Exhaust Temp     | 31.000     | degrees C  | ok    | na        | 3.000     | 8.000     | na        | 75.000    | na
Temp             | 44.000     | degrees C  | ok    | na        | 3.000     | 8.000     | 85.000    | 90.000    | na
Temp             | 41.000     | degrees C  | ok    | na        | 3.000     | 8.000     | 86.000    | 90.000    | na
Fan1 RPM         | 3720.000   | RPM        | ok    | na        | 360.000   | 600.000   | na        | na        | na"""

    def test_ipmi(self):
        t = drivers.parse_thresholds(self.SENSOR)
        self.assertEqual(t, {"Inlet Temp": [42.0], "Exhaust Temp": [75.0], "Temp": [85.0, 86.0]})
        s = parse_sdr(SDR, t)
        self.assertEqual([x["warn"] for x in s["temps"]], [42.0, 75.0, 85.0, 86.0])  # same-name CPUs keep their order

    def test_event_log(self):
        info = ("SEL Information\nVersion          : 1.5 (v1.5, v2 compliant)\nEntries          : 482\n"
                "Free Space       : 8704 bytes\nPercent Used     : 47%\n")
        elist = ("   1 | 08/02/2026 | 11:02:31 | Event Logging Disabled SEL | Log area reset/cleared | Asserted\n"
                 " 1e2 | 10/15/2026 | 23:41:07 | Fan Fan1 RPM | Lower Critical going low  | Asserted\n")
        sel = drivers.parse_sel(info, elist)
        self.assertEqual((sel["used_pct"], sel["entries"]), (47, 482))
        self.assertEqual(sel["recent"][0], ["1e2", "10/15/2026", "23:41:07", "Fan Fan1 RPM", "Lower Critical going low", "Asserted"])
        self.assertIsNone(drivers.parse_sel("Percent Used     : unknown\n", "")["used_pct"])  # some BMCs do not say

    def test_fan_floors(self):
        floors = drivers.parse_fan_floors(self.SENSOR + "\nFan2 RPM | 3600.000 | RPM | ok | na | 360.000 | na | na | na | na")
        self.assertEqual(floors, {"Fan1 RPM": [600.0], "Fan2 RPM": [360.0]})  # non-critical, else critical
        self.assertEqual([f["floor"] for f in parse_sdr(SDR, None, floors)["fans"]], [600.0, 360.0])
        _, fans, _ = parse_redfish({"Fans": [{"Name": "Fan1", "Reading": 5400, "ReadingUnits": "RPM",
                                              "LowerThresholdCritical": 1080},
                                             {"Name": "Fan2", "Reading": 30, "ReadingUnits": "Percent",
                                              "LowerThresholdCritical": 10}]})
        self.assertEqual([f["floor"] for f in fans], [1080.0, None])  # a percentage has no RPM floor

    def test_ipmitool_exiting_1_after_the_list_still_gives_them(self):
        # iDRACs print every sensor, then exit 1 over one they cannot read (an empty CPU socket's)
        real = drivers.subprocess.run
        drivers.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, self.SENSOR, "Unable to read sensor Temp: Device Not Present") if "sensor" in cmd else \
            subprocess.CompletedProcess(cmd, 0, SDR if "sdr" in cmd else "Chassis Power is on", "")
        try:
            d = drivers.DellDriver("10.0.0.9", "root", "calvin")
            d.model = ""
            d.read()
            self.assertIsNone(d.thresholds)  # the first reading goes straight to the fans
            s = d.read()
        finally:
            drivers.subprocess.run = real
        self.assertEqual((d.thresholds["Temp"], d.thresholds_error), ([85.0, 86.0], ""))
        self.assertEqual(s["temps"][0]["warn"], 42.0)
        with self.assertRaises(DriverError):  # anything else still fails on an error
            drivers.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "partial", "Error")
            try:
                d.ipmi("sdr", "elist", "full")
            finally:
                drivers.subprocess.run = real

    def test_redfish(self):
        temps, _, _ = parse_redfish({"Temperatures": [
            {"Name": "PCI 1", "ReadingCelsius": 50, "UpperThresholdCritical": 100, "UpperThresholdFatal": 110},
            {"Name": "Inlet", "ReadingCelsius": 20, "UpperThresholdNonCritical": 42, "UpperThresholdCritical": 47},
            {"Name": "DIMM", "ReadingCelsius": 30, "UpperThresholdCritical": 0}]})
        self.assertEqual([t["warn"] for t in temps], [100.0, 42.0, None])


class Dell(unittest.TestCase):
    def test_unknown_power_state(self):
        d = FakeDell()
        d.answer = lambda args: "Chassis Power is unknown" if args[:2] == ("chassis", "power") else SDR
        self.assertIsNone(d.read()["power"])

    def test_retake_lets_go_then_takes_hold_with_all_fans(self):
        d = FakeDell()
        d.set_speed(30)
        self.assertEqual(d.fan_ids, [0, 1, 2, 3])                     # fell back to fans one by one
        self.assertIn("set one by one", d.notice)
        d.sent.clear()
        d.retake(30)
        self.assertEqual(d.sent[:3], [("raw", "0x30", "0x30", "0x01", "0x01"), ("raw", "0x30", "0x30", "0x01", "0x00"),
                                      ("raw", "0x30", "0x30", "0x02", "0xff", "0x1e")])  # auto, manual, all fans again

    def test_per_fan_fallback(self):
        d = FakeDell()
        d.set_speed(30)
        self.assertEqual(d.fan_ids, [0, 1, 2, 3])
        d.sent.clear()
        d.set_speed(40)
        self.assertEqual(d.sent, [("raw", "0x30", "0x30", "0x01", "0x00")] +
                         [("raw", "0x30", "0x30", "0x02", f"0x0{i}", "0x28") for i in range(4)])

    def test_refusal_hands_back_to_automatic(self):
        d = DeadDell()
        with self.assertRaises(DriverError):
            d.set_speed(30)
        self.assertEqual(d.sent[-1], ("raw", "0x30", "0x30", "0x01", "0x01"))


class Supermicro(unittest.TestCase):
    def test_full_mode_then_both_zones(self):
        d = FakeSupermicro()
        d.set_speed(35)
        self.assertEqual(d.sent, [("raw", "0x30", "0x45", "0x00"),          # the mode it had, read first
                                  ("raw", "0x30", "0x45", "0x01", "0x01"),
                                  ("raw", "0x30", "0x70", "0x66", "0x01", "0x00", "0x23"),
                                  ("raw", "0x30", "0x70", "0x66", "0x01", "0x01", "0x23")])
        d.sent.clear()
        d.set_speed(40)  # Full mode is not switched again on every cycle
        self.assertEqual(d.sent, [("raw", "0x30", "0x70", "0x66", "0x01", "0x00", "0x28"),
                                  ("raw", "0x30", "0x70", "0x66", "0x01", "0x01", "0x28")])
        d.set_auto()
        self.assertEqual(d.sent[-1], ("raw", "0x30", "0x45", "0x01", "0x04"))  # HeavyIO back, not Optimal
        d.set_speed(40)  # after a release, Full mode is needed again
        self.assertEqual(d.sent[-3], ("raw", "0x30", "0x45", "0x01", "0x01"))

    def test_original_mode_survives_a_restart(self):
        first = FakeSupermicro()
        first.set_speed(30)
        again = FakeSupermicro()
        again.data_dir, again.mode_file = first.data_dir, first.mode_file
        again.answer = lambda args: " 01\n"  # the BMC is still in Full mode from before the restart
        again.set_speed(30)
        again.set_auto()
        self.assertEqual(again.sent[-1], ("raw", "0x30", "0x45", "0x01", "0x04"))
        self.assertNotIn(("raw", "0x30", "0x45", "0x00"), again.sent)  # read from disk, not taken for Full


class ILO4(unittest.TestCase):
    def test_read(self):
        d = FakeILO()
        r = d.read()
        self.assertEqual((d.model, r["power"], r["watts"]), ("ProLiant DL360 Gen9", "on", 118))
        self.assertEqual([f["pct"] for f in r["fans"]], [23, 25])
        self.assertTrue(all(f["rpm"] is None for f in r["fans"]))
        self.assertEqual([t["name"] for t in r["temps"]], ["01-Inlet Ambient", "CPU 1", "CPU 2", "32-PCI 1"])
        self.assertEqual((r["inlet"], r["exhaust"]), (21, None))

    def test_caps(self):
        d = FakeILO()
        d.read()
        d.set_speed(30)
        self.assertEqual(d.ssh_sent, ["fan p 0 max 77", "fan p 1 max 77"])
        d.set_speed(30)
        self.assertEqual(len(d.ssh_sent), 2)  # an unchanged cap is not re-sent every cycle
        d.set_auto()
        self.assertEqual(d.ssh_sent[-2:], ["fan p 0 max 255", "fan p 1 max 255"])


class Redirects(unittest.TestCase):
    def test_redfish_refuses_redirects(self):
        import http.server
        import threading
        import urllib.error
        import urllib.request

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redfish/v1/Chassis/":
                    body = self.headers.get("Authorization", "").encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(308 if self.path == "/redfish/v1/Chassis" else 302)
                self.send_header("Location", "/redfish/v1/Chassis/" if self.path == "/redfish/v1/Chassis"
                                 else "http://127.0.0.1:1/steal")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        opener = urllib.request.build_opener(drivers.SameOriginRedirect)
        with self.assertRaises(urllib.error.HTTPError) as cm:  # another host: refused
            opener.open(f"{base}/redfish/v1", timeout=5)
        cm.exception.close()
        req = urllib.request.Request(f"{base}/redfish/v1/Chassis", headers={"Authorization": "Basic x"})
        with opener.open(req, timeout=5) as r:  # same BMC, trailing slash: followed, auth kept
            self.assertEqual(r.read(), b"Basic x")
        srv.shutdown()
        srv.server_close()


class Diagnostics(unittest.TestCase):
    def test_redact(self):
        doc = {"Model": "ProLiant DL360 Gen9", "SerialNumber": "CZ1234", "UUID": "x",
               "EthernetInterfaces": {"@odata.id": "/redfish/v1/Managers/1/EthernetInterfaces"},
               "Oem": {"Hp": {"HostName": "ilo-rack", "Fans": [{"MACAddress": "aa:bb"}]}}}
        r = drivers.redact(doc)
        self.assertEqual(r["Model"], "ProLiant DL360 Gen9")
        self.assertEqual((r["SerialNumber"], r["Oem"]["Hp"]["HostName"], r["Oem"]["Hp"]["Fans"][0]["MACAddress"]),
                         ("<redacted>",) * 3)
        self.assertEqual(r["EthernetInterfaces"]["@odata.id"], "/redfish/v1/Managers/1/EthernetInterfaces")

    def test_redfish_walk(self):
        docs = {"/redfish/v1": {"RedfishVersion": "1.0.0"},
                "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1/"}]},
                "/redfish/v1/Chassis/1/": {"Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal/"}, "SerialNumber": "S"},
                "/redfish/v1/Chassis/1/Thermal/": ILO_THERMAL,
                "/redfish/v1/Systems": {"Members": []}}
        rf = drivers.RedfishDriver("10.0.0.9")

        def get(path):
            if path not in docs:
                raise drivers.DriverError("HTTP 404")
            return docs[path]
        rf.get = get
        out = rf.diagnose()
        self.assertEqual(out["/redfish/v1/Chassis/1/Thermal/"]["Fans"][0]["FanName"], "Fan 1")
        self.assertEqual(out["/redfish/v1/Chassis/1/"]["SerialNumber"], "<redacted>")


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make test certificates")
class CertificatePinning(unittest.TestCase):
    """A BMC that suddenly shows another certificate is refused: its password goes with every request."""

    def make_cert(self, folder, name):
        key, crt = os.path.join(folder, name + ".key"), os.path.join(folder, name + ".crt")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=bmc",
                        "-keyout", key, "-out", crt], check=True, capture_output=True)
        return crt, key

    def serve(self, cert):
        import http.server
        import ssl
        import threading

        class BMC(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"RedfishVersion": "1.0.0"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), BMC)
        srv.handle_error = lambda *a: None  # the refused handshake is the point, not noise
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*cert)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_first_certificate_is_kept(self):
        folder = tempfile.mkdtemp(prefix="fanctl-pin-")
        first, second = self.make_cert(folder, "a"), self.make_cert(folder, "b")
        port = self.serve(first)
        rf = drivers.RedfishDriver(f"127.0.0.1:{port}", "root", "pw", False, folder)
        rf.pin = True
        self.assertEqual(rf.get("/redfish/v1")["RedfishVersion"], "1.0.0")   # pinned on first use
        self.assertEqual(rf.get("/redfish/v1")["RedfishVersion"], "1.0.0")
        rf.host = f"127.0.0.1:{self.serve(second)}"                         # same host, another certificate
        with self.assertRaises(drivers.DriverError) as cm:
            rf.get("/redfish/v1")
        self.assertIn("certificate changed", str(cm.exception))
        drivers.forget_pin(folder, rf.host)                                  # accepted in the dashboard
        self.assertEqual(rf.get("/redfish/v1")["RedfishVersion"], "1.0.0")

    def test_scans_and_tests_leave_no_pin(self):
        folder = tempfile.mkdtemp(prefix="fanctl-pin-")
        port = self.serve(self.make_cert(folder, "a"))
        rf = drivers.RedfishDriver(f"127.0.0.1:{port}", "root", "pw", False, folder)
        rf.get("/redfish/v1")
        self.assertFalse(os.path.exists(os.path.join(folder, "redfish-pins.json")))


class Detection(unittest.TestCase):
    def test_suggestions(self):
        cases = [
            (("Dell", "Integrated Dell Remote Access Controller", "2.65.65.65"), "dell"),
            (("Dell", "iDRAC", "3.30.30.30"), "dell"),
            (("Dell", "iDRAC", "4.40.00.00"), "redfish"),
            (("Hp", "iLO 4", "2.77"), "redfish"),
            (("HPE", "iLO 5", "2.72"), "redfish"),
            (("Supermicro", "", ""), "supermicro"),
            (("Lenovo", "XClarity Controller", ""), "redfish"),
            (("", "", ""), None),
        ]
        for args, kind in cases:
            with self.subTest(args=args):
                self.assertEqual(drivers.suggest(*args)[0], kind)
        self.assertIn("unlocked", drivers.suggest("Hp", "iLO 4", "2.77")[1])

    def test_ilo_service_root(self):
        root = {"Oem": {"Hp": {"Manager": [{"ManagerType": "iLO 4", "ManagerFirmwareVersion": "2.77"}]}}, "Product": ""}
        real = drivers.RedfishDriver.get
        drivers.RedfishDriver.get = lambda self, path: root
        try:
            r = drivers.detect("10.0.0.9")
        finally:
            drivers.RedfishDriver.get = real
        self.assertEqual((r["vendor"], r["product"], r["firmware"], r["suggested"]), ("Hp", "iLO 4", "2.77", "redfish"))


class Discovery(unittest.TestCase):
    def test_only_private_ranges(self):
        self.assertEqual(len(drivers.scan_targets("192.168.1.0/24")), 254)
        self.assertEqual(drivers.scan_targets("10.0.0.5/32"), ["10.0.0.5"])
        for bad in ("8.8.8.0/24", "127.0.0.0/30", "10.0.0.0/16", "not a range"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                drivers.scan_targets(bad)

    def test_ipmi_probe(self):
        import socket
        import threading
        bmc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bmc.bind(("127.0.0.1", 0))

        def answer():
            data, addr = bmc.recvfrom(512)
            if data == drivers.IPMI_PROBE:
                bmc.sendto(bytes.fromhex("0600ff0700000000000000000010811c6320008e0001d900000000"), addr)

        threading.Thread(target=answer, daemon=True).start()
        self.assertTrue(drivers.probe_ipmi("127.0.0.1", port=bmc.getsockname()[1]))
        self.assertFalse(drivers.probe_ipmi("127.0.0.1", timeout=0.3, port=bmc.getsockname()[1]))  # nobody answers now
        bmc.close()

    def test_probe_and_scan(self):
        roots = {"10.0.0.1": {"RedfishVersion": "1.0.0", "Oem": {"Hp": {"Manager": [{"ManagerType": "iLO 4", "ManagerFirmwareVersion": "2.77"}]}}},
                 "10.0.0.2": None}
        real = drivers.probe_redfish, drivers.probe_ipmi
        drivers.probe_redfish = lambda host, timeout=2: roots.get(host)
        drivers.probe_ipmi = lambda host, timeout=1.5, port=623: host in ("10.0.0.1", "10.0.0.2")
        try:
            found = drivers.scan("10.0.0.0/29")
        finally:
            drivers.probe_redfish, drivers.probe_ipmi = real
        self.assertEqual([f["host"] for f in found], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual((found[0]["vendor"], found[0]["suggested"], found[0]["redfish"]), ("Hp", "redfish", True))
        self.assertEqual((found[1]["suggested"], found[1]["redfish"], found[1]["ipmi"]), ("ipmi", False, True))


class Leaks(unittest.TestCase):
    def test_a_timeout_does_not_carry_the_bmc_address_or_user(self):
        real = drivers.subprocess.run

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        drivers.subprocess.run = slow
        try:
            with self.assertRaises(DriverError) as e:
                drivers.DellDriver("10.0.0.9", "rootuser", "calvin").ipmi("sdr", "elist", "full")
        finally:
            drivers.subprocess.run = real
        self.assertNotIn("10.0.0.9", str(e.exception))
        self.assertNotIn("rootuser", str(e.exception))

    def test_scans_stay_private(self):
        with self.assertRaises(ValueError):
            drivers.scan_targets("::ffff:8.8.8.0/120")


class IpmiConnection(unittest.TestCase):
    def run_with(self, answer, host="10.0.0.9:6230", cipher=None):
        seen, real = [], drivers.subprocess.run

        def fake(cmd, **kw):
            seen.append(cmd)
            return answer(cmd)
        drivers.subprocess.run = fake
        try:
            d = drivers.DellDriver(host, "root", "calvin")
            d.cipher = cipher
            out = d.ipmi("chassis", "power", "status")
        finally:
            drivers.subprocess.run = real
        return d, seen, out

    def test_port_and_cipher_reach_ipmitool(self):
        _, seen, _ = self.run_with(lambda cmd: subprocess.CompletedProcess(cmd, 0, "Chassis Power is on", ""), cipher=17)
        self.assertEqual(seen[0][:12], ["ipmitool", "-I", "lanplus", "-H", "10.0.0.9", "-U", "root", "-E", "-p", "6230",
                                         "-C", "17"])

    def test_cipher_3_when_the_default_cannot_open_a_session(self):
        def bmc(cmd):
            ok = "-C" in cmd and cmd[cmd.index("-C") + 1] == "3"
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, "Chassis Power is on" if ok else "",
                                               "" if ok else "Error: Unable to establish IPMI v2 / RMCP+ session")
        d, seen, out = self.run_with(bmc, host="10.0.0.9")
        self.assertEqual((len(seen), d.cipher, out), (2, 3, "Chassis Power is on"))
        self.assertIn("cipher suite 3", d.notice)


class ModernRedfish(unittest.TestCase):
    """A BMC with ThermalSubsystem, Sensors and EnvironmentMetrics and no Thermal (OpenBMC, newer firmware)."""
    TREE = {
        "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        "/redfish/v1/Chassis/1": {"ThermalSubsystem": {"@odata.id": "/redfish/v1/Chassis/1/ThermalSubsystem"},
                                  "Sensors": {"@odata.id": "/redfish/v1/Chassis/1/Sensors"},
                                  "EnvironmentMetrics": {"@odata.id": "/redfish/v1/Chassis/1/EnvironmentMetrics"}},
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
        "/redfish/v1/Systems/1": {"Model": "R760", "PowerState": "On"},
        "/redfish/v1/Chassis/1/ThermalSubsystem/Fans?$expand=.($levels=1)": {"Members": [{"@odata.id": "/f/1"}]},
        "/f/1": {"Name": "Fan 1", "SpeedPercent": {"Reading": 30, "SpeedRPM": 5400}, "Status": {"Health": "OK"}},
        "/redfish/v1/Chassis/1/Sensors?$expand=.($levels=1)": {"Members": [
            {"Name": "CPU1 Temp", "ReadingType": "Temperature", "Reading": 52, "PhysicalContext": "CPU",
             "Thresholds": {"UpperCaution": {"Reading": 88}}},
            {"Name": "PSU1 Power", "ReadingType": "Power", "Reading": 210}]},
        "/redfish/v1/Chassis/1/EnvironmentMetrics": {"PowerWatts": {"Reading": 230}},
    }

    def test_read(self):
        tree = self.TREE

        class Modern(drivers.RedfishDriver):
            def _get(self, path):
                if path not in tree:
                    raise DriverError(f"Redfish {path}: HTTP 404")
                return tree[path]
        d = Modern("10.0.0.9", "root", "calvin")
        s = d.read()
        self.assertEqual([(t["name"], t["value"], t["cpu"], t["warn"]) for t in s["temps"]], [("CPU1 Temp", 52.0, True, 88.0)])
        self.assertEqual((s["fans"][0]["rpm"], s["watts"], s["power"]), (5400, 230, "on"))
