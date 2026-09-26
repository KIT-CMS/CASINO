#!/usr/bin/env bash

# Worker bootstrap for jobs that need a CMSSW area. "{{name}}" variables are rendered by
# CasinoCMSSWHTCondorWorkflow.htcondor_job_config(). Each bundle fetch runs in a subshell so
# its cd/source cannot leak into the shell that finally runs `python -m law`; `|| exit`/`|| return`
# propagate the real failure code (fetches retry 5x against a contended storage endpoint).

export USER="{{user}}"
export JOB_DIR="${PWD}"
export BASE_DIR="${PWD}/repo"
export CASINO_CMSSW_CACHE_DIR="${PWD}/cmssw-cache"

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

# CMSSW cache bundle -> ${CASINO_CMSSW_CACHE_DIR}
(
    cd "${JOB_DIR}" || exit "$?"
    (
        source "${JOB_DIR}/{{wlcg_tools}}" || return "$?"
        law_wlcg_get_file "{{cmssw_uris}}" '{{cmssw_pattern}}' "${JOB_DIR}/cmssw-cache.tgz" "5" || return "$?"
    ) || exit "$?"
    tar -xzf "cmssw-cache.tgz" || exit "$?"
    rm -f "cmssw-cache.tgz"
    echo "CMSSW cache setup done."
) || return "$?"

cd "${BASE_DIR}" || return "$?"

# The container's OSG WN client seeds these with python3.6 paths and LCG only prepends, so
# clear them first or LCG's gfal2 bindings abort on import.
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH GFAL_PLUGIN_DIR GFAL_CONFIG_DIR

source "{{lcg_stack}}" || return "$?"
source "${BASE_DIR}/setup.sh" || return "$?"
return "0"
