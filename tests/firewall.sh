#!/bin/sh
# The firewall (board/x86_64/rootfs-overlay/usr/lib/cherry/firewall.nft) lets
# in what the README says, nothing more. It is loaded into a host made of
# network namespaces, with a LAN on its Ethernet link, two containers on veth
# links and a zone of two on a bridge, their links named as nspawn names them,
# and networkd's masquerading and an nspawn port forward stood in for. Each
# case is a ping, a TCP or UDP exchange with an echo server, or a DHCP lease;
# the README's openings at runtime come last. IPv4 only: the smoke test covers
# IPv6. Needs nft, ip, busybox (ping, udhcpc, udhcpd) and python3, and
# namespaces of its own: as root, or through user namespaces (unshare -r).
set -eu
cd "$(dirname "$0")/.."
script=$PWD/tests/firewall.sh
rules=$PWD/board/x86_64/rootfs-overlay/usr/lib/cherry/firewall.nft

skip() {
	echo "firewall: SKIPPED, $*"
	exit 0
}
# First, into namespaces of its own: the network namespaces below go under a
# private /run, and nothing touches the machine's own network.
if [ "${CHERRY_FIREWALL_INSIDE:-}" != 1 ]; then
	for tool in nft ip busybox python3 unshare; do
		command -v "$tool" >/dev/null || skip "$tool is not installed"
	done
	unshare -r -n -m sh -c 'mount -t tmpfs tmpfs /run' 2>/dev/null ||
		skip "no namespaces of its own (unshare -r -n -m: user namespaces off?)"
	export CHERRY_FIREWALL_INSIDE=1
	# shellcheck disable=SC2016 # $0 of the inner shell
	exec unshare -r -n -m sh -c 'mount -t tmpfs tmpfs /run && exec "$0"' "$script"
fi

tmp=$(mktemp -d)
pids=
# udhcpd writes its leases into $tmp as it exits: wait for it before rm.
trap 'kill $pids 2>/dev/null; wait; rm -rf "$tmp"' EXIT
# probe serve: TCP and UDP echo on port 7. probe tcp|udp <address> <port> [<source port>]: an echo comes back.
cat >"$tmp/probe" <<'EOF'
import socket, sys, threading
if sys.argv[1] == "serve":
    def tcp():
        s = socket.create_server(("0.0.0.0", 7))
        while True:
            c, _ = s.accept()
            c.sendall(b"echo")
            c.close()
    threading.Thread(target=tcp, daemon=True).start()
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind(("0.0.0.0", 7))
    while True:
        data, peer = u.recvfrom(16)
        u.sendto(data, peer)
proto, address, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
s.settimeout(1)
if len(sys.argv) > 4:
    s.bind(("0.0.0.0", int(sys.argv[4])))
try:
    if proto == "tcp":
        s.connect((address, port))
    else:
        s.sendto(b"echo", (address, port))
    sys.exit(0 if s.recv(16) == b"echo" else 1)
except OSError:
    sys.exit(1)
EOF

#   lan 192.0.2.2 -- eth0 192.0.2.1 [host] ve-a 10.0.1.1 -- ca 10.0.1.2
#                                          ve-b 10.0.2.1 -- cb 10.0.2.2
#                                          vz-z 10.0.3.1 (bridge) -- vb-c -- cc 10.0.3.2
#                                                                 -- vb-d -- cd 10.0.3.3
for ns in host lan ca cb cc cd; do
	ip netns add $ns
	ip -n $ns link set lo up
done
link() {
	ip link add "$1" netns host type veth peer name "$3" netns "$2"
	ip -n host link set "$1" up
	ip -n "$2" link set "$3" up
}
link eth0 lan eth0
link ve-a ca host0
link ve-b cb host0
ip -n host link add vz-z type bridge
ip -n host link set vz-z up
link vb-c cc host0
link vb-d cd host0
ip -n host link set vb-c master vz-z
ip -n host link set vb-d master vz-z
ip -n host addr add 192.0.2.1/24 dev eth0
ip -n lan addr add 192.0.2.2/24 dev eth0
# A LAN host that routes to the containers through the host.
ip -n lan route add 10.0.0.0/8 via 192.0.2.1
ip -n host addr add 10.0.1.1/28 dev ve-a
ip -n host addr add 10.0.2.1/28 dev ve-b
ip -n host addr add 10.0.3.1/24 dev vz-z
for c in ca=10.0.1.2/28=10.0.1.1 cb=10.0.2.2/28=10.0.2.1 cc=10.0.3.2/24=10.0.3.1 cd=10.0.3.3/24=10.0.3.1; do
	ns=${c%%=*} gateway=${c##*=}
	addr=${c#*=}
	ip -n "$ns" addr add "${addr%=*}" dev host0
	ip -n "$ns" route add default via "$gateway"
done
ip netns exec host sysctl -q -w net.ipv4.ip_forward=1
# Cherry's kernel has no bridge netfilter (linux.fragment): a zone's bridge
# switches its packets without the inet hooks. Where this kernel has it, off.
if [ -e /proc/sys/net/bridge ]; then
	ip netns exec host sysctl -q -w net.bridge.bridge-nf-call-iptables=0
fi
# What networkd and nspawn add for the containers: masquerading, and a port
# forward of the host's 8080 to ca's echo server (--port=8080:7).
ip netns exec host nft -f - <<'EOF'
table ip io.systemd.nat {
	chain prerouting {
		type nat hook prerouting priority dstnat + 1;
		fib daddr type local tcp dport 8080 dnat to 10.0.1.2:7
	}
	chain postrouting {
		type nat hook postrouting priority srcnat + 1;
		ip saddr { 10.0.1.0/28, 10.0.2.0/28, 10.0.3.0/24 } masquerade
	}
}
EOF
ip netns exec host nft -f "$rules"
for ns in host lan ca cb cc cd; do
	ip netns exec $ns python3 "$tmp/probe" serve &
	pids="$pids $!"
done
sleep 1

failed=0
n=0
# ok|no <case> <command...>: the command succeeds (ok) or fails (no).
case_() {
	want=$1 name=$2
	shift 2
	n=$((n + 1))
	if "$@" >/dev/null 2>&1; then got=ok; else got=no; fi
	if [ "$got" != "$want" ]; then
		echo "firewall: $name: expected $want, got $got" >&2
		failed=1
	fi
}
ping_() { ip netns exec "$1" busybox ping -c 1 -W 2 "$2"; }
probe() {
	ns=$1
	shift
	ip netns exec "$ns" python3 "$tmp/probe" "$@"
}

# The host, from the LAN: ping only.
case_ ok "LAN pings the host" ping_ lan 192.0.2.1
case_ no "LAN connects to the host over TCP" probe lan tcp 192.0.2.1 7
case_ no "LAN sends UDP to the host" probe lan udp 192.0.2.1 7
case_ no "LAN sends UDP to the host's DHCP server port" probe lan udp 192.0.2.1 67
case_ no "LAN sends UDP from port 67 to the host's DHCP client port" probe lan udp 192.0.2.1 68 67
# The host, outward: replies come back.
case_ ok "host connects to the LAN over TCP" probe host tcp 192.0.2.2 7
case_ ok "host exchanges UDP with the LAN" probe host udp 192.0.2.2 7
case_ ok "host connects to itself over loopback" probe host tcp 127.0.0.1 7
case_ ok "host connects to its own address" probe host tcp 192.0.2.1 7
# The host, from its containers: ping and DHCP only.
case_ ok "container pings its gateway" ping_ ca 10.0.1.1
case_ no "container connects to its gateway over TCP" probe ca tcp 10.0.1.1 7
case_ no "container sends UDP to its gateway" probe ca udp 10.0.1.1 7
case_ no "container connects to the host's LAN address" probe ca tcp 192.0.2.1 7
case_ no "zone member connects to its bridge over TCP" probe cc tcp 10.0.3.1 7
# Containers, outward, masqueraded.
case_ ok "container connects to the LAN over TCP" probe ca tcp 192.0.2.2 7
case_ ok "container exchanges UDP with the LAN" probe ca udp 192.0.2.2 7
case_ ok "zone member connects to the LAN over TCP" probe cc tcp 192.0.2.2 7
# Containers, between one another: ping only, but a zone is one network.
case_ ok "container pings another" ping_ ca 10.0.2.2
case_ no "container connects to another over TCP" probe ca tcp 10.0.2.2 7
case_ no "container sends UDP to another" probe ca udp 10.0.2.2 7
case_ no "container connects to a zone member" probe ca tcp 10.0.3.2 7
case_ no "zone member connects to a container" probe cc tcp 10.0.1.2 7
case_ ok "zone member connects to another of its zone" probe cc tcp 10.0.3.3 7
# Containers, from the LAN: ping only, not through a port forward either.
case_ ok "LAN pings a container" ping_ lan 10.0.1.2
case_ no "LAN connects to a container" probe lan tcp 10.0.1.2 7
case_ no "LAN connects to a zone member" probe lan tcp 10.0.3.2 7
case_ no "LAN connects through a port forward" probe lan tcp 192.0.2.1 8080

# A container leases its address from a DHCP server on the host that, like
# networkd's, takes requests on a UDP socket.
cat >"$tmp/udhcpd.conf" <<EOF
start 10.0.1.5
end 10.0.1.14
interface ve-a
lease_file $tmp/udhcpd.leases
EOF
touch "$tmp/udhcpd.leases"
ip netns exec host busybox udhcpd -f "$tmp/udhcpd.conf" >/dev/null 2>&1 &
pids="$pids $!"
sleep 1
case_ ok "container leases an address on its link" ip netns exec ca busybox udhcpc -i host0 -f -q -n -t 3 -T 1 -s /bin/true

# The README's runtime openings, then the image's rules back.
ip netns exec host nft add rule inet cherry input tcp dport 7 accept
ip netns exec host nft add rule inet cherry forward ct status dnat accept
case_ ok "LAN connects to the host once a rule lets it in" probe lan tcp 192.0.2.1 7
case_ ok "LAN connects through a port forward once a rule lets it in" probe lan tcp 192.0.2.1 8080
ip netns exec host nft -f "$rules"
case_ no "LAN connects to the host once the rules are loaded again" probe lan tcp 192.0.2.1 7
case_ ok "networkd's table is still there" ip netns exec host nft list table ip io.systemd.nat

if [ "$failed" -ne 0 ]; then
	echo "firewall: $rules lets in what it should not, or keeps out what it should not" >&2
	exit 1
fi
echo "firewall: $n cases pass"
