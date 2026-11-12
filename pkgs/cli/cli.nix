# SPDX-License-Identifier: MIT
#
# What the `pdm-purifier` command builds for a project, with the nixpkgs and
# the PDM the command was built with:
# - with `package`, that local distribution's sdist and wheel, which
#   `publish` uploads;
# - without it, the manifest that `pdm-purifier prefetch` keeps for the
#   plugin to serve PDM from, for the Python version `pythonVersion` that
#   the project's environment runs.
#
# `src` is the absolute path of the directory that holds the project and
# the local packages it depends on by path. `projectDir` is the directory
# of the project inside `src`. `lockFile` is the absolute path of
# pdm-purifier.lock.json, inside the project or outside it. `pdmLock` is the
# absolute path of a pdm.lock outside the project, which takes the place of
# the one in `src`.
{
  nixpkgs,
  # The store path of the PDM, with the plugin, that the command runs.
  pdm,
  # The source of the shared purifier library.
  purifier,
  src,
  projectDir ? ".",
  lockFile,
  pdmLock ? null,
  package ? null,
  pythonVersion ? null,
}:
let
  pkgs = import nixpkgs { };

  # A directory without what PDM, Python and git leave in it.
  source = builtins.path {
    path = /. + src;
    name = "source";
    filter =
      p: type:
      !builtins.elem (baseNameOf p) [
        ".git"
        ".venv"
        "__pypackages__"
        "__pycache__"
        "dist"
        ".pdm-build"
        ".pdm-python"
        ".pdm-purifier"
      ];
  };

  # Use the actual interpreter, not only its wheel-selection metadata.
  python =
    if pythonVersion == null then
      pkgs.python3
    else
      pkgs.${"python" + builtins.replaceStrings [ "." ] [ "" ] pythonVersion}
        or (throw "pdm-purifier: nixpkgs has no Python ${pythonVersion}");

  buildPdmProject = pkgs.callPackage ../build-pdm-project/default.nix {
    purifier = import (builtins.storePath purifier) { inherit (pkgs) lib; };
    pdm-with-plugin = builtins.storePath pdm;
  };

  project = buildPdmProject (
    {
      inherit projectDir;
      src = source;
      lockFile = /. + lockFile;
      inherit python;
    }
    // pkgs.lib.optionalAttrs (pdmLock != null) { pdmLock = /. + pdmLock; }
  );
in
if package == null then
  project.manifest
else if !(project.packages ? ${package}) then
  throw "pdm-purifier: ${package} is not a local distribution of this project; they are ${toString (builtins.attrNames project.packages)}"
else if ((builtins.fromJSON (builtins.readFile (/. + lockFile))).projectInputs or [ ]) != [ ] then
  project.packages.${package}.projections.python
else
  project.packages.${package}
