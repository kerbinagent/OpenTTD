"""Pure fixed-profile launcher checks; no native, backend server or model process."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from backend.store import Store
from tools import mixed_world


class OvernightProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "openttd"
        self.binary.write_text("fixture executable\n")
        self.binary.chmod(0o700)
        self.baseset = self.root / "baseset"
        self.baseset.mkdir()
        (self.baseset / "opengfx.obg").write_text("fixture graphics\n")
        self.state = self.root / "world"

    def arguments(self, profile="overnight", *extra):
        return mixed_world.parse_args(["--binary", str(self.binary), "--baseset", str(self.baseset),
            "--state-dir", str(self.state), "--world", "overnight-test", "--transport", "http",
            "--profile", profile, *extra])

    def provision(self, args):
        self.state.mkdir(mode=0o700)
        previous = os.umask(0o077)
        try:
            return mixed_world.provision(self.state, args)
        finally:
            os.umask(previous)

    def test_overnight_provisions_seven_immutable_public_bindings_and_one_worker(self):
        args = self.arguments()
        self.assertEqual((args.map_size, args.seed), (512, 20261021))
        manifest = self.provision(args)
        self.assertEqual(manifest["profile"], "overnight")
        self.assertEqual(manifest["generation"], {"map_size": 512, "seed": 20261021,
                                                  "starting_year": 1950, "landscape": "temperate"})
        self.assertEqual(args.workers, {"human": "r0"})
        self.assertEqual(set(manifest["runs"]), set(args.roles))
        self.assertEqual(len(manifest["runs"]), 7)
        self.assertTrue((self.state / "r0/config/openttd/secrets.cfg").is_file())
        self.assertFalse((self.state / "r1").exists())
        store = Store(self.state / "world.sqlite3", world_id=args.world)
        try:
            for role, binding in args.roles.items():
                token_path = self.state / "credentials" / f"{role}.token"
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                principal = store.authenticate(token_path.read_text().strip())
                self.assertEqual(principal.run_id, manifest["runs"][role]["run_id"])
                self.assertEqual(principal.world_id, args.world)
                self.assertEqual((principal.identity_id, principal.company_id, principal.mode, principal.kind),
                                 (binding["identity_id"], binding["company_id"], binding["mode"], binding["kind"]))
        finally:
            store.close()
        self.assertEqual(args.roles["human"]["mode"], "visual")
        self.assertEqual({binding["company_id"] for binding in args.roles.values()}, set(range(7)))
        self.assertTrue(all(binding["identity_id"].startswith("overnight-") for binding in args.roles.values()))

    def test_generation_config_and_park_override_are_profile_scoped(self):
        overnight = self.arguments()
        manifest = self.provision(overnight)
        config = (self.state / "engine/config/openttd/openttd.cfg").read_text()
        self.assertIn("map_x = 9\nmap_y = 9", config)
        self.assertIn("starting_year = 1950\nlandscape = temperate", config)
        self.assertNotIn("max_lag_time = 2000", config)
        stack = mixed_world.Stack(self.state, overnight, manifest)
        stack.stop_requested = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stack.serve()
        self.assertIn("Matching native human client:", output.getvalue())
        self.assertIn("Overnight company IDs:", output.getvalue())
        self.assertIn("Only r0 is a native renderer", output.getvalue())
        self.assertNotIn("Luna companies:", output.getvalue())
        with mock.patch.dict(os.environ, {"OPENTTD_RENDERER_PARK_MS": "40000"}):
            self.assertNotIn("OPENTTD_RENDERER_PARK_MS", stack.environment("r0", renderer=True))
            mixed = self.arguments("mixed")
            mixed_stack = mixed_world.Stack(self.state, mixed, manifest)
            self.assertEqual(mixed_stack.environment("r0", renderer=True)["OPENTTD_RENDERER_PARK_MS"], "40000")
            luna = self.arguments("luna")
            luna_stack = mixed_world.Stack(self.state, luna, manifest)
            self.assertEqual(luna_stack.environment("r0", renderer=True)["OPENTTD_RENDERER_PARK_MS"], "40000")
        # The older profile defaults remain unchanged by the new generation mode.
        self.assertEqual((mixed.map_size, mixed.seed), (512, None))
        self.assertEqual((luna.map_size, luna.seed), (128, 20261018))

    def test_restart_accepts_only_matching_profile_and_rejects_initial_save(self):
        args = self.arguments()
        manifest = self.provision(args)
        manifest["ready"] = True
        manifest["renderer_keys"] = {"human": "a" * 64}
        mixed_world.publish_manifest(self.state, manifest)
        save = self.state / "engine-state" / "world.sav"
        mixed_world.write_new(save, "checkpoint fixture\n")
        mixed_world.write_new(self.state / "engine-state" / "ledger.json", json.dumps({"journal": {
            "world_id": args.world, "world_epoch": manifest["world_epoch"],
            "checkpoint": {"save_path": str(save), "checkpoint_id": "checkpoint-1"}}}))
        restarted, selected = mixed_world.load_existing(self.state, args)
        self.assertEqual((restarted["world_epoch"], selected), (manifest["world_epoch"], save))
        for other in ("mixed", "luna"):
            with self.subTest(other=other), self.assertRaises(mixed_world.StartupError):
                mixed_world.load_existing(self.state, self.arguments(other))
        initial = self.root / "initial.sav"
        initial.write_text("save")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.arguments("overnight", "--initial-save", str(initial))

    def test_agent_join_pool_is_opt_in_agent_only_and_restart_stable(self):
        args = self.arguments("mixed")
        manifest = self.provision(args)
        self.assertIsNone(mixed_world.agent_join_pool(self.state, args, manifest))
        self.assertFalse((self.state / "agent-join-pool.json").exists())
        credentials = {path.name: path.read_bytes() for path in (self.state / "credentials").iterdir()}
        args.agent_joins = True
        path = mixed_world.agent_join_pool(self.state, args, manifest)
        first = path.read_bytes()
        value = json.loads(first)
        self.assertEqual(value["world_epoch"], manifest["world_epoch"])
        self.assertEqual(set(value["runs"]), {manifest["runs"][role]["run_id"]
            for role in ("text-agent", "visual-agent")})
        self.assertNotIn(manifest["runs"]["human"]["run_id"], value["runs"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(mixed_world.agent_join_pool(self.state, args, manifest), path)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual({p.name: p.read_bytes() for p in (self.state / "credentials").iterdir()}, credentials)
        changed = {**manifest, "world_epoch": "other-world-incarnation"}
        with self.assertRaises(mixed_world.StartupError):
            mixed_world.agent_join_pool(self.state, args, changed)

    def test_public_admission_labels_only_new_agent_slots_neutrally(self):
        for profile in ("luna", "mixed", "overnight"):
            with self.subTest(profile=profile):
                self.state = self.root / ("public-" + profile)
                args = self.arguments(profile, "--agent-joins")
                bindings = {role: dict(binding) for role, binding in args.roles.items()}
                manifest = self.provision(args)
                store = Store(self.state / "world.sqlite3", world_id=args.world)
                try:
                    for role, binding in bindings.items():
                        principal = store.get_principal(manifest["runs"][role]["run_id"])
                        expected = ("Text agent" if binding["mode"] == "text" else "Image agent") if binding["kind"] == "agent" else binding["display_name"]
                        self.assertEqual(principal.display_name, expected)
                        for key in ("identity_id", "company_id", "mode", "kind"):
                            self.assertEqual(getattr(principal, key), binding[key])
                    self.assertEqual(args.roles, bindings)
                finally:
                    store.close()

    def test_nonpublic_luna_keeps_experiment_labels(self):
        args = self.arguments("luna")
        manifest = self.provision(args)
        store = Store(self.state / "world.sqlite3", world_id=args.world)
        try:
            for role, binding in args.roles.items():
                principal = store.get_principal(manifest["runs"][role]["run_id"])
                self.assertEqual(principal.display_name, binding["display_name"])
        finally:
            store.close()

    def test_public_profile_provisions_only_world_and_spectator_capacity(self):
        args = self.arguments("public")
        self.assertEqual(args.map_size, 512)
        self.assertTrue(args.agent_joins)
        self.assertEqual(args.roles, {})
        self.assertEqual(args.workers, {})
        self.assertEqual(args.capture_slots, {"capture-0": "rp0", "capture-1": "rp1"})
        manifest = self.provision(args)
        self.assertEqual(manifest["runs"], {})
        self.assertEqual(list((self.state / "credentials").iterdir()), [])
        self.assertIsNone(mixed_world.agent_join_pool(self.state, args, manifest))
        store = Store(self.state / "world.sqlite3", world_id=args.world)
        try:
            self.assertEqual(store.world_epoch(args.world), manifest["world_epoch"])
            for table in ("runs", "memberships", "credentials"):
                self.assertEqual(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        finally:
            store.close()
        self.assertEqual(self.arguments("public", "--capture-workers", "0").capture_slots, {})
        self.assertEqual(len(self.arguments("public", "--capture-workers", "15").capture_slots), 15)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.arguments("luna", "--capture-workers", "3")

    def test_public_start_never_creates_or_opens_company_for_renderer(self):
        args = self.arguments("public")
        manifest = self.provision(args)
        stack = mixed_world.Stack(self.state, args, manifest)
        commands, requests = {}, []
        checkpoint_reads = 0

        def spawn(name, command, **kwargs):
            commands[name] = command
            child = mock.Mock()
            child.poll.return_value = None
            child.stdin = io.BytesIO()
            stack.children[name] = child
            return child

        def rpc(kind, **fields):
            nonlocal checkpoint_reads
            requests.append(kind)
            result = {}
            if kind == "hello":
                result = {"role": "world"}
            elif kind == "checkpoint.status":
                checkpoint_reads += 1
                result = {"status": "completed", "load_verified": True, "recovery_required": False,
                          "checkpoint_id": "after" if checkpoint_reads >= 4 else "before"}
            elif kind == "roster":
                result = [{"public_key": str(i + 1) * 64, "company_id": 255} for i in range(2)]
            return {"status": "completed", "engine_instance_id": "native", "result": result}

        def renderer_engine(socket, **kwargs):
            number = 1 if Path(socket).stem == "rp0" else 2
            engine = mock.Mock()
            engine.call.return_value = {"world_id": args.world, "world_epoch": manifest["world_epoch"],
                                       "result": {"role": "renderer", "native_public_key": str(number) * 64}}
            return engine

        with (mock.patch.object(stack, "spawn", side_effect=spawn),
              mock.patch.object(stack, "rpc", side_effect=rpc),
              mock.patch("tools.mixed_world.UnixEngine", side_effect=renderer_engine),
              contextlib.redirect_stdout(io.StringIO())):
            stack.start()
        self.assertNotIn("company.ensure", requests)
        self.assertNotIn("company.access", requests)
        self.assertIn("--dynamic-agents", commands["backend"])
        self.assertNotIn("--agent-join-pool", commands["backend"])
        for name in ("rp0", "rp1"):
            self.assertIn(f"127.0.0.1:{args.native_port}#255", commands[name])
        config = json.loads((self.state / "renderer-config.json").read_text())
        self.assertEqual(config["version"], 2)
        self.assertEqual(set(config["slots"]), {"capture-0", "capture-1"})
        self.assertEqual(manifest["renderer_keys"], {})
        self.assertEqual(set(manifest["capture_slot_keys"]), {"capture-0", "capture-1"})

    def ready_legacy(self):
        args = self.arguments("luna", "--agent-joins")
        manifest = self.provision(args)
        manifest["ready"] = True
        manifest["renderer_keys"] = {role: str(i + 1) * 64 for i, role in enumerate(args.workers)}
        mixed_world.publish_manifest(self.state, manifest)
        save = self.state / "engine-state" / "world.sav"
        mixed_world.write_new(save, "checkpoint fixture\n")
        mixed_world.write_new(self.state / "engine-state" / "ledger.json", json.dumps({"journal": {
            "world_id": args.world, "world_epoch": manifest["world_epoch"],
            "checkpoint": {"save_path": str(save), "checkpoint_id": "checkpoint-1"}}}))
        legacy, _ = mixed_world.renderer_configuration(self.state, args, manifest)
        mixed_world.write_new(self.state / "renderer-config.json", json.dumps(legacy))
        return args, manifest, save, legacy

    def test_legacy_dynamic_extension_preserves_bindings_and_requires_recorded_mode(self):
        old, manifest, save, legacy = self.ready_legacy()
        preserved = {path: path.read_bytes() for path in self.state.rglob("*")
                     if path.is_file() and (path.suffix in (".token", ".cfg"))}
        bindings = json.loads(json.dumps({key: manifest[key] for key in ("runs", "renderer_keys")}))
        args = self.arguments("luna", "--dynamic-agents")
        upgraded, selected = mixed_world.load_existing(self.state, args)
        self.assertEqual(selected, save)
        self.assertEqual(upgraded, manifest)
        self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
        stack = mixed_world.Stack(self.state, args, upgraded)
        commands, requests = {}, []
        checkpoint_reads = 0
        keys = {worker: str(i + 1) * 64 for i, worker in enumerate(
            (*args.workers.values(), *args.capture_slots.values()))}

        def spawn(name, command, **kwargs):
            commands[name] = command
            child = mock.Mock()
            child.poll.return_value = None
            child.stdin = io.BytesIO()
            stack.children[name] = child
            return child

        def rpc(kind, **fields):
            nonlocal checkpoint_reads
            requests.append((kind, fields))
            result = {}
            if kind == "hello":
                result = {"role": "world"}
            elif kind == "checkpoint.status":
                checkpoint_reads += 1
                result = {"status": "completed", "load_verified": True, "recovery_required": False,
                          "checkpoint_id": "after" if checkpoint_reads >= 4 else "before"}
            elif kind == "roster":
                companies = {worker: args.roles[role]["company_id"] for role, worker in args.workers.items()}
                result = [{"public_key": key, "company_id": companies.get(worker, 255)}
                          for worker, key in keys.items()]
            return {"status": "completed", "engine_instance_id": "native", "result": result}

        def renderer_engine(socket, **kwargs):
            engine = mock.Mock()
            engine.call.return_value = {"world_id": args.world, "world_epoch": manifest["world_epoch"],
                "result": {"role": "renderer", "native_public_key": keys[Path(socket).stem]}}
            return engine

        with (mock.patch.object(stack, "spawn", side_effect=spawn),
              mock.patch.object(stack, "rpc", side_effect=rpc),
              mock.patch("tools.mixed_world.UnixEngine", side_effect=renderer_engine),
              contextlib.redirect_stdout(io.StringIO())):
            stack.start(save)
        current = json.loads((self.state / "renderer-config.json").read_text())
        self.assertEqual(current["runs"], legacy["runs"])
        self.assertEqual(set(current["slots"]), set(args.capture_slots))
        self.assertEqual({key: upgraded[key] for key in bindings}, bindings)
        self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
        self.assertTrue(upgraded["dynamic_agents"])
        self.assertIn("--dynamic-agents", commands["backend"])
        self.assertIn("--agent-join-pool", commands["backend"])
        for worker in args.capture_slots.values():
            self.assertIn(f"127.0.0.1:{args.native_port}#255", commands[worker])
        self.assertTrue(all(fields["create"] is False for kind, fields in requests if kind == "company.ensure"))
        self.assertEqual(mixed_world.load_existing(self.state, args)[0], upgraded)
        for changed in (old, self.arguments("luna", "--dynamic-agents", "--capture-workers", "1")):
            with self.assertRaises(mixed_world.StartupError):
                mixed_world.load_existing(self.state, changed)
        renderer_path = self.state / "renderer-config.json"
        saved_map = renderer_path.read_bytes()
        renderer_path.unlink()
        with self.assertRaises(mixed_world.StartupError):
            mixed_world.load_existing(self.state, args)
        mixed_world.write_new(renderer_path, saved_map.decode())
        changed = json.loads(saved_map)
        changed["slots"]["capture-0"]["socket"] = str(self.state / "ipc" / "changed.sock")
        mixed_world.publish_private_json(renderer_path, changed)
        with self.assertRaises(mixed_world.StartupError):
            mixed_world.load_existing(self.state, args)

    def test_first_extension_retries_v2_config_without_replacing_private_keys(self):
        old, manifest, _, _ = self.ready_legacy()
        args = self.arguments("luna", "--dynamic-agents")
        mixed_world.load_existing(self.state, args)
        retained = {self.state / worker / "config/openttd/secrets.cfg":
                    (self.state / worker / "config/openttd/secrets.cfg").read_bytes()
                    for worker in args.capture_slots.values()}
        _, config = mixed_world.renderer_configuration(self.state, args, manifest)
        mixed_world.publish_private_json(self.state / "renderer-config.json", config)
        self.assertEqual(mixed_world.load_existing(self.state, args)[0], manifest)
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)
        with self.assertRaises(mixed_world.StartupError):
            mixed_world.load_existing(self.state, old)
        renderer_path = self.state / "renderer-config.json"
        retained_map = self.state / "retained-renderer-config.json"
        renderer_path.rename(retained_map)
        with self.assertRaises(mixed_world.StartupError):
            mixed_world.load_existing(self.state, args)
        self.assertFalse(renderer_path.exists())
        retained_map.rename(renderer_path)
        directory = self.state / "rp0"
        held = self.state / "retained-rp0"
        directory.rename(held)
        with self.assertRaises(mixed_world.StartupError):
            mixed_world.load_existing(self.state, args)
        self.assertFalse(directory.exists())
        held.rename(directory)
        next(iter(retained)).unlink()
        with self.assertRaises(mixed_world.StartupError):
            mixed_world.load_existing(self.state, args)
        self.assertFalse(next(iter(retained)).exists())


if __name__ == "__main__":
    unittest.main()
