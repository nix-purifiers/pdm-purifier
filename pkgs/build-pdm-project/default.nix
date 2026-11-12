# SPDX-License-Identifier: MIT
#
# Builds a PDM project in the Nix sandbox from pdm.lock and
# pdm-purifier.lock.json.
#
# For the system and Python it builds with, this function picks the locked
# files that installing the project can need: for each locked version, its
# wheels that install here, or else its sdist. It fetches each file and each
# locked git checkout, and writes a manifest of them. PDM then runs with the
# pdm-purifier plugin in serve mode: the plugin answers every request PDM
# makes from the manifest, so PDM installs from pdm.lock, and builds what it
# has to build, with no network and no other files.
#
# The result has the attributes of the purifier contract (CONTRACT.md in
# github:nix-purifiers/purifier):
# - `packages.<name>`, for the project and each local package it depends on
#   by path: what `pdm build` makes, its sdist and wheel, with the attributes
#   `sdist` and `wheel` naming the two files.
# - `tests`: the project's tests, run in the sandbox with every locked
#   group installed.
# - `release`: the project's own sdist and wheel, which
#   `pdm-purifier publish` uploads. Null for a project that is not a
#   distribution.
# - `fetches`: the shared library's entries of every file and checkout the
#   build fetches.
# and three of its own:
# - `environment`: a virtual environment with the project and its runtime
#   dependencies installed (the locked groups in `groups`), and the
#   project's scripts in `bin/`.
# - `programs.<script>`: each console and GUI script of the project and its
#   local packages, from `environment`, for `nix run`.
# - `manifest`: the manifest that the plugin serves PDM from, which
#   `pdm-purifier prefetch` keeps in .pdm-purifier/manifest.
#
# The flake's `overlays.default` adds this file to the scope
# `pkgs.pdm-purifier` as `buildPdmProject`, through `callPackage`. It takes
# the packages it needs by name, and `buildPdmProject.override` replaces any
# of them.
{
  lib,
  stdenv,
  runCommand,
  writeText,
  git,
  glibc,
  python3,
  # The fetchers and the tools that the shared library's fetchEntry builds
  # with, for the kinds of entry this lock has.
  fetchurl,
  fetchgit,
  fetchhg,
  fetchsvn,
  fetchbzr,
  unzip,
  zstd,
  # The PDM, with the pdm-purifier plugin, that runs every build.
  pdm-with-plugin,
  # The shared purifier library (github:nix-purifiers/purifier).
  purifier,
}:
{
  # The directory that holds everything the project reads: the project
  # itself and the local packages it depends on by path.
  src,
  # Replacements for the fetchers of the shared purifier library, passed
  # to its fetchEntry unchanged, such as a `fetchurl` that sends
  # credentials or uses a mirror.
  fetchers ? { },
  # The project directory inside `src`, which holds pyproject.toml, pdm.lock
  # and pdm-purifier.lock.json.
  projectDir ? ".",
  # The Python the project builds and runs with. It must satisfy the
  # project's requires-python.
  python ? python3,
  # The pdm.lock groups that `environment` installs.
  groups ? [ "default" ],
  # The command `tests` runs in the project directory. By default it is
  # `pdm run test` when the project defines a `test` script under
  # [tool.pdm.scripts], and `pdm run python -m pytest` otherwise.
  testCommand ? null,
  # pdm-purifier.lock.json. A file outside `src` keeps the build external:
  # `src` holds pdm.lock and the project, and the flake that calls the
  # builder keeps pdm-purifier.lock.json.
  lockFile ? (if projectDir == "." then src else src + "/${projectDir}") + "/pdm-purifier.lock.json",
  # pdm.lock. A file outside `src` takes the place of the copy in `src`, for
  # a project whose committed pdm.lock is missing, stale or written by
  # another version of PDM.
  pdmLock ? (if projectDir == "." then src else src + "/${projectDir}") + "/pdm.lock",
  # What the builds of sdists and local packages need beyond Python and a C
  # compiler: tools such as pkg-config, and the system libraries a C
  # extension links against.
  nativeBuildInputs ? [ ],
  buildInputs ? [ ],
  # Environment variables for every PDM run in the build, such as
  # `PDM_BUILD_SCM_VERSION`, which a project that takes its version from git
  # needs because `src` has no .git directory.
  env ? { },
  projectDependencies ? null,
}:
let
  inherit (builtins)
    attrNames
    concatMap
    convertHash
    filter
    groupBy
    listToAttrs
    map
    ;
  pdmWithPlugin = pdm-with-plugin;

  originalRoot = if projectDir == "." then src else src + "/${projectDir}";
  # The source the builds read: `src`, with the pdm.lock given in place of
  # the copy in it when that file is not the copy in `src`.
  src' =
    if toString pdmLock == toString (originalRoot + "/pdm.lock") then
      src
    else
      runCommand "source" { } ''
        cp -r ${src} "$out"
        chmod -R u+w "$out"
        cp ${pdmLock} "$out"/${lib.escapeShellArg projectDir}/pdm.lock
      '';
  # The lock in memory (read-lock.nix), which checkLock of the shared
  # library refuses with its problems, each naming `pdm-purifier lock`.
  lock = purifier.checkLock (
    import ../../lib-overlays/default/read-lock.nix { inherit lib; } {
      inherit
        src
        projectDir
        lockFile
        pdmLock
        ;
    }
  );
  inputRecords = lock.projectInputs;
  dependencies =
    if projectDependencies == null then
      purifier.lockedProjectInputs {
        inputs = inputRecords;
        system = stdenv.hostPlatform.system;
      }
    else
      projectDependencies;
  supplied = purifier.projectInputs {
    inherit dependencies;
    formats = [ "python" ];
  };
  hasInputs = supplied != [ ];
  inputViews = writeText "pdm-python-inputs.json" (
    builtins.toJSON (
      map (input: {
        repository = toString input.repository;
        catalog = input.repository.catalog;
      }) supplied
    )
  );
  oldRepositories = writeText "pdm-python-original-repositories.json" (
    builtins.toJSON (map (input: input.nativeRepository) inputRecords)
  );
  inputBindings =
    runCommand "pdm-python-bindings"
      { nativeBuildInputs = [ (python.withPackages (ps: [ ps.packaging ])) ]; }
      ''
        python ${./python_bindings.py} --tools ${purifier.projectInputsTool} \
          --views ${inputViews} --old-repositories ${oldRepositories} --destination "$out"
      '';

  locked = purifier.entriesFor stdenv.hostPlatform.system lock;
  # The store path of a download entry of the lock, from the shared purifier
  # library: a file, or a version control checkout. A git entry with
  # `leaveDotGit` comes with a .git directory that the library rebuilds
  # from the entry's commit object, which PDM clones from.
  fetchEntry = purifier.fetchEntry (
    {
      inherit
        runCommand
        unzip
        zstd
        python3
        writeText
        fetchurl
        fetchgit
        fetchhg
        fetchsvn
        fetchbzr
        ;
    }
    // fetchers
  );
  pyproject = builtins.fromTOML (builtins.readFile (originalRoot + "/pyproject.toml"));
  projectName = pyproject.project.name or "project";

  platform = stdenv.hostPlatform;
  target = "Python ${python.pythonVersion} on ${platform.system}";
  installs = purifier.wheels (
    {
      inherit (python) pythonVersion;
    }
    // (
      if platform.isDarwin then
        {
          # macOS wheel tags name the CPU arm64.
          arch = "arm64";
          macosVersion = platform.darwinMinVersion;
        }
      else
        {
          arch = platform.parsed.cpu.name;
          glibcVersion = glibc.version;
        }
    )
  );

  isWheel = entry: lib.hasSuffix ".whl" entry.file;

  # For each locked version, the files a build on this system may install:
  # its wheels that install, or else its sdist.
  versions = groupBy (entry: "${entry.name}==${entry.version}") (filter (entry: entry ? url) locked);
  chosen = concatMap (
    key:
    let
      entries = versions.${key};
      wheels = filter (entry: isWheel entry && installs entry.file) entries;
      sdists = filter (entry: !isWheel entry) entries;
    in
    if wheels != [ ] then
      wheels
    else if sdists != [ ] then
      sdists
    else
      throw "pdm-purifier: no wheel of ${key} installs with ${target}, and it has no sdist"
  ) (attrNames versions);

  fetched = map (
    entry:
    entry
    // {
      store = fetchEntry entry;
      sha256 = convertHash {
        inherit (entry) hash;
        toHashFormat = "base16";
      };
    }
  ) chosen;

  checkouts = map (entry: entry // { store = fetchEntry entry; }) (
    filter (entry: !(entry ? url)) locked
  );
  gitCheckouts = filter (entry: entry ? git) checkouts;
  otherCheckouts = filter (entry: !(entry ? git)) checkouts;
  vcsOf =
    entry:
    lib.findFirst (kind: entry ? ${kind}) (throw "unreachable") [
      "hg"
      "svn"
      "bzr"
    ];

  manifest = writeText "pdm-purifier-manifest.json" (
    builtins.toJSON {
      pythonInputs = if hasInputs then "${inputBindings}/inputs.json" else null;
      files = listToAttrs (
        map (entry: {
          name = entry.url;
          value = "${entry.store}";
        }) fetched
      );
      projects = lib.mapAttrs (
        _: entries:
        map (entry: {
          inherit (entry) file url sha256;
          requiresPython = entry.requiresPython or null;
        }) entries
      ) (groupBy (entry: entry.name) fetched);
      findLinks = map (source: source.url) (
        filter (source: (source.type or "index") == "find_links") (pyproject.tool.pdm.source or [ ])
      );
      # Each locked git repository URL, to its checkout in the store.
      git = map (entry: {
        inherit (entry.git) url;
        store = "${entry.store}";
      }) gitCheckouts;
      # Each locked Mercurial, Subversion and Bazaar checkout, which the
      # plugin copies in place of a clone.
      vcs = map (
        entry:
        let
          kind = vcsOf entry;
        in
        {
          vcs = kind;
          inherit (entry.${kind}) url rev;
          inherit (entry) name version;
          subdirectory = entry.subdirectory or null;
          store = "${entry.store}";
        }
      ) otherCheckouts;
    }
  );

  # Git reads its configuration from GIT_CONFIG_COUNT, GIT_CONFIG_KEY_<n>
  # and GIT_CONFIG_VALUE_<n>. Each locked repository URL is rewritten to its
  # checkout in the store, which has the locked commit in its .git.
  gitConfig = [
    {
      key = "protocol.file.allow";
      value = "always";
    }
    {
      key = "safe.directory";
      value = "*";
    }
  ]
  ++ map (entry: {
    key = "url.${entry.store}.insteadOf";
    value = entry.git.url;
  }) gitCheckouts;

  gitEnvironment = lib.concatStrings (
    lib.imap0 (i: item: ''
      export GIT_CONFIG_KEY_${toString i}=${lib.escapeShellArg item.key}
      export GIT_CONFIG_VALUE_${toString i}=${lib.escapeShellArg item.value}
    '') gitConfig
  );

  # The environment every PDM run in the sandbox gets.
  setupPdm = ''
    # The Python setup hook of nixpkgs puts the site-packages of PDM and its
    # dependencies on PYTHONPATH, which the project's virtual environment and
    # the isolated build environments would otherwise import before their
    # locked packages. PDM sets its own search path.
    unset PYTHONPATH NIX_PYTHONPATH
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
    export PDM_CACHE_DIR="$TMPDIR/pdm-cache"
    export PDM_CHECK_UPDATE=false
    export PDM_VENV_BACKEND=venv
    export PDM_PURIFIER_MODE=serve
    export PDM_PURIFIER_MANIFEST=${manifest}
    export GIT_CONFIG_COUNT=${toString (lib.length gitConfig)}
    ${gitEnvironment}
  '';

  buildTools = [
    pdmWithPlugin
    git
    python
  ]
  ++ nativeBuildInputs;

  # The project's local distributions, from the lock's `outputs`
  # (read-lock.nix), each with its name, path and scripts.
  outputs = lib.mapAttrsToList (name: output: output // { inherit name; }) lock.outputs;
  isProjectOutput = output: output.path == ".";
  projectIsDistribution = lib.any isProjectOutput outputs;

  environment = stdenv.mkDerivation {
    name = "${projectName}-environment";
    inherit buildInputs env;
    src = src';
    nativeBuildInputs = buildTools;
    dontConfigure = true;
    dontFixup = true;
    buildPhase = ''
      runHook preBuild
      ${setupPdm}
      cd ${lib.escapeShellArg projectDir}
      ${python.interpreter} -m venv --without-pip "$out"
      # PDM installs into the virtual environment of the interpreter the
      # project uses.
      pdm use --first --ignore-remembered "$out/bin/python"
      export PDM_PURIFIER_EXPORT_WHEELS="$out/nix-support/wheels"
      pdm sync --prod --no-editable ${
        lib.concatMapStringsSep " " (group: "-G ${lib.escapeShellArg group}") groups
      } ${lib.optionalString (!projectIsDistribution) "--no-self"}
      runHook postBuild
    '';
    dontInstall = true;
    passthru = { inherit manifest; };
  };

  package =
    output:
    let
      dist = stdenv.mkDerivation {
        name = "${output.name}-dist";
        inherit buildInputs env;
        src = src';
        nativeBuildInputs = buildTools;
        dontConfigure = true;
        dontFixup = true;
        buildPhase = ''
          runHook preBuild
          ${setupPdm}
          cd ${lib.escapeShellArg projectDir}
          pdm use --first --ignore-remembered --project ${lib.escapeShellArg output.path} ${python.interpreter}
          pdm build --no-clean --dest "$out" --project ${lib.escapeShellArg output.path}
          # The gzip header of an sdist holds the time of the build, which
          # makes two builds of the same sources differ. Compressing the
          # archive again without a name and a time makes the bytes depend
          # on the sources alone.
          for sdist in "$out"/*.tar.gz; do
            gzip -dc "$sdist" | gzip -n > "$sdist.tmp"
            mv "$sdist.tmp" "$sdist"
          done
          runHook postBuild
        '';
        dontInstall = true;
      };
      # The file names hold the version, which a build may compute, so each
      # file is copied out under a name of its own.
      copy =
        kind: pattern:
        runCommand "${output.name}-${kind}" { } ''
          cp ${dist}/${pattern} "$out"
        '';
    in
    dist
    // {
      wheel = copy "wheel" "*.whl";
      sdist = copy "sdist" "*.tar.gz";
    }
    // {
      projections = projectionFor [ output.name ] output.name;
    };

  publicationEnvironment = environment.overrideAttrs (_: {
    name = "${projectName}-publication-environment";
    buildPhase = ''
      runHook preBuild
      ${setupPdm}
      cd ${lib.escapeShellArg projectDir}
      ${python.interpreter} -m venv --without-pip "$out"
      pdm use --first --ignore-remembered "$out/bin/python"
      export PDM_PURIFIER_EXPORT_WHEELS="$out/nix-support/wheels"
      pdm sync --no-editable ${
        lib.concatMapStringsSep " " (group: "-G ${lib.escapeShellArg group}") (
          (builtins.fromTOML (builtins.readFile pdmLock)).metadata.groups or [ "default" ]
        )
      } ${lib.optionalString (!projectIsDistribution) "--no-self"}
      runHook postBuild
    '';
  });
  projectionFor =
    roots: name:
    let
      scope =
        runCommand "${name}-publication-scope"
          {
            nativeBuildInputs = [ (python3.withPackages (ps: [ ps.packaging ])) ];
            outputs = [
              "out"
              "runtime"
            ];
            repositories = builtins.toJSON (map (output: toString packages.${output.name}) outputs);
            roots = builtins.toJSON roots;
            passAsFile = [
              "repositories"
              "roots"
            ];
          }
          ''
            python ${./wheel_scope.py} --environment ${publicationEnvironment} \
              --repositories "$repositoriesPath" --roots "$rootsPath" \
              --destination "$out" --runtime "$runtime"
            mkdir -p "$runtime/nix-support"
            printf '%s\n' ${publicationEnvironment} > "$runtime/nix-support/inputs"
          '';
    in
    purifier.pythonProjections {
      repository = scope.out;
      runtimeRepository = scope.runtime;
      inherit dependencies runCommand python3;
    };
  projections = projectionFor (
    if projectOutput == null then pyproject.project.dependencies or [ ] else [ projectOutput.name ]
  ) projectName;

  programs = listToAttrs (
    concatMap (
      output:
      map (script: {
        name = script;
        value = runCommand script { meta.mainProgram = script; } ''
          mkdir -p "$out/bin"
          ln -s ${environment}/bin/${script} "$out/bin/${script}"
        '';
      }) output.scripts
    ) outputs
  );
  packages = listToAttrs (
    map (output: {
      inherit (output) name;
      value = package output;
    }) outputs
  );

  scripts = pyproject.tool.pdm.scripts or { };
  test =
    if testCommand != null then
      testCommand
    else if scripts ? test then
      "pdm run test"
    else
      "pdm run python -m pytest";

  tests = stdenv.mkDerivation {
    name = "${projectName}-tests";
    inherit buildInputs env;
    src = src';
    nativeBuildInputs = buildTools;
    dontConfigure = true;
    dontFixup = true;
    buildPhase = ''
      runHook preBuild
      ${setupPdm}
      cd ${lib.escapeShellArg projectDir}
      ${python.interpreter} -m venv --without-pip "$TMPDIR/venv"
      pdm use --first --ignore-remembered "$TMPDIR/venv/bin/python"
      # Every locked group, the development groups among them.
      pdm sync ${lib.optionalString (!projectIsDistribution) "--no-self"}
      ${test}
      runHook postBuild
    '';
    installPhase = ''
      touch "$out"
    '';
  };

  projectOutput = lib.findFirst isProjectOutput null outputs;
in
{
  inherit
    environment
    manifest
    projections
    programs
    packages
    tests
    ;
  release = if projectOutput == null then null else packages.${projectOutput.name};
  # Every entry of the lock, for every system.
  fetches = lock.entries;
}
