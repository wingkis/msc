#!/bin/sh
# msc-verify.sh — re-check a memory's refs from the machine that wrote them.
#
# The server can reach url:/file:/mem: refs; it cannot reach a git object or
# decide whether "dgx" is still the host this was true on. Those belong here, on
# the client, next to the checkout they were observed in.
#
# Dry-run by default. A probe that rewrites the library reflexively is a probe
# that corrupts it the first time two agents disagree about a timeout. Pass
# --yes to write verdicts back (POST /verify, POST /invalidate).
#
# usage: MSC_URL=http://host:8000 msc-verify.sh [--yes] [--limit N] [slug ...]

set -eu

URL="${MSC_URL:-}"
YES=0
LIMIT=20
SLUGS=""

command -v curl >/dev/null 2>&1 || { echo "msc-verify: curl is required" >&2; exit 2; }
command -v sed  >/dev/null 2>&1 || { echo "msc-verify: sed is required"  >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --yes)   YES=1 ;;
        --url)   shift; URL="${1:-}" ;;
        --limit) shift; LIMIT="${1:-20}" ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        -*)      echo "msc-verify: unknown option $1" >&2; exit 2 ;;
        *)       SLUGS="$SLUGS $1" ;;
    esac
    shift
done

[ -n "$URL" ] || { echo "msc-verify: set MSC_URL (or --url); no endpoint is baked in" >&2; exit 2; }
URL="${URL%/}"

HOST_NOW=$(hostname -s 2>/dev/null || hostname)

probe_one() {
    # prints "verdict<TAB>detail"; verdict is ok | missing | unchecked
    _ref=$1
    _kind=${_ref%%:*}
    _target=${_ref#*:}
    case "$_kind" in
      git)
        if git rev-parse --git-dir >/dev/null 2>&1 && git cat-file -e "$_target" 2>/dev/null; then
            printf 'ok\tthis checkout has that object'
        else
            printf 'unchecked\tthis checkout has no such object (%s)' "$(pwd)"
        fi ;;
      file)
        if [ -e "$_target" ]; then printf 'ok\tpath exists'
        else printf 'missing\tpath is gone: %s' "$_target"; fi ;;
      url)
        # curl prints 000 itself when it cannot connect, and exits non-zero.
        # Appending our own 000 would make the code "000000", which reads as a
        # 2xx-ish fallthrough and reports a dead endpoint as confirmed.
        _code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' -I "$_target" 2>/dev/null || true)
        [ -n "$_code" ] || _code=000
        # A verb the endpoint does not support is not a dead endpoint. Ask again
        # the way a client would before concluding anything.
        case "$_code" in
          405|501)
              _code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$_target" 2>/dev/null || true)
              [ -n "$_code" ] || _code=000 ;;
        esac
        case "$_code" in
          000)     printf 'missing\tno answer from %s' "$_target" ;;
          404)     printf 'missing\tHTTP %s (endpoint is gone)' "$_code" ;;
          401|403) printf 'unchecked\tHTTP %s (answers, not to us)' "$_code" ;;
          4*|5*)   printf 'unchecked\tHTTP %s (answers; unconfirmed)' "$_code" ;;
          *)       printf 'ok\tHTTP %s' "$_code" ;;
        esac ;;
      mem)
        _st=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "$URL/api/v1/memories/$_target" 2>/dev/null || true)
        [ -n "$_st" ] || _st=000
        if [ "$_st" = "200" ]; then printf 'ok\tmemory is live'
        else printf 'missing\tno live memory %s (HTTP %s)' "$_target" "$_st"; fi ;;
      host)
        if [ "$_target" = "$HOST_NOW" ]; then printf 'ok\tthis is that host'
        else printf 'unchecked\written on %s, we are on %s' "$_target" "$HOST_NOW"; fi ;;
      cmd) printf 'unchecked\tcmd: refs are never executed by a probe' ;;
      *)   printf 'unchecked\tunknown kind %s' "$_kind" ;;
    esac
}

refs_of() {
    # One ref per line, inner spaces intact: cmd: refs and file paths contain
    # them, and stripping every space (or splitting the document on commas) both
    # mangles values and drops the first entry glued to "refs": [.
    curl -s -m 5 "$URL/api/v1/memories/$1?include_invalidated=true" \
      | sed -n 's/.*"refs":[[:space:]]*\[\([^]]*\)\].*/\1/p' \
      | sed 's/"[[:space:]]*,[[:space:]]*"/\n/g' \
      | sed 's/^\[//; s/^"//; s/"$//; s/^[[:space:]]*//; s/[[:space:]]*$//' \
      | sed -n '/^\(git\|file\|url\|mem\|cmd\|host\):/p'
}

_write_bad() {
    _reason="re-check on $HOST_NOW could not find:$(echo "$2" | tr ' ' ',')"
    if curl -s -o /dev/null -X POST "$URL/api/v1/memories/$1/invalidate" \
        -H 'Content-Type: application/json' -H 'X-Agent: msc-verify' \
        -d "{\"reason\":\"$_reason\"}"; then echo "  -> POST /invalidate"; else echo "  -> invalidate write FAILED"; fi
}

_write_ok() {
    if curl -s -o /dev/null -X POST "$URL/api/v1/memories/$1/verify" \
        -H 'Content-Type: application/json' -H 'X-Agent: msc-verify' \
        -d '{"verdict":"ok"}'; then echo "  -> POST /verify ok"; else echo "  -> verify write FAILED"; fi
}

check_slug() {
    _slug=$1
    _refs=$(refs_of "$_slug")
    if [ -z "$_refs" ]; then echo "  (no refs — nothing to check)"; return 0; fi
    _missing=""
    _checked=0
    _unchecked=0
    # read -r, not "for": a ref with spaces must survive as one value. A heredoc
    # (not a pipe) keeps the counters in this shell.
    while IFS= read -r _ref; do
        [ -n "$_ref" ] || continue
        _line=$(probe_one "$_ref")
        _verdict=${_line%%	*}
        _detail=${_line#*	}
        printf '  %-46s %-9s %s\n' "$_ref" "$_verdict" "$_detail"
        case "$_verdict" in
          missing)   _missing="$_missing $_ref" ;;
          ok)        _checked=$((_checked + 1)) ;;
          unchecked) _unchecked=$((_unchecked + 1)) ;;
        esac
    done <<REFS
$_refs
REFS
    if [ -n "$_missing" ]; then
        echo "  => contradicted by:$_missing"
        [ "$YES" = "1" ] && _write_bad "$_slug" "$_missing"
    elif [ "$_checked" = "0" ]; then
        echo "  => uncheckable here ($_unchecked ref(s) belong to another machine)"
    else
        echo "  => confirmed ($_checked ok, $_unchecked unchecked)"
        [ "$YES" = "1" ] && _write_ok "$_slug"
    fi
    return 0
}

if [ -z "$SLUGS" ]; then
    echo "msc-verify: scanning the $LIMIT most recently updated memories (with refs)"
    SLUGS=$(curl -s -m 5 "$URL/api/v1/memories?include_invalidated=true&limit=$LIMIT" \
             | tr ',' '\n' | sed -n 's/^[[:space:]]*"slug":[[:space:]]*"\([^"]*\)".*/\1/p')
    if [ -z "$SLUGS" ]; then echo "msc-verify: nothing to scan"; exit 0; fi
fi

if [ "$YES" != "1" ]; then echo "msc-verify: dry-run (pass --yes to write verdicts back)"; fi
for _s in $SLUGS; do
    echo "$_s"
    check_slug "$_s"
done
