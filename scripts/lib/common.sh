# scripts/lib/common.sh — shared shell helpers for pragmata-workspace glue scripts.
#
# Source this from any script in scripts/ as the first real line:
#
#     source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
#
# It sets WORKSPACE_ROOT, defines logging / guard helpers, exposes the venv
# binaries (PY, PRAGMATA), and loads tunables (config/workspace.env) then
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
#     overrides (FOO=bar make ...) are never clobbered by the files.

set -uo pipefail

# --- workspace root: this file is scripts/lib/common.sh -> two dirs up ---
WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export WORKSPACE_ROOT

# --- venv binaries (single source of truth) ---
PY="$WORKSPACE_ROOT/.venv/bin/python"
PRAGMATA="$WORKSPACE_ROOT/.venv/bin/pragmata"
export PY PRAGMATA

# --- logging (all to stderr) ---
ts()      { date -Iseconds; }
log()     { printf '[%s] %s\n'        "$(ts)" "$*" >&2; }
warn()    { printf '[%s] WARN: %s\n'  "$(ts)" "$*" >&2; }
fatal()   { printf '[%s] FATAL: %s\n' "$(ts)" "$*" >&2; exit "${2:-1}"; }
section() { printf '\n=== %s ===\n'   "$*" >&2; }

cd_root() { cd "$WORKSPACE_ROOT" || fatal "cannot cd to $WORKSPACE_ROOT"; }

# --- dotenv loader: KEY=VALUE lines, existing env wins, no inline comments ---
load_dotenv() {
  local file="$1" line key val
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"             # left-trim
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"; val="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ -z "${!key:-}" ]] && export "$key=$val"
  done < "$file"
}

# Tunables first, then secrets; a pre-set environment beats both.
load_dotenv "$WORKSPACE_ROOT/config/workspace.env"
load_dotenv "$WORKSPACE_ROOT/.env"

# Pin the pragmata source: if PRAGMATA_SRC is set (in .env), shadow the installed
# package on PYTHONPATH so EVERY script resolves to it — both the `pragmata` CLI
# ($PRAGMATA) and bare `import pragmata` ($PY). Unset → the installed package.
# The wiring is version-controlled here; .env (gitignored) supplies the path.
[[ -n "${PRAGMATA_SRC:-}" ]] && export PYTHONPATH="$PRAGMATA_SRC${PYTHONPATH:+:$PYTHONPATH}"

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
    item="$(echo "$item" | xargs)"   # trim surrounding whitespace
    [[ -n "$item" ]] && printf '%s\n' "$item"
  done
}
