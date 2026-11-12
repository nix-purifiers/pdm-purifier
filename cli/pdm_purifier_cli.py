# SPDX-License-Identifier: MIT
"""The `pdm-purifier` command.

    pdm-purifier [--lockfile PATH] lock
                                      write pdm-purifier.lock.json for
                                      pdm.lock as it is
    pdm-purifier [--lockfile PATH] update [PACKAGE...]
                                      update pdm.lock (all packages, or
                                      the named ones), then relock
    pdm-purifier [--lockfile PATH] prefetch
                                      fetch the locked files into the Nix
                                      store, and install every locked group
    pdm-purifier [--lockfile PATH] publish [--package NAME] [-- PDM-PUBLISH-ARGS...]
                                      upload what the Nix build made

Every verb works on the PDM project in the current directory, with its
pdm-purifier.lock.json, or with the lockfile at PATH when --lockfile names
one outside the project. The command runs the PDM of its package, which has
the pdm-purifier plugin, and builds with the Nix library and nixpkgs it was
built with (PDM_PURIFIER_NIX_LIB, PDM_PURIFIER_NIXPKGS, PDM_PURIFIER_PDM).

After `pdm-purifier prefetch`, the plain `pdm` of the same package serves
every download from the locked files, so the development loop works offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

LOCKFILE = "pdm-purifier.lock.json"
LOCKFILE_ENV = "PDM_PURIFIER_LOCKFILE"
TOOL_LOCKFILE_ENV = "PDM_PURIFIER_TOOL_LOCKFILE"
STATE_DIR = ".pdm-purifier"


class Error(Exception):
    pass


def log(message: str) -> None:
    print(f"pdm-purifier: {message}", file=sys.stderr)


def setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise Error(f"{name} is not set; run the pdm-purifier package, which sets it")
    return value


def pdm_executable() -> str:
    return str(Path(setting("PDM_PURIFIER_PDM")) / "bin" / "pdm")


def project_root() -> Path:
    root = Path.cwd()
    if not (root / "pyproject.toml").exists():
        raise Error(f"{root} has no pyproject.toml; run pdm-purifier in the PDM project's directory")
    return root


def lockfile_path(root: Path, option: Path | None) -> Path:
    """The pdm-purifier lockfile: --lockfile, else PDM_PURIFIER_LOCKFILE,
    else pdm-purifier.lock.json next to pdm.lock. The dev shell of a flake
    that keeps a build external can set PDM_PURIFIER_LOCKFILE."""
    if option is not None:
        return option.absolute()
    if os.environ.get(LOCKFILE_ENV):
        return Path(os.environ[LOCKFILE_ENV]).absolute()
    return root / LOCKFILE


def pdm_lock_path(root: Path, option: Path | None) -> Path:
    """The pdm.lock to use: --tool-lockfile, else
    PDM_PURIFIER_TOOL_LOCKFILE, else pdm.lock in the project. The dev shell
    of a flake that keeps a build external can set the variable."""
    if option is not None:
        return option.absolute()
    if os.environ.get(TOOL_LOCKFILE_ENV):
        return Path(os.environ[TOOL_LOCKFILE_ENV]).absolute()
    return root / "pdm.lock"


def is_external(root: Path, pdm_lock: Path) -> bool:
    return pdm_lock.resolve() != (root / "pdm.lock").resolve()


def prepare_pdm_lock(root: Path, pdm_lock: Path) -> None:
    """Start an outside pdm.lock from the copy in the project when it does
    not exist yet. The copy in the project stays as it is."""
    tree_copy = root / "pdm.lock"
    if is_external(root, pdm_lock) and not pdm_lock.exists() and tree_copy.exists():
        pdm_lock.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree_copy, pdm_lock)


def state_dir(lockfile: Path) -> Path:
    """Where the command keeps its garbage-collector roots: .pdm-purifier/
    beside the lockfile, which for an external build is outside the
    project."""
    return lockfile.parent / STATE_DIR


def inputs_hash(root: Path, pdm_lock: Path | None = None) -> str:
    digest = hashlib.sha256()
    digest.update((pdm_lock or root / "pdm.lock").read_bytes())
    digest.update((root / "pyproject.toml").read_bytes())
    return digest.hexdigest()


def content_hash(data: dict) -> str:
    values = {key: value for key, value in data.items() if key != 'contentHash'}
    return hashlib.sha256(json.dumps(values, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def read_lockfile(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise Error(f"{path} is not valid JSON ({error}); restore it from version control or run `pdm-purifier lock`") from None
    if not isinstance(data, dict):
        raise Error(f"{path} is not a pdm-purifier lockfile; run `pdm-purifier lock`")
    return data


def lock_is_current(root: Path, lockfile: Path, pdm_lock: Path | None = None) -> bool:
    pdm_lock = pdm_lock or root / "pdm.lock"
    if not lockfile.exists() or not pdm_lock.exists():
        return False
    return read_lockfile(lockfile).get("inputsHash") == inputs_hash(root, pdm_lock)


def require_current(root: Path, lockfile: Path, pdm_lock: Path) -> None:
    if not lockfile.exists():
        raise Error(f"{lockfile} is missing; run `pdm-purifier lock`")
    if not lock_is_current(root, lockfile, pdm_lock):
        raise Error(f"{lockfile} does not match {pdm_lock.name} and pyproject.toml; run `pdm-purifier lock`")


def source_root(root: Path, pdm_lock: Path | None = None, lockfile: Path | None = None) -> tuple[Path, str]:
    """The directory Nix reads, which holds the project and every local
    package pdm.lock names by path, and the project's place inside it."""
    lock = tomllib.loads((pdm_lock or root / "pdm.lock").read_text())
    original = lockfile or root / LOCKFILE
    records = read_lockfile(original).get('projectInputs', []) if original.exists() else []
    provided = [entry['nativeRepository'] for entry in records if 'nativeRepository' in entry]
    paths = [root.resolve()]
    for package in lock.get("package", []):
        if "path" in package and not any(Path(package['path']).is_relative_to(repository) for repository in provided):
            paths.append((root / package["path"]).resolve())
    common = Path(os.path.commonpath([str(p) for p in paths]))
    return common, os.path.relpath(root.resolve(), common)


def nix_build(root: Path, lockfile: Path, pdm_lock: Path, out_link: Path, **args: str) -> Path:
    if "pythonVersion" not in args:
        version = project_python_version(root,pdm_lock)
        if version is not None:
            args["pythonVersion"] = version
    src, project_dir = source_root(root, pdm_lock, lockfile)
    command = [
        "nix-build",
        str(Path(setting("PDM_PURIFIER_NIX_LIB")) / "../cli/cli.nix"),
        "--argstr", "nixpkgs", setting("PDM_PURIFIER_NIXPKGS"),
        "--argstr", "pdm", setting("PDM_PURIFIER_PDM"),
        "--argstr", "purifier", setting("PDM_PURIFIER_LIBRARY"),
        "--argstr", "src", str(src),
        "--argstr", "projectDir", project_dir,
        "--argstr", "lockFile", str(lockfile.resolve()),
        "--out-link", str(out_link),
    ]
    if is_external(root, pdm_lock):
        command += ["--argstr", "pdmLock", str(pdm_lock.resolve())]
    for key, value in args.items():
        command += ["--argstr", key, value]
    out_link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, stdout=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise Error("the Nix build failed")
    return Path(result.stdout.strip().splitlines()[-1])


def pdm_environment(root: Path, pdm_lock: Path) -> dict[str, str]:
    """The environment of a PDM run without the plugin's variables. PDM
    reads an outside pdm.lock from PDM_LOCKFILE."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PDM_PURIFIER_")
           or k == 'PDM_PURIFIER_PROJECT_INPUTS'}
    env.setdefault('PDM_VENV_BACKEND', 'venv')
    if is_external(root, pdm_lock):
        env["PDM_LOCKFILE"] = str(pdm_lock.resolve())
    return env


def online_pdm(root: Path, pdm_lock: Path, *args: str) -> int:
    """Run PDM against the network, with the plugin's serving turned off."""
    env = pdm_environment(root, pdm_lock)
    env["PDM_PURIFIER_MODE"] = "off"
    return subprocess.run([pdm_executable(), *args], cwd=root, env=env).returncode


def lock(root: Path, lockfile: Path, pdm_lock: Path, inputs=None, refresh=False, packages=None) -> None:
    try:
        previous = read_lockfile(lockfile) if lockfile.exists() else {}
    except Error:
        previous = {}
    original = previous.get('projectInputs', [])
    references = inputs if inputs is not None else [entry['reference'] for entry in original]
    if not references:
        return native_lock(root, lockfile, pdm_lock)
    sys.path.insert(0, setting('PDM_PURIFIER_PROJECT_INPUTS_TOOLS'))
    sys.path.insert(0, setting('PDM_PURIFIER_NIX_LIB'))
    import project_inputs
    import python_bindings
    try:
        pinned = project_inputs.pin(references, original, refresh)
        views = project_inputs.python_views(pinned, state_dir(lockfile) / 'project-inputs')
        old_roots = [entry['nativeRepository'] for entry in original if 'nativeRepository' in entry]
        configuration = python_bindings.write(views, old_roots, state_dir(lockfile) / 'bindings', python=project_python(root,pdm_lock))
        before = os.environ.get('PDM_PURIFIER_PROJECT_INPUTS')
        os.environ['PDM_PURIFIER_PROJECT_INPUTS'] = str(configuration)
        try:
            prepare_pdm_lock(root, pdm_lock)
            command = ['update', '--no-sync', *(packages or [])] if refresh else ['lock', '--update-reuse']
            if online_pdm(root, pdm_lock, *command) != 0:
                raise Error('PDM could not resolve the Nix-provided inputs')
            native_lock(root, lockfile, pdm_lock)
        finally:
            if before is None:
                os.environ.pop('PDM_PURIFIER_PROJECT_INPUTS', None)
            else:
                os.environ['PDM_PURIFIER_PROJECT_INPUTS'] = before
        data = read_lockfile(lockfile)
        for entry, view in zip(pinned, views):
            entry['nativeRepository'] = str(view['repository'])
        data['projectInputs'] = pinned
        data['contentHash'] = content_hash(data)
        lockfile.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    except (ValueError, subprocess.CalledProcessError) as error:
        raise Error(str(error)) from error


def native_lock(root: Path, lockfile: Path, pdm_lock: Path) -> None:
    """Write pdm-purifier.lock.json for pdm.lock as it is. Where pdm.lock is
    missing, `pdm lock` writes it; where it no longer matches
    pyproject.toml, `pdm lock --update-reuse` brings it up to date and keeps
    every pinned version it can. An outside pdm.lock starts as a copy of the
    one in the project, which stays as it is."""
    prepare_pdm_lock(root, pdm_lock)
    if not pdm_lock.exists():
        if online_pdm(root, pdm_lock, "lock") != 0:
            raise Error("`pdm lock` failed")
    elif online_pdm(root, pdm_lock, "lock", "--check") != 0:
        if online_pdm(root, pdm_lock, "lock", "--update-reuse") != 0:
            raise Error("`pdm lock --update-reuse` failed")
    with tempfile.TemporaryDirectory(prefix="pdm-purifier-lock-") as work:
        env = pdm_environment(root, pdm_lock) | {
            k: v for k, v in os.environ.items() if k.startswith("PDM_PURIFIER_")
        }
        env.update(
            PDM_PURIFIER_MODE="record",
            PDM_PURIFIER_RECORD=str(Path(work) / "record.jsonl"),
            PDM_PURIFIER_LOCKFILE=str(lockfile.resolve()),
            # An empty cache, so that every build downloads, and so records,
            # its build requirements.
            PDM_CACHE_DIR=str(Path(work) / "cache"),
            PDM_CHECK_UPDATE="false",
        )
        result = subprocess.run([pdm_executable(), "purifier", "lock"], cwd=root, env=env)
    if result.returncode != 0:
        raise Error("the lock run failed")


def update(root: Path, lockfile: Path, pdm_lock: Path, packages: list[str], inputs=None) -> None:
    """Update pdm.lock with `pdm update`, for every package or the named
    ones, without installing anything, and then relock
    pdm-purifier.lock.json against it."""
    previous = read_lockfile(lockfile) if lockfile.exists() else {}
    if inputs is not None or previous.get('projectInputs'):
        return lock(root, lockfile, pdm_lock, inputs, refresh=True, packages=packages)
    prepare_pdm_lock(root, pdm_lock)
    if online_pdm(root, pdm_lock, "update", "--no-sync", *packages) != 0:
        raise Error("`pdm update` failed")
    lock(root, lockfile, pdm_lock)


def project_python(root: Path, pdm_lock: Path) -> str | None:
    env = pdm_environment(root, pdm_lock)
    env["PDM_PURIFIER_MODE"] = "off"
    found = subprocess.run(
        [pdm_executable(), "info", "--python"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    if found.returncode != 0 or not found.stdout.strip():
        return None
    return found.stdout.strip()


def project_python_version(root: Path, pdm_lock: Path) -> str | None:
    python = project_python(root,pdm_lock)
    if python is None:
        return None
    version = subprocess.run(
        [python, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
    )
    return version.stdout.strip() if version.returncode == 0 else None


def prefetch(root: Path, lockfile: Path, pdm_lock: Path) -> None:
    """Fetch every locked file this system and Python install into the Nix
    store, keep them with a garbage-collector root in .pdm-purifier/, and
    install every locked group into the project's environment from them.

    .pdm-purifier/inputs records the hash of pdm.lock and pyproject.toml the
    manifest was made for. The plugin serves plain `pdm` from the manifest
    while the project still has that hash."""
    require_current(root, lockfile, pdm_lock)
    extra = {}
    version = project_python_version(root, pdm_lock)
    if version is not None:
        extra["pythonVersion"] = version
    nix_build(root, lockfile, pdm_lock, state_dir(lockfile) / "manifest", **extra)
    (state_dir(lockfile) / "inputs").write_text(inputs_hash(root, pdm_lock) + "\n")
    # The plugin finds the manifest beside the lockfile, and serves PDM from
    # it.
    env = pdm_environment(root, pdm_lock)
    env[LOCKFILE_ENV] = str(lockfile)
    result = subprocess.run([pdm_executable(), "sync", "--dev"], cwd=root, env=env)
    if result.returncode != 0:
        raise Error("`pdm sync` from the locked files failed")
    log("prefetched; pdm now installs, runs and builds this project from the locked files, offline")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def local_distributions(root: Path, pdm_lock: Path | None = None) -> dict[str, str]:
    """The project's local distributions by name, with their paths: the
    project itself when PDM builds it, and each package pdm.lock names by a
    path to a directory. The Nix library finds the same ones
    (pkgs/build-pdm-project/default.nix)."""
    found = {}
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    if pyproject.get("tool", {}).get("pdm", {}).get("distribution", "build-system" in pyproject):
        found[normalize_name(pyproject.get("project", {}).get("name", root.name))] = "."
    for package in tomllib.loads((pdm_lock or root / "pdm.lock").read_text()).get("package", []):
        manifest = root / package.get("path", "") / "pyproject.toml"
        if "path" in package and manifest.exists():
            name = tomllib.loads(manifest.read_text()).get("project", {}).get("name", Path(package["path"]).name)
            found[normalize_name(name)] = package["path"]
    return found


def publish(root: Path, lockfile: Path, pdm_lock: Path, packages: list[str], publish_args: list[str]) -> None:
    require_current(root, lockfile, pdm_lock)
    by_name = local_distributions(root, pdm_lock)
    names = packages or sorted(by_name)
    if not names:
        raise Error("this project builds no distribution to publish")
    for name in names:
        if name not in by_name:
            raise Error(f"{name} is not a local distribution of this project; they are {', '.join(sorted(by_name))}")
        dist = nix_build(root, lockfile, pdm_lock, state_dir(lockfile) / f"dist-{name}", package=name)
        package_dir = (root / by_name[name]).resolve()
        log(f"publishing {name} from {dist}")
        env = pdm_environment(root, pdm_lock)
        env["PDM_PURIFIER_MODE"] = "off"
        with tempfile.TemporaryDirectory(prefix="pdm-purifier-publish-") as temporary:
            staged = Path(temporary)
            for file in dist.iterdir():
                if file.is_file() and file.name.endswith((".whl", ".tar.gz", ".zip")):
                    (staged / file.name).symlink_to(file)
            result = subprocess.run(
                [pdm_executable(), "publish", "--project", str(package_dir), "--no-build", "--dest", str(staged), *publish_args],
                env=env,
            )
        if result.returncode != 0:
            raise Error(f"pdm publish failed for {name}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="pdm-purifier", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lockfile", type=Path, help=f"the pdm-purifier lockfile (default: ${LOCKFILE_ENV}, else {LOCKFILE} next to pdm.lock)")
    parser.add_argument(
        "--tool-lockfile",
        type=Path,
        help=f"the pdm.lock to use (default: ${TOOL_LOCKFILE_ENV}, else pdm.lock in the project)",
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    lock_parser = verbs.add_parser("lock", help="write pdm-purifier.lock.json, keeping pdm.lock's resolution")
    lock_parser.add_argument('--input', action='append', help='a Nix producer exposing a Python wheelhouse')
    update_parser = verbs.add_parser("update", help="update pdm.lock with `pdm update`, then relock")
    update_parser.add_argument('--input', action='append', help='a Nix producer exposing a Python wheelhouse')
    update_parser.add_argument("packages", nargs="*", help="the packages to update (default: all)")
    verbs.add_parser("prefetch", help="fetch the locked files, and install every locked group offline")
    publish_parser = verbs.add_parser("publish", help="upload what the Nix build made")
    publish_parser.add_argument("--package", action="append", default=[], help="a local distribution to publish (repeatable)")
    publish_parser.add_argument("publish_args", nargs=argparse.REMAINDER, help="arguments for `pdm publish`, after --")
    options = parser.parse_args(argv)
    try:
        root = project_root()
        lockfile = lockfile_path(root, options.lockfile)
        pdm_lock = pdm_lock_path(root, options.tool_lockfile)
        if options.verb == "lock":
            lock(root, lockfile, pdm_lock, inputs=options.input)
        elif options.verb == "update":
            update(root, lockfile, pdm_lock, options.packages, options.input)
        elif options.verb == "prefetch":
            prefetch(root, lockfile, pdm_lock)
        elif options.verb == "publish":
            extra = options.publish_args
            if extra and extra[0] == "--":
                extra = extra[1:]
            publish(root, lockfile, pdm_lock, options.package, extra)
    except Error as error:
        log(str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
