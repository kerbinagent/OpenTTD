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


if __name__ == "__main__":
    unittest.main()
