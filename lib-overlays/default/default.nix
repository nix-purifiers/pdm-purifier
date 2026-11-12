# SPDX-License-Identifier: MIT
#
# The `pdm-purifier` library namespace, with the reader of the lockfiles into
# the lock in memory of the purifier contract:
#
#   lib.pdm-purifier.readLock { src, lockFile ? ..., pdmLock ? ..., projectDir ? "." }
#
# (lib-overlays/default/read-lock.nix). The builder `buildPdmProject` is a package in
# `overlays.default`.
{ ... }:
{

  overlay = final: prev: {
    pdm-purifier = (prev.pdm-purifier or { }) // {
      readLock = import ./read-lock.nix { lib = final; };
    };
  };

}
