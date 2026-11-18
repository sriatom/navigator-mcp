#!/bin/bash
# palab1 flow tests: runs real traffic from the test hosts and prints PASS/FAIL against the design.
# Usage: bash flows.sh (on the containerlab host)
pass=0; fail=0
t() {  # t <id> <expect ok|blocked> <host> <description> <command...>
  id=$1; exp=$2; h=$3; desc=$4; shift 4
  out=$(docker exec clab-palab1-$h sh -c "$*" 2>&1); rc=$?
  got=$([ $rc -eq 0 ] && echo ok || echo blocked)
  if [ "$got" = "$exp" ]; then r=PASS; pass=$((pass+1)); else r=FAIL; fail=$((fail+1)); fi
  printf '%-4s %-4s %-8s %-9s %s\n     -> %s\n' "$id" "$r" "$exp" "$h" "$desc" "$(echo "$out" | tail -1 | cut -c1-110)"
}
C="curl -s --max-time 5"
t F1  ok      USER-HQ   "HQ user -> internet web 203.0.113.10:80 (PAT 198.51.100.6)"  "$C http://203.0.113.10/"
t F2  ok      USER-HQ   "HQ user -> ping 8.8.8.8"                                       "ping -c2 -W2 8.8.8.8"
t F3  ok      INET-HOST "internet -> public VIP 198.51.100.5:80 (DNAT to DMZ-WEB:8080)" "$C http://198.51.100.5/"
t F4  blocked INET-HOST "internet -> DMZ-WEB real IP 172.16.50.10:8080"               "$C http://172.16.50.10:8080/"
t F5  ok      USER-HQ   "HQ user -> public VIP 198.51.100.5:80 (hairpin / U-turn NAT)"  "$C http://198.51.100.5/"
t F6  ok      USER-HQ   "HQ user -> DMZ-WEB 172.16.50.10:8080"                          "$C http://172.16.50.10:8080/"
t F7  ok      USER-HQ   "HQ user -> APP1 10.20.20.10:8080"                              "$C http://10.20.20.10:8080/"
t F8  ok      USER-HQ   "HQ user -> app VIP 10.20.20.100:443 (DNAT to APP1:8080)"       "$C http://10.20.20.100:443/"
t F9  blocked USER-HQ   "HQ user -> DB1 10.30.30.10:5432 (DENY-CORE-TO-DB)"             "$C http://10.30.30.10:5432/"
t F10 ok      APP1      "APP1 -> DB1 10.30.30.10:5432"                                  "$C http://10.30.30.10:5432/"
t F11 ok      DMZ-WEB   "DMZ-WEB -> APP1 10.20.20.10:8080 (two firewalls)"              "$C http://10.20.20.10:8080/"
t F12 blocked DMZ-WEB   "DMZ-WEB -> DB1 10.30.30.10:5432"                               "$C http://10.30.30.10:5432/"
t F13 blocked DMZ-WEB   "DMZ-WEB -> HQ user 10.10.10.10:80 (DENY-DMZ-TO-INSIDE)"        "$C http://10.10.10.10/"
t F14 ok      DMZ-WEB   "DMZ-WEB -> internet 203.0.113.10:80 (static SNAT 198.51.100.7)" "$C http://203.0.113.10/"
t F15 ok      USER-BR   "branch user -> APP1 10.20.20.10:8080 (as 100.64.10.10)"        "$C http://10.20.20.10:8080/"
t F16 blocked USER-BR   "branch user -> DB1 10.30.30.10:5432"                           "$C http://10.30.30.10:5432/"
t F17 ok      USER-BR   "branch user -> internet web 203.0.113.10:80 (double NAT)"      "$C http://203.0.113.10/"
t F18 blocked USER-BR   "branch user -> ping 8.8.8.8 (PA-BR allows, PA-EDGE denies)"    "ping -c2 -W2 8.8.8.8"
t F19 ok      APP1      "APP1 -> branch host 100.64.10.10:80 (PA-BR inbound DNAT)" "$C http://100.64.10.10/"
t F20 blocked USER-HQ   "HQ user -> branch host 100.64.10.10:80 (overlapping 10.10.10.0/24)" "$C http://100.64.10.10/"
t F21 blocked DB1       "DB1 -> internet 203.0.113.10:80 (no rule DB -> CORE)"          "$C http://203.0.113.10/"
t F22 blocked INET-HOST "internet -> HQ user 10.10.10.10 (no route at ISP)"             "$C http://10.10.10.10/"
echo "== $pass passed, $fail failed"
echo "== who did the servers see (source IPs after NAT):"
for h in INET-HOST DMZ-WEB APP1 DB1 USER-BR; do
  echo "  $h: $(docker exec clab-palab1-$h sh -c 'tail -50 /var/log/nginx/access.log 2>/dev/null | awk "{print \$1}" | sort | uniq -c | tr "\n" " "')"
done
