# SPDX-License-Identifier: MIT
#
# The default package overlay. It adds one attribute to the package set, the
# scope `pkgs.pdm-purifier`, which holds the packages of pkgs/ (`.plugin`,
# `.pdm-with-plugin`, `.cli`) and the builder `.buildPdmProject`
# (pkgs/build-pdm-project/default.nix). The builder is a package for
# `callPackage` in the scope, so `buildPdmProject.override` replaces the
# packages it takes, and `pdm-purifier.overrideScope` replaces what the
# whole scope shares. A consumer that lists this flake in `projects` holds
# the overlay as `pdm-purifier/default`, which its package sets apply by
# default.
#
# The command takes the source of the shared purifier library, for the
# builds it runs outside a flake.
{ closure-lib, closure-inputs, ... }:
let
  packages = closure-lib.caisson.nixpkgs.mkPackagesOverlay ../../pkgs "pdm-purifier";
in
{
  overlay =
    final: prev:
    let
      added = packages final prev;
    in
    added
    // {
      pdm-purifier = added.pdm-purifier.overrideScope (
        self: super: {
          cli = super.cli.override { purifierSource = closure-inputs.purifier; };
          buildPdmProject = self.callPackage ../../pkgs/build-pdm-project/default.nix {
            inherit (closure-lib) purifier;
          };
        }
      );
    };
}
