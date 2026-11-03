"""Content-addressed local scenario bundles for reproducible plan execution."""
import hashlib
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile

from .catalog import Catalog, MANIFEST, _check, _local_file, _read_json
from .errors import Fault


def _bundle(folder, scene):
    _check(scene, MANIFEST)
    names = {"scenario.json", *scene.get("snapshot_files", [])}
    if (folder / "plan.json").exists():
        names.add("plan.json")
    if scene.get("documents"):
        filename = scene["documents"]
        if filename.startswith(".") or Path(filename).suffix.lower() != ".json":
            raise ValueError("A published document collection must be a local JSON file")
        names.add(scene["documents"])
    if any(tool["adapter"]["type"] == "python" for tool in scene["tools"]):
        names.add("adapter.py")
    files = {}
    for name in sorted(names):
        path = _local_file(folder, name)
        if ":" in name or path.resolve() != path:
            raise ValueError("Linked or special files cannot be published")
        files[name] = path.read_bytes()
    return files


def _digest(files):
    entries = [(name, hashlib.sha256(content).hexdigest()) for name, content in files.items()]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Bundle:
    """Verified metadata and source bytes with no imported candidate code."""
    root: Path
    scenarios: dict
    adapter_sources: dict


class Snapshots:
    def __init__(self, database, live):
        self.directory = Path(database).resolve().parent / "plan-snapshots"
        self.live = live
        self.cache = {}
        self.current = {}
        self.baselines = {}
        # Capture one release at startup; changes to a project take effect after restart.
        for scene_id, scene in live.scenarios.items():
            folder = live.root / scene_id
            digest = self._publish(folder, scene)
            self.baselines[scene_id] = digest
            if (folder / "plan.json").exists():
                self.current[scene_id] = digest

    def baseline(self, scene_id):
        return self.baselines.get(scene_id)

    def capture(self, folder, scene_id):
        scene = _read_json(folder, "scenario.json")
        if scene.get("id") != scene_id:
            raise ValueError("Candidate scene does not match the requested scene")
        digest = self._publish(folder, scene)
        return digest, self.inspect(digest, scene_id)

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

    def inspect(self, snapshot_id, scene_id):
        if not re.fullmatch("[0-9a-f]{64}", snapshot_id) or not re.fullmatch("[a-z][a-z0-9_]*", scene_id):
            raise Fault("RELEASE_SNAPSHOT_INVALID", "场景快照标识无效", 409)
        directory = self.directory / snapshot_id
        folder = directory / scene_id
        try:
            if folder.resolve() != folder or set(p.name for p in directory.iterdir()) != {scene_id}:
                raise ValueError("Unexpected snapshot layout")
            scene = _read_json(folder, "scenario.json")
            files = _bundle(folder, scene)
            if scene["id"] != scene_id or _digest(files) != snapshot_id:
                raise ValueError("Snapshot digest mismatch")
            return Bundle(directory, {scene_id: scene}, {scene_id: files.get("adapter.py")})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Fault("RELEASE_SNAPSHOT_INVALID", "场景快照缺失或已损坏", 409) from exc

    def open(self, snapshot_id, scene_id):
        bundle = self.inspect(snapshot_id, scene_id)
        key = (snapshot_id, scene_id)
        if key not in self.cache:
            try:
                self.cache[key] = Catalog(bundle.root, adapter_sources=bundle.adapter_sources)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise Fault("RELEASE_SNAPSHOT_INVALID", "场景快照无法加载", 409) from exc
        return self.cache[key]
