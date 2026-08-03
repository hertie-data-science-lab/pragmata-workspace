# scripts/lib/common.sh — shared shell helpers for pragmata-workspace glue scripts.
#
# Source this from any script in scripts/ as the first real line:
#
#     source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
#
# It sets WORKSPACE_ROOT, defines logging / guard helpers, exposes the venv
# binaries (PY, PRAGMATA), and loads tunables (configs/settings.conf) then
# secrets (.env). It does NOT cd anywhere on its own — call `cd_root` when a
# script needs to run from the workspace root (every orchestrator does; a
# pure stdin/stdout filter would skip it).
#
# Conventions (so every script behaves the same way):
#   - We set `-u` and `pipefail` but NOT `-e`: the orchestrators must continue
#     past per-item failures, so errors are handled explicitly via `|| fatal`,
#     `|| { warn ...; continue; }`, or return-code checks.
#   - All diagnostics (log/warn/fatal/section) go to STDERR, leaving stdout
#     clean for scripts that emit data (e.g. merge_yaml).
#   - .env / config precedence is "existing environment wins", so per-run
#     overrides (FOO=bar make ...) are never clobbered by the files. An EMPTY
#     value counts as unset throughout — the loaders fill it and require_env
#     rejects it. workspace.py's loader follows the same rule.

set -uo pipefail

# --- workspace root: this file is scripts/lib/common.sh -> two dirs up ---
WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export WORKSPACE_ROOT

# --- venv binaries (single source of truth) ---
PY="$WORKSPACE_ROOT/.venv/bin/python"
PRAGMATA="$WORKSPACE_ROOT/.venv/bin/pragmata"
export PY PRAGMATA

# pragmata base_dir: tools write <DATA_DIR>/{annotation,querygen,eval} as siblings.
DATA_DIR="$WORKSPACE_ROOT/data"
export DATA_DIR

# --- logging (all to stderr) ---
ts()      { date -Iseconds; }
log()     { printf '[%s] %s\n'        "$(ts)" "$*" >&2; }
warn()    { printf '[%s] WARN: %s\n'  "$(ts)" "$*" >&2; }
# $2 is the exit code, so the message is $1 only - $* would print the code as text.
fatal()   { printf '[%s] FATAL: %s\n' "$(ts)" "$1" >&2; exit "${2:-1}"; }
section() { printf '\n=== %s ===\n'   "$*" >&2; }

cd_root() { cd "$WORKSPACE_ROOT" || fatal "cannot cd to $WORKSPACE_ROOT"; }

# --- trim leading/trailing whitespace from $1 into the variable named by $2 ---
#     Parameter expansion, not `echo | xargs`: xargs applies shell-like quoting and
#     would eat quotes and backslashes out of the value.
trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  printf -v "$2" '%s' "${s%"${s##*[![:space:]]}"}"
}

# --- dotenv loader: KEY=VALUE lines, no inline comments ---
load_dotenv() {
  local file="$1" line key val
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    trim "$line" line
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    trim "${line%%=*}" key
    trim "${line#*=}" val
    [[ -z "${!key:-}" ]] && export "$key=$val"
  done < "$file"
}

# Tunables first, then secrets; a pre-set environment beats both.
load_dotenv "$WORKSPACE_ROOT/configs/settings.conf"
load_dotenv "$WORKSPACE_ROOT/.env"

# pragmata is pinned in pyproject.toml and installed into .venv; eval shadows its own
# pin per-call (see workspace.py:eval_pragmata()).

# --- guard: fail fast if any required env var is unset/empty ---
require_env() {
  local missing=() v
  for v in "$@"; do [[ -n "${!v:-}" ]] || missing+=("$v"); done
  [[ ${#missing[@]} -eq 0 ]] || fatal "missing required env: ${missing[*]} (check .env)"
}

# --- guard: free disk on the workspace volume (MB). abort < min, warn < warn ---
check_disk() {
  local min="${1:-${DISK_MIN_FREE_MB:-100}}" warn_at="${2:-${DISK_WARN_FREE_MB:-500}}"
  local free_mb; free_mb="$(df -m . | awk 'NR==2 {print $4}')"
  if (( free_mb < min )); then
    fatal "only ${free_mb}MB free (need >=${min}MB)" 5
  elif (( free_mb < warn_at )); then
    warn "only ${free_mb}MB free (below ${warn_at}MB warn threshold)"
  else
    log "disk: ${free_mb}MB free"
  fi
}

# --- parse a comma-separated list into a newline list (trimmed, blanks dropped).
#     Usage: mapfile -t items < <(split_csv "$arg")  ---
split_csv() {
  local IFS=',' item
  for item in ${1:-}; do
    trim "$item" item
    [[ -n "$item" ]] && printf '%s\n' "$item"
  done
}

# --- config stems: every <dir>/*.yaml stem, `_`-prefixed helpers excluded (sorted).
#     The single source of truth for "which domains/specs exist" on the shell side;
#     workspace.py::domains() is its Python twin. Nullglob-safe: an empty directory
#     yields nothing rather than a literal glob that passes a non-empty guard.
#     Usage: mapfile -t stems < <(config_stems configs/annotation/domains)  ---
config_stems() {
  local dir="$1" f stem
  for f in "$dir"/*.yaml; do
    [[ -e "$f" ]] || continue
    stem="${f##*/}"; stem="${stem%.yaml}"
    [[ "$stem" == _* ]] || printf '%s\n' "$stem"
  done
}
