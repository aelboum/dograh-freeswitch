#!/bin/sh
# Renders bootstrap FreeSWITCH config from environment variables at container
# start (mirrors dograh-init's render-at-startup pattern for nginx/coturn —
# see ../../docker-compose.yaml and ARCHITECTURE.md §4a for the bootstrap-vs-
# tenant-runtime-config split this implements), then execs FreeSWITCH.
#
# The image is treated as an immutable runtime artifact: this script renders
# templates into FreeSWITCH's real config directory fresh on every container
# start, so changes belong in .env + a restart, never a hand-edit inside a
# running container (see freeswitch/Dockerfile's top comment).
set -eu

TEMPLATES_DIR="/usr/local/freeswitch/conf.templates"
# NOT /usr/local/freeswitch/conf — this build's compiled-in default config
# directory is $prefix/etc/freeswitch (autotools' standard sysconfdir-based
# layout, since ./configure only set --prefix, not --sysconfdir). That's
# where `make install` puts the complete stock/vanilla conf tree (root
# freeswitch.xml, every other module's autoload_configs/*.xml, dialplan/
# default.xml, directory/, etc.) — confirmed empirically: FreeSWITCH ignores
# a same-named conf/ directory entirely and reads only this one. Render
# here so our bootstrap overrides land on top of the real, complete tree
# instead of a parallel, incomplete one FreeSWITCH never reads.
CONF_DIR="/usr/local/freeswitch/etc/freeswitch"

required_vars="PUBLIC_HOST SIP_EXTERNAL_IP RTP_PUBLIC_IP ESL_PASSWORD \
DOGRAH_ESL_ALLOWED_CIDRS SIP_GATEWAY_NAME SIP_GATEWAY_USERNAME \
SIP_GATEWAY_PASSWORD SIP_GATEWAY_REALM SIP_GATEWAY_PROXY"

missing=""
for var in $required_vars; do
    eval "value=\${$var:-}"
    if [ -z "$value" ]; then
        missing="$missing $var"
    fi
done
if [ -n "$missing" ]; then
    echo "docker-entrypoint.sh: missing required env var(s):$missing" >&2
    echo "See ../../.env.example for the full bootstrap env var contract." >&2
    exit 1
fi

# envsubst has no default-value syntax — set defaults here so every template
# referencing these is guaranteed a value.
: "${SIP_EXTERNAL_PORT:=5060}"
: "${SIP_EXTERNAL_TLS_PORT:=5061}"
: "${RTP_PORT_RANGE_START:=20000}"
: "${RTP_PORT_RANGE_END:=20199}"
export PUBLIC_HOST SIP_EXTERNAL_IP RTP_PUBLIC_IP SIP_EXTERNAL_PORT SIP_EXTERNAL_TLS_PORT \
    ESL_PASSWORD SIP_GATEWAY_NAME SIP_GATEWAY_USERNAME SIP_GATEWAY_PASSWORD \
    SIP_GATEWAY_REALM SIP_GATEWAY_PROXY RTP_PORT_RANGE_START RTP_PORT_RANGE_END

# Only these are ever substituted — deliberately excludes FreeSWITCH's own
# $${var} internal pre-process syntax (e.g. $${domain}), which must survive
# untouched. See vars.xml.template's comments.
envsubst_vars='$PUBLIC_HOST $SIP_EXTERNAL_IP $RTP_PUBLIC_IP $SIP_EXTERNAL_PORT $SIP_EXTERNAL_TLS_PORT $ESL_PASSWORD $SIP_GATEWAY_NAME $SIP_GATEWAY_USERNAME $SIP_GATEWAY_PASSWORD $SIP_GATEWAY_REALM $SIP_GATEWAY_PROXY'

# Build the ACL <node> lines from a comma-separated CIDR list — envsubst
# can't loop, so this is a plain text substitution on top of it.
acl_nodes=""
old_ifs="$IFS"
IFS=','
for cidr in $DOGRAH_ESL_ALLOWED_CIDRS; do
    cidr_trimmed=$(echo "$cidr" | tr -d ' ')
    [ -z "$cidr_trimmed" ] && continue
    acl_nodes="${acl_nodes}      <node type=\"allow\" cidr=\"${cidr_trimmed}\"/>
"
done
IFS="$old_ifs"

render() {
    src="$1"
    dest="$2"
    mkdir -p "$(dirname "$dest")"
    case "$src" in
        */acl.conf.xml.template)
            tmp=$(mktemp)
            envsubst "$envsubst_vars" < "$src" > "$tmp"
            awk -v nodes="$acl_nodes" '{gsub(/##DOGRAH_ACL_NODES##/, nodes); print}' "$tmp" > "$dest"
            rm -f "$tmp"
            ;;
        *)
            envsubst "$envsubst_vars" < "$src" > "$dest"
            ;;
    esac
}

# Walk every file under conf.templates/, mirroring its layout into conf/.
# *.template files are rendered; everything else (modules.conf.xml,
# dialplan/public.xml, dialplan/public/00_inbound_did.xml — none of which
# need per-deployment substitution) is copied verbatim, overwriting the
# stock vanilla conf/ tree installed by `make install` in the Dockerfile's
# build stage only where this deployment intentionally differs from it.
find "$TEMPLATES_DIR" -type f | while read -r src; do
    rel="${src#"$TEMPLATES_DIR"/}"
    case "$rel" in
        *.template)
            dest="$CONF_DIR/${rel%.template}"
            render "$src" "$dest"
            ;;
        *)
            dest="$CONF_DIR/$rel"
            mkdir -p "$(dirname "$dest")"
            cp "$src" "$dest"
            ;;
    esac
done

# The gateway file's real name comes from SIP_GATEWAY_NAME, not the
# template's own filename.
if [ -f "$CONF_DIR/sip_profiles/external/gateway.xml" ]; then
    mv "$CONF_DIR/sip_profiles/external/gateway.xml" \
       "$CONF_DIR/sip_profiles/external/${SIP_GATEWAY_NAME}.xml"
fi

# Narrow FreeSWITCH's RTP port range to match what docker-compose.vps.yml
# actually publishes on the host (RTP_PORT_RANGE_START/END). The compiled-in
# default (16384-32768, ~16k ports — what the source Raspberry Pi deployment
# uses, see RASPBERRY_FREESWITCH_BACKUP.md) is impractical to publish
# individually over Docker's bridge networking (docker-proxy/iptables would
# have to set up 16k+ port mappings, making `docker compose up` extremely
# slow). A smaller, capacity-sized range (200 ports by default = up to ~100
# concurrent two-way audio calls) keeps startup fast; widen
# RTP_PORT_RANGE_START/END in .env (and the matching docker-compose.vps.yml
# ports entry) if real concurrent-call capacity ever exceeds that — see
# VPS_FIREWALL.md and VPS_ARCHITECTURE.md for the tradeoff.
switch_conf="$CONF_DIR/autoload_configs/switch.conf.xml"
if [ -f "$switch_conf" ]; then
    sed -i \
        -e "s#<!--\s*<param name=\"rtp-start-port\" value=\"[0-9]*\"/>\s*-->#<param name=\"rtp-start-port\" value=\"${RTP_PORT_RANGE_START}\"/>#" \
        -e "s#<!--\s*<param name=\"rtp-end-port\" value=\"[0-9]*\"/>\s*-->#<param name=\"rtp-end-port\" value=\"${RTP_PORT_RANGE_END}\"/>#" \
        "$switch_conf"
fi

echo "docker-entrypoint.sh: bootstrap config rendered, starting FreeSWITCH" >&2
exec "$@"
