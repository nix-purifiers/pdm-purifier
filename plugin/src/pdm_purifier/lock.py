# SPDX-License-Identifier: MIT
"""`pdm purifier lock`: write pdm-purifier.lock.json beside pdm.lock.

pdm-purifier.lock.json holds only what pdm.lock cannot record. The Nix
library reads everything else from pdm.lock and pyproject.toml:

- `urls`: the URL of each file pdm.lock lists without one. pdm.lock names
  each file and its SHA-256 hash, and has URLs only with the static_urls
  strategy. The command asks PDM's own finder, with platform filtering off,
  for each URL, so the lockfile covers every platform pdm.lock covers.
- `buildRequirements`: the files that building a package installs, of
  packages pdm.lock does not pin. The command runs every build that
  installing the project can need, with the recording transport under PDM:
  the project itself (sdist, wheel and editable wheel), each local and
  version control dependency, and each registry or URL dependency that has
  an sdist and no wheel for every platform. While it runs, a build
  requirement that pdm.lock pins resolves to the pinned version
  (`pinned_versions`), so its files are pdm.lock's and are not recorded
  again. It then adds every file of each other recorded version, so a build
  on another platform finds its wheels too.
- `checkouts`: for each version control dependency, by repository URL, the
  Nix hash of its checkout, and for git the raw commit object from which
  Nix rebuilds the .git directory that PDM clones. A git submodule, which
  pdm.lock does not name, has its revision too.

The command runs with PDM_PURIFIER_MODE=record, which `pdm-purifier lock`
sets together with an empty PDM cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from packaging.tags import mac_platforms
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)

from pdm_purifier.transport import RECORD_ENV, normalize_name

# The versions pdm.lock pins, by normalized project name, while a lock run
# is running. The plugin's finder patch (plugin._patch_finder) offers a build
# only these versions of a pinned project.
pinned_versions: dict[str, set[str]] = {}

# The lock format this command writes. lib-overlays/default/lock-version.nix holds the
# same number, and the Nix library refuses any other.
FORMAT_VERSION = 2
LOCKFILE = "pdm-purifier.lock.json"
VCS_KEYS = ("git", "hg", "svn", "bzr")
# The architectures of the Linux systems that the Nix build supports.
SUPPORTED_ARCHITECTURES = ("x86_64", "aarch64")
# The platform tags of the wheels that install on the macOS system that the
# Nix build supports: arm64, on the oldest macOS that nixpkgs supports
# (stdenv.hostPlatform.darwinMinVersion), as the shared library's `wheels`
# chooses them.
MACOS_PLATFORMS = frozenset(mac_platforms((14, 0), "arm64"))
# 2000-01-01 00:00:00 UTC, a time that zip files can hold.
STORE_COPY_TIME = 946684800


class LockError(Exception):
    pass


def sri(hex_digest: str) -> str:
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode()


def inputs_hash(root: Path, pdm_lock: Path | None = None) -> str:
    """SHA-256 over pdm.lock and pyproject.toml, in that order. The Nix
    library computes the same hash to refuse a stale pdm-purifier.lock.json.
    ``pdm_lock`` is the pdm.lock PDM uses, which is the one in the project
    unless PDM_LOCKFILE names another."""
    digest = hashlib.sha256()
    digest.update((pdm_lock or root / "pdm.lock").read_bytes())
    digest.update((root / "pyproject.toml").read_bytes())
    return digest.hexdigest()


def name_and_version(filename: str) -> tuple[str, str] | None:
    try:
        name, version, _, _ = parse_wheel_filename(filename)
        return str(name), str(version)
    except InvalidWheelFilename:
        pass
    try:
        name, version = parse_sdist_filename(filename)
        return str(name), str(version)
    except InvalidSdistFilename:
        return None


def is_universal_wheel(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    _, _, _, tags = parse_wheel_filename(filename)
    return any(tag.abi == "none" and tag.platform == "any" for tag in tags)


def has_linux_wheel(filename: str, arch: str, python: tuple[int, int]) -> bool:
    """Whether a wheel installs on Linux ``arch`` with CPython ``python``:
    its tags name a Linux platform of that architecture, and a Python
    interface the interpreter satisfies (any Python 3, the stable ABI from an
    older CPython, or this CPython)."""
    return has_wheel(filename, lambda platform: "linux" in platform and platform.endswith(arch), python)


def has_macos_wheel(filename: str, python: tuple[int, int]) -> bool:
    """Whether a wheel installs on the supported macOS system with CPython
    ``python``: its tags name one of MACOS_PLATFORMS, and a Python interface
    the interpreter satisfies."""
    return has_wheel(filename, MACOS_PLATFORMS.__contains__, python)


def has_wheel(filename: str, platform: Callable[[str], bool], python: tuple[int, int]) -> bool:
    """Whether a wheel has a tag whose platform ``platform`` accepts, with a
    Python interface that CPython ``python`` satisfies."""
    if not filename.endswith(".whl"):
        return False
    _, _, _, tags = parse_wheel_filename(filename)
    major, minor = python
    for tag in tags:
        if not platform(tag.platform):
            continue
        if tag.abi == "none" and tag.interpreter.startswith("py"):
            return True
        if tag.interpreter == f"cp{major}{minor}":
            return True
        if tag.abi == "abi3" and tag.interpreter.startswith("cp") and tag.interpreter[2:].isdigit():
            if int(tag.interpreter[2:]) <= int(f"{major}{minor}"):
                return True
    return False


def is_sdist(filename: str) -> bool:
    return not filename.endswith(".whl")


def make_editable(checkout: Path) -> None:
    """Make a copy of a checkout from the Nix store writable, and give every
    file the time 2000-01-01. The store gives its files the time 1970, which
    build backends that write zip files, such as flit-core, refuse."""
    for path in [checkout, *checkout.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)
            os.utime(path, (STORE_COPY_TIME, STORE_COPY_TIME))


def write_pkg_info(source: Path, name: str, version: str) -> None:
    """Give a checkout without version control metadata the version pdm.lock
    pins. A Mercurial, Subversion or Bazaar checkout from Nix has no .hg,
    .svn or .bzr directory, so setuptools-scm (and hatch-vcs, which uses it)
    cannot compute the version from history. setuptools-scm then reads the
    version from PKG-INFO, as it does in an sdist."""
    pkg_info = source / "PKG-INFO"
    if not pkg_info.exists():
        pkg_info.write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


class Locker:
    def __init__(self, project: Any) -> None:
        self.project = project
        self.environment = project.environment
        self.root = Path(project.root)
        self.work = Path(tempfile.mkdtemp(prefix="pdm-purifier-"))
        record = os.environ.get(RECORD_ENV)
        if not record or os.environ.get("PDM_PURIFIER_MODE") != "record":
            raise LockError("run `pdm-purifier lock`, which records the downloads of the builds")
        self.record = Path(record)
        # The URL of each pdm.lock file that pdm.lock lists without one.
        self.urls: dict[str, str] = {}
        # Every file pdm.lock lists, by name, and every URL it has.
        self.locked_files: dict[str, list[str]] = {}
        self.locked_urls: set[str] = set()
        # The build requirements pdm.lock does not pin, by file name.
        self.build_requirements: dict[str, dict] = {}
        # Version control checkouts by repository URL, and the revision of
        # each git checkout.
        self.checkouts: dict[str, dict] = {}
        self.git_revs: dict[str, str] = {}
        self._pages: dict[str, dict[str, Any]] = {}
        pinned_versions.clear()
        for package in self.packages():
            if "version" in package and not any(key in package for key in (*VCS_KEYS, "path", "url")):
                pinned_versions.setdefault(normalize_name(package["name"]), set()).add(package["version"])

    # -- index lookups -----------------------------------------------------

    def links(self, name: str) -> dict[str, Any]:
        """Every file of ``name`` on the project's indexes, for every
        platform, by file name. The first index that lists a file wins."""
        key = normalize_name(name)
        if key not in self._pages:
            found: dict[str, Any] = {}
            with self.environment.get_finder(env_spec=self.environment.allow_all_spec) as finder:
                for package in finder.find_all_packages(name, allow_yanked=True):
                    link = package.link
                    if link is None or link.is_vcs or link.is_file:
                        continue
                    found.setdefault(link.filename, link)
            self._pages[key] = found
        return self._pages[key]

    def add_build_requirement(self, name: str, filename: str, url: str, sha256: str, requires_python: str | None) -> None:
        parsed = name_and_version(filename)
        if parsed is None:
            raise LockError(f"{name}: {filename} is not a wheel or an sdist")
        entry = {
            "name": normalize_name(name),
            "version": parsed[1],
            "file": filename,
            "url": url,
            "hash": sri(sha256),
            "requiresPython": requires_python,
        }
        previous = self.build_requirements.get(filename)
        if previous is not None and previous["hash"] != entry["hash"]:
            raise LockError(f"two different files are named {filename}")
        self.build_requirements[filename] = entry

    def link_sha256(self, link: Any) -> str:
        hashes = link.hashes or {}
        if "sha256" in hashes:
            return hashes["sha256"]
        if link.hash_name == "sha256" and link.hash:
            return link.hash
        response = self.environment.session.get(link.url_without_fragment)
        response.raise_for_status()
        return hashlib.sha256(response.content).hexdigest()

    # -- pdm.lock ----------------------------------------------------------

    def packages(self) -> list[dict]:
        try:
            return list(self.project.lockfile["package"])
        except KeyError:
            return []

    def lock_registry_package(self, package: dict) -> list[str]:
        """Find the URL of each file of ``package`` that pdm.lock lists
        without one, and return the names of all its files."""
        name = package["name"]
        filenames = []
        for item in package.get("files", []):
            algo, _, _ = item["hash"].partition(":")
            if algo != "sha256":
                raise LockError(f"{name}: pdm.lock has a {algo} hash; pdm-purifier needs sha256")
            if "url" in item:
                url = item["url"].split("#")[0]
                filename = url.rsplit("/", 1)[-1]
            else:
                filename = item["file"]
                link = self.links(name).get(filename)
                if link is None:
                    raise LockError(
                        f"{name}: none of the project's indexes lists {filename}, which pdm.lock names. "
                        "An index may have removed the file since pdm.lock was written. "
                        "Run `pdm lock --update-reuse` to bring pdm.lock up to date."
                    )
                url = link.url_without_fragment
                self.urls[filename] = url
            filenames.append(filename)
            self.locked_urls.add(url)
        self.locked_files[normalize_name(name)] = self.locked_files.get(normalize_name(name), []) + filenames
        return filenames

    def lock_url_package(self, package: dict) -> list[str]:
        url = package["url"].split("#")[0]
        for item in package.get("files", []):
            algo, _, _ = item["hash"].partition(":")
            if algo != "sha256":
                raise LockError(f"{package['name']}: pdm.lock has a {algo} hash; pdm-purifier needs sha256")
        self.locked_urls.add(url)
        return [url.rsplit("/", 1)[-1]]

    # -- git ---------------------------------------------------------------

    def git_command(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
        return result.stdout

    def prefetch(self, url: str, rev: str) -> str:
        """The NAR hash of the plain checkout, without .git, which fetchgit
        reproduces exactly."""
        out = subprocess.run(
            ["nix-prefetch-git", "--quiet", "--url", url, "--rev", rev],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return json.loads(out)["hash"]

    def add_git(self, url: str, rev: str, repository: Path, *, submodule: bool) -> None:
        """Lock the git checkout of ``url`` at ``rev``. ``repository`` is a
        clone that has the commit, from which the entry takes the raw commit
        object and the submodule commits (gitlinks) of its tree. The Nix
        build rebuilds the .git directory from them and the plain checkout,
        because a hash over a .git directory is not reproducible. A
        submodule's entry has its revision, which pdm.lock does not."""
        if url in self.git_revs:
            if self.git_revs[url] != rev:
                raise LockError(f"{url} is locked at two revisions; pdm-purifier keeps one per URL")
            return
        self.git_revs[url] = rev
        commit = subprocess.run(
            ["git", "cat-file", "commit", rev], cwd=repository, check=True, capture_output=True
        ).stdout
        gitlinks = []
        for line in self.git_command("ls-tree", "-r", "-z", rev, cwd=repository).split("\0"):
            if line.startswith("160000 commit "):
                meta, path = line.split("\t", 1)
                gitlinks.append({"path": path, "rev": meta.split()[2]})
        self.checkouts[url] = {
            "commitObject": {"object": base64.b64encode(commit).decode(), "gitlinks": gitlinks},
            "hash": self.prefetch(url, rev),
        }
        if submodule:
            self.checkouts[url]["rev"] = rev

    # -- Mercurial, Subversion and Bazaar ----------------------------------

    def prefetch_other(self, vcs: str, fields: dict[str, Any]) -> tuple[str, Path]:
        """The NAR hash and store path of a checkout, made by the nixpkgs
        fetcher that the Nix build uses (fetchhg, fetchsvn or fetchbzr).

        The fetcher runs through the Nix daemon, first with a placeholder
        hash, whose mismatch error names the real one, and then with the real
        hash. The hash so comes from the build's own fetcher, and the fetch
        uses the daemon's network access."""
        nixpkgs = os.environ.get("PDM_PURIFIER_NIXPKGS")
        if not nixpkgs:
            raise LockError("PDM_PURIFIER_NIXPKGS is not set; run `pdm-purifier lock`")
        fetcher = {"hg": "fetchhg", "svn": "fetchsvn", "bzr": "fetchbzr"}[vcs]
        # The entry's field names, as the shared library names them, mapped
        # to the arguments of the nixpkgs fetcher.
        renamed = {"subrepos": "fetchSubrepos"}
        arguments = " ".join(f"{renamed.get(key, key)} = {json.dumps(value)};" for key, value in fields.items())

        def build(hash_value: str) -> subprocess.CompletedProcess[str]:
            expression = f"with import {nixpkgs} {{ }}; {fetcher} {{ {arguments} hash = {json.dumps(hash_value)}; }}"
            return subprocess.run(
                ["nix", "--extra-experimental-features", "nix-command", "build", "--impure", "--no-link", "--print-out-paths", "--expr", expression],
                capture_output=True,
                text=True,
            )

        attempt = build("sha256-" + "A" * 43 + "=")
        found = re.search(r"got:\s+(sha256-\S+)", attempt.stderr)
        if found is None:
            raise LockError(f"fetching {fields['url']} at {fields['rev']} with {fetcher} failed:\n{attempt.stderr}")
        sri_hash = found.group(1)
        final = build(sri_hash)
        if final.returncode != 0:
            raise LockError(f"fetching {fields['url']} at {fields['rev']} with {fetcher} failed:\n{final.stderr}")
        return sri_hash, Path(final.stdout.strip().splitlines()[-1])

    def lock_other_vcs_package(self, package: dict, vcs: str) -> Path:
        """Lock a Mercurial, Subversion or Bazaar dependency, and return a
        writable copy of its checkout, for the build that records its build
        requirements."""
        url = package[vcs].removeprefix(f"{vcs}+")
        rev = str(package["revision"])
        name = package["name"]
        fields: dict[str, Any] = {"url": url, "rev": rev}
        if vcs == "svn":
            # A plain `svn checkout`, which is what unearth runs, fetches
            # externals and expands keywords.
            fields.update(ignoreExternals=False, ignoreKeywords=False)
        elif vcs == "hg":
            # `hg update` after unearth's clone checks out subrepositories.
            fields.update(subrepos=True)
        sri_hash, store = self.prefetch_other(vcs, fields)
        subdirectory = package.get("subdirectory")
        self.checkouts[url] = {"hash": sri_hash}
        checkout = self.work / vcs / f"{normalize_name(name)}-{rev[:12]}"
        if not checkout.exists():
            shutil.copytree(store, checkout, symlinks=True)
            make_editable(checkout)
        source = checkout / subdirectory if subdirectory else checkout
        write_pkg_info(source, name, package["version"])
        return source

    def lock_git_package(self, package: dict) -> Path:
        """Lock a git dependency and its submodules, and return its checkout,
        for the build that records its build requirements."""
        url = package["git"].removeprefix("git+")
        rev = package["revision"]
        name = package["name"]
        checkout = self.work / "git" / f"{normalize_name(name)}-{rev[:12]}"
        if not checkout.exists():
            self.git_command("clone", "--quiet", url, str(checkout))
            self.git_command("reset", "--hard", "--quiet", rev, cwd=checkout)
            if (checkout / ".gitmodules").exists():
                self.git_command("submodule", "update", "--init", "--recursive", "--quiet", cwd=checkout)
        self.add_git(url, rev, checkout, submodule=False)
        if (checkout / ".gitmodules").exists():
            listing = self.git_command(
                "submodule",
                "foreach",
                "--recursive",
                "--quiet",
                'echo "$(git config remote.origin.url) $sha1 $displaypath"',
                cwd=checkout,
            )
            for line in listing.splitlines():
                sub_url, sub_rev, sub_path = line.split(" ", 2)
                self.add_git(sub_url, sub_rev, checkout / sub_path, submodule=True)
        subdirectory = package.get("subdirectory")
        return checkout / subdirectory if subdirectory else checkout

    # -- builds ------------------------------------------------------------

    def build(self, source: Path, *, sdist: bool = False, editable: bool = False) -> None:
        """Build ``source``, so that the recording transport writes down
        what its builds download."""
        from pdm.builders import EditableBuilder, SdistBuilder, WheelBuilder

        out = self.work / "dist" / hashlib.sha256(str(source).encode()).hexdigest()[:16]
        out.mkdir(parents=True, exist_ok=True)
        self.project.core.ui.echo(f"Building {source} to record its build requirements", err=True)
        if sdist:
            SdistBuilder(source, self.environment).build(str(out))
        WheelBuilder(source, self.environment).build(str(out))
        if editable:
            EditableBuilder(source, self.environment).build(str(self.work / "editable"))

    def unpack_sdist(self, name: str, url: str) -> Path:
        response = self.environment.session.get(url)
        response.raise_for_status()
        filename = url.rsplit("/", 1)[-1]
        archive = self.work / "sdists" / filename
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(response.content)
        dest = self.work / "unpacked" / filename
        dest.mkdir(parents=True, exist_ok=True)
        if filename.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        else:
            with tarfile.open(archive) as tf:
                tf.extractall(dest, filter="data")
        tops = list(dest.iterdir())
        return tops[0] if len(tops) == 1 and tops[0].is_dir() else dest

    def needs_build(self, filenames: list[str]) -> bool:
        """A package is built from its sdist, so that the build requirements
        are recorded, when a system that pdm-purifier supports has no wheel
        for it: no universal wheel, and no wheel of the lock run's Python for
        each of Linux x86_64, Linux aarch64 and macOS arm64."""
        if not any(is_sdist(f) for f in filenames):
            return False
        if any(is_universal_wheel(f) for f in filenames):
            return False
        interpreter = self.environment.interpreter
        python = (interpreter.major, interpreter.minor)
        linux = all(any(has_linux_wheel(f, arch, python) for f in filenames) for arch in SUPPORTED_ARCHITECTURES)
        return not (linux and any(has_macos_wheel(f, python) for f in filenames))

    def recorded(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines() if line.strip()]

    def add_build_requirements(self) -> None:
        """Lock every file of each version the builds downloaded that
        pdm.lock does not have."""
        locked = {f for files in self.locked_files.values() for f in files}
        versions: set[tuple[str, str]] = set()
        for item in self.recorded():
            filename = item["url"].rsplit("/", 1)[-1]
            if item["url"] in self.locked_urls or filename in locked:
                continue
            parsed = name_and_version(filename)
            if parsed is None:
                raise LockError(f"a build downloaded {item['url']}, which is not a distribution file")
            versions.add(parsed)
            link = self.links(parsed[0]).get(filename)
            requires_python = link.requires_python if link is not None else None
            self.add_build_requirement(parsed[0], filename, item["url"], item["sha256"], requires_python)
        for name, version in sorted(versions):
            for filename, link in self.links(name).items():
                if name_and_version(filename) == (name, version) and filename not in self.build_requirements:
                    self.add_build_requirement(name, filename, link.url_without_fragment, self.link_sha256(link), link.requires_python)

    # -- the whole lock ----------------------------------------------------

    def run(self) -> dict:
        project = self.project
        if not project.lockfile.exists():
            raise LockError("pdm.lock is missing; run `pdm lock` first")
        if not project.is_lockfile_hash_match():
            raise LockError("pdm.lock does not match pyproject.toml; run `pdm lock` first")

        builds: list[tuple[Path, dict]] = []
        sdist_builds: list[tuple[str, str]] = []
        for package in self.packages():
            if "git" in package:
                builds.append((self.lock_git_package(package), {}))
            elif any(key in package for key in VCS_KEYS):
                vcs = next(key for key in VCS_KEYS if key in package)
                builds.append((self.lock_other_vcs_package(package, vcs), {}))
            elif "path" in package:
                path = (self.root / package["path"]).resolve()
                if path.is_dir():
                    builds.append((path, {"editable": bool(package.get("editable")), "local": True}))
            elif "url" in package:
                filenames = self.lock_url_package(package)
                if self.needs_build(filenames):
                    sdist_builds.append((package["name"], package["url"].split("#")[0]))
            else:
                filenames = self.lock_registry_package(package)
                if self.needs_build(filenames):
                    sdist = next(f for f in filenames if is_sdist(f))
                    sdist_builds.append((package["name"], self.url_of(package, sdist)))

        # The local distributions: the project itself, and each path
        # dependency.
        if project.is_distribution:
            self.build(self.root, sdist=True, editable=True)
        for source, options in builds:
            self.build(source, sdist=options.get("local", False), editable=options.get("editable", False))
        for name, url in sdist_builds:
            self.build(self.unpack_sdist(name, url))
        self.add_build_requirements()

        return {
            "version": FORMAT_VERSION,
            "inputsHash": inputs_hash(self.root, Path(self.project.lockfile._path)),
            "urls": dict(sorted(self.urls.items())),
            "buildRequirements": [self.build_requirements[k] for k in sorted(self.build_requirements)],
            "checkouts": dict(sorted(self.checkouts.items())),
        }

    def url_of(self, package: dict, filename: str) -> str:
        for item in package.get("files", []):
            if item.get("file") == filename and "url" in item:
                return item["url"].split("#")[0]
            if "url" in item and item["url"].split("#")[0].endswith("/" + filename):
                return item["url"].split("#")[0]
        return self.urls[filename]

    def close(self) -> None:
        pinned_versions.clear()
        shutil.rmtree(self.work, ignore_errors=True)


def content_hash(data: dict) -> str:
    """SHA-256 over the lockfile without its contentHash, as compact JSON
    with sorted keys: the text Nix's builtins.toJSON makes of the same data,
    so the Nix library computes the same hash to refuse an edited lockfile."""
    rest = {key: value for key, value in data.items() if key != "contentHash"}
    canonical = json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def lock(project: Any) -> Path:
    locker = Locker(project)
    try:
        data = locker.run()
    finally:
        locker.close()
    data["contentHash"] = content_hash(data)
    # `pdm-purifier lock` names the lockfile, which may be outside the
    # project, in PDM_PURIFIER_LOCKFILE.
    path = Path(os.environ.get("PDM_PURIFIER_LOCKFILE") or Path(project.root) / LOCKFILE)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path
