#!/usr/bin/env bash
set -euo pipefail
read -r -a docker_command <<< "${DOCKER:-docker}"
exec "${docker_command[@]}" "$@"
