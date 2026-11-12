# Design

## One transport under every PDM session

PDM makes every HTTP request through an httpx client that
`BaseEnvironment._build_session` builds: index pages, distribution files,
URL dependencies, and the build requirements it installs for isolated
builds. The plugin (`plugin/src/pdm_purifier/__init__.py`) wraps that
method and gives each client one transport for every URL
(`plugin/src/pdm_purifier/transport.py`). `PDM_PURIFIER_MODE` selects it:

- `record`: the request goes to the transport PDM chose, and each
  distribution file PDM downloads is appended, with its URL and SHA-256, to
  the file `PDM_PURIFIER_RECORD` names.
- `serve`: nothing reaches the network. A locked file comes from the Nix
  store. A request for the index page of a locked project gets a PEP 691
  page that lists only that project's locked files, with their URLs and
  hashes, so PDM resolves to the same files. A find-links location gets an
  HTML page of every locked file. Anything else gets a 404 and a message.
- `off`: the plugin changes nothing.

Without the variable, the plugin serves a project that
`pdm-purifier prefetch` prepared (`transport.activate`). Prefetch keeps the
manifest in `.pdm-purifier/manifest` and the hash of pdm.lock and
pyproject.toml it was made for in `.pdm-purifier/inputs`, beside the
lockfile. When the hash still matches, the plugin sets serve mode and the
git URL rewrites itself, so plain `pdm` works offline. When the project
changed, PDM runs online with a warning. A project that was never prefetched
is unchanged.

The transport sits under the client, so it serves the project's own
indexes, whatever URLs `pyproject.toml` gives them. PDM's configuration
cannot do that: `pypi.ignore_stored_index` drops the indexes of the
configuration, not those of `pyproject.toml`.

## The lock run

`pdm-purifier lock` runs `pdm purifier lock`, the plugin's command
(`plugin/src/pdm_purifier/lock.py`), in record mode with an empty PDM
cache. It writes `pdm-purifier.lock.json`, which holds only what pdm.lock
cannot record, plus its `version`, its `inputsHash` (SHA-256 over pdm.lock
and pyproject.toml) and a `contentHash` (SHA-256 over the rest of the file):

- `urls`: the URL of each file that pdm.lock lists without one, by file
  name. PDM's own finder reports it with platform filtering off.
- `buildRequirements`: the files that building a package installs, of
  packages pdm.lock does not pin: every file of each version a build
  downloaded, so a build on another platform finds its wheels. While the
  lock run builds, the plugin offers PDM only pdm.lock's versions of a
  project that pdm.lock pins (`lock.pinned_versions`), so a build
  requirement that pdm.lock already has resolves to pdm.lock's file and is
  not recorded again.
- `checkouts`: for each version control repository, by URL, the Nix hash of
  its checkout. A git entry also holds the raw commit object and the
  submodule commits (`gitlinks`) of the commit's tree, and a git submodule,
  which pdm.lock does not name, holds its revision. A Mercurial, Subversion
  or Bazaar hash is that of the checkout that nixpkgs' `fetchhg`,
  `fetchsvn` or `fetchbzr` makes. The lock run gets it by building the
  fetcher through the Nix daemon.

Nix derives everything else with `fromTOML` at evaluation: the names,
versions, file names and SHA-256 hashes of the files from pdm.lock, the
revisions of version control packages from pdm.lock, the local
distributions and their scripts from pyproject.toml and the pyproject.toml
of each path package, and the find-links locations from
`[[tool.pdm.source]]`.

The builds are what records the build requirements: the project (sdist,
wheel and editable wheel), each local package, each version control
dependency, and each registry or URL dependency with an sdist and no wheel
for every platform.

## The lock in memory

`lib-overlays/default/lock.nix` reads pdm-purifier.lock.json and refuses it when it has
another format version, lacks a field, has a `contentHash` that does not
match, has an `inputsHash` that does not match pdm.lock and pyproject.toml,
lacks the URL of a file pdm.lock lists without one, or lacks the checkout of
a version control package. Each refusal names `pdm-purifier lock`.

`lib-overlays/default/read-lock.nix` (`lib.pdm-purifier.readLock`) turns the result into
the shape of the purifier contract: `entries` (`lib-overlays/default/fetches.nix`),
`outputs`, and `problems`, which holds the refusal. The builder passes it to
`checkLock` of the shared library, which fails evaluation with the
problems, and takes the entries for its system with `entriesFor`.

## The Nix build

`pkgs/build-pdm-project/default.nix` takes, for each locked version, the wheels
that install with the build's Python on the build's system, or else the
sdist. The shared purifier library (github:nix-purifiers/purifier) decides
which wheels install, from the tags in their file names, and fetches every
entry, with `fetchers` passed through. The builder writes a manifest of all
of them. Its `fetches` attribute is the entries of the lock.

Git dependencies are cloned by PDM, so their entries ask the library for a
`.git` directory. The library rebuilds it from the entry's commit object
and gitlinks, and checks that the commit has the locked ID and the tree
of the checkout. Git's
`url.<path>.insteadOf` sends each clone, and each submodule clone, to its
rebuilt repository. The plugin copies a Mercurial, Subversion or Bazaar
checkout in place of unearth's clone, reports its locked revision, and
writes a `PKG-INFO` with the locked version, which setuptools-scm reads
when there is no version control metadata.

PDM then runs in serve mode: `pdm sync --prod --no-editable` into a
virtual environment for `environment`, `pdm build` for each
`packages.<name>`, and the project's test command with every group
installed for `tests`.

## The command

`pdm-purifier lock` and `update` run the plugin's lock command online and
write the lockfile at `--lockfile`, `PDM_PURIFIER_LOCKFILE`, or next to
pdm.lock. `prefetch` builds the manifest with Nix (`pkgs/cli/cli.nix`),
keeps it alive with a GC root in `.pdm-purifier/` beside the lockfile, and
installs every group with `pdm sync`. `publish` builds a distribution with
Nix and runs `pdm publish --no-build` on what Nix built.
