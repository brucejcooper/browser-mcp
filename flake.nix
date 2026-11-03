{
  description = "browser-mcp: an MCP server that browses and searches the web in a real Firefox";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
    in
    {
      packages = forAll (pkgs: rec {
        browser-mcp = pkgs.callPackage ./nix/package.nix { inherit version; };
        default = browser-mcp;
        image = pkgs.callPackage ./nix/image.nix { inherit browser-mcp; };
      });

      nixosModules.default = import ./nix/module.nix self;

      devShells = forAll (pkgs: {
        default = pkgs.mkShell (
          self.packages.${pkgs.stdenv.hostPlatform.system}.browser-mcp.runtimeEnv
          // {
          packages = [
            (pkgs.python3.withPackages (
              ps:
              self.packages.${pkgs.stdenv.hostPlatform.system}.browser-mcp.dependencies
              ++ [
                ps.pytest
                ps.pytest-asyncio
                ps.websockets # the live view's tests' client
              ]
            ))
            pkgs.mitmproxy
            pkgs.openssl
          ]
          ++ self.packages.${pkgs.stdenv.hostPlatform.system}.browser-mcp.runtimeTools;
          shellHook = ''
            export PYTHONPATH=$PWD/src''${PYTHONPATH:+:$PYTHONPATH}
          '';
        }
        );
        # What scripts/release.sh needs, on the runner.
        release = pkgs.mkShell {
          packages = [
            pkgs.coreutils
            pkgs.curl
            pkgs.gzip
            pkgs.jq
          ];
        };
      });

      checks = forAll (pkgs: {
        inherit (self.packages.${pkgs.stdenv.hostPlatform.system}) browser-mcp;
        # The module evaluates, and its unit builds.
        module =
          (nixpkgs.lib.nixosSystem {
            inherit (pkgs.stdenv.hostPlatform) system;
            modules = [
              self.nixosModules.default
              {
                boot.isContainer = true;
                system.stateVersion = "26.05";
                services.browser-mcp = {
                  enable = true;
                  settings.http.host = "0.0.0.0";
                  tokenFile = "/run/keys/browser-mcp-token";
                  settings.limits.per_day = 500;
                };
              }
            ];
          }).config.systemd.units."browser-mcp.service".unit;
      });
    };
}
