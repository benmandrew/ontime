{
  description = "ontime — live bus departures dashboard built on UK bus open data";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;

        # Runtime dependencies, shared by the dev shell and the package.
        runtimeDeps = ps: with ps; [
          requests
          fastapi
          uvicorn
          python-dotenv
        ];

        # Test and tooling dependencies, dev shell only.
        devDeps = ps: with ps; [
          pytest
          pytest-cov
          httpx
        ];

        pythonEnv = python.withPackages (ps: runtimeDeps ps ++ devDeps ps);

        ontime = python.pkgs.buildPythonApplication {
          pname = "ontime";
          version = "0.1.0";
          src = ./.;
          format = "pyproject";

          nativeBuildInputs = [ python.pkgs.setuptools ];
          propagatedBuildInputs = runtimeDeps python.pkgs;
          nativeCheckInputs = devDeps python.pkgs;

          # The e2e suite needs no network: it serves the recorded SIRI fixture.
          checkPhase = ''
            runHook preCheck
            ${pythonEnv}/bin/pytest -q tests/
            runHook postCheck
          '';

          meta = {
            description = "Private live-departures dashboard for Greater Manchester bus stops";
            mainProgram = "ontime";
          };
        };
      in
      {
        packages = {
          default = ontime;
          inherit ontime;
        };

        apps.default = {
          type = "app";
          program = "${ontime}/bin/ontime";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.ruff
            pkgs.mypy
            pkgs.sqlite
            pkgs.jq
          ];

          shellHook = ''
            export PYTHONPATH="$PWD:$PYTHONPATH"
            export ONTIME_ROOT="$PWD"
            if [ ! -f .env ]; then
              echo "ontime: no .env yet — cp .env.example .env and add your BODS key"
            fi
            echo "ontime dev shell · python $(${pythonEnv}/bin/python -V 2>&1 | cut -d' ' -f2) · ruff $(${pkgs.ruff}/bin/ruff --version | cut -d' ' -f2)"
            echo "  python -m ontime.ingest    build the timetable cache"
            echo "  python -m ontime.web       serve the dashboard"
            echo "  python -m ontime.history   derive stop events and relearn segments"
            echo "  pytest -q                  run the test suite"
          '';
        };

        checks.default = ontime;
      });
}
