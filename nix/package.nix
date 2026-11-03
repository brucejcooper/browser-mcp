# The server, with the Firefox, fonts and tools it runs with. The NixOS module
# and the Docker image both use this, so they share one fingerprint.
{
  lib,
  python3Packages,
  playwright-driver,
  xvfb,
  x11vnc,
  novnc,
  nss,
  makeFontsConf,
  dejavu_fonts,
  liberation_ttf,
  noto-fonts-color-emoji,
  version,
}:
let
  # Playwright's Firefox only.
  browsers = playwright-driver.browsers.override {
    withChromium = false;
    withChromiumHeadlessShell = false;
    withWebkit = false;
    withFfmpeg = false;
  };
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
    inherit browsers runtimeTools runtimeEnv;
  };

  meta = {
    description = "An MCP server that browses and searches the web in a real Firefox";
    license = lib.licenses.mit;
    mainProgram = "browser-mcp";
    platforms = lib.platforms.linux;
  };
}
