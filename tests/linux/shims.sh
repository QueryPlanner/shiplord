#!/bin/bash
# Test-environment shims for a container without systemd; the code under test is untouched.
#   docker:      "ghcr.io is served by the local registry"
#   systemctl:   reload caddy through its admin API, SIGHUP the test sshd, report transient job units
#   systemd-run: start a detached process and record its pid
set -e
[ -e /usr/bin/docker.real ] || mv /usr/bin/docker /usr/bin/docker.real
cat > /usr/bin/docker <<'SH'
#!/bin/bash
args=(); for a in "$@"; do args+=("${a//ghcr.io\//localhost:5000/}"); done
exec /usr/bin/docker.real "${args[@]}"
SH
cat > /usr/bin/systemctl <<'SH'
#!/bin/bash
case "$1" in
  reload) [ "$2" = caddy ] && exec /usr/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; exit 0;;
  try-reload-or-restart) kill -HUP "$(cat /run/test-sshd.pid)"; exit 0;;
  is-active) unit="${2%.service}"; pidfile="/run/shiplord-units/$unit.pid"
             if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then echo active; exit 0; else echo inactive; exit 3; fi;;
  *) exit 1;;
esac
SH
cat > /usr/bin/systemd-run <<'SH'
#!/bin/bash
unit=""
while [ $# -gt 0 ]; do
  case "$1" in --unit) unit="$2"; shift 2;; --) shift; break;; --description) shift 2;; *) shift;; esac
done
mkdir -p /run/shiplord-units
setsid bash -c 'echo $$ > "$0"; exec "$@"' "/run/shiplord-units/$unit.pid" "$@" > "/run/shiplord-units/$unit.log" 2>&1 < /dev/null &
SH
chmod +x /usr/bin/docker /usr/bin/systemctl /usr/bin/systemd-run
