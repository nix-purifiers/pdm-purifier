# SPDX-License-Identifier: MIT
#
# The pdm-purifier plugin, built with `python3`, the interpreter nixpkgs'
# PDM runs on. PDM finds it through its `pdm` entry point.
{
  lib,
  python3,
  version ? import ../version.nix,
}:
let
  python = python3;
in
python.pkgs.buildPythonPackage {
  pname = "pdm-purifier";
  inherit version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../plugin
    ];
  };
  sourceRoot = "source/plugin";

  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail 'version = "0.0.0.dev0"' 'version = "${version}"'
  '';

  build-system = [ python.pkgs.setuptools ];

  # The plugin imports PDM, which is the program that loads it, so PDM is
  # not one of its dependencies.
  dontCheckRuntimeDeps = true;
  doCheck = false;

  meta = {
    description = "The PDM plugin of pdm-purifier";
    license = lib.licenses.mit;
  };
}
