# The server, with the Firefox, fonts and tools it runs with. The NixOS module
# and the Docker image both use this, so they share one fingerprint.
{
  lib,
  callPackage,
  python3Packages,
  playwright-driver,
  runCommand,
  unzip,
  zip,
  xvfb,
  x11vnc,
  novnc,
  nss,
  mesa,
  libGL,
  makeFontsConf,
  dejavu_fonts,
  liberation_ttf,
  noto-fonts-color-emoji,
  version,
}:
let
  # Playwright's Firefox only.
  upstreamBrowsers = playwright-driver.browsers.override {
    withChromium = false;
    withChromiumHeadlessShell = false;
    withWebkit = false;
    withFfmpeg = false;
  };
  # With Juggler (Playwright's driver inside Firefox) patched in one place:
  # with no locale given, it still sets every page's language override, to the
  # string "undefined", which then becomes navigator.languages and the
  # Accept-Language header. Patched, it sets none, so Firefox's own
  # intl.accept_languages applies, as in a person's Firefox. The rest is
  # Playwright's build as it is; the binaries are copied because Firefox finds
  # its omni.ja next to its real path.
  browsers = runCommand "playwright-browsers-firefox" { nativeBuildInputs = [ unzip zip ]; } ''
    mkdir $out
    for d in ${upstreamBrowsers}/*; do
      name=$(basename $d)
      case $name in
        firefox-*) cp -rs --no-preserve=mode $(readlink -f $d) $out/$name ;;
        *) ln -s $(readlink -f $d) $out/$name ;;
      esac
    done
    chmod -R u+w $out
    for ff in $out/firefox-*/firefox; do
      from=${upstreamBrowsers}/''${ff#$out/}
      rm $ff/omni.ja $ff/firefox $ff/firefox-bin
      cp $from/firefox $from/firefox-bin $ff/
      mkdir omni
      unzip -q $from/omni.ja -d omni
      substituteInPlace omni/chrome/juggler/content/TargetRegistry.js --replace-fail \
        'languageOverride = this._browserContext.languageOverride;' \
        'languageOverride = this._browserContext.languageOverride ?? "";'
      (cd omni && find . -type f | sort | zip -q -0 -X -D $ff/omni.ja -@)
      rm -r omni
    done
  '';
  # The other engine (browser.engine = "camoufox"), in the package too, so
  # switching is a config change only.
  camoufox = callPackage ./camoufox.nix { };
  fontsConf = makeFontsConf {
    fontDirectories = [
      dejavu_fonts
      liberation_ttf
      noto-fonts-color-emoji
    ];
  };
  runtimeTools = [
    xvfb
    nss.tools
    # The live view a person solves a challenge in.
    x11vnc
  ];
  runtimeEnv = {
    PLAYWRIGHT_BROWSERS_PATH = "${browsers}";
    BROWSER_MCP_CAMOUFOX = "${camoufox}/${camoufox.executable}";
    # WebGL, as desktop Firefox always has it: Mesa's software renderer
    # (llvmpipe), through libglvnd, with no GPU or /dev/dri needed.
    LD_LIBRARY_PATH = lib.makeLibraryPath [
      libGL
      mesa
    ];
    __EGL_VENDOR_LIBRARY_DIRS = "${mesa}/share/glvnd/egl_vendor.d";
    LIBGL_DRIVERS_PATH = "${mesa}/lib/dri";
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "1";
    FONTCONFIG_FILE = "${fontsConf}";
    BROWSER_MCP_NOVNC_DIR = "${novnc}/share/webapps/novnc";
  };
in
python3Packages.buildPythonApplication {
  pname = "browser-mcp";
  inherit version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../src
      ../tests
      ../examples
    ];
  };

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    mcp
    playwright
    tldextract
    prometheus-client
    uvicorn
    starlette
    wsproto
  ];

  nativeCheckInputs = with python3Packages; [
    pytestCheckHook
    pytest-asyncio
    websockets # the live view's tests' client
  ];
  # The browser tests need a display and run in CI's dev shell instead.
  disabledTestMarks = [
    "browser"
    "live"
  ];

  makeWrapperArgs =
    lib.mapAttrsToList (k: v: "--set-default ${k} ${v}") runtimeEnv
    ++ [ "--prefix PATH : ${lib.makeBinPath runtimeTools}" ];

  passthru = {
    inherit browsers camoufox runtimeTools runtimeEnv;
  };

  meta = {
    description = "An MCP server that browses and searches the web in a real Firefox";
    license = lib.licenses.mit;
    mainProgram = "browser-mcp";
    platforms = lib.platforms.linux;
  };
}
