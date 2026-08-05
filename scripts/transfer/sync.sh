#!/bin/bash
# scripts/transfer/sync.sh {push|pull} ...
#
# Moves eval data between the CPU annotation box (BSt Azure) and the GPU eval box
# (Hertie) over a shared Azure Blob container — the two VMs have no route to each
# other, but both reach Blob over HTTPS. Git carries code (control plane); this
# carries data (data plane). Every transfer is pinned by a sha256 manifest and
# re-verified on the receiving end.
#
#   push <src> <prefix>   upload a local tree to blob <prefix>/ (+ <prefix>/MANIFEST.sha256)
#                         reads pragmata tool trees in place; never writes into them.
#                         e.g. sync.sh push data/annotation/exports exports
#   pull <prefix>         download blob <prefix>/ into data/transfer/<prefix>/, then verify.
#                         REPLACES that directory: what arrives is the whole tree.
#                         e.g. sync.sh pull predictions
#   verify <prefix>       re-check data/transfer/<prefix>/ against its manifest (sha256sum -c)
#
# Auth: EVAL_BLOB_ACCOUNT / EVAL_BLOB_CONTAINER / EVAL_BLOB_SAS in .env (SAS is
# data-plane, no ARM rights needed). `push` and `pull` require those and the `az` CLI;
# `verify` reads only the local tree and needs neither.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root

TRANSFER_ROOT="$DATA_DIR/transfer"

# Temp paths to remove however the script leaves. On EXIT rather than RETURN, because
# `fatal` exits the shell outright and a RETURN trap never fires on that path; and at file
# scope rather than as a function-local, because the trap body runs where the function's
# locals are already gone (and `set -u` would abort on the name).
_scratch=()
clean_scratch() { (( ${#_scratch[@]} )) && rm -rf "${_scratch[@]}"; return 0; }
trap clean_scratch EXIT

# Credentials and the CLI are demanded by the paths that talk to the container, not at
# file scope: `verify` is local-only (sha256sum against an already-pulled tree) and must
# still run on a box that has neither.
require_blob() {
  require_env EVAL_BLOB_ACCOUNT EVAL_BLOB_CONTAINER EVAL_BLOB_SAS
  command -v az >/dev/null 2>&1 \
    || fatal "az CLI not found. Install the Azure CLI (or adapt this to azcopy) — needed on both boxes."
}

# az invocation shared args (account + sas + container).
az_blob() { az storage blob "$@" --account-name "$EVAL_BLOB_ACCOUNT" --sas-token "$EVAL_BLOB_SAS"; }

# Sorted per-file sha256 manifest of a tree, relative to it, to a given output file.
# Excludes any existing MANIFEST.sha256 so the manifest never lists itself.
# `xargs -r`: without it an empty tree still runs sha256sum, which then reads STDIN and
# writes one bogus line — a manifest of nothing that verifies as a file called "-".
write_manifest() {
  local root="$1" out="$2"
  ( cd "$root" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 -r sha256sum ) > "$out" \
    || fatal "manifest computation failed for $root"
  [[ -s "$out" ]] || fatal "no files under $root — an empty tree has nothing to pin"
}

cmd_push() {
  local src="${1:-}" prefix="${2:-}"
  [[ -n "$src" && -n "$prefix" ]] || fatal "usage: $0 push <src> <prefix>"
  [[ -d "$src" ]] || fatal "no such source tree: $src"
  [[ "$prefix" == *..* ]] && fatal "prefix must not contain '..': $prefix"
  require_blob

  # Pseudonymity boundary. This is the edge past which data leaves the box, so the
  # no-real-names invariant is enforced here, not only in the exporter that writes the
  # tree. The check itself is shared with annotation-freeze, the other boundary that must
  # not immortalise a name — see scripts/lib/check_pseudonymised.py.
  # Exit 1 is "names found"; anything else non-zero is "the check did not complete", which
  # is a different thing to report — the tree is unchecked, not condemned.
  local pii_rc=0
  python3 "$WORKSPACE_ROOT/scripts/lib/check_pseudonymised.py" "$src" || pii_rc=$?
  if (( pii_rc == 1 )); then
    fatal "refusing to push $src: it carries non-pseudonymised annotator identities"
  elif (( pii_rc != 0 )); then
    fatal "refusing to push $src: the pseudonymity check did not complete (exit $pii_rc) — the tree is unchecked, not cleared"
  fi

  local manifest; manifest="$(mktemp)"
  _scratch+=("$manifest")
  write_manifest "$src" "$manifest"
  local nfiles snap
  nfiles="$(wc -l < "$manifest" | tr -d ' ')"
  snap="$(sha256sum "$manifest" | cut -d' ' -f1)"

  section "push: $src -> $EVAL_BLOB_CONTAINER/$prefix/ ($nfiles files)"
  az_blob upload-batch --destination "$EVAL_BLOB_CONTAINER" --destination-path "$prefix" \
    --source "$src" --overwrite >/dev/null \
    || fatal "blob upload-batch failed ($src -> $prefix)"
  az_blob upload --container-name "$EVAL_BLOB_CONTAINER" --name "$prefix/MANIFEST.sha256" \
    --file "$manifest" --overwrite >/dev/null \
    || fatal "manifest upload failed ($prefix/MANIFEST.sha256)"

  log "pushed $nfiles files"
  # The pin line for a future reproducibility bundle: one hash for the whole snapshot.
  printf 'snapshot %s: sha256:%s  (%s files)\n' "$prefix" "$snap" "$nfiles"
}

# Ownership guard: resolve the intended path and assert it stays under
# data/transfer/, so received data can never land in a tool's own tree (or
# anywhere else) regardless of '..' segments or an absolute-looking prefix.
# Callers MUST append `|| exit 1`: `fatal` inside a "$( )" kills only the subshell, so a
# refused path would otherwise return empty and the caller carry on with it.
transfer_target() {
  local prefix="${1:?}" target
  target="$(realpath -m "$TRANSFER_ROOT/$prefix")"
  [[ "$target" == "$TRANSFER_ROOT"/* ]] || fatal "refusing path outside data/transfer/: $prefix"
  printf '%s\n' "$target"
}

cmd_pull() {
  local prefix="${1:-}"
  [[ -n "$prefix" ]] || fatal "usage: $0 pull <prefix>"
  local target; target="$(transfer_target "$prefix")" || exit 1   # guard before any I/O
  require_blob

  check_disk
  mkdir -p "$TRANSFER_ROOT"
  section "pull: $EVAL_BLOB_CONTAINER/$prefix/ -> data/transfer/$prefix/"
  # Downloaded into a staging dir and swapped in, rather than over the top of whatever is
  # already there: download-batch only adds and overwrites, so a file deleted at the
  # source would survive locally and `sha256sum -c` would still pass — it checks only the
  # files the manifest lists. Staging keeps the previous tree in place until a download
  # has actually succeeded, so a failed pull leaves data rather than a hole.
  local staging; staging="$(mktemp -d "$TRANSFER_ROOT/.pull-${prefix//\//_}.XXXXXX")" \
    || fatal "cannot create a staging dir under data/transfer/"
  _scratch+=("$staging")
  # download-batch preserves the blob path, so <prefix>/foo lands at <staging>/<prefix>/foo.
  az_blob download-batch --source "$EVAL_BLOB_CONTAINER" --destination "$staging" \
    --pattern "$prefix/*" >/dev/null \
    || fatal "blob download-batch failed ($prefix)"
  [[ -d "$staging/$prefix" ]] || fatal "nothing under $prefix/ in $EVAL_BLOB_CONTAINER — nothing was downloaded"

  rm -rf "$target"
  mkdir -p "$(dirname "$target")"
  mv "$staging/$prefix" "$target" || fatal "cannot move the downloaded tree into data/transfer/$prefix"
  cmd_verify "$prefix"
}

cmd_verify() {
  local prefix="${1:-}"
  [[ -n "$prefix" ]] || fatal "usage: $0 verify <prefix>"
  local target; target="$(transfer_target "$prefix")" || exit 1
  [[ -f "$target/MANIFEST.sha256" ]] || fatal "no MANIFEST.sha256 under data/transfer/$prefix — nothing to verify against"
  ( cd "$target" && sha256sum -c MANIFEST.sha256 ) >&2 \
    || fatal "manifest verification FAILED for data/transfer/$prefix — data is corrupt"
  log "verified data/transfer/$prefix against manifest"
}

case "${1:-}" in
  push)   shift; cmd_push "$@" ;;
  pull)   shift; cmd_pull "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  *) fatal "usage: $0 {push <src> <prefix> | pull <prefix> | verify <prefix>}" ;;
esac
