# SPDX-License-Identifier: MIT
#
# The `pdm-purifier` command. It runs the PDM of this package set with the
# plugin, which is also the PDM that buildPdmProject runs, and builds with
# the Nix library and the nixpkgs of this package set.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  mkShell,
  python3,
  git,
  nix-prefetch-git,
  path,
  pdm-with-plugin,
  # The source of the shared purifier library.
  purifierSource,
}:
let
  pdm = pdm-with-plugin;
  python = python3.withPackages (ps: [ ps.packaging ]);
in
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "pdm-purifier";
  version = import ../version.nix;

  src = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../cli
      ../../nix/lib
    ];
  };

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin $out/share/pdm-purifier
    cp cli/pdm_purifier_cli.py $out/share/pdm-purifier/
    cp -r nix/lib $out/share/pdm-purifier/nix-lib
    makeWrapper ${python.interpreter} $out/bin/pdm-purifier \
      --add-flags "-I $out/share/pdm-purifier/pdm_purifier_cli.py" \
      --prefix PATH : ${
        # The lock run prefetches git checkouts with nix-prefetch-git, and the
        # other version control systems with their nixpkgs fetchers through
        # the Nix daemon.
        lib.makeBinPath [
          git
          nix-prefetch-git
        ]
      } \
      --set PDM_PURIFIER_NIX_LIB $out/share/pdm-purifier/nix-lib \
      --set PDM_PURIFIER_NIXPKGS ${path} \
      --set PDM_PURIFIER_PDM ${pdm} \
      --set PDM_PURIFIER_LIBRARY ${purifierSource} \
      --set PDM_PURIFIER_PROJECT_INPUTS_TOOLS ${purifierSource}/tools
    # The development loop runs this PDM, whose plugin serves a prefetched
    # project from the locked files.
    ln -s ${pdm}/bin/pdm $out/bin/pdm
    runHook postInstall
  '';

  passthru.devShell = mkShell {
    packages = [ finalAttrs.finalPackage ];
  };

  meta = {
    description = "Lock, run and publish PDM projects that Nix builds";
    license = lib.licenses.mit;
    mainProgram = "pdm-purifier";
  };
})
