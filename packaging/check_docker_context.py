"""Check Gravelle's deliberately restricted Docker context before building.

This validates this project's allowlist form of .dockerignore, rather than
attempting to implement Docker's full pattern language. Unsupported rules fail
closed and require review when the context policy changes.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path, PurePosixPath
import shlex


ROOT = Path(__file__).resolve().parents[1]


def safe_relative(name: str) -> str:
    value = PurePosixPath(name.rstrip("/"))
    if not name or value.is_absolute() or ".." in value.parts or "\\" in name:
        raise ValueError("Invalid context path: " + name)
    return value.as_posix()


def context_files(root: Path = ROOT) -> list[str]:
    rules = [line.strip() for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not rules or rules[0] != "*":
        raise ValueError("Docker context must begin by excluding all files.")
    result = set()
    for rule in rules[1:]:
        if not rule.startswith("!"):
            raise ValueError("Only explicit allowlist rules may follow the initial '*': " + rule)
        name = rule[1:]
        recursive = name.endswith("/**")
        relative = safe_relative(name[:-3] if recursive else name)
        if any(character in relative for character in "*?["):
            raise ValueError("Wildcard allowlist rules require review: " + rule)
        path = root / relative
        if path.is_symlink() or not path.exists() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Missing or linked Docker context input: " + relative)
        if recursive:
            if not path.is_dir():
                raise ValueError("Recursive rule must select a directory: " + rule)
            candidates = path.rglob("*")
        elif path.is_file():
            candidates = [path]
        else:
            # The directory exception only permits traversal. Docker still
            # needs a separate '/**' rule to admit files below that directory.
            candidates = []
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
                raise ValueError("Linked context input: " + str(candidate))
            if candidate.is_file():
                result.add(candidate.relative_to(root).as_posix())
    return sorted(result)


def copy_sources(root: Path = ROOT) -> list[str]:
    sources = []
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    for line in dockerfile.splitlines():
        instruction, _, arguments = line.strip().partition(" ")
        if instruction.upper() != "COPY":
            continue
        values = json.loads(arguments) if arguments.startswith("[") else shlex.split(arguments)
        if len(values) < 2 or any(value.startswith("--") for value in values):
            raise ValueError("Unsupported Docker COPY requires context-policy review: " + line)
        sources.extend(safe_relative(value) for value in values[:-1])
    if not sources:
        raise ValueError("Dockerfile does not copy any application files.")
    return sorted(set(sources))


def validate(root: Path = ROOT) -> dict:
    root = root.resolve()
    files = context_files(root)
    included = set(files)
    sources = copy_sources(root)
    required = set()
    for name in sources:
        path = root / name
        if path.is_dir():
            required.update(item.relative_to(root).as_posix() for item in path.rglob("*") if item.is_file())
        else:
            required.add(name)
    missing = sorted(required - included)
    if missing:
        raise ValueError("Docker COPY inputs are excluded by .dockerignore: " + ", ".join(missing))
    extra = sorted(included - required - {"Dockerfile"})
    if extra:
        raise ValueError("Unneeded Docker context files: " + ", ".join(extra))
    manifest = json.loads((root / "packaging/source_manifest.json").read_text(encoding="utf-8"))
    outside_manifest = sorted(included - set(manifest))
    if outside_manifest:
        raise ValueError("Context files were not reviewed in the source manifest: " + ", ".join(outside_manifest))
    forbidden_roots = {"work", "dist", "data", "data-windows", "library", ".git", ".env", "outputs"}
    forbidden = [name for name in files if PurePosixPath(name).parts[0] in forbidden_roots
                 or PurePosixPath(name).suffix.lower() in {".sqlite3", ".db", ".log", ".pem", ".key"}]
    if forbidden:
        raise ValueError("Private data in Docker context: " + ", ".join(forbidden))
    # Every locally imported top-level module must be available in the image.
    local_modules = {path.stem: path.name for path in root.glob("*.py")}
    for name in files:
        if not name.endswith(".py"):
            continue
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for module in modules:
                required_name = local_modules.get(module.split(".")[0])
                if required_name and required_name not in included:
                    raise ValueError(name + " imports an excluded local module: " + required_name)
    return {"status": "passed", "context_files": len(files), "copy_sources": sources,
            "files": files, "private_data_excluded": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    try:
        report = validate()
    except (OSError, ValueError) as error:
        report = {"status": "failed", "error": str(error)}
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
