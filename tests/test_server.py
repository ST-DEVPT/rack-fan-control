import json
import unittest

from fanctl.config import DATA_DIR as DATA, write_json
from fanctl.server import SERVERS, Server, env_servers, validate_server


class Environment(unittest.TestCase):
    def test_none(self):
        self.assertEqual(env_servers({}), [])

    def test_single_server_of_1x(self):
        s = env_servers({"IDRAC_HOST": "10.0.0.5", "IDRAC_NAME": "R720"})[0]
        self.assertEqual((s["id"], s["driver"], s["legacy"]), ("r720", "dell", True))

    def test_numbered_and_drivers(self):
        s = env_servers({"IDRAC_2_HOST": "b", "IDRAC_1_HOST": "a", "IDRAC_1_NAME": "Same", "IDRAC_2_NAME": "Same",
                         "IDRAC_2_DRIVER": "redfish"})
        self.assertEqual([x["id"] for x in s], ["same", "same-2"])
        self.assertEqual((s[1]["host"], s[1]["driver"]), ("b", "redfish"))
        self.assertEqual(env_servers({"IDRAC_1_HOST": "x", "IDRAC_1_DRIVER": "bogus"}), [])

    def test_server_prefix(self):
        s = env_servers({"SERVER_1_HOST": "10.0.0.7", "SERVER_1_DRIVER": "supermicro", "SERVER_1_NAME": "X11"})
        self.assertEqual((s[0]["id"], s[0]["driver"], s[0]["legacy"]), ("x11", "supermicro", False))

    def test_loaded(self):
        self.assertEqual(list(SERVERS)[:2], ["rack-a", "rack-b"])
        self.assertEqual(SERVERS["rack-a"].driver.kind, "demo")


class Validation(unittest.TestCase):
    def test_accepts(self):
        ok = validate_server({"name": "DL360", "driver": "redfish", "host": "10.0.0.9",
                              "username": "Administrator", "password": "pw"})
        self.assertEqual((ok["password"], ok["verify_tls"]), ("pw", False))
        self.assertEqual(validate_server({"name": "Renamed"}, ok)["password"], "pw")  # edit keeps the password
        self.assertEqual(validate_server({"name": "Demo", "driver": "demo"})["host"], "")
        for driver, host in (("redfish", "fe80::1"), ("dell", "fe80::1"), ("redfish", "[fe80::1]:443"),
                             ("redfish", "10.0.0.5:8443"), ("dell", "local")):
            validate_server({"name": "n", "driver": driver, "host": host, "username": "u", "password": "p"})

    def test_rejects(self):
        for bad in ({"name": "", "driver": "dell", "host": "h", "username": "u", "password": "p"},
                    {"name": "x", "driver": "nope", "host": "h", "username": "u", "password": "p"},
                    {"name": "x", "driver": "dell", "host": "h:70000", "username": "u", "password": "p"},
                    {"name": "x", "driver": "redfish", "host": "https://h/x", "username": "u", "password": "p"},
                    {"name": "x", "driver": "redfish", "host": "local", "username": "u", "password": "p"},
                    {"name": "x", "driver": "dell", "host": "-oProxyCommand=x", "username": "u", "password": "p"},
                    {"name": "x", "driver": "dell", "host": "h", "username": "u"},
                    {"name": "x", "driver": "dell", "host": "h", "username": "", "password": "p"},
                    {"name": "x", "driver": "redfish", "host": "h", "username": "u", "password": "p", "verify_tls": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_server(bad)


class Persistence(unittest.TestCase):
    def test_1x_settings_are_converted(self):
        srv = SERVERS["rack-a"]
        write_json(srv.settings_file, {"mode": "dell"})
        self.assertEqual(srv.settings()["mode"], "auto")
        srv.settings_file.unlink()

    def test_history_round_trip(self):
        a = SERVERS["rack-a"]
        a.cycle()
        a.save_history()
        copy = Server({"id": "rack-a", "name": "Rack A", "driver": "demo"})
        self.assertTrue(copy.load_history())
        self.assertEqual(copy.history[-1]["t"], a.history[-1]["t"])


class SmartMemory(unittest.TestCase):
    def test_learned_map_is_kept_and_can_be_forgotten(self):
        srv = Server({"id": "learner", "name": "Learner", "driver": "demo"})
        srv.learned.update({"20": 25.0, "24": 40.0})
        srv.save_history()
        copy = Server({"id": "learner", "name": "Learner", "driver": "demo"})
        self.assertEqual(copy.learned, {"20": 25.0, "24": 40.0})
        copy.forget_learned("test")
        self.assertEqual(Server({"id": "learner", "name": "Learner", "driver": "demo"}).learned, {})

    def test_smart_cycle_reports_what_it_does(self):
        srv = Recorder("smart")
        write_json(srv.settings_file, {"mode": "smart", "smart_target": 60})
        srv.cycle()
        self.assertTrue(srv.state["reason"].startswith("smart: "))
        self.assertEqual(srv.state["smart"]["sensor"], "CPU")
        self.assertEqual(srv.calls, [srv.state["applied_speed"]])
        write_json(srv.settings_file, {"mode": "fixed"})
        srv.cycle()
        self.assertIsNone(srv.state["smart"])
        srv.settings_file.unlink()


class Checks(unittest.TestCase):
    def test_failed_fans(self):
        from fanctl.control import failed_fans
        spin = lambda n, rpm: {"name": n, "rpm": rpm, "pct": None, "ok": True}  # noqa: E731
        self.assertEqual(failed_fans([spin("A", 4000), spin("B", 0), spin("C", 4100)]), ["B"])
        self.assertEqual(failed_fans([spin("A", 200), spin("B", 250)]), [])        # all slow: a quiet server
        self.assertEqual(failed_fans([{"name": "P", "rpm": None, "pct": 0, "ok": True},
                                      {"name": "Q", "rpm": None, "pct": 30, "ok": True}]), ["P"])
        self.assertEqual(failed_fans([dict(spin("A", 4000), ok=False)]), ["A"])

    def test_ignored_commands_are_noticed(self):
        import time as t
        srv = Recorder("ignored")
        fans = lambda rpm: [{"name": "F", "rpm": rpm, "pct": None, "ok": True}]  # noqa: E731
        with srv.lock:
            srv.check_response("manual", 20, fans(4000), {})
            srv.check_response("manual", 60, fans(4000), {})   # raised by 40 points
            srv.probe["t"] -= 31
            srv.check_response("manual", 60, fans(4050), {})   # 30 s later the RPM has not moved
        self.assertTrue(srv.ignored)
        self.assertIn("did not follow", srv.events[0]["msg"])
        with srv.lock:
            srv.check_response("manual", 30, fans(4050), {})
            srv.probe["t"] = t.time() - 31
            srv.check_response("manual", 30, fans(2500), {})   # this time it followed
        self.assertFalse(srv.ignored)


class Scheduled(unittest.TestCase):
    def test_profile_caps_the_speed(self):
        srv = Recorder("scheduled")
        always = {"name": "Night", "days": list(range(7)), "start": "00:00", "end": "00:00", "max_speed": 25, "smart_target": None}
        write_json(srv.settings_file, {"mode": "fixed", "fixed_speed": 50, "schedule": [always]})
        srv.cycle()
        self.assertEqual(srv.calls, [25])
        self.assertTrue(srv.state["reason"].endswith(", Night cap 25%"))
        srv.settings_file.unlink()


class Durability(unittest.TestCase):
    def test_corrupt_settings_fall_back_to_automatic(self):
        srv = Recorder("corrupt")
        srv.settings_file.write_text('{"mode": "fixed", "fixed_spe')       # power cut mid-write
        self.assertEqual(srv.settings()["mode"], "auto")
        self.assertTrue(list(srv.settings_file.parent.glob(srv.settings_file.name + ".corrupt-*")))
        self.assertIn("corrupt", srv.events[0]["msg"])
        self.assertEqual(srv.settings()["mode"], "auto")                    # and it stays automatic
        srv.settings_file.unlink()

    def test_invalid_settings_fall_back_to_automatic(self):
        srv = Recorder("invalid")
        write_json(srv.settings_file, {"mode": "fixed", "fixed_speed": "fast"})
        self.assertEqual(srv.settings()["mode"], "auto")
        srv.settings()
        self.assertEqual(sum("invalid" in e["msg"] for e in srv.events), 1)  # said once, not every cycle
        srv.settings_file.unlink()

    def test_concurrent_writes_never_collide(self):
        import threading
        from fanctl.config import DATA_DIR
        target, errors = DATA_DIR / "race.json", []

        def writer(n):
            try:
                for i in range(30):
                    write_json(target, {"writer": n, "i": i, "pad": "x" * 2000})
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        import json
        self.assertEqual(json.loads(target.read_text())["i"], 29)
        self.assertEqual(list(DATA_DIR.glob("race.json.*.tmp")), [])           # nothing left behind
        target.unlink()


class Recorder(Server):
    """A demo server whose fan commands are recorded."""
    def __init__(self, sid):
        super().__init__({"id": sid, "name": sid, "driver": "demo"})
        self.calls = []
        drv = self.driver
        drv.set_auto = lambda: self.calls.append("auto")
        drv.set_speed = lambda pct: self.calls.append(pct)


def reading(temps, fans=None, power="on"):
    """A BMC reading: temps as (name, value, ok)."""
    return {"temps": [{"name": n, "entity": "", "value": v, "cpu": n.startswith("CPU"), "ok": ok, "warn": None}
                      for n, v, ok in temps],
            "fans": fans or [{"name": f"Fan{i}", "rpm": 4000, "pct": None, "ok": True} for i in range(1, 4)],
            "watts": 150, "power": power, "inlet": 22, "exhaust": None}


class Scripted(Recorder):
    """A recorded server whose BMC answers what the test says."""
    def __init__(self, sid, settings=None):
        super().__init__(sid)
        self.answers = []
        self.driver.read = self.next_reading
        write_json(self.settings_file, settings or {"mode": "fixed", "fixed_speed": 30})

    def next_reading(self):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return json.loads(json.dumps(answer))  # a fresh copy each time, as a real read gives


class FailSafe(unittest.TestCase):
    """When the controller does not know, the BMC decides."""
    OK = [("CPU 1", 50, True), ("CPU 2", 48, True), ("Inlet Temp", 22, True)]

    def tearDown(self):
        for f in DATA.glob("settings-fs-*.json"):
            f.unlink()

    def test_no_cpu_reading_means_automatic(self):
        srv = Scripted("fs-nocpu")
        srv.answers = [reading([("Inlet Temp", 22, True), ("DIMM", 35, True)])]
        srv.cycle()
        self.assertEqual((srv.calls, srv.state["effective"]), (["auto"], "auto"))
        self.assertIn("no CPU", srv.state["reason"])

    def test_one_missed_reading_is_not_an_unreachable_bmc(self):
        from fanctl.drivers import DriverError
        srv = Scripted("fs-blip")
        srv.answers = [reading(self.OK)]
        srv.cycle()
        srv.answers = [DriverError("timed out"), reading(self.OK)]
        srv.cycle()                                   # one dropped reading: nothing said, nothing handed over
        srv.cycle()
        self.assertEqual((srv.calls[-1], srv.state["error"], srv.state["effective"]), (30, None, "manual"))
        self.assertNotIn("Cannot read", " ".join(e["msg"] for e in srv.events))

    def test_unreadable_bmc_gets_the_fans_back(self):
        from fanctl.drivers import DriverError
        srv = Scripted("fs-blind")
        srv.answers = [reading(self.OK)]
        srv.cycle()
        self.assertEqual(srv.calls, [30])
        srv.answers = [DriverError("timed out")]
        srv.cycle()
        srv.cycle()                                   # tried once a minute, not every cycle
        self.assertEqual(srv.calls, [30, "auto"])
        self.assertEqual(srv.state["effective"], "auto")

    def test_a_sensor_that_disappears_trips_the_failsafe(self):
        srv = Scripted("fs-missing")
        srv.answers = [reading(self.OK), reading(self.OK[:1] + self.OK[2:])]
        srv.cycle()
        srv.cycle()                                   # one missing reading is forgiven
        self.assertFalse(srv.state["failsafe"])
        srv.cycle()
        self.assertTrue(srv.state["failsafe"])
        self.assertEqual(srv.state["reason"], "CPU 2 stopped reporting")
        self.assertEqual(srv.calls[-1], "auto")

    def test_a_faulty_sensor_or_a_failed_fan_trips_it(self):
        srv = Scripted("fs-fault")
        srv.answers = [reading(self.OK[:2] + [("PCIe", 60, False)])]
        srv.cycle()
        self.assertEqual(srv.state["reason"], "PCIe reports a fault")
        fan = Scripted("fs-fan")
        dead = [{"name": "Fan1", "rpm": 0, "pct": None, "ok": True}] + [
            {"name": f"Fan{i}", "rpm": 4000, "pct": None, "ok": True} for i in (2, 3)]
        fan.answers = [reading(self.OK), reading(self.OK, dead)]   # Fan1 turned, then stopped
        fan.cycle()
        fan.cycle()
        fan.cycle()                                   # two readings to count as failed
        fan.cycle()
        self.assertEqual((fan.state["reason"], fan.calls[-1]), ("fan Fan1 failed", "auto"))

    def test_a_bay_that_never_turned_is_not_a_failed_fan(self):
        srv = Scripted("fs-bay")
        empty = [{"name": "Fan6", "rpm": 0, "pct": None, "ok": True}] + [
            {"name": f"Fan{i}", "rpm": 4000, "pct": None, "ok": True} for i in (1, 2)]
        srv.answers = [reading(self.OK, empty)]
        for _ in range(3):
            srv.cycle()
        self.assertFalse(srv.state["failsafe"])
        self.assertEqual(srv.calls[-1], 30)
        self.assertIn("taken as an empty bay", " ".join(e["msg"] for e in srv.events))
        broken = [dict(empty[0], ok=False)] + empty[1:]              # unless the BMC itself says it failed
        srv.answers = [reading(self.OK, broken)]
        srv.cycle()
        srv.cycle()
        srv.cycle()
        self.assertTrue(srv.state["failsafe"])

    def test_a_power_state_the_bmc_cannot_tell_is_the_last_one_known(self):
        srv = Scripted("fs-power")
        srv.driver.retake = lambda pct: srv.calls.append(("retake", pct))
        srv.answers = [reading(self.OK)]
        srv.cycle()
        srv.answers = [reading(self.OK, power=None)]                # "Chassis Power is unknown"
        srv.cycle()
        self.assertEqual((srv.state["power"], srv.calls[-1]), ("on", 30))  # not off, not a power-on

    def test_every_take_over_lets_go_and_takes_hold_again(self):
        srv = Scripted("fs-take")
        srv.driver.retake = lambda pct: srv.calls.append(("retake", pct))
        srv.answers = [reading(self.OK)]
        srv.cycle()
        srv.cycle()
        srv.answers = [reading([("CPU 1", 80, True)])]
        srv.cycle()                                                  # failsafe: the BMC has them
        srv.answers = [reading(self.OK)]
        srv.failsafe_since -= 301
        srv.cycle()                                                  # back: taken again, not just told
        self.assertEqual(srv.calls, [("retake", 30), 30, "auto", ("retake", 50)])

    def test_failsafe_holds_then_eases_down(self):
        srv = Scripted("fs-hold", {"mode": "fixed", "fixed_speed": 30, "ramp_down_seconds": 60})
        srv.answers = [reading([("CPU 1", 80, True)])]
        srv.cycle()
        self.assertTrue(srv.state["failsafe"])
        srv.answers = [reading([("CPU 1", 40, True)])]
        srv.cycle()                                   # cool at once, but the BMC keeps the fans for a while
        self.assertTrue(srv.state["failsafe"])
        self.assertIn("failsafe held", srv.state["reason"])
        srv.failsafe_since -= 301
        srv.cycle()
        self.assertFalse(srv.state["failsafe"])
        self.assertEqual(srv.calls[-1], 50)           # back to manual from 50 %, not straight to 30 %

    def test_a_fan_under_its_bmc_minimum_raises_the_floor(self):
        srv = Scripted("fs-rpmfloor", {"mode": "fixed", "fixed_speed": 10, "min_speed": 0})
        slow = [{"name": f"Fan{i}", "rpm": 500, "pct": None, "ok": True, "floor": 600} for i in (1, 2, 3)]
        srv.answers = [reading(self.OK, slow)]
        srv.cycle()                                   # 10 % sent
        srv.cycle()                                   # its reading: 500 RPM, under 600
        srv.cycle()                                   # twice: one odd reading is not it
        self.assertEqual(srv.rpm_floor, 15)
        srv.cycle()
        self.assertEqual(srv.calls[-1], 15)
        self.assertIn("BMC fan minimum 15%", srv.state["reason"])
        self.assertIn("under its BMC minimum of 600 RPM", srv.events[1]["msg"] + srv.events[0]["msg"])

    def test_vendor_floor(self):
        srv = Scripted("fs-floor", {"mode": "fixed", "fixed_speed": 20})
        srv.driver.min_speed, srv.driver.vendor = 25, "Supermicro"
        srv.answers = [reading(self.OK)]
        srv.cycle()
        self.assertEqual(srv.calls, [25])
        self.assertIn("Supermicro floor 25%", srv.state["reason"])


class Calibration(unittest.TestCase):
    OK = FailSafe.OK

    def tearDown(self):
        for f in list(DATA.glob("settings-cal-*.json")) + list(DATA.glob("calibration-cal-*.json")):
            f.unlink()

    def calibrate(self, srv, rpm_at, cpu=None, cycles=100):
        """Cycles until the calibration ends, each fan reading rpm_at(step speed, fan number)."""
        srv.answers = [reading(self.OK)]
        srv.cycle()
        srv.CALIB_SETTLE = 0  # no time passes in a test
        self.assertIsNone(srv.start_calibration("tester"))
        for n in range(cycles):
            if not srv.calibrating():
                break
            pct = srv.calib["steps"][srv.calib["i"]]
            temps = [("CPU 1", cpu(n) if cpu else 50, True)] + self.OK[1:]
            srv.answers = [reading(temps, [{"name": f"Fan{i}", "rpm": rpm_at(pct, i), "pct": None, "ok": True}
                                           for i in (1, 2, 3)])]  # the same fans as before it started
            srv.cycle()
        return srv.calib

    def test_measures_every_step_and_suggests_a_minimum(self):
        srv = Scripted("cal-full")
        c = self.calibrate(srv, lambda pct, i: max(2000, 1000 + 50 * pct))  # under 20 % no slower
        self.assertEqual(c["status"], "done")
        self.assertEqual([r["pct"] for r in c["results"]], list(Server.CALIB_STEPS))
        self.assertEqual(c["results"][0]["fans"], {"Fan1": 6000, "Fan2": 6000, "Fan3": 6000})
        self.assertEqual((c["floor"], c["suggested"], c["fan_watts"]), (20, 20, 0))  # the power draw held
        self.assertEqual(sorted(set(srv.calls)), [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100])  # every step sent
        srv.answers = [reading(self.OK)]
        srv.cycle()
        self.assertEqual(srv.calls[-1], 30)                            # then the settings again
        self.assertEqual(Server({"id": "cal-full", "name": "x", "driver": "demo"}).calib["suggested"], 20)  # kept

    def test_a_fan_that_stops_trips_the_failsafe_and_the_steps_above_still_count(self):
        srv = Scripted("cal-stall")
        c = self.calibrate(srv, lambda pct, i: 0 if i == 2 and pct < 10 else 2000 + 40 * pct)
        self.assertEqual((c["status"], c["at"]), ("stopped", 5))
        self.assertEqual(c["reason"], "the failsafe took over (fan Fan2 failed)")
        self.assertEqual((c["suggested"], srv.calls[-1]), (10, "auto"))

    def test_never_goes_under_a_fans_bmc_minimum(self):
        srv = Scripted("cal-floor")
        rpm = lambda pct, i: 1000 + 50 * pct                                # 2000 at 20 %, 1750 at 15 %
        fans = lambda pct: [{"name": f"Fan{i}", "rpm": rpm(pct, i), "pct": None, "ok": True, "floor": 1600}
                            for i in (1, 2)]
        srv.answers = [reading(self.OK, fans(30))]
        srv.cycle()
        srv.CALIB_SETTLE = 0
        self.assertIsNone(srv.start_calibration())
        for _ in range(100):
            if not srv.calibrating():
                break
            srv.answers = [reading(self.OK, fans(srv.calib["steps"][srv.calib["i"]]))]
            srv.cycle()
        c = srv.calib
        self.assertEqual((c["status"], c["results"][-1]["pct"]), ("done", 15))  # 1750 RPM: within 15 % of 1600
        self.assertIn("close to its BMC minimum of 1600 RPM", c["reason"])
        self.assertNotIn(10, srv.calls)                                     # 1500 RPM was never asked for

    def test_stops_when_the_cpu_warms(self):
        srv = Scripted("cal-warm")
        c = self.calibrate(srv, lambda pct, i: 2000 + 40 * pct, cpu=lambda n: 50 + n)
        self.assertEqual((c["status"], c["reason"]), ("stopped", "the CPU warmed by 10°C"))
        self.assertIsNone(c["suggested"])
        srv.answers = [reading(self.OK)]
        srv.cycle()
        self.assertEqual(srv.calls[-1], 30)

    def test_refused_when_it_cannot_be_trusted(self):
        dry = Scripted("cal-dry", {"mode": "fixed", "fixed_speed": 30, "dry_run": True})
        dry.answers = [reading(self.OK)]
        dry.cycle()
        self.assertIn("dry run", dry.start_calibration())
        hot = Scripted("cal-hot")
        hot.answers = [reading([("CPU 1", 70, True)])]
        hot.cycle()
        self.assertIn("10°C or more below the failsafe", hot.start_calibration())
        self.assertFalse(hot.calibrating())

    def test_a_restart_in_the_middle_counts_as_stopped(self):
        write_json(DATA / "calibration-cal-cut.json", {"status": "running", "results": [], "samples": [{}]})
        c = Server({"id": "cal-cut", "name": "x", "driver": "demo"}).calib
        self.assertEqual((c["status"], c["reason"]), ("stopped", "Fan Control restarted"))


class TakingBack(unittest.TestCase):
    """Issue #3: an R420 (iDRAC 7) took the fans back while the host started and ignored the manual
    mode re-sent every cycle; the fans stayed on the iDRAC's control for 30 hours unnoticed."""
    OK = FailSafe.OK

    def tearDown(self):
        for f in DATA.glob("settings-tb-*.json"):
            f.unlink()

    def server(self, sid):
        srv = Scripted(sid)
        srv.FOLLOW_STEADY = 0  # no time passes in a test
        srv.driver.retake = lambda pct: srv.calls.append(("retake", pct))
        return srv

    @staticmethod
    def fans(rpm):
        return [{"name": f"Fan{i}", "rpm": rpm, "pct": None, "ok": True} for i in (1, 2, 3)]

    def test_taken_back_on_start_and_when_the_host_powers_on(self):
        srv = self.server("tb-power")
        srv.answers = [reading(self.OK)]
        srv.cycle()
        self.assertEqual(srv.calls, [("retake", 30)])                   # on start, whatever the BMC did before
        srv.cycle()
        self.assertEqual(srv.calls[-1], 30)                              # then the usual command
        srv.answers = [reading(self.OK, power="off")]
        srv.cycle()
        srv.answers = [reading(self.OK)]
        srv.cycle()
        self.assertEqual(srv.calls[-2:], ["auto", ("retake", 30)])
        self.assertIn("Host powered on", srv.events[1]["msg"] + srv.events[0]["msg"])
        self.assertEqual(len(srv.retakes), 2)                            # and twice more while it starts
        srv.retakes = [0, 0]
        srv.cycle()
        self.assertEqual((srv.calls[-1], srv.retakes), (("retake", 30), []))

    def test_fans_that_stop_following_are_noticed_and_taken_back(self):
        srv = self.server("tb-follow")
        srv.answers = [reading(self.OK, self.fans(3000))]
        srv.cycle()
        self.assertEqual(srv.rpm_at, {})                                 # nothing learned right after a take-back
        srv.retook -= srv.RETAKE_EVERY                                   # time passes
        for _ in range(3):
            srv.cycle()
        self.assertEqual((srv.rpm_at, srv.state["following"]), ({30: 3000}, True))
        srv.answers = [reading(self.OK, self.fans(5200))]                # the iDRAC's own idle speed
        for _ in range(srv.FOLLOW_AFTER):
            srv.cycle()
        self.assertTrue(srv.not_following)
        self.assertFalse(srv.state["following"])
        self.assertIn("the BMC seems to have taken them back", srv.events[0]["msg"])
        self.assertEqual(srv.state["effective"], "manual")               # the BMC has them: safe, no failsafe
        srv.retook = 0
        srv.cycle()
        srv.cycle()
        self.assertIn(("retake", 30), srv.calls[-2:])                    # taken back, without waiting
        srv.answers = [reading(self.OK, self.fans(3050))]
        srv.cycle()
        self.assertFalse(srv.not_following)
        self.assertEqual(srv.events[0]["msg"], "The fans follow Fan Control again")

    def test_smart_mode_learns_nothing_while_the_bmc_has_the_fans(self):
        import fanctl.server as server_module
        srv = self.server("tb-smart")
        write_json(srv.settings_file, {"mode": "smart", "smart_target": 60})
        srv.answers = [reading([("CPU 1", 60, True)])]
        real = server_module.smart_step
        server_module.smart_step = lambda memory, learned, settings, *a: (  # learns unless told it is a dry run
            None if settings["dry_run"] else learned.__setitem__(5, 30), (30, {"reason": "smart"}))[1]
        try:
            srv.not_following = True
            srv.cycle()
            self.assertEqual(srv.learned, {})                            # what it learned went nowhere
            srv.not_following = False
            srv.cycle()
            self.assertEqual(srv.learned, {5: 30})                       # and does once they follow
        finally:
            server_module.smart_step = real

    def test_the_calibration_is_what_a_speed_should_give(self):
        srv = self.server("tb-cal")
        srv.calib = {"status": "done", "results": [{"pct": 40, "rpm": 6000}, {"pct": 20, "rpm": 4000}]}
        self.assertEqual((srv.expected_rpm(30), srv.expected_rpm(20), srv.expected_rpm(50)), (5000, 4000, None))
        srv.calib, srv.rpm_at = None, {20: 4000, 30: 5000, 60: 9000}
        self.assertEqual((srv.expected_rpm(25), srv.expected_rpm(45)), (4500, None))  # never across a wide gap


class HandOver(unittest.TestCase):
    def tearDown(self):
        for f in list(DATA.glob("settings-ho-*.json")) + list(DATA.glob("handover-ho-*.json")):
            f.unlink()

    def test_the_bmc_has_the_fans_until_the_time_is_up(self):
        import time
        srv = Scripted("ho-a")
        srv.answers = [reading(FailSafe.OK)]
        srv.cycle()
        srv.hand_over(60, "tester")
        srv.cycle()
        self.assertEqual((srv.calls[-1], srv.state["effective"], srv.state["failsafe"]), ("auto", "auto", False))
        self.assertIn("handed to the BMC until", srv.state["reason"])
        self.assertIsNotNone(Server({"id": "ho-a", "name": "x", "driver": "demo"}).handed_until)  # a restart keeps it
        self.assertIn("handed to the BMC", srv.start_calibration())
        srv.handed_until = time.time() - 1                            # the hour is up
        srv.cycle()
        self.assertEqual(srv.calls[-1], 30)
        self.assertIn("The hand-over to the BMC ended; control resumes", [e["msg"] for e in srv.events])

    def test_taken_back_early(self):
        srv = Scripted("ho-b")
        srv.answers = [reading(FailSafe.OK)]
        srv.hand_over(240)
        srv.hand_over(0, "tester")
        srv.cycle()
        self.assertEqual((srv.handed_until, srv.calls[-1]), (None, 30))
        self.assertFalse(srv.handed_file.exists())


class Shutdown(unittest.TestCase):
    def test_all_servers_released_together(self):
        import time as t
        from fanctl.server import release_all
        slow, fast = Recorder("slow"), Recorder("fast")
        slow.driver.set_auto = lambda: (t.sleep(2), slow.calls.append("auto"))
        start = t.time()
        self.assertEqual(release_all([slow, fast], deadline=10), [])
        self.assertLess(t.time() - start, 3.5)        # in parallel: one slow BMC doesn't delay the rest
        self.assertEqual((slow.calls, fast.calls), (["auto"], ["auto"]))
        self.assertTrue(slow.stop.is_set() and fast.stop.is_set())

    def test_a_stuck_loop_does_not_block_the_release(self):
        srv = Recorder("stuck")
        srv.cmd_lock.acquire()                        # a loop stuck mid-command
        try:
            self.assertTrue(srv.release(timeout=0.2))
        finally:
            srv.cmd_lock.release()
        self.assertEqual(srv.calls, ["auto"])


class DryRun(unittest.TestCase):
    def test_decides_but_sends_nothing(self):
        srv = Recorder("dry")
        write_json(srv.settings_file, {"mode": "fixed", "fixed_speed": 30, "dry_run": True})
        srv.cycle()
        srv.cycle()
        self.assertEqual(srv.calls, ["auto"])       # handed to the BMC once, then nothing
        self.assertTrue(srv.state["dry_run"])
        self.assertEqual(srv.state["applied_speed"], 30)
        self.assertIn("Dry run: would set fans to 30%", srv.events[0]["msg"])
        write_json(srv.settings_file, {"mode": "fixed", "fixed_speed": 30})
        srv.cycle()
        self.assertEqual(srv.calls, ["auto", 30])
        srv.settings_file.unlink()


class Stopping(unittest.TestCase):
    def test_no_command_after_release(self):
        import threading
        import time as _time
        srv = Recorder("slow")
        reading = threading.Event()
        real_read = srv.driver.read

        def slow_read():
            reading.set()
            _time.sleep(1)
            return real_read()

        srv.driver.read = slow_read
        t = threading.Thread(target=srv.cycle)
        t.start()
        reading.wait()
        srv.stop.set()
        srv.release()                 # while the cycle is still reading
        t.join()
        self.assertEqual(srv.calls, ["auto"])  # the late cycle sent nothing after the release


class LongHistory(unittest.TestCase):
    def test_five_minute_buckets(self):
        srv = Server({"id": "long", "name": "long", "driver": "demo"})
        for i in range(61):  # 0..300 s, every 5 s
            srv.record({"t": 1000 + i * 5, "cpu": 50 + (i % 2), "speed": 20 if i < 40 else None,
                        "inlet": 22, "exhaust": 35, "rpm": 4000, "fanpct": None, "watts": 150})
        self.assertEqual(len(srv.long), 1)
        p = srv.long[0]
        self.assertEqual((p["t"], p["cpu_max"], p["speed"], p["watts"]), (1300, 51, 20, 150))
        self.assertAlmostEqual(p["cpu"], 50.5, delta=0.05)

    def test_mostly_automatic_bucket_has_no_speed(self):
        from fanctl.control import aggregate
        pts = [{"t": i, "cpu": 50, "speed": None if i < 7 else 30} for i in range(10)]
        self.assertIsNone(aggregate(pts)["speed"])


class HostSensors(unittest.TestCase):
    def tearDown(self):
        for f in DATA.glob("settings-hs-*.json"):
            f.unlink()

    def test_pushed_temperatures_count_and_a_silent_one_trips_the_failsafe(self):
        import time
        srv = Scripted("hs-gpu")
        srv.answers = [reading(FailSafe.OK)]
        srv.push_sensors([{"name": "GPU0", "value": 60, "warn": 85}], time.time())
        srv.cycle()
        gpu = [t for t in srv.state["sensors"]["temps"] if t["name"] == "host: GPU0"]
        self.assertEqual((gpu[0]["value"], gpu[0]["warn"], gpu[0]["cpu"]), (60.0, 85.0, False))
        srv.push_sensors([{"name": "GPU0", "value": 83, "warn": 85}], time.time())
        srv.cycle()
        self.assertTrue(srv.state["failsafe"])                       # near its threshold: the BMC takes over
        srv.failsafe_since -= 301
        srv.pushed["GPU0"]["t"] -= 1000                               # the host stopped sending
        for _ in range(3):
            srv.cycle()
        self.assertEqual(srv.state["reason"], "host: GPU0 stopped reporting")
        srv.forget_pushed("tester")
        srv.failsafe_since -= 301
        srv.cycle()
        self.assertFalse(srv.state["failsafe"])

    def test_nonsense_is_refused(self):
        srv = Scripted("hs-bad")
        for bad in ([{"name": "x", "value": 500}], [{"name": "", "value": 40}], "GPU", [{"name": "x", "value": 40, "warn": 5}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                srv.push_sensors(bad, 0)


class ComposeOverrides(unittest.TestCase):
    def test_an_edited_compose_server_comes_back_edited(self):
        import fanctl.server as srvmod
        env = [{"id": "cmp", "name": "From compose", "driver": "demo", "host": "", "source": "environment"}]
        saved = [{"id": "cmp", "name": "Edited here", "driver": "demo", "host": "", "overrides": True},
                 {"id": "gone", "name": "Was compose", "driver": "demo", "host": "", "overrides": True}]
        real = srvmod.env_servers, srvmod.dashboard_servers, dict(SERVERS)
        srvmod.env_servers, srvmod.dashboard_servers = (lambda: [dict(c) for c in env]), (lambda: [dict(r) for r in saved])
        SERVERS.clear()
        try:
            srvmod.load_registry()
            self.assertEqual((SERVERS["cmp"].name, SERVERS["cmp"].cfg["source"]), ("Edited here", "override"))
            self.assertEqual(SERVERS["gone"].cfg["source"], "dashboard")  # its compose entry is gone: its own server
            self.assertNotIn("overrides", SERVERS["gone"].cfg)
        finally:
            srvmod.env_servers, srvmod.dashboard_servers = real[0], real[1]
            SERVERS.clear()
            SERVERS.update(real[2])
