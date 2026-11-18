# palab-host: network-multitool (traceroute, mtr, iperf3, nmap, tcpdump, ab, curl) + sshd + sudo, with a setup script that puts eth0 (mgmt)
# in VRF MGMT and starts nginx, iperf3 and sshd. Build: docker build -t palab-host:1 .
FROM ghcr.io/hellt/network-multitool:latest
RUN apk add --no-cache openssh-server sudo iproute2 \
 && ssh-keygen -A \
 && adduser -D -s /bin/bash admin && echo 'admin:admin' | chpasswd \
 && echo 'admin ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/admin \
 && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config \
 && sed -i 's/^AllowTcpForwarding.*/AllowTcpForwarding yes/' /etc/ssh/sshd_config
COPY palab-host-setup /usr/local/bin/palab-host-setup
RUN chmod 755 /usr/local/bin/palab-host-setup
