#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/azure-common.sh"
azure_demo destroy "$@"
