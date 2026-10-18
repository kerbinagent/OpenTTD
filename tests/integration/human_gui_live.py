"""Opt-in real SDL human route acceptance, using only its owned X11 window.

Requires Linux X11 (including WSLg), Pillow, ImageMagick ``import``, OpenGFX and
an integration binary. No display focus, global pointer movement, engine test
hook or gameplay API is used. Native console commands only position the camera.

Isolated fixture run::

    OPENTTD_TEST_BINARY=/path/to/openttd OPENTTD_TEST_BASESET=/path/to/baseset \
      python3 tests/integration/human_gui_live.py --port 15979

To participate in an existing regression-fixture world, pass a private metadata
JSON containing world_id, world_epoch, engine_instance_id, socket_path and port::

    ... python3 tests/integration/human_gui_live.py --external /tmp/world.json --hold

External mode never starts/stops that world or changes its configuration. It
requires company 0 to allow native joining, have no vehicles, and the route site to be unused. The
route remains running afterwards; --hold keeps only this test's native client
open until Enter or EOF. Artifacts are retained under /tmp in both modes.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from PIL import Image, ImageChops, ImageStat
from engine_harness import LiveWorld


class ObservedWorld:
    """A fenced, read-only observer connection to an independently owned world."""

    completed = staticmethod(LiveWorld.completed)
    observe = LiveWorld.observe

    def __init__(self, metadata):
        self.world_id = metadata["world_id"]
        self.epoch = metadata["world_epoch"]
        self.instance = metadata["engine_instance_id"]
        self.socket_path = Path(metadata["socket_path"])
        self.port = metadata["port"]
        self.calls = []
        hello = self.rpc("hello")
        if hello.get("engine_instance_id") != self.instance:
            raise AssertionError("External world restarted; obtain fresh metadata")
        self.actor = {"identity_id": "human-gui-readonly-observer", "run_id": uuid.uuid4().hex,
                      "company_id": 0, "mode": "text"}
        self.completed(self.rpc("session.bind", actor=self.actor))

    def rpc(self, kind, **fields):
        if kind not in {"hello", "session.bind", "observation.query", "chat.roster"}:
            raise AssertionError(f"Human GUI observer forbids mutating RPC {kind}")
        self.calls.append(kind)
        return LiveWorld.rpc(self, kind, **fields)

    def log_tail(self):
        return "External world lifecycle is not owned by this test"


class KeyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
                ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
                ("state", ctypes.c_uint), ("keycode", ctypes.c_uint),
                ("same_screen", ctypes.c_int)]


class MotionEvent(ctypes.Structure):
    _fields_ = KeyEvent._fields_[:-2] + [("is_hint", ctypes.c_char), ("same_screen", ctypes.c_int)]


class Event(ctypes.Union):
    _fields_ = [("key", KeyEvent), ("motion", MotionEvent), ("pad", ctypes.c_long * 24)]


class OwnedWindow:
    """Send events and capture pixels only after rechecking the owned PID/XID."""

    def __init__(self, process, directory, window_class):
        self.process, self.directory = process, directory
        self.window_class = window_class.encode()
        self.window = None
        self.x = ctypes.CDLL(ctypes.util.find_library("X11"))
        x = self.x
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XOpenDisplay.restype = ctypes.c_void_p
        self.display = x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("No X11 display; SDL GUI acceptance is unsupported here")
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        self.root = x.XDefaultRootWindow(self.display)
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XInternAtom.restype = ctypes.c_ulong
        self.pid_atom = x.XInternAtom(self.display, b"_NET_WM_PID", False)
        self.class_atom = x.XInternAtom(self.display, b"WM_CLASS", False)
        x.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)), ctypes.POINTER(ctypes.c_uint)]
        x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XFlush.argtypes = [ctypes.c_void_p]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                ctypes.c_long, ctypes.POINTER(Event)]
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XKeysymToKeycode.restype = ctypes.c_uint

    def pid(self, window):
        kind, count, remaining = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        fmt = ctypes.c_int()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x.XGetWindowProperty(self.display, window, self.pid_atom,
            0, 1, False, 0, ctypes.byref(kind), ctypes.byref(fmt), ctypes.byref(count),
            ctypes.byref(remaining), ctypes.byref(data))
        try:
            if status == 0 and fmt.value == 32 and count.value:
                return ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0]
            return None
        finally:
            if data:
                self.x.XFree(data)

    def own_class(self, window):
        # PID namespaces can reuse numeric PIDs across unrelated SDL clients.
        # Require this launch's random WM_CLASS as well as its PID.
        kind, count, remaining = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        fmt = ctypes.c_int()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x.XGetWindowProperty(self.display, window, self.class_atom,
            0, 128, False, 0, ctypes.byref(kind), ctypes.byref(fmt), ctypes.byref(count),
            ctypes.byref(remaining), ctypes.byref(data))
        try:
            return status == 0 and fmt.value == 8 and self.window_class in ctypes.string_at(data, count.value).split(b"\0")
        finally:
            if data:
                self.x.XFree(data)

    def find(self):
        stack = [self.root]
        while stack:
            window = stack.pop()
            if self.pid(window) == self.process.pid and self.own_class(window):
                self.window = window
                return True
            root, parent, count = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_uint()
            children = ctypes.POINTER(ctypes.c_ulong)()
            if self.x.XQueryTree(self.display, window, ctypes.byref(root),
                    ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count)):
                stack.extend(children[i] for i in range(count.value))
                if children:
                    self.x.XFree(children)
        return False

    def verify(self):
        if self.process.poll() is not None:
            raise AssertionError("Owned SDL client exited")
        if not self.window or self.pid(self.window) != self.process.pid or not self.own_class(self.window):
            raise AssertionError("Owned native window disappeared or changed PID")

    def event(self, kind, x=0, y=0, state=0, detail=0):
        self.verify()
        event = Event()
        k = event.key
        k.type, k.send_event = kind, True
        k.display, k.window, k.root = self.display, self.window, self.root
        k.x, k.y, k.x_root, k.y_root = x, y, x, y
        k.state, k.keycode, k.same_screen = state, detail, True
        mask = {2: 1, 3: 2, 4: 4, 5: 8, 6: 64}[kind]
        if not self.x.XSendEvent(self.display, self.window, False, mask, ctypes.byref(event)):
            raise RuntimeError("SDL/X11 rejected owned-window events; no fallback permitted")
        self.x.XFlush(self.display)

    def keycode(self, name):
        code = self.x.XKeysymToKeycode(self.display, self.x.XStringToKeysym(name.encode()))
        if not code:
            raise ValueError(f"Unknown X11 key {name}")
        return code

    def key(self, name, modifiers=()):
        state = 0
        masks = {"Shift_L": 1, "Control_L": 4, "Alt_L": 8}
        for modifier in modifiers:
            self.event(2, state=state, detail=self.keycode(modifier))
            state |= masks[modifier]
        self.event(2, state=state, detail=self.keycode(name))
        time.sleep(0.04)
        self.event(3, state=state, detail=self.keycode(name))
        for modifier in reversed(modifiers):
            self.event(3, state=state, detail=self.keycode(modifier))
            state &= ~masks[modifier]

    def click(self, x, y):
        self.event(6, x, y)
        time.sleep(0.06)
        self.event(4, x, y, detail=1)
        time.sleep(0.06)
        self.event(5, x, y, state=256, detail=1)

    def drag(self, start, end):
        self.event(6, *start)
        time.sleep(0.08)
        self.event(4, *start, detail=1)
        for step in range(1, 13):
            self.event(6, *(round(a + (b - a) * step / 12) for a, b in zip(start, end)), state=256)
            time.sleep(0.04)
        self.event(5, *end, state=256, detail=1)

    def camera(self, command):
        if command not in {"zoomto 2", "scrollto instant 40535"}:
            raise AssertionError("Only pinned camera console commands are permitted")
        self.key("grave")
        time.sleep(0.25)
        for character in command:
            self.key("space" if character == " " else character)
        self.key("Return")
        time.sleep(0.2)
        self.key("grave")

    def capture(self, name):
        self.verify()
        path = self.directory / f"{name}.png"
        subprocess.run(["import", "-window", str(self.window), str(path)],
                       check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with Image.open(path) as source:
            image = source.convert("RGB")
        if image.size != (1280, 800) or max(ImageStat.Stat(image).stddev) < 10:
            raise AssertionError("Owned SDL window has wrong size or is not rendering")
        return image

    def close(self):
        self.x.XCloseDisplay(self.display)


class HumanRoute:
    def __init__(self, world, window, timeout):
        self.world, self.window, self.timeout = world, window, timeout
        self.evidence = {"gameplay_rpc_mutations": 0, "input": "owned PID/XID XSendEvent",
                         "screenshots": [], "observations": {}, "stages": []}

    def wait(self, label, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.window.verify()
            value = predicate()
            if value:
                self.evidence["stages"].append({"name": label, "time": time.time()})
                print(label, flush=True)
                return value
            time.sleep(0.12)
        self.window.capture("failure")
        raise AssertionError(f"Timed out waiting for {label}; no gameplay fallback used")

    def screenshot(self, name):
        image = self.window.capture(name)
        self.evidence["screenshots"].append(f"{name}.png")
        return image

    def ui_change(self, name, action, region):
        before = self.window.capture("before-" + name).crop(region)
        action()
        def changed():
            after = self.window.capture(name).crop(region)
            difference = ImageChops.difference(before, after)
            return sum(ImageStat.Stat(difference).mean) > 12
        self.wait(name, changed, timeout=5)
        self.evidence["screenshots"].append(f"{name}.png")

    def tile(self, number):
        result = self.world.observe("map.region", {"x": number % 256, "y": number // 256,
                                                  "width": 1, "height": 1})
        return result["tiles"][0]

    def vehicles(self):
        return self.world.observe("vehicles", {"limit": 64})["items"]

    def orders(self):
        return self.world.observe("orders", {"vehicle_id": self.handle})

    def route_revenue(self, baseline):
        # Company income is attributable only while this is its sole vehicle.
        vehicles = self.vehicles()
        if len(vehicles) != 1 or vehicles[0]["vehicle_id"] != self.handle:
            raise AssertionError("Company 0 gained another vehicle; cannot attribute company revenue to the human route")
        company = self.world.observe("company")
        return company if self.income(company) > baseline else None

    @staticmethod
    def income(company):
        return company["income_this_quarter"] + sum(row["income"] for row in company["quarter_history"])

    def run(self):
        w = self.window
        self.evidence.update(world_id=self.world.world_id, world_epoch=self.world.epoch,
                             engine_instance_id=self.world.instance, window=hex(w.window), pid=w.process.pid)
        before_company = self.world.observe("company")
        before_vehicles = {json.dumps(row["vehicle_id"], sort_keys=True) for row in self.vehicles()}
        if before_vehicles:
            raise AssertionError("Company 0 must have no preexisting vehicles so revenue is attributable to this route")
        if before_company["company_id"] != 0:
            raise AssertionError("Human must own company 0")
        for tile in (40529, 40541, 40791):
            value = self.tile(tile)
            if value.get("depot") or value.get("station_id"):
                raise AssertionError(f"Fixture route site {tile} is occupied; refusing to modify it")
        self.evidence["observations"]["before_company"] = before_company
        self.screenshot("initial")
        w.click(6, 28)  # close native online-player window
        w.camera("zoomto 2")
        w.camera("scrollto instant 40535")
        self.screenshot("route-camera")
        self.ui_change("road-toolbar", lambda: w.key("F8", ("Shift_L",)), (678, 22, 985, 60))
        w.key("3")
        w.drag((800, 325), (480, 485))
        self.wait("main road built", lambda: all(self.tile(t).get("road_bits") for t in (40531, 40535, 40539)))
        self.screenshot("road-built")
        self.ui_change("depot-picker", lambda: w.key("5"), (678, 60, 816, 182))
        w.click(714, 104)  # northwest depot entrance
        w.click(672, 421)
        self.wait("depot built", lambda: self.tile(40791).get("depot_direction") == 3)
        self.screenshot("depot-built")
        self.ui_change("bus-stop-picker", lambda: w.key("6"), (817, 60, 884, 232))
        w.click(714, 160)  # southwest-facing terminus
        w.click(832, 309)
        self.wait("first passenger stop built", lambda: self.tile(40529).get("station_id"))
        self.screenshot("first-stop")
        w.click(780, 110)  # northeast-facing terminus
        w.click(448, 509)
        self.wait("second passenger stop built", lambda: self.tile(40541).get("station_id"))
        self.screenshot("second-stop")
        w.key("Escape")
        w.click(683, 29)
        self.ui_change("depot-window", lambda: w.click(672, 421), (0, 25, 340, 125))
        self.ui_change("buy-bus", lambda: w.click(71, 115), (341, 25, 580, 281))
        w.click(401, 275)
        def new_bus():
            return next((row for row in self.vehicles()
                         if json.dumps(row["vehicle_id"], sort_keys=True) not in before_vehicles
                         and row["vehicle_type"] == "road" and row["engine_id"] == 116), None)
        bus = self.wait("human bus purchased", new_bus)
        self.handle = bus["vehicle_id"]
        self.evidence["vehicle_id"] = self.handle
        built_company = self.world.observe("company")
        if not (built_company["balance"] < before_company["balance"]
                and built_company["loan"] == before_company["loan"]
                and built_company["infrastructure"]["road"] > before_company["infrastructure"]["road"]
                and built_company["infrastructure"]["station_tiles"] == before_company["infrastructure"]["station_tiles"] + 2):
            raise AssertionError("Native construction did not incur costs and add the intended infrastructure")
        self.evidence["observations"]["built_company"] = built_company
        self.screenshot("bus-purchased")
        w.click(346, 29)
        self.ui_change("orders-window", lambda: w.click(240, 183), (250, 138, 413, 241))
        # quick_goto is pinned true: opening an empty order list activates Go To.
        w.click(832, 309)
        self.wait("first order added", lambda: len(self.orders()["orders"]) == 1)
        w.click(448, 509)
        self.wait("second order added", lambda: len(self.orders()["orders"]) == 2)
        self.evidence["observations"]["orders_before_modification"] = self.orders()
        self.screenshot("orders-complete")
        w.key("Escape")
        w.click(18, 143)
        w.click(118, 234)
        self.wait("human bus started", lambda: next((v for v in self.vehicles()
                  if v["vehicle_id"] == self.handle and not v["stopped"]), None))
        self.screenshot("route-running")
        loaded = self.wait("passengers loaded", lambda: next((v for v in self.vehicles()
                  if v["vehicle_id"] == self.handle and any(p["load"] > 0 for p in v["parts"])), None),
                  timeout=self.timeout)
        self.evidence["observations"]["loaded_bus"] = loaded
        baseline_income = self.income(before_company)
        earned = self.wait("passenger gross revenue earned", lambda: self.route_revenue(baseline_income), timeout=self.timeout)
        self.evidence["observations"]["revenue_before_modification"] = earned
        self.ui_change("modify-orders-window", lambda: w.click(240, 183), (250, 138, 413, 241))
        w.click(140, 158)
        before_order = self.orders()["orders"][0]
        w.key("h")  # ordinary native order Non-stop hotkey
        modified = self.wait("human changed first order", lambda: (o if (o := self.orders())["orders"][0]["non_stop"] != before_order["non_stop"] else None))
        self.evidence["observations"]["orders_after_modification"] = modified
        self.screenshot("orders-modified")
        modification_income = self.income(self.world.observe("company"))
        self.evidence["income_baseline_after_modification"] = modification_income
        continued = self.wait("modified route earned further gross revenue", lambda: self.route_revenue(modification_income), timeout=self.timeout)
        self.evidence["observations"]["final_company"] = continued
        self.evidence["observations"]["final_bus"] = next(v for v in self.vehicles() if v["vehicle_id"] == self.handle)
        self.screenshot("modified-route-delivering")
        self.evidence["gross_revenue_delta"] = self.income(continued) - baseline_income
        self.evidence["result"] = "passed"
        path = w.directory / "human-gui-result.json"
        path.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(json.dumps({"result": "passed", "artifacts": str(w.directory),
                          "vehicle_id": self.handle, "gross_revenue_delta": self.evidence["gross_revenue_delta"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external", type=Path, help="Private metadata for an existing fixture world")
    parser.add_argument("--port", type=int, default=15979, help="Only for a new isolated world")
    parser.add_argument("--hold", action="store_true", help="Keep this owned SDL window until Enter/EOF")
    parser.add_argument("--route-timeout", type=float, default=180, help="Seconds allowed per cargo/revenue milestone")
    args = parser.parse_args()
    binary = Path(os.environ["OPENTTD_TEST_BINARY"]).resolve()
    baseset = Path(os.environ["OPENTTD_TEST_BASESET"]).resolve()
    if not binary.is_file() or not baseset.is_dir() or not shutil.which("import") or not os.environ.get("DISPLAY"):
        raise RuntimeError("Need binary, OpenGFX, ImageMagick import and X11 DISPLAY")
    directory = Path(tempfile.mkdtemp(prefix="ottd-human-gui-"))
    directory.chmod(0o700)
    owned_world = None
    client = window = log = scenario = None
    try:
        if args.external:
            world = ObservedWorld(json.loads(args.external.read_text()))
        else:
            os.environ["OPENTTD_TEST_KEEP"] = "1"
            owned_world = world = LiveWorld(binary, baseset, port=args.port)
            world.start()
            world.finish_recovery()
            world.actor = world.bind(0)
            access = world.rpc("company.access", company_id=0, allow_any=True, add_keys=[], remove_keys=[])
            if access.get("status") not in {"queued", "completed"}:
                raise AssertionError(access)
        data, config = directory / "data", directory / "config"
        (data / "openttd").mkdir(parents=True)
        (config / "openttd").mkdir(parents=True)
        (data / "openttd/baseset").symlink_to(baseset, target_is_directory=True)
        name = "GUIHuman-" + uuid.uuid4().hex[:8]
        window_class = "OpenTTD-Human-Acceptance-" + uuid.uuid4().hex
        (config / "openttd/openttd.cfg").write_text(
            f"[network]\nclient_name = {name}\n[gui]\nshow_finances = false\nquick_goto = true\n"
            "[misc]\ngui_scale = 100\nlanguage = english.lng\n")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("OPENTTD_INTEGRATION_")}
        environment.update(XDG_DATA_HOME=str(data), XDG_CONFIG_HOME=str(config),
                           SDL_VIDEO_X11_WMCLASS=window_class)
        if os.environ.get("OPENTTD_TEST_LIBRARY_PATH"):
            environment["LD_LIBRARY_PATH"] = os.environ["OPENTTD_TEST_LIBRARY_PATH"] + os.pathsep + environment.get("LD_LIBRARY_PATH", "")
        log = (directory / "client.log").open("wb", buffering=0)
        client = subprocess.Popen([str(binary), "-n", f"127.0.0.1:{world.port}#1", "-x",
            "-I", "OpenGFX", "-vsdl", "-snull", "-mnull", "-r1280x800", "-d", "net=3,desync=1"],
            cwd=directory, env=environment, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
        window = OwnedWindow(client, directory, window_class)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if client.poll() is not None:
                raise RuntimeError((directory / "client.log").read_text(errors="replace"))
            if window.find() and any(row["label"] == name and row["company_id"] == 0
                    for row in world.completed(world.rpc("chat.roster"))):
                break
            time.sleep(0.12)
        else:
            raise AssertionError("Native client never became a ready company 0 SDL window")
        scenario = HumanRoute(world, window, args.route_timeout)
        scenario.run()
        if args.hold:
            print("Owned SDL client remains open. Enter/EOF closes only this client.", flush=True)
            try:
                input()
            except EOFError:
                pass
    finally:
        if scenario and scenario.evidence.get("result") != "passed":
            (directory / "human-gui-failure.json").write_text(json.dumps(scenario.evidence, indent=2) + "\n")
        if window:
            window.close()
        if client and client.poll() is None:
            client.terminate()
            try:
                client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=3)
        if log:
            log.close()
        if owned_world:
            owned_world.close()
        print(f"Human GUI artifacts: {directory}", flush=True)


if __name__ == "__main__":
    main()
