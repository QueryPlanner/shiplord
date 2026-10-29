# One disposable Linux image for every real-host test suite: sshd, sudo, Caddy, Docker CLI, systemd, gcc.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      python3 python3-venv python3-pip sudo openssh-server openssh-client git curl ca-certificates gnupg \
      debian-keyring debian-archive-keyring apt-transport-https docker.io systemd systemd-sysv dbus \
      gcc libc6-dev iproute2 procps psmisc openssl >/dev/null \
 && curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
 && curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list \
 && apt-get update -qq && apt-get install -y -qq caddy >/dev/null \
 && systemctl mask getty.target console-getty.service systemd-logind.service systemd-networkd-wait-online.service docker.service docker.socket containerd.service >/dev/null 2>&1 || true \
 && python3 -m venv /venv && /venv/bin/pip install -q 'typer>=0.12,<1' setuptools
ENV PATH=/venv/bin:$PATH
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
