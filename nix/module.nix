# services.browser-mcp: the server as a hardened systemd service, with its own
# user, state directory and Xvfb (the server starts it), over streamable HTTP.
self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.browser-mcp;
  toml = pkgs.formats.toml { };
  inherit (lib) mkOption types;
  configFile = toml.generate "browser-mcp.toml" cfg.settings;
in
{
  options.services.browser-mcp = {
    enable = lib.mkEnableOption "browser-mcp, an MCP server that browses and searches the web in a real Firefox";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "browser-mcp.packages.\${system}.default";
    };

    settings = mkOption {
      description = ''
        The config file, as in the README's reference (`examples/config.toml`):
        `browser`, `output`, `limits`, `deny`, `proxy`, `tls`, `log`, `metrics`,
        `http`, `stdio`, `view`, `search`, `engines` and `sites`. Unset settings keep the
        server's defaults.
      '';
      default = { };
      example = lib.literalExpression ''
        {
          browser = { locale = "en-GB"; timezone = "Europe/London"; };
          limits.per_site_per_day = 100;
          sites.example = { url = "https://shop.example.com/"; input = "#search"; };
        }
      '';
      type = types.submodule {
        freeformType = toml.type;
        options = {
          http = {
            host = mkOption {
              type = types.str;
              default = "127.0.0.1";
              description = "The bind address. Anything but loopback needs `tokenFile`.";
            };
            port = mkOption {
              type = types.port;
              default = 8080;
            };
          };
          log.file = mkOption {
            type = types.str;
            default = "/var/log/browser-mcp/pages.log";
            description = "The page log, one JSON line per page; empty for the journal.";
          };
          tls.ca_files = mkOption {
            type = types.listOf types.path;
            default = [ ];
            description = "Extra CA files to trust, e.g. a TLS-inspecting proxy's root.";
          };
        };
      };
    };

    tokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "The bearer token file (not in the Nix store); required for a bind other than loopback.";
    };

    proxyPasswordFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "The upstream proxy's password, for `settings.proxy.username`.";
    };

    environment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        HTTPS_PROXY = "http://proxy.example:3128";
        NO_PROXY = ".example.internal";
      };
      description = "Extra environment, e.g. the proxy variables.";
    };

    memoryMax = mkOption {
      type = types.str;
      default = "3G";
      description = "The unit's MemoryMax: Firefox, Xvfb and the server together.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open the HTTP port.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          lib.elem cfg.settings.http.host [
            "127.0.0.1"
            "::1"
            "localhost"
          ]
          || cfg.tokenFile != null;
        message = "services.browser-mcp: a bind other than loopback needs tokenFile.";
      }
    ];

    users.users.browser-mcp = {
      isSystemUser = true;
      group = "browser-mcp";
      home = "/var/lib/browser-mcp";
    };
    users.groups.browser-mcp = { };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.settings.http.port ];

    systemd.services.browser-mcp = {
      description = "browser-mcp, an MCP server that browses in a real Firefox";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      environment = {
        HOME = "/var/lib/browser-mcp";
        XDG_CACHE_HOME = "/var/cache/browser-mcp";
        BROWSER_MCP_STATE_DIR = "/var/lib/browser-mcp";
      }
      // lib.optionalAttrs (cfg.proxyPasswordFile != null) {
        BROWSER_MCP_PROXY_PASSWORD_FILE = "%d/proxy-password";
      }
      // cfg.environment;
      serviceConfig = {
        ExecStart = lib.escapeShellArgs (
          [
            (lib.getExe cfg.package)
            "--config"
            configFile
            "--transport"
            "http"
          ]
          ++ lib.optionals (cfg.tokenFile != null) [
            "--token-file"
            "%d/token"
          ]
        );
        LoadCredential =
          lib.optional (cfg.tokenFile != null) "token:${cfg.tokenFile}"
          ++ lib.optional (cfg.proxyPasswordFile != null) "proxy-password:${cfg.proxyPasswordFile}";
        User = "browser-mcp";
        Group = "browser-mcp";
        StateDirectory = "browser-mcp";
        StateDirectoryMode = "0700";
        CacheDirectory = "browser-mcp";
        LogsDirectory = "browser-mcp";
        WorkingDirectory = "/var/lib/browser-mcp";
        Restart = "on-failure";
        RestartSec = 5;
        MemoryMax = cfg.memoryMax;
        TasksMax = 512;

        # Hardening. Firefox needs to map executable memory (its JIT), so no
        # MemoryDenyWriteExecute; its own sandbox does without user namespaces.
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        PrivateUsers = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        SystemCallArchitectures = "native";
        UMask = "0077";
      };
    };
  };
}
