"""登记已验收的 Windows 隔离环境，并用同一进程校验和启动工作台。"""
import argparse
import os
from pathlib import Path
import runpy
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_check import environment_report

# 与 20261026 版 Windows x86_64 构建的本机验收记录逐项比对。
EXPECTED_SQLITE = {
    "version": "3.53.1",
    "source_id": "2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9",
    "threadsafety": 3,
    "extension_sha256": "59562874c925f20b567fafb8475f35c69081a163c2cad89b2cc8c9b91ab3a6a4",
    "library_sha256": "4400b656ff114e6e5513510f10b8757a02268e7e45893f1a6fbb4d77a51a281d",
}
EXPECTED_LOCK = "430f0a30407fa921216fedb61606d4b246e8b7d60b6dd9281fa1a852224beb00"


def verify(report):
    if sys.prefix == sys.base_prefix:
        raise ValueError("An isolated virtual environment is required")
    if report["status"] != "PASS":
        raise ValueError("Runtime validation failed: " + ", ".join(report["problems"]))
    if report["python"] != "3.11.18" or report["sqlite"] != EXPECTED_SQLITE:
        raise ValueError("Python or native SQLite differs from the verified build")
    if report["lock_sha256"] != EXPECTED_LOCK:
        raise ValueError("Dependency lock differs from the verified release")


def register():
    with tempfile.NamedTemporaryFile(dir=ROOT, delete=False) as registration:
        path = Path(registration.name)
    try:
        executable = Path(sys.executable).resolve()
        if len(executable.drive) != 2 or not executable.is_absolute():
            raise ValueError("Registered Python must be on a local drive")
        path.write_text(str(executable), encoding="utf-8")
        os.replace(path, ROOT / ".runtime-python")
    finally:
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--register", action="store_true")
    modes.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    try:
        verify(environment_report())
        if args.register:
            register()
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.launch:
        runpy.run_path(str(ROOT / "run.py"), run_name="__main__")
    else:
        print("Verified isolated Python 3.11.18 / SQLite 3.53.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
