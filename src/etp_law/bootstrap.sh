#!/usr/bin/env bash

# Worker bootstrap for jobs without a CMSSW area; see bootstrap_cmssw_job.sh for the structure.

export USER="{{user}}"
export JOB_DIR="${PWD}"
export BASE_DIR="${PWD}/repo"

# analysis repo bundle
(
    mkdir -p "${BASE_DIR}"
    cd "${BASE_DIR}" || exit "$?"
    (
        source "${JOB_DIR}/{{wlcg_tools}}" || return "$?"
        law_wlcg_get_file "{{repo_uris}}" '{{repo_pattern}}' "${BASE_DIR}/repo.tgz" "5" || return "$?"
    ) || exit "$?"
    tar -xzf "repo.tgz" || exit "$?"
    rm -f "repo.tgz"
    echo "Analysis repo setup done."
) || return "$?"

cd "${BASE_DIR}" || return "$?"

# The container's OSG WN client seeds these with python3.6 paths and LCG only prepends, so
# clear them first or LCG's gfal2 bindings abort on import.
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH GFAL_PLUGIN_DIR GFAL_CONFIG_DIR

source "{{lcg_stack}}" || return "$?"
source "${BASE_DIR}/setup.sh" || return "$?"
return "0"
