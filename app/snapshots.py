"""Content-addressed local scenario bundles for reproducible plan execution."""
import hashlib
import json
from pathlib import Path
import re
import tempfile

from .catalog import Catalog, _local_file
from .errors import Fault


def _bundle(folder, scene):
    names = {"scenario.json", "plan.json", *scene.get("snapshot_files", [])}
    if scene.get("documents"):
        names.add(scene["documents"])
    if any(tool["adapter"]["type"] == "python" for tool in scene["tools"]):
        names.add("adapter.py")
    return {name: _local_file(folder, name).read_bytes() for name in sorted(names)}


def _digest(files):
    entries = [(name, hashlib.sha256(content).hexdigest()) for name, content in files.items()]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()


class Snapshots:
    def __init__(self, database, live):
        self.directory = Path(database).resolve().parent / "plan-snapshots"
        self.live = live
        self.cache = {}
        self.current = {}
        # Capture one release at startup; changes to a project take effect after restart.
        for scene_id, scene in live.scenarios.items():
            folder = live.root / scene_id
            if (folder / "plan.json").exists():
                self.current[scene_id] = self._publish(folder, scene)

    def _publish(self, folder, scene):
        files = _bundle(folder, scene)
        if json.loads(files["scenario.json"]) != scene:
            raise ValueError("Scenario changed while loading; restart after deployment")
        digest = _digest(files)
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / digest
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=self.directory) as scratch:
                staging = Path(scratch) / "bundle"
                source = staging / scene["id"]
                source.mkdir(parents=True)
                for name, content in files.items():
                    (source / name).write_bytes(content)
                try:
                    staging.rename(destination)
                except OSError:
                    if not destination.is_dir():
                        raise
        return digest

    def open(self, snapshot_id, scene_id):
        if snapshot_id in self.cache:
            return self.cache[snapshot_id]
        if not re.fullmatch("[0-9a-f]{64}", snapshot_id) or not re.fullmatch("[a-z][a-z0-9_]*", scene_id):
            raise Fault("RELEASE_SNAPSHOT_INVALID", "场景快照标识无效", 409)
        directory = self.directory / snapshot_id
        folder = directory / scene_id
        try:
            if folder.resolve() != folder or set(p.name for p in directory.iterdir()) != {scene_id}:
                raise ValueError("Unexpected snapshot layout")
            scene = json.loads(_local_file(folder, "scenario.json").read_bytes())
            if scene["id"] != scene_id or _digest(_bundle(folder, scene)) != snapshot_id:
                raise ValueError("Snapshot digest mismatch")
            catalog = Catalog(directory)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Fault("RELEASE_SNAPSHOT_INVALID", "场景快照缺失或已损坏", 409) from exc
        self.cache[snapshot_id] = catalog
        return catalog
