# SPDX-License-Identifier: MIT
#
# readLock of the purifier contract: reads pdm.lock and
# pdm-purifier.lock.json of a project into the lock in memory of the
# purifier library.
#
#   lib.pdm-purifier.readLock { src, lockFile ? <in tree>, pdmLock ? <in tree>, projectDir ? "." }
#   # =>
#   {
#     purifier = "pdm-purifier";
#     entries  = [ ... ];   # every locked file and checkout (fetches.nix),
#                           # for every system
#     outputs  = { <name> = { path; scripts; }; };
#     problems = [ ... ];   # lock.nix's refusals, each naming
#                           # `pdm-purifier lock`
#   }
#
# `outputs` are the local distributions: the project itself when PDM
# builds it (`tool.pdm.distribution`, which defaults to whether
# pyproject.toml has a build-system), and each package pdm.lock names by a
# path to a directory. Each has its path in the project and the console and
# GUI scripts of its pyproject.toml.
{ lib }:
{
  # The directory that holds the project and the local packages it depends
  # on by path.
  src,
  # The project directory inside `src`, which holds pyproject.toml and
  # pdm.lock.
  projectDir ? ".",
  # pdm-purifier.lock.json, in the project or outside `src`.
  lockFile ? (if projectDir == "." then src else src + "/${projectDir}") + "/pdm-purifier.lock.json",
  # pdm.lock, in the project or outside `src`.
  pdmLock ? (if projectDir == "." then src else src + "/${projectDir}") + "/pdm.lock",
}:
let
  inherit (builtins)
    attrNames
    filter
    fromTOML
    pathExists
    readFile
    ;

  root = if projectDir == "." then src else src + "/${projectDir}";
  read = import ./lock.nix {
    inherit root lockFile pdmLock;
    onError = problem: { inherit problem; };
  };
  usable = !(read ? problem);
  entries = import ./fetches.nix {
    inherit lib;
    lock = read;
  };

  pyproject = fromTOML (readFile (root + "/pyproject.toml"));
  normalize =
    name:
    lib.concatStringsSep "-" (filter builtins.isString (builtins.split "[-_.]+" (lib.toLower name)));
  isDistribution = pyproject.tool.pdm.distribution or (pyproject ? build-system);
  localDistribution =
    path:
    let
      metadata = (fromTOML (readFile (root + "/${path}/pyproject.toml"))).project or { };
    in
    {
      name = normalize (metadata.name or (baseNameOf path));
      value = {
        inherit path;
        scripts = attrNames (metadata.scripts or { }) ++ attrNames (metadata.gui-scripts or { });
      };
    };
  pathPackages = filter (
    package: package ? path && pathExists (root + "/${package.path}/pyproject.toml")
  ) read.pdmLock;
in
{
  purifier = "pdm-purifier";
  entries = if usable then entries.files ++ entries.checkouts else [ ];
  projectInputs = if usable then read.projectInputs or [ ] else [ ];
  outputs =
    if usable then
      lib.listToAttrs (
        lib.optional isDistribution (localDistribution ".")
        ++ map (package: localDistribution package.path) pathPackages
      )
    else
      { };
  problems = if usable then [ ] else [ read.problem ];
}
