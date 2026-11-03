"""按内容摘要保存本地场景包，使任务可按相同版本重现。"""
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile
from types import MappingProxyType

from .catalog import Catalog, MANIFEST, _check, _local_file, _reject_duplicate_keys, _validate_documents
from .errors import Fault


def snapshot_files(folder, scene=None, *, max_file_bytes=None):
    """收集经校验的声明文件；发布和离线评测共用这份文件清单。"""
    folder = Path(folder).absolute()
    if folder.resolve() != folder:
        raise ValueError("linked scenario directory")

    def read_file(name):
        path = _local_file(folder, name)
        with path.open("rb") as stream:
            if max_file_bytes is None:
                return stream.read()
            content = stream.read(max_file_bytes + 1)
        if len(content) > max_file_bytes:
            raise ValueError("fixture file too large")
        return content

    files = {"scenario.json": read_file("scenario.json")}
    manifest = json.loads(files["scenario.json"].decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    _check(manifest, MANIFEST)
    if manifest["id"] != folder.name:
        raise ValueError("scenario ID must match its directory")
    if scene is not None and json.dumps(scene, sort_keys=True) != json.dumps(manifest, sort_keys=True):
        raise ValueError("Scenario changed while loading; restart after deployment")

    names = set(manifest.get("snapshot_files", []))
    if (folder / "plan.json").exists():
        names.add("plan.json")
    documents = manifest.get("documents")
    if documents:
        names.add(documents)
    if any(tool["adapter"]["type"] == "python" for tool in manifest["tools"]):
        names.add("adapter.py")
    for name in sorted(names - files.keys()):
        files[name] = read_file(name)
    if documents:
        # 旧资料可能以 .txt 等后缀保存；只有符合资料协议的 JSON 才进入快照。
        records = json.loads(files[documents].decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        _validate_documents(documents, records)
    return files


def _digest(files):
    entries = [(name, hashlib.sha256(files[name]).hexdigest()) for name in sorted(files)]
    serialized_entries = json.dumps(entries, ensure_ascii=False).encode()
    return hashlib.sha256(serialized_entries).hexdigest()


@dataclass(frozen=True)
class Bundle:
    """已核验的元数据与源码字节，此时尚未导入候选代码。"""
    root: Path
    scenarios: dict
    adapter_sources: dict
    files: Mapping[str, bytes]


class Snapshots:
    def __init__(self, database, live):
        self.directory = Path(database).resolve().parent / "plan-snapshots"
        self.live = live
        self.cache = {}
        self.current = {}
        self.baselines = {}
        # 启动时捕获当前版本。重启后才会读取本地项目文件的更新。
        for scene_id, scene in live.scenarios.items():
            folder = live.root / scene_id
            digest = self._publish(scene_id, snapshot_files(folder, scene))
            self.baselines[scene_id] = digest
            if (folder / "plan.json").exists():
                self.current[scene_id] = digest

    def baseline(self, scene_id):
        return self.baselines.get(scene_id)

    def capture(self, folder, scene_id):
        if folder.name != scene_id:
            raise ValueError("Candidate scene does not match the requested scene")
        digest = self._publish(scene_id, snapshot_files(folder))
        return digest, self.inspect(digest, scene_id)

    def _publish(self, scene_id, files):
        digest = _digest(files)
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / digest
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=self.directory) as scratch:
                staging = Path(scratch) / "bundle"
                source = staging / scene_id
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
            files = snapshot_files(folder)
            if _digest(files) != snapshot_id:
                raise ValueError("Snapshot digest mismatch")
            scene = json.loads(files["scenario.json"])
            return Bundle(directory, {scene_id: scene}, {scene_id: files.get("adapter.py")}, MappingProxyType(files))
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
