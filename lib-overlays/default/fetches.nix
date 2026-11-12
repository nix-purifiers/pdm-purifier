# SPDX-License-Identifier: MIT
#
# The download entries of a project, in the shapes of the shared purifier
# library, from pdm.lock and pdm-purifier.lock.json (as lock.nix returns
# them, with pdm.lock's packages under `pdmLock`).
#
# - `files`: every file pdm.lock lists, with its URL from pdm.lock or from
#   the lockfile's `urls`, and every file of the lockfile's
#   `buildRequirements`. Each entry also has `name`, `version`, `file` and
#   `requiresPython`, which the manifest's index pages list.
# - `checkouts`: each version control package of pdm.lock, at the revision
#   pdm.lock pins, with the hash (and for git the commit object) from the
#   lockfile's `checkouts`, and each git submodule the lockfile adds. Each
#   entry also has `name`, and those of pdm.lock have `version` and
#   `subdirectory`.
{ lib, lock }:
let
  inherit (builtins)
    attrNames
    concatMap
    convertHash
    filter
    head
    match
    ;

  vcsKeys = [
    "git"
    "hg"
    "svn"
    "bzr"
  ];
  vcsOf = package: lib.findFirst (key: package ? ${key}) null vcsKeys;
  isRegistry = package: vcsOf package == null && !(package ? path || package ? url);

  # "sha256:<hex>" of pdm.lock, as an SRI hash.
  sri =
    hash:
    convertHash {
      hash = lib.removePrefix "sha256:" hash;
      hashAlgo = "sha256";
      toHashFormat = "sri";
    };
  fileOfUrl = url: lib.last (lib.splitString "/" (head (lib.splitString "#" url)));
  short = rev: builtins.substring 0 12 rev;

  fileEntry =
    {
      name,
      version,
      file,
      url,
      hash,
      requiresPython,
    }:
    {
      inherit
        name
        version
        file
        url
        hash
        requiresPython
        ;
      path = "files/${file}";
      mirrors = [ ];
    };

  registryFiles =
    package:
    map (
      item:
      let
        file = if item ? url then fileOfUrl item.url else item.file;
      in
      fileEntry {
        inherit (package) name version;
        inherit file;
        url = if item ? url then head (lib.splitString "#" item.url) else lock.urls.${file};
        hash = sri item.hash;
        requiresPython = package.requires_python or null;
      }
    ) (package.files or [ ]);

  urlFiles =
    package:
    map (
      item:
      fileEntry {
        inherit (package) name version;
        file = fileOfUrl package.url;
        url = head (lib.splitString "#" package.url);
        hash = sri item.hash;
        requiresPython = package.requires_python or null;
      }
    ) (package.files or [ ]);

  files =
    concatMap registryFiles (filter isRegistry lock.pdmLock)
    ++ concatMap urlFiles (filter (package: package ? url && vcsOf package == null) lock.pdmLock)
    ++ map (entry: fileEntry entry) lock.buildRequirements;

  vcsPackages = filter (package: vcsOf package != null) lock.pdmLock;
  urlOf = package: lib.removePrefix "${vcsOf package}+" package.${vcsOf package};

  checkout =
    package:
    let
      vcs = vcsOf package;
      url = urlOf package;
      rev = toString package.revision;
      locked = lock.checkouts.${url};
      common = {
        inherit (package) name version;
        subdirectory = package.subdirectory or null;
        path = "${vcs}/${package.name}-${short rev}";
        inherit (locked) hash;
      };
    in
    common
    // {
      ${vcs} =
        {
          git = {
            inherit url rev;
            submodules = false;
            leaveDotGit = true;
            inherit (locked) commitObject;
          };
          # A plain `svn checkout`, which is what PDM runs, fetches externals
          # and expands keywords.
          svn = {
            inherit url rev;
            ignoreExternals = false;
            ignoreKeywords = false;
          };
          # `hg update` after PDM's clone checks out subrepositories.
          hg = {
            inherit url rev;
            subrepos = true;
          };
          bzr = { inherit url rev; };
        }
        .${vcs};
    };

  # A git submodule, which pdm.lock does not name, is named for its URL.
  submodule =
    url:
    let
      locked = lock.checkouts.${url};
      base = lib.removeSuffix ".git" (lib.last (filter (part: part != "") (lib.splitString "/" url)));
      name = if match "[A-Za-z0-9._+-]+" base != null then base else "submodule";
    in
    {
      inherit name;
      path = "git/${name}-${short locked.rev}";
      git = {
        inherit url;
        inherit (locked) rev commitObject;
        submodules = false;
        leaveDotGit = true;
      };
      inherit (locked) hash;
    };

  submodules = map submodule (filter (url: lock.checkouts.${url} ? rev) (attrNames lock.checkouts));
in
{
  inherit files;
  checkouts = map checkout vcsPackages ++ submodules;
}
