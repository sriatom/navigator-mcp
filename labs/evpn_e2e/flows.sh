#!/usr/bin/env bash
# evpn_e2e_v3 flow tests: ping from the SERVICE nodes (cEOS, one VRF per "host") and compare with the design.
# Run on the containerlab host (needs docker). Read-only: pings only, nothing is changed.
P=clab-evpn_e2e-
pass=0; fail=0
flow() { # id node vrf dst expect(ok|no) why
  local id=$1 node=$2 vrf=$3 dst=$4 want=$5 why=$6 got
  if docker exec ${P}${node} Cli -p 15 -c "ping vrf $vrf $dst repeat 2 timeout 1" | grep -q " 0% packet loss"; then got=ok; else got=no; fi
  if [ "$got" = "$want" ]; then r=PASS; pass=$((pass+1)); else r=FAIL; fail=$((fail+1)); fi
  printf '%-4s %-4s %-9s vrf %-2s -> %-16s expect %-3s got %-3s  %s\n' "$id" "$r" "$node" "$vrf" "$dst" "$want" "$got" "$why"
}
flow F1  SERVICE1 A 192.168.11.200  ok "VLAN 11 L2 VNI 10011 across PD leaf pairs"
flow F2  SERVICE1 A 192.168.21.200  ok "routed in VRF PCS (L3 VNI 30100)"
flow F3  SERVICE1 B 192.168.22.200  ok "VLAN 22 L2 VNI 10022"
flow F4  SERVICE1 A 192.168.11.254  ok "VLAN 11 stretched PD -> WAN (EVPN domain gateway)"
flow F5  SERVICE1 A 192.168.61.254  ok "PCS -> WCS over EVPN, PDFW1 not crossed"
flow F6  SERVICE1 A 1.1.1.1         ok "ISP route imported into PCS (RT 10301:64999), PDFW1 not crossed"
flow F7  SERVICE1 A 31.31.31.31     ok "not advertised: default route -> PDFW1 -> WANFAB -> ISP"
flow F8  SERVICE1 A 192.168.111.100 ok "PD -> QA: WANFAB -> QAFW1 -> QASVCL"
flow F9  SERVICE4 A 192.168.21.200  ok "QA -> PD: WANFAB -> PDFW1 -> PDSVCL"
flow F10 SERVICE4 A 192.168.111.254 ok "VLAN 111 stretched QA -> WAN"
flow F11 SERVICE4 A 1.1.1.1         ok "ISP route imported into QACS, QAFW1 not crossed"
flow F12 SERVICE4 A 31.31.31.31     ok "default route -> QAFW1 -> WANFAB -> ISP"
flow F13 SERVICE4 C 192.168.11.100  ok "QA VLAN 122 -> PD: WANFAB -> PDFW1 -> PDSVCL"
flow F14 SERVICE3 C 192.168.11.100  ok "ISP reaches a PD host: WANFAB -> PDFW1 -> PDSVCL"
flow F15 SERVICE1 C 192.168.13.10   no "VLAN 13 is L2-only, no gateway"
flow F16 SERVICE4 B 192.168.113.10  no "VLAN 113 is L2-only, no gateway"
flow F17 SERVICE1 A 172.30.10.200   no "host loopbacks are not advertised"
echo "----"; echo "$pass passed, $fail failed"
