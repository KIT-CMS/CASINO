#!/usr/bin/env bash
# Exports BASE_DIR, LAW_HOME and LAW_CONFIG_FILE and puts src/ on PYTHONPATH.
# Safe to source from any cwd; HTCondor workers source it with BASE_DIR=${PWD}/repo.

add_to_path() {
    [ -n "$1" ] && export PATH="$1:$PATH"
}

add_to_pythonpath() {
    [ -n "$1" ] && export PYTHONPATH="$1:$PYTHONPATH"
}

setup_dasgoclient() {
    local source_path="${CASINO_DASGOCLIENT_SOURCE:-/cvmfs/cms.cern.ch/common/dasgoclient}"
    local target_dir="${CONDA_PREFIX:-}/bin"

    if [ -n "${CONDA_PREFIX:-}" ] && [ -x "$source_path" ] && [ -d "$target_dir" ]; then
        ln -sf "$source_path" "$target_dir/dasgoclient"
    fi
}

BASE_DIR="$(dirname "$(readlink -f -- "${BASH_SOURCE:-$0}")")"
LAW_HOME="${LAW_HOME:-$BASE_DIR/.law_tmp}"
LAW_CONFIG_FILE="${LAW_CONFIG_FILE:-$BASE_DIR/law.cfg}"
export BASE_DIR LAW_HOME LAW_CONFIG_FILE

mkdir -p "${LAW_HOME}"

add_to_pythonpath "$BASE_DIR/src"
add_to_pythonpath "$BASE_DIR"

# populated by BundleRepo for HTCondor workers
[ -d "$BASE_DIR/vendor/site-packages" ] && add_to_pythonpath "$BASE_DIR/vendor/site-packages"
[ -d "$BASE_DIR/vendor/bin" ] && add_to_path "$BASE_DIR/vendor/bin"
[ -d "$BASE_DIR/.venv/bin" ] && add_to_path "$BASE_DIR/.venv/bin"

setup_dasgoclient
