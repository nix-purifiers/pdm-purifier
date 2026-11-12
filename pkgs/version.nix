# SPDX-License-Identifier: MIT
# Nix source builds identify their contents; publishers pass a release version.
let
  source = builtins.path {
    path = ../.;
    name = "pdm-purifier-source";
    filter = path: _: builtins.baseNameOf path != ".git";
  };
in
"0.0.0.dev0+${builtins.substring 0 12 (builtins.baseNameOf source)}"
