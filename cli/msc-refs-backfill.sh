#!/bin/sh
# msc-refs-backfill.sh — lift the provenance old memories already carry in prose
# into the refs a re-check can actually pull on.
#
# The bodies name hosts, commit SHAs, file paths and endpoints — readable, and
# unverifiable at the same time: /memories/preflight only works on parsed
# handles. This script confirms each candidate *from the machine it runs on* and
# attaches only what survives that. Anything it cannot confirm stays out: an
# unverifiable ref reads like a guarantee, which is worse than no ref.
#
# Dry-run by default. --yes writes, one PUT per memory with {"refs": [...]}.
# Refs already present are left alone unless --force (a re-check may have
# rewritten them deliberately).
#
# usage: MSC_URL=http://host:8000 msc-refs-backfill.sh [--yes] [--force]
#                                                     [--limit N] [slug ...]

set -eu

URL="${MSC_URL:-}"
YES=0
FORCE=0
LIMIT=0
MAX_GIT=4          # SHAs per memory: enough to pin a change, not a changelog
MAX_FILE=3
MAX_URL=3
MAX_REFS=8
GIT_DIRS="${MSC_GIT_DIRS:-$HOME/msc}"
SLUGS=""

command -v curl >/dev/null 2>&1 || { echo "backfill: curl is required" >&2; exit 2; }
command -v jq   >/dev/null 2>&1 || { echo "backfill: jq is required"   >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --yes)    YES=1 ;;
        --force)  FORCE=1 ;;
        --limit)  shift; LIMIT="${1:-0}" ;;
        --url)    shift; URL="${1:-}" ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        -*)       echo "backfill: unknown option $1" >&2; exit 2 ;;
        *)        SLUGS="${SLUGS:+$SLUGS
}$1" ;;
    esac
    shift
done

[ -n "$URL" ] || { echo "backfill: MSC_URL (or --url) is required" >&2; exit 2; }
curl -s -m 5 -o /dev/null "$URL/api/v1/health" \
    || { echo "backfill: cannot reach $URL/api/v1/health" >&2; exit 1; }

body_of()  { curl -s -m 10 "$URL/api/v1/memories/$1" | jq -r '.body // ""'; }
refs_of()  { curl -s -m 10 "$URL/api/v1/memories/$1" | jq -r '(.refs // [])[]'; }

# host:<name> comes from the standard first line the writers already emit. It is
# not confirmed here — the point of a host: ref is that a *later* reader on that
# machine can answer, so attaching the claimed host is the whole job.
# The .local suffix goes because mDNS writers append it and `hostname -s` does
# not: kept verbatim, a same-host re-check answers "written on mmm6.local, we
# are on mmm6" forever.
ref_host() {
    printf '%s\n' "$1" | sed -n '1p' \
        | sed -n 's/^host:[[:space:]]*\([A-Za-z0-9._-]\{1,\}\).*/\1/p' \
        | sed 's/\.local$//' | sed 's/^/host:/'
}

ref_gits() {
    # Body order, not sorted: the first SHAs a memory cites are the ones it is
    # about. Existence in a real repo is the filter — an md5 or a date that
    # looks hex-shaped resolves to nothing and never becomes a ref.
    printf '%s' "$1" \
        | grep -oE '(^|[^0-9a-fA-Z])[0-9a-f]{7,12}([^0-9a-fA-Z]|$)' \
        | grep -oE '[0-9a-f]{7,12}' | awk '!seen[$0]++' | head -40 \
        | while IFS= read -r sha; do
            for dir in $GIT_DIRS; do
                [ -d "$dir" ] || continue
                if git -C "$dir" cat-file -e "$sha^{commit}" 2>/dev/null; then
                    printf 'git:%s\n' "$sha"
                    break
                fi
            done
        done | head -n "$MAX_GIT"
}

ref_files() {
    printf '%s' "$1" | grep -oE '/[A-Za-z0-9_][A-Za-z0-9_./-]{4,}' \
        | awk '!seen[$0]++' | head -60 | while IFS= read -r raw; do
        path=$(printf '%s' "$raw" | sed 's/[.,;:)]*$//')
        path=$(printf '%s' "$path" | sed 's:/\+$::')
        case "$path" in *'..'*) continue ;; esac
        [ -n "$path" ] || continue
        # Existing is not the same as being the source of the claim: `/bin/sh`,
        # `/dev/null` and `/usr/local` sit in every code fence and mean nothing.
        # Keep project/config paths three levels deep or more — the ones a later
        # reader can open and read the fact out of.
        case "$path" in
            /etc/*|/home/*|/var/*|/app/*|/opt/*|/srv/*|/Users/*|/usr/local/*) ;;
            *) continue ;;
        esac
        case "$path" in */bin|*/sbin|/dev/*) continue ;; esac
        [ "$(printf '%s' "$path" | awk -F/ '{print NF-1}')" -ge 3 ] || continue
        [ -e "$path" ] && printf 'file:%s\n' "$path"
    done | head -n "$MAX_FILE"
}

ref_urls() {
    printf '%s' "$1" | grep -oE 'https?://[^[:space:]"'"'"'`)>,;（】]+' \
        | awk '!seen[$0]++' | head -12 | while IFS= read -r url; do
        code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' -I "$url" 2>/dev/null || true)
        [ -n "$code" ] || code=000
        case "$code" in 405|501)   # the verb is unsupported, not the endpoint
            code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$url" 2>/dev/null || true)
            [ -n "$code" ] || code=000 ;;
        esac
        # A dead or vanished endpoint is not provenance — attaching it would
        # contradict the memory on the spot, which is a conclusion, not a quote.
        case "$code" in 000|404) continue ;; esac
        # localhost only means something where it was written; that claim is
        # what host: is for, and a reader elsewhere would "confirm" the wrong thing.
        case "$url" in
            *://localhost*|*://127.0.0.1*|*://\[::1\]*) continue ;;
        esac
        printf 'url:%s\n' "$url"
    done | head -n "$MAX_URL"
}

if [ -z "$SLUGS" ]; then
    SLUGS=$(curl -s -m 20 "$URL/api/v1/memories?limit=500" | jq -r '.[].slug')
fi
[ "$LIMIT" -gt 0 ] && SLUGS=$(printf '%s\n' "$SLUGS" | head -"$LIMIT")

[ "$YES" = "1" ] || echo "backfill: dry-run (pass --yes to write)"
echo "backfill: scanning $(printf '%s\n' "$SLUGS" | grep -c .) memories; git dirs: $GIT_DIRS"

changed=0
untouched=0
total_refs=0
for slug in $SLUGS; do
    [ -n "$slug" ] || continue
    have=$(refs_of "$slug" | tr '\n' ' ')
    if [ -n "$(printf '%s' "$have" | tr -d ' ')" ] && [ "$FORCE" != "1" ]; then
        untouched=$((untouched + 1))
        continue
    fi
    body=$(body_of "$slug")
    if [ -z "$body" ]; then
        # Silent skip is how a scan comes to report a clean library by accident:
        # these rows hold their whole claim in the description, so there is
        # nothing in a body to parse. Say so — a counted zero is a lie of the
        # same shape as the GNU-sed empty match on macOS.
        untouched=$((untouched + 1))
        printf '  %-46s (正文为空 —— 内容只在描述里，没有可解析的出处)\n' "$slug"
        continue
    fi

    { ref_host "$body"; ref_gits "$body"; ref_files "$body"; ref_urls "$body"; } \
        | awk 'NF && !seen[$0]++' > /tmp/msc-refs.$$ || true
    refs=$(head -"$MAX_REFS" /tmp/msc-refs.$$)
    rm -f /tmp/msc-refs.$$

    count=$(printf '%s\n' "$refs" | sed '/^$/d' | grep -c . || true)
    if [ "$count" = "0" ]; then
        untouched=$((untouched + 1))
        printf '  %-46s (nothing confirmable here)\n' "$slug"
        continue
    fi
    changed=$((changed + 1))
    total_refs=$((total_refs + count))
    printf '  %-46s %s\n' "$slug" "$(printf '%s' "$refs" | tr '\n' ' ')"
    if [ "$YES" = "1" ]; then
        payload=$(printf '%s\n' "$refs" | jq -R . | jq -sc '{refs: .}')
        code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' -X PUT \
            -H 'Content-Type: application/json' -H 'X-Agent: refs-backfill' \
            -d "$payload" "$URL/api/v1/memories/$slug")
        case "$code" in
            2*) ;;
            *) printf '    -> PUT FAILED HTTP %s\n' "$code" >&2 ;;
        esac
    fi
done

if [ "$YES" = "1" ]; then
    echo "backfill: wrote refs on $changed memories ($total_refs refs total); $untouched left as they were"
else
    echo "backfill: would write refs on $changed memories ($total_refs refs total); $untouched left alone (--yes to apply)"
fi
