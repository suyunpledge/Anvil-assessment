"""Export only the installed dependency closure for Windows / Python 3.12.

Run in the validated environment; does not install or upgrade anything.
The resulting file pins versions, not wheel hashes. Keep wheels for offline use.
"""
import argparse
import platform
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def read_requirements(path):
    result = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            result.extend(read_requirements(path.parent / line[3:].strip()))
        else:
            result.append(Requirement(line))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if platform.system() != "Windows" or platform.python_version_tuple()[:2] != ("3", "12"):
        parser.error("Generate this deployment lock on Windows with Python 3.12")
    pending = read_requirements(args.requirements)
    visited, pins = set(), {}
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        installed = distribution(name)
        if requirement.specifier and installed.version not in requirement.specifier:
            raise ValueError(f"Installed {name}=={installed.version} violates {requirement}")
        pins[name] = installed.version
        for extra in {"", *requirement.extras}:
            if (name, extra) in visited:
                continue
            visited.add((name, extra))
            for raw in installed.requires or []:
                child = Requirement(raw)
                if child.marker is None or child.marker.evaluate({"extra": extra}):
                    child.marker = None
                    pending.append(child)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "# Installed dependency closure; Windows x64 / Python 3.12 only.\n"
        "# Regenerate with scripts/lock_dependencies.py after validating upgrades.\n"
        + "".join(f"{name}=={value}\n" for name, value in sorted(pins.items())), encoding="utf-8")
    print(f"Pinned {len(pins)} distributions -> {args.output}")


if __name__ == "__main__":
    main()
