# Camoufox (https://github.com/daijro/camoufox): Firefox with its fingerprint
# set in the engine, and its own Juggler for Playwright. Upstream's release
# build, pinned, patched to run on NixOS the way nixpkgs patches Playwright's
# Firefox: with firefox-bin's libraries.
#
# To bump: set `version` to the new release's tag (without the "v"), and each
# hash to the sha256 its release page lists for camoufox-<version>-lin.<arch>.zip
# (`nix store prefetch-file <url>` gives the same, as SRI).
{
  lib,
  stdenv,
  fetchurl,
  unzip,
  firefox-bin,
}:
let
  version = "158.0.1-beta.41";
  arch =
    {
      x86_64-linux = "x86_64";
      aarch64-linux = "arm64";
    }
    .${stdenv.hostPlatform.system}
      or (throw "camoufox: no release build for ${stdenv.hostPlatform.system}");
  hash =
    {
      x86_64 = "sha256:3f2eb1d0451fa58e45e43e55f5e6181565069791ae60c0574f0ceb1f01341c33";
      arm64 = "sha256:60e25f6eb95e13d9ec655bc02913d51057e669cb31542e6ace9d2190ab5380af";
    }
    .${arch};
in
stdenv.mkDerivation {
  pname = "camoufox";
  inherit version;

  src = fetchurl {
    url = "https://github.com/daijro/camoufox/releases/download/v${version}/camoufox-${version}-lin.${arch}.zip";
    inherit hash;
  };

  # The zip also bundles fonts for every OS a generated profile may claim
  # (2 GB, Windows' and macOS' among them). This one is Linux, with the
  # package's own fontconfig, as Firefox has it: so only the emoji font
  # Mozilla's own Linux builds ship is kept.
  unpackPhase = ''
    mkdir src
    unzip -q $src -x 'fonts/*' -d src
    unzip -q $src fonts/TwemojiMozilla.ttf -d src
  '';

  inherit (firefox-bin.unwrapped)
    buildInputs
    runtimeDependencies
    appendRunpaths
    patchelfFlags
    ;
  nativeBuildInputs = [ unzip ] ++ firefox-bin.unwrapped.nativeBuildInputs;

  installPhase = ''
    mkdir -p $out
    cp -R src $out/camoufox
  '';

  passthru.executable = "camoufox/camoufox-bin";

  meta = {
    description = "Firefox with its fingerprint set in the engine, for Playwright";
    homepage = "https://github.com/daijro/camoufox";
    license = lib.licenses.mpl20;
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
    platforms = [
      "x86_64-linux"
      "aarch64-linux"
    ];
  };
}
