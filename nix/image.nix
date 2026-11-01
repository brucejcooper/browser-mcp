# The Docker image: the same server, Firefox and Python as the NixOS package,
# so it shows sites the same fingerprint. Built by Nix, for linux/amd64 and
# linux/arm64; `docker load` takes the streamed tarball.
{
  dockerTools,
  cacert,
  browser-mcp,
}:
let
  uid = "1000";
  nss = dockerTools.fakeNss.override {
    extraPasswdLines = [ "mcp:x:${uid}:${uid}:browser-mcp:/state:/noshell" ];
    extraGroupLines = [ "mcp:x:${uid}:" ];
  };
in
dockerTools.streamLayeredImage {
  name = "browser-mcp";
  tag = browser-mcp.version;
  contents = [
    browser-mcp
    cacert
    nss
    # Xvfb runs xkbcomp through /bin/sh.
    dockerTools.binSh
  ];
  fakeRootCommands = ''
    mkdir -p tmp/.X11-unix state logs config certs
    chmod 1777 tmp tmp/.X11-unix
    chown ${uid}:${uid} state logs
  '';
  config = {
    Entrypoint = [ "${browser-mcp}/bin/browser-mcp" ];
    # Inside the container it binds every address, so the bearer token is
    # required (BROWSER_MCP_TOKEN_FILE); publish the port on 127.0.0.1 only.
    Cmd = [
      "--transport"
      "http"
      "--host"
      "0.0.0.0"
      "--port"
      "8080"
    ];
    User = "${uid}:${uid}";
    WorkingDir = "/state";
    Env = [
      "HOME=/state"
      "XDG_CACHE_HOME=/tmp/cache"
      "BROWSER_MCP_STATE_DIR=/state"
      "SSL_CERT_FILE=${cacert}/etc/ssl/certs/ca-bundle.crt"
    ];
    ExposedPorts."8080/tcp" = { };
    Volumes = {
      "/state" = { };
      "/logs" = { };
    };
    Labels = {
      "org.opencontainers.image.title" = "browser-mcp";
      "org.opencontainers.image.description" = browser-mcp.meta.description;
      "org.opencontainers.image.licenses" = "MIT";
      "org.opencontainers.image.version" = browser-mcp.version;
    };
  };
}
