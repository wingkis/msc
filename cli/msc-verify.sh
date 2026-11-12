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
# usage: MSC_URL=http://host:8000 msc-verify.sh [--yes] [--brief] [--limit N] [slug ...]
#
# --brief prints one tab-separated line per memory plus a SUMMARY line, for a
# caller that wants to mention the result instead of reading a table: the agent
# hook runs it in the background at SessionStart and injects the summary. Human
# prose goes to stderr in that mode, so stdout stays parseable.
#
# Machine scope (v1.2). Every ref kind is read for what it can mean *here*:
#   url:/mem:  global — any box may judge it
#   ip:        true only on the box holding that address
#   host:      same, but the weak identity: names get renamed (n1 was note02
#              until 2026-11-12) and nothing on this LAN resolves them. A host:
#              ref that holds an address is judged as an address.
#   git:       only in the checkout that holds the object; absence is unchecked,
#              never "gone"
#   file:      a statement about ONE machine's filesystem. This probe answers
#              only on the box the memory identifies as origin, by address
#              first: an ip:/host: ref naming an address, else the ip: field of
#              the body hostline, else names. Elsewhere — and everywhere when no
#              box is on record at all — unchecked. A ref outranks the hostline
#              because the ref says what the memory is about while the hostline
#              only says who wrote it: an ssh session reads the wrong box
#              otherwise. Absence on a box that never held the path is not
#              evidence, and a path whose shape belongs to another OS
#              (/etc/systemd on macOS) is not evidence either.
# A verdict written back says which box checked it (X-Agent: msc-verify@<ip>,
# and the reason string), so "confirmed" is never anonymous.

set -eu

URL="${MSC_URL:-}"
YES=0
BRIEF=0
LIMIT=20
SLUGS=""

command -v curl >/dev/null 2>&1 || { echo "msc-verify: curl is required" >&2; exit 2; }
command -v sed  >/dev/null 2>&1 || { echo "msc-verify: sed is required"  >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --yes)   YES=1 ;;
        --brief) BRIEF=1 ;;
        --url)   shift; URL="${1:-}" ;;
        --limit) shift; LIMIT="${1:-20}" ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*)      echo "msc-verify: unknown option $1" >&2; exit 2 ;;
        *)       SLUGS="$SLUGS $1" ;;
    esac
    shift
done

[ -n "$URL" ] || { echo "msc-verify: set MSC_URL (or --url); no endpoint is baked in" >&2; exit 2; }
URL="${URL%/}"

HOST_NOW=$(hostname -s 2>/dev/null || hostname)

norm_host() {
    # Same normalisation msc-sync.sh uses to build MSC_HOST: drop the mDNS
    # suffix, lower-case, keep alphanumerics. Refs and `hostname -s` disagree
    # about ".local" and case on some boxes; comparing raw strings made a host
    # fail to recognise itself.
    printf '%s' "$1" | sed 's/\.local$//' | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9]//g'
}

is_ipv4() {
    printf '%s' "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
}

my_ips() {
    # Every IPv4 this box answers on, one per line. Two sources because neither
    # of them is everywhere: `ip` does not exist on macOS, and net-tools on
    # Linux still spells the address 「addr:1.2.3.4」 on some boxes.
    {
        if command -v ip >/dev/null 2>&1; then
            ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
        fi
        if command -v ifconfig >/dev/null 2>&1; then
            ifconfig 2>/dev/null | awk '$1 == "inet" {v = $2; sub(/^addr:/, "", v); print v}'
        fi
    } | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u
}

MY_IPS=$(my_ips)
# The label in the verdict trail: prefer the address this box reaches the MSC
# server from, so the trail says the number a reader can actually ssh to,
# rather than whichever interface sorted first (tailscale used to win there).
MY_LABEL=""
_srv_host=$(printf '%s' "$URL" | awk -F/ '{print $3}' | cut -d: -f1)
if [ -n "$_srv_host" ]; then
    if command -v ip >/dev/null 2>&1; then
        MY_LABEL=$(ip -4 route get "$_srv_host" 2>/dev/null \
            | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}')
    fi
    if [ -z "$MY_LABEL" ] && command -v route >/dev/null 2>&1; then
        MY_LABEL=$(route -n get "$_srv_host" 2>/dev/null | awk '/^[ \t]*source:/{print $2}')
    fi
fi
[ -n "$MY_LABEL" ] || MY_LABEL=$(printf '%s\n' "$MY_IPS" | head -1)
[ -n "$MY_LABEL" ] || MY_LABEL=$HOST_NOW

has_ip() {
    [ -n "$MY_IPS" ] || return 1
    printf '%s\n' "$MY_IPS" | grep -qxF "$1"
}

HOST_ME=$(norm_host "$HOST_NOW")

# Origin of the document being checked, set per memory by check_slug.
# _file_scope=0 means "this box may not assert about these paths" — which is
# the starting state here, not the exception: see set_scope.
_origin=""
_origin_flat=""
_origin_kind=""
_origin_known=0
_file_scope=0

set_scope() {
    # Decide whether this box owns the paths of the document being checked.
    # $1 is origin_of's output: "kind<TAB>value" lines, already in precedence
    # order. Only the first kind that holds anything gets a say — matching a
    # stale hostname against a current address would let the weaker identity
    # veto the stronger one.
    #
    # Default closed. A memory that names no box names no filesystem: "I cannot
    # tell where this was seen" is not evidence that it is gone here. Leaving it
    # open made machine scope depend on being documented — measured 2026-11-13,
    # six dgx-born memories with no hostline came back CONTRADICTED on n1 and
    # n4 over paths (/home/astro/msc, /etc/X11/xorg.conf) that never existed on
    # either box; --yes from there would have invalidated them on evidence that
    # is only someone else's absence.
    _origin=$1
    _origin_kind=""
    _origin_flat=""
    _origin_known=0
    _file_scope=0
    [ -n "$_origin" ] || return 0
    _origin_kind=$(printf '%s\n' "$_origin" | cut -f1 | head -1)
    _cand=$(printf '%s\n' "$_origin" | awk -F'\t' -v k="$_origin_kind" \
        '$1 == k && $2 != "" {print $2}')
    if [ -z "$_cand" ]; then _origin_kind=""; return 0; fi
    _origin_flat=$(printf '%s' "$_cand" | tr '\n' ',' | sed 's/,$//')
    _origin_known=1
    case "$_origin_kind" in
      ip)
        for _addr in $_cand; do
            if has_ip "$_addr"; then _file_scope=1; break; fi
        done ;;
      name)
        # legacy path: the memory records a name and no address anywhere. Names
        # rot — n1 answered to note02 until 2026-11-12 — so this tier is only a
        # fallback, and it is the reason ip: refs exist.
        if [ -n "$HOST_ME" ]; then
            case " $(printf '%s\n' "$_cand" | tr '\n' ' ') " in
                *" $HOST_ME "*) _file_scope=1 ;;
            esac
        fi ;;
    esac
    return 0
}

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
        # A path is a statement about *this* machine's filesystem. Judging a
        # foreign one "gone" is the probe inventing staleness — the exact bug
        # 2.6.1 fixed on the server for its own mount set, and running
        # --yes from the wrong box would have contradicted a dozen good
        # memories on evidence that only ever existed elsewhere.
        #
        # Shape guards are not enough. dgx, n1 and n4 are all Linux and all
        # user "astro", so a dgx path passes both of them and gets judged
        # where it never existed — measured 2026-11-13: n4 reported
        # msc-rag-live-20261110 CONTRADICTED over /home/astro/msc/docker-compose.yml.
        # The authority is the memory's own origin, not the path's shape.
        if [ "$_file_scope" != "1" ]; then
            # Two different reasons for the same verdict, and a reader who is
            # trying to fix the memory needs the right one: name the origin
            # (wrong box) or add the missing anchor (no box on record at all).
            if [ "$_origin_known" = "1" ]; then
                printf 'unchecked\torigin is %s (%s), this box is %s' \
                    "$_origin_flat" "$_origin_kind" "$MY_LABEL"
            else
                printf 'unchecked\tno origin on record; a path is only decidable where it was seen'
            fi
            return 0
        fi
        _os=$(uname -s)
        case "$_target" in
          /home/*)  [ "$_os" = "Linux" ] || { printf 'unchecked\tnot a %s path (%s)' "$_os" "$_target"; return 0; } ;;
          /Users/*) [ "$_os" = "Darwin" ] || { printf 'unchecked\tnot a %s path (%s)' "$_os" "$_target"; return 0; } ;;
          # Home directories are not the only thing that betrays an OS: macOS
          # has /etc (a symlink into /private/etc) but no systemd and no snapd.
          # Judging /etc/systemd/journald.conf 「gone」 from mmm6 was the probe
          # inventing staleness again — three dgx memories came back
          # CONTRADICTED there on 2026-11-13.
          /etc/systemd/*|/etc/X11/*|/etc/apt/*|/etc/security/limits.d/*|/var/lib/snapd/*|/snap/*|/proc/*|/sys/*|/run/*|/lib/systemd/*|/usr/lib/systemd/*)
              [ "$_os" = "Linux" ] || { printf 'unchecked\tnot a %s path (%s)' "$_os" "$_target"; return 0; } ;;
          /Applications/*|/Library/*|/System/*)
              [ "$_os" = "Darwin" ] || { printf 'unchecked\tnot a %s path (%s)' "$_os" "$_target"; return 0; } ;;
        esac
        # awk, not sed: BSD sed has no \| alternation, so the GNU-only pattern
        # above matched nothing on every mac and let one box declare another
        # user's home directory gone.
        _owner=$(printf '%s' "$_target" | awk -F/ '{if ($1 == "" && ($2 == "home" || $2 == "Users") && $3 != "") print $3}')
        if [ -n "$_owner" ] && [ "$_owner" != "$(id -un)" ]; then
            printf 'unchecked\tbelongs to another user on another machine (%s)' "$_owner"
            return 0
        fi
        if [ -e "$_target" ]; then printf 'ok\tpath exists'
        else printf 'missing\tpath is gone: %s' "$_target"; fi ;;
      url)
        # curl prints 000 for every failure it has, and those failures mean
        # different things. A timeout is a busy service; a name that will not
        # resolve is this LAN (nothing here resolves names at all); a refused
        # port is most often a sleeping machine. None of them is an answer, and
        # only an answer can contradict a memory: the mmm6 endpoints this
        # library points at (:1234, :8080) go silent every time the laptop
        # closes, and reading that silence as 「gone」 would contradict a dozen
        # good memories on the hour the Mac happens to be asleep. Measured
        # 2026-11-13 from dgx, where even github.com/openai/codex times out on
        # GET while its HEAD answers 200 — so 「no answer」 says more about the
        # route than about the endpoint. The server-side twin of this rule is
        # _probe_ref(), which already separates TimeoutException (unchecked)
        # from ConnectError; this side has curl's exit code to do it with.
        _rc=0
        _code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -I "$_target" 2>/dev/null) || _rc=$?
        [ -n "$_code" ] || _code=000
        # A verb the endpoint does not support is not a dead endpoint. Ask again
        # the way a client would before concluding anything.
        case "$_code" in
          405|501)
              _rc=0
              _code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$_target" 2>/dev/null) || _rc=$?
              [ -n "$_code" ] || _code=000 ;;
        esac
        if [ "$_code" = "000" ]; then
            case "$_rc" in
              6)  printf 'unchecked\t%s does not resolve here' "$_target" ;;
              28) printf 'unchecked\ttimed out; slow is not gone (%s)' "$_target" ;;
              *)  printf 'unchecked\tno HTTP answer (curl exit %s); silence is not an answer' "$_rc" ;;
            esac
        else
            case "$_code" in
              404|410) printf 'missing\tHTTP %s (endpoint says it is gone)' "$_code" ;;
              401|403) printf 'unchecked\tHTTP %s (answers, not to us)' "$_code" ;;
              4*|5*)   printf 'unchecked\tHTTP %s (answers; unconfirmed)' "$_code" ;;
              *)       printf 'ok\tHTTP %s' "$_code" ;;
            esac
        fi ;;
      mem)
        # Same rule: a library that does not answer is not a library whose
        # entries have been deleted.
        _rc=0
        _st=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$URL/api/v1/memories/$_target" 2>/dev/null) || _rc=$?
        [ -n "$_st" ] || _st=000
        if [ "$_st" = "200" ]; then printf 'ok\tmemory is live'
        elif [ "$_st" = "404" ]; then printf 'missing\tno live memory %s (HTTP 404)' "$_target"
        elif [ "$_st" = "000" ]; then printf 'unchecked\tthe library did not answer (curl exit %s)' "$_rc"
        else printf 'unchecked\tHTTP %s looking for %s' "$_st" "$_target"; fi ;;
      ip)
        # The address, not the label: n1 answered to note02 until 2026-11-12 and
        # nothing on this LAN resolves names at all, so an ip: ref is the only
        # machine identity that keeps working across a rename.
        if ! is_ipv4 "$_target"; then
            printf 'unchecked\tip: ref is not an IPv4 address (%s)' "$_target"
        elif has_ip "$_target"; then printf 'ok\tthis box answers on %s' "$_target"
        else printf 'unchecked\t%s is not this box (%s)' "$_target" "$MY_LABEL"; fi ;;
      host)
        # Without an identity nothing may be asserted. A probe that cannot tell
        # where it is answered "ok" for every host: for a probe the failure
        # direction matters more than the coverage (the test suite caught this).
        #
        # A host: ref holding an address is judged as an address — writers do
        # put numbers there, and names are the weaker identity either way.
        if is_ipv4 "$_target"; then
            if has_ip "$_target"; then printf 'ok\tthis box answers on %s' "$_target"
            else printf 'unchecked\t%s is not this box (%s)' "$_target" "$MY_LABEL"; fi
        elif [ -z "$HOST_ME" ]; then printf 'unchecked\tcannot identify this host'
        elif [ "$(norm_host "$_target")" = "$HOST_ME" ]; then printf 'ok\tthis is that host'
        else printf 'unchecked\twritten on %s, we are on %s' "$_target" "$HOST_NOW"; fi ;;
      cmd) printf 'unchecked\tcmd: refs are never executed by a probe' ;;
      *)   printf 'unchecked\tunknown kind %s' "$_kind" ;;
    esac
}

refs_of() {
    # One ref per line, inner spaces intact: cmd: refs and file paths contain
    # them, and splitting the document on commas (or stripping every space)
    # mangles values and drops the first entry glued to "refs": [.
    #
    # awk, not sed, and deliberately no \| alternation and no \n replacement:
    # BSD sed (macOS) rejects both — GNU-only sed silently matched nothing
    # here, so the whole client-side check reported "no refs" on every mac.
    # The document comes in on stdin: refs and origin must be read out of the
    # same snapshot, so a memory is fetched once, not twice.
    awk '
        {
            line = $0
            while (match(line, /"refs":[ \t]*\[[^]]*\]/)) {
                chunk = substr(line, RSTART, RLENGTH)
                sub(/^"refs":[ \t]*\[/, "", chunk)
                sub(/\]$/, "", chunk)
                n = split(chunk, parts, /","/)
                for (i = 1; i <= n; i++) {
                    p = parts[i]
                    gsub(/^"|"$/, "", p)
                    gsub(/^[ \t]+/, "", p)
                    gsub(/[ \t]+$/, "", p)
                    if (p ~ /^(git|file|url|mem|cmd|host|ip):/) print p
                }
                line = substr(line, RSTART + RLENGTH)
            }
        }'
}

origin_of() {
    # Which box the claims in this document belong to. Prints "kind<TAB>value"
    # in precedence order, one tier only; empty output means no box is on record.
    #   ip    an ip: ref, or a host: ref whose value is an address
    #   ip    the ip: field of the body hostline
    #   name  a host: ref naming a machine
    #   name  the host: field of the body hostline
    #
    # Address before name: a hostname is a label that gets renamed (n1 answered
    # to note02 until 2026-11-12, and nothing on this LAN resolves names at all
    # — every ssh here goes by number). An address is what the box answers on
    # today, so an ip: ref or a hostline ip: outranks any name.
    #
    # Refs before hostline: a host:/ip: ref is a deliberate statement about what
    # the memory concerns, while the hostline only records who wrote it — an
    # agent that ssh'd from mmm6 into dgx writes host: mmm6 over paths that only
    # ever lived on dgx. Measured 2026-11-13: dgx-memlock-64g-verified-20261110
    # and two mmm6 session logs came back CONTRADICTED on mmm6 for
    # /etc/security/limits.d/... and /var/lib/snapd/snaps, all dgx-only.
    awk '
        function ipv4(v) {
            return (v ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/)
        }
        function norm(h) {
            h = tolower(h)
            # same mDNS suffix norm_host() strips on the live hostname: a
            # memory that names "dgx.local" must still be about dgx
            sub(/\.local$/, "", h)
            gsub(/[^a-z0-9]/, "", h)
            return h
        }
        function emit_ip(h,   v) {
            v = h
            gsub(/[ \t]/, "", v)
            if (ipv4(v) && !(v in seeni)) { seeni[v] = 1; ipv[++nipr] = v }
        }
        function emit_name(h,   v) {
            v = norm(h)
            if (v != "" && !(v in seenn)) { seenn[v] = 1; nmv[++nnmr] = v }
        }
        {
            line = $0
            while (match(line, /"refs":[ \t]*\[[^]]*\]/)) {
                chunk = substr(line, RSTART, RLENGTH)
                sub(/^"refs":[ \t]*\[/, "", chunk)
                sub(/\]$/, "", chunk)
                n = split(chunk, parts, /","/)
                for (i = 1; i <= n; i++) {
                    p = parts[i]
                    gsub(/^"|"$/, "", p)
                    gsub(/^[ \t]+/, "", p)
                    gsub(/[ \t]+$/, "", p)
                    if (p ~ /^ip:/) emit_ip(substr(p, 4))
                    else if (p ~ /^host:/) {
                        hv = substr(p, 6)
                        if (ipv4(hv)) emit_ip(hv); else emit_name(hv)
                    }
                }
                line = substr(line, RSTART + RLENGTH)
            }
            b = index($0, "\"body\":\"")
            if (b > 0) {
                rest = substr($0, b + 8)
                j = index(rest, "\\n")
                if (j > 0) rest = substr(rest, 1, j - 1)
                # "host: dgx" (hostline) and "host:dgx" (ref) both occur
                if (match(rest, /^host:[ \t]*[^ \t]+/)) {
                    v = substr(rest, RSTART, RLENGTH)
                    sub(/^host:[ \t]*/, "", v)
                    if (ipv4(v)) { if (hl_ip == "") hl_ip = v }
                    else if (hl_name == "") hl_name = norm(v)
                }
                # the ip: field of the hostline — 记忆命名原则 puts it there,
                # and it is the identity that survives a rename
                if (hl_ip == "" && match(rest, /[ \t]ip:[ \t]*[0-9.]+/)) {
                    v = substr(rest, RSTART, RLENGTH)
                    sub(/^[ \t]*ip:[ \t]*/, "", v)
                    if (ipv4(v)) hl_ip = v
                }
            }
        }
        END {
            # one tier only, first tier that has anything. No `return` here:
            # mawk rejects it outside a function, and this box runs mawk.
            if (nipr > 0) { for (i = 1; i <= nipr; i++) print "ip\t" ipv[i] }
            else if (hl_ip != "") print "ip\t" hl_ip
            else if (nnmr > 0) { for (i = 1; i <= nnmr; i++) print "name\t" nmv[i] }
            else if (hl_name != "") print "name\t" hl_name
        }'
}

_write_bad() {
    # the address, not the label — a hostname in the trail rots the day the box
    # is renamed, and then nobody can tell who judged what
    _reason="re-check on $MY_LABEL could not find:$(echo "$2" | tr ' ' ',')"
    if curl -s -o /dev/null -X POST "$URL/api/v1/memories/$1/invalidate" \
        -H 'Content-Type: application/json' -H "X-Agent: msc-verify@$MY_LABEL" \
        -d "{\"reason\":\"$_reason\"}"; then echo "  -> POST /invalidate"; else echo "  -> invalidate write FAILED"; fi
}

_write_ok() {
    # The trail has to say which box checked. An anonymous "confirmed" is the
    # same hole as a wrong-box one: the reader cannot tell them apart.
    _why="re-check on $MY_LABEL: ${2:-0} ok, ${3:-0} unchecked (origin ${_origin_flat:-none}/${_origin_kind:-none})"
    if curl -s -o /dev/null -X POST "$URL/api/v1/memories/$1/verify" \
        -H 'Content-Type: application/json' -H "X-Agent: msc-verify@$MY_LABEL" \
        -d "{\"verdict\":\"ok\",\"reason\":\"$_why\"}"; then echo "  -> POST /verify ok"; else echo "  -> verify write FAILED"; fi
}

check_slug() {
    _slug=$1
    _doc=$(curl -s -m 5 "$URL/api/v1/memories/$_slug?include_invalidated=true")
    _refs=$(printf '%s\n' "$_doc" | refs_of)
    _origin=$(printf '%s\n' "$_doc" | origin_of)
    set_scope "$_origin"
    if [ -z "$_refs" ]; then
        [ "$BRIEF" = "1" ] || echo "  (no refs — nothing to check)"
        return 0
    fi
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
        [ "$BRIEF" = "1" ] || printf '  %-46s %-9s %s\n' "$_ref" "$_verdict" "$_detail"
        case "$_verdict" in
          missing)   _missing="$_missing $_ref" ;;
          ok)        _checked=$((_checked + 1)) ;;
          unchecked) _unchecked=$((_unchecked + 1)) ;;
        esac
    done <<REFS
$_refs
REFS
    if [ "$BRIEF" = "1" ]; then
        # one machine-readable line per memory; the hook only reads these
        _missing_flat=$(echo "$_missing" | tr ' ' ',' | sed 's/^,//; s/,$//')
        if [ -n "$_missing" ]; then
            printf 'CONTRADICTED\t%s\t%s\n' "$_slug" "$_missing_flat"
        elif [ "$_checked" = "0" ]; then
            printf 'UNCHECKABLE\t%s\t%d\n' "$_slug" "$_unchecked"
        else
            printf 'CONFIRMED\t%s\t%d/%d\n' "$_slug" "$_checked" "$((_checked + _unchecked))"
        fi
        BRIEF_TOTAL=$((BRIEF_TOTAL + 1))
        [ -z "$_missing" ] || BRIEF_CONTRADICTED=$((BRIEF_CONTRADICTED + 1))
        [ "$_checked" = "0" ] && [ -z "$_missing" ] && BRIEF_UNCHECKABLE=$((BRIEF_UNCHECKABLE + 1))
        [ "$_checked" != "0" ] && [ -z "$_missing" ] && BRIEF_CONFIRMED=$((BRIEF_CONFIRMED + 1))
        return 0
    fi
    if [ -n "$_missing" ]; then
        echo "  => contradicted by:$_missing"
        [ "$YES" = "1" ] && _write_bad "$_slug" "$_missing"
    elif [ "$_checked" = "0" ]; then
        echo "  => uncheckable here ($_unchecked ref(s) belong to another machine)"
    else
        echo "  => confirmed ($_checked ok, $_unchecked unchecked)"
        [ "$YES" = "1" ] && _write_ok "$_slug" "$_checked" "$_unchecked"
    fi
    return 0
}

BRIEF_TOTAL=0
BRIEF_CONFIRMED=0
BRIEF_CONTRADICTED=0
BRIEF_UNCHECKABLE=0

_say() {
    # human chatter: stdout unless the output is being parsed
    [ "$BRIEF" = "1" ] || echo "$@"
}

if [ -z "$SLUGS" ]; then
    _say "msc-verify: scanning the $LIMIT most recently updated memories (with refs)"
    SLUGS=$(curl -s -m 5 "$URL/api/v1/memories?include_invalidated=true&limit=$LIMIT" \
             | tr ',' '\n' | sed -n 's/^[[:space:]]*"slug":[[:space:]]*"\([^"]*\)".*/\1/p')
    if [ -z "$SLUGS" ]; then
        [ "$BRIEF" = "1" ] && printf 'SUMMARY\ttotal=0\n'
        echo "msc-verify: nothing to scan"
        exit 0
    fi
fi

if [ "$YES" != "1" ]; then _say "msc-verify: dry-run (pass --yes to write verdicts back)"; fi
for _s in $SLUGS; do
    [ "$BRIEF" = "1" ] || echo "$_s"
    check_slug "$_s"
done

if [ "$BRIEF" = "1" ]; then
    printf 'SUMMARY\ttotal=%d confirmed=%d contradicted=%d uncheckable=%d\n' \
        "$BRIEF_TOTAL" "$BRIEF_CONFIRMED" "$BRIEF_CONTRADICTED" "$BRIEF_UNCHECKABLE"
fi
