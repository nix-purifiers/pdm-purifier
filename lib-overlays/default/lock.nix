# SPDX-License-Identifier: MIT
#
# Reads pdm-purifier.lock.json of a project, and refuses it when it was
# edited by hand, is incomplete, or does not belong to the project's current
# pdm.lock and pyproject.toml. Each refusal names `pdm-purifier lock`, which
# writes a lockfile that passes.
#
# `pdm-purifier lock` stores two hashes (plugin/src/pdm_purifier/lock.py):
# - `inputsHash`, a SHA-256 over pdm.lock and pyproject.toml, in that order;
# - `contentHash`, a SHA-256 over the lockfile without `contentHash`, as the
#   compact JSON with sorted keys that builtins.toJSON makes.
# This file computes both from the project and the lockfile, and compares.
# It then checks that the lockfile has what pdm.lock leaves out: a URL for
# each file pdm.lock lists without one, and a checkout for each version
# control package.
{
  # The project directory, which holds pyproject.toml, pdm.lock, and by
  # default pdm-purifier.lock.json.
  root,
  lockFile ? root + "/pdm-purifier.lock.json",
  pdmLock ? root + "/pdm.lock",
  # What a refusal returns, given its message. The checks of this
  # repository pass a function that keeps the message, to test it.
  onError ? throw,
}:
let
  inherit (builtins)
    addErrorContext
    any
    concatMap
    concatStringsSep
    filter
    hashString
    head
    isAttrs
    length
    pathExists
    readFile
    removeAttrs
    toJSON
    ;

  version = import ./lock-version.nix;
  fix = "Run `pdm-purifier lock`.";

  # A file that is not JSON fails in fromJSON, which no Nix expression can
  # catch, so the message comes as the context of that error.
  data = addErrorContext "while reading ${toString lockFile}, which is not valid JSON. ${fix}" (
    builtins.fromJSON (readFile lockFile)
  );

  fields = [
    "version"
    "contentHash"
    "inputsHash"
    "urls"
    "buildRequirements"
    "checkouts"
  ];
  missing = filter (field: !(data ? ${field})) fields;

  inputsHash = hashString "sha256" (readFile pdmLock + readFile (root + "/pyproject.toml"));
  contentHash = hashString "sha256" (toJSON (removeAttrs data [ "contentHash" ]));

  packages = (builtins.fromTOML (readFile pdmLock)).package or [ ];
  vcsKeys = [
    "git"
    "hg"
    "svn"
    "bzr"
  ];
  isVcs = package: any (key: package ? ${key}) vcsKeys;
  vcsUrl =
    package:
    let
      key = head (filter (key: package ? ${key}) vcsKeys);
    in
    builtins.replaceStrings [ "${key}+" ] [ "" ] package.${key};
  isRegistry = package: !(isVcs package || package ? path || package ? url);

  # The files pdm.lock lists without a URL that the lockfile has no URL for,
  # and the version control repositories it has no checkout for.
  unlocatedFiles = concatMap (
    package:
    map (item: "${item.file} of ${package.name}") (
      filter (item: !(item ? url) && !(data.urls ? ${item.file})) (package.files or [ ])
    )
  ) (filter isRegistry packages);
  missingCheckouts = map vcsUrl (
    filter (package: !(data.checkouts ? ${vcsUrl package})) (filter isVcs packages)
  );
  # The first three items, for a message.
  someOf =
    items:
    if length items > 3 then
      concatStringsSep ", " (builtins.genList (builtins.elemAt items) 3) + ", ..."
    else
      concatStringsSep ", " items;
in
if !(pathExists lockFile) then
  onError "pdm-purifier: ${toString lockFile} is missing. ${fix}"
else if !(isAttrs data) then
  onError "pdm-purifier: pdm-purifier.lock.json is not a pdm-purifier lockfile. ${fix}"
else if (data.version or null) != version then
  onError "pdm-purifier: pdm-purifier.lock.json has format version ${
    toString (data.version or "none")
  }, and this pdm-purifier reads version ${toString version}. ${fix}"
else if missing != [ ] then
  onError "pdm-purifier: pdm-purifier.lock.json has no ${concatStringsSep ", " missing}. ${fix}"
else if data.contentHash != contentHash then
  onError "pdm-purifier: pdm-purifier.lock.json was changed after `pdm-purifier lock` wrote it (its contentHash does not match). ${fix}"
else if data.inputsHash != inputsHash then
  onError "pdm-purifier: pdm-purifier.lock.json does not match pdm.lock and pyproject.toml. ${fix}"
else if unlocatedFiles != [ ] then
  onError "pdm-purifier: pdm-purifier.lock.json has no URL for ${someOf unlocatedFiles}, which pdm.lock lists. ${fix}"
else if missingCheckouts != [ ] then
  onError "pdm-purifier: pdm-purifier.lock.json has no checkout of ${someOf missingCheckouts}, which pdm.lock names. ${fix}"
else
  data // { pdmLock = packages; }
