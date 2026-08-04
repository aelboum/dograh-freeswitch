# VPS Firewall

Generic rules — works on any provider (no cloud-specific security-group API
calls). Apply these at the host level (ufw shown, raw iptables equivalent
given alongside) in addition to your cloud provider's own network
firewall/security-group if it has one — the two are complementary, not
alternatives.

**Why this matters here specifically**: the Raspberry Pi this stack is
migrated from has **no host firewall at all** today (confirmed live audit:
`iptables` policy `ACCEPT`, `ufw` inactive — see
`RASPBERRY_FREESWITCH_BACKUP.md`). That was safe only because the Pi has no
public IP and reaches its SIP trunk purely via outbound-registration NAT
traversal. **None of that safety net exists on a VPS** — a VPS has a real,
routable public IP, so a real firewall is mandatory, not optional.

## Baseline rules

```bash
# Default deny inbound, allow outbound (do this first)
ufw default deny incoming
ufw default allow outgoing

# SIP signaling — must be public for the trunk to reach FreeSWITCH
ufw allow 5060/udp
ufw allow 5060/tcp

# RTP media — must be public. NOT the Pi's full compiled-in default range
# (16384-32768, ~16k ports) — Docker bridge networking makes publishing that
# many individual ports impractically slow to start. This deployment narrows
# both the published range AND FreeSWITCH's own rtp-start-port/rtp-end-port
# to match (RTP_PORT_RANGE_START/END in .env, default below) — see
# VPS_ARCHITECTURE.md's "Why 200 ports" note. Match this rule to whatever
# you actually set in .env if you widen it.
ufw allow 20000:20199/udp

# HTTP(S) — only needed if NOT running exclusively behind the Cloudflare
# tunnel (i.e. if you also want direct nginx/Traefik ingress on this VPS)
ufw allow 80/tcp
ufw allow 443/tcp

# SSH — keep your existing access rule; don't lock yourself out
ufw allow 22/tcp   # or your actual SSH port if changed

ufw enable
```

Raw `iptables` equivalent (if `ufw` isn't available on your distro):

```bash
iptables -P INPUT DROP
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p udp --dport 5060 -j ACCEPT
iptables -A INPUT -p tcp --dport 5060 -j ACCEPT
iptables -A INPUT -p udp --dport 20000:20199 -j ACCEPT
iptables -A INPUT -p tcp --dport 80 -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -j ACCEPT
```

## What is explicitly NOT opened

- **`8021` (ESL) — never opened on the host, under any circumstance.**
  `freeswitch-manager` reaches FreeSWITCH's ESL port over the internal
  Docker network (`freeswitch:8021`), not via a published host port. This is
  enforced at the network layer (no `ports:` entry for `8021` in
  `docker-compose.vps.yml`) in addition to FreeSWITCH's own
  `apply-inbound-acl` allow-list — defense in depth, not either/or.
- **`5432`/`6379`/`9000` (Postgres/Redis/MinIO)** — internal Docker network
  only, never published to the host in `docker-compose.vps.yml`.

## Optional hardening: narrow SIP/RTP to your provider's IP range

If your SIP trunk provider publishes a fixed source-IP range (many do —
check their docs/support), narrow the `5060`/RTP rules to it instead of
`any`:

```bash
ufw allow from <provider-cidr> to any port 5060 proto udp
ufw allow from <provider-cidr> to any port 5060 proto tcp
ufw allow from <provider-cidr> to any port 20000:20199 proto udp
```

This is a **"do this if you can"** recommendation, not a default rule —
some providers use wide or rotating IP ranges (or none published at all), in
which case the broad `allow` rules above are what you're left with, and
other mitigations below matter more.

## Future recommendations (documented only, not implemented this pass)

These belong to the security posture described in `ARCHITECTURE.md` §9 and
are called out here because they're specifically network/firewall-adjacent:

- **`fail2ban`** watching FreeSWITCH's log for repeated failed
  `REGISTER`/`INVITE` auth attempts against `5060`, auto-banning source IPs
  — the standard mitigation for SIP brute-force scanning, which any
  publicly-reachable `5060` will attract.
- **Rate limiting** at the API-gateway layer (see `ARCHITECTURE.md` §3) for
  HTTP-facing endpoints, separate from the SIP-layer concern above.
- **SIP fraud indicators** worth alerting on once real tenants exist:
  sudden spikes in concurrent outbound calls, calls to premium/international
  destination ranges outside a tenant's normal pattern, registration
  attempts from unexpected source IPs.

None of the above is installed or configured by `install.sh` in this pass —
they're documented next steps once this deployment carries real traffic.

## Verifying rules after applying

```bash
ufw status verbose
# or
iptables -L -n -v
```

Confirm `8021` does **not** appear in the output at all (not even as a
`DENY` rule — it should have no host-level binding to begin with, since
`docker-compose.vps.yml` never publishes it).
