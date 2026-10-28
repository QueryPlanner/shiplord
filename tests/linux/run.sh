#!/usr/bin/env bash
# Run a real-host test suite in a disposable container.
#
#   tests/linux/run.sh <mode>     modes: unit | caddy | host | docker | e2e | systemd | all
#
#   unit     integration tests as root (real sshd and sudo), then the whole suite as an unprivileged user
#   caddy    route transactions against a running Caddy
#   host     bootstrap, provisioning, and the SSH forced-command path
#   docker   the deploy engine against the host Docker daemon and a local registry
#   e2e      the real CLI against sshd, sudo, Caddy, Docker (systemctl/systemd-run are shimmed)
#   systemd  the native lifecycle against real systemd as PID 1
#
# Needs Docker. The docker and e2e modes use the host daemon through its socket and host networking;
# the systemd mode runs a privileged container. Everything it creates is removed afterwards.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
image=${SHIPLORD_LINUX_IMAGE:-shiplord-linux}

build() { docker build -q -t "$image" "$here" >/dev/null; }

stage='rm -rf /work && mkdir /work && tar -C /src --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude="*.egg-info" -cf - . | tar -C /work -xf - && cd /work && pip install -q -e . 2>&1 | tail -2'

oneshot() { # oneshot <extra docker flags...> -- <bash script>
  local flags=()
  while [ "$1" != "--" ]; do flags+=("$1"); shift; done; shift
  docker run --rm --entrypoint bash "${flags[@]}" -v "$repo":/src:ro -v "$here/shims.sh":/shims.sh:ro "$image" -c "set -eo pipefail; $stage; $1"
}

run_unit() {
  oneshot -- 'id tester >/dev/null 2>&1 || useradd -m tester
    echo "== integration as root"
    env SHIPLORD_TEST_SUDO=1 SHIPLORD_TEST_SSHD=1 python -m unittest tests.test_sudo_integration tests.test_ssh_integration 2>&1 | tail -15
    chown -R tester /work
    echo "== full suite as an unprivileged user"
    su -s /bin/bash tester -c "set -o pipefail; cd /work && PATH=$PATH python -m unittest discover -s tests 2>&1 | tail -30"'
}
run_caddy()  { oneshot -- 'env SHIPLORD_TEST_CADDY=1 python -m unittest tests.test_caddy_integration -v 2>&1 | tail -30'; }
run_host()   { oneshot -- 'env SHIPLORD_TEST_HOST=1 python -m unittest tests.test_host_integration -v 2>&1 | tail -40'; }
run_docker() {
  oneshot -v /var/run/docker.sock:/var/run/docker.sock --network host -- \
    'env SHIPLORD_TEST_DOCKER=1 python -m unittest tests.test_deploy_integration -v 2>&1 | tail -40'
}
run_e2e() {
  oneshot -v /var/run/docker.sock:/var/run/docker.sock --network host -- \
    'bash /shims.sh && env SHIPLORD_TEST_E2E=1 python -m unittest tests.test_e2e -v 2>&1 | tail -80'
}
run_systemd() {
  local name=shiplord-sysd-$$
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --privileged --cgroupns=host --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --name "$name" "$image" >/dev/null
  trap 'docker rm -f "'"$name"'" >/dev/null 2>&1 || true' RETURN
  local state=""
  for _ in $(seq 1 45); do
    state=$(docker exec "$name" systemctl is-system-running 2>/dev/null || true)
    case "$state" in running|degraded) break;; esac; sleep 2
  done
  [ "$state" = running ] || [ "$state" = degraded ] || { echo "systemd did not boot (state: $state)"; return 1; }
  docker exec "$name" bash -c 'rm -rf /work /tmp/e2e && mkdir /work'
  tar -C "$repo" --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude='*.egg-info' -cf - . | docker exec -i "$name" tar -C /work -xf -
  docker exec "$name" bash -c 'cd /work && pip install -q -e . 2>&1 | tail -2'
  docker exec -e SHIPLORD_TEST_SYSTEMD=1 "$name" bash -c 'set -o pipefail; cd /work && python -m unittest tests.test_e2e_systemd -v 2>&1 | tail -60'
}

mode=${1:-}
case "$mode" in
  unit|caddy|host|docker|e2e|systemd) build; "run_$mode";;
  all) build; for m in unit caddy host docker e2e systemd; do echo "######## $m"; "run_$m"; done;;
  *) sed -n '2,15p' "$0"; exit 2;;
esac
