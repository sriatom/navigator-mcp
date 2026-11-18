#!/bin/bash
# palab1 background traffic generator (runs inside the test hosts, data plane only).
#   bash traffic.sh start [minutes]   # default 60; mix of web, ping, denied attempts, ab bursts, iperf3
#   bash traffic.sh stop
#   bash traffic.sh status
# Everything it does is visible on the firewalls: show session all, show rule-hit-count, traffic logs.
MIN=${2:-60}
H="INET-HOST DMZ-WEB USER-HQ APP1 DB1 USER-BR"

loop() {  # loop <host> <script body>; runs until the deadline, pidfile /tmp/palab-traffic.pid
  docker exec -d clab-palab1-$1 sh -c "echo \$\$ > /tmp/palab-traffic.pid; END=\$((\$(date +%s)+$MIN*60));
    while [ \$(date +%s) -lt \$END ]; do $2; sleep \$((3 + RANDOM % 5)); done; rm -f /tmp/palab-traffic.pid"
}
C="curl -s -o /dev/null --max-time 3"

case "$1" in
start)
  # allowed web and ping (each pass a few requests) plus a denied attempt now and then
  loop USER-HQ   "$C http://203.0.113.10/; $C http://198.51.100.5/; $C http://10.20.20.10:8080/; $C http://10.20.20.100:443/; ping -c1 -W1 8.8.8.8 >/dev/null; [ \$((RANDOM%4)) = 0 ] && $C http://10.30.30.10:5432/; [ \$((RANDOM%6)) = 0 ] && $C http://100.64.10.10/"
  loop USER-BR   "$C http://10.20.20.10:8080/; $C http://203.0.113.10/; [ \$((RANDOM%3)) = 0 ] && ping -c1 -W1 8.8.8.8 >/dev/null; [ \$((RANDOM%5)) = 0 ] && $C http://10.30.30.10:5432/"
  loop INET-HOST "$C http://198.51.100.5/; [ \$((RANDOM%5)) = 0 ] && $C http://172.16.50.10:8080/"
  loop DMZ-WEB   "$C http://10.20.20.10:8080/; $C http://203.0.113.10/; [ \$((RANDOM%5)) = 0 ] && $C http://10.30.30.10:5432/"
  loop APP1      "$C http://10.30.30.10:5432/; $C http://100.64.10.10/; $C http://203.0.113.10/"
  loop DB1       "[ \$((RANDOM%4)) = 0 ] && $C http://203.0.113.10/"
  # bursts: Apache Bench against the public VIP and the app VIP every ~2 min
  loop INET-HOST "[ \$((RANDOM%25)) = 0 ] && ab -q -n 300 -c 10 http://198.51.100.5/ >/dev/null 2>&1"
  loop USER-HQ   "[ \$((RANDOM%25)) = 0 ] && ab -q -n 300 -c 10 http://10.20.20.100:443/ >/dev/null 2>&1"
  # throughput: 15 s iperf3 at 20 Mbit/s through the firewalls every ~5 min
  loop USER-HQ   "[ \$((RANDOM%60)) = 0 ] && iperf3 -c 10.20.20.10 -t 15 -b 20M >/dev/null 2>&1"
  loop USER-BR   "[ \$((RANDOM%60)) = 0 ] && iperf3 -c 10.20.20.10 -t 15 -b 20M >/dev/null 2>&1"
  echo "traffic started for $MIN min on: $H"
  ;;
stop)
  for h in $H; do docker exec clab-palab1-$h sh -c 'pkill -f "END=" ; pkill curl; pkill ab; pkill -f "iperf3 -c"; rm -f /tmp/palab-traffic.pid' 2>/dev/null; done
  echo "traffic stopped"
  ;;
status)
  for h in $H; do printf '%-10s loops=%s\n' $h "$(docker exec clab-palab1-$h sh -c 'ps -o args | grep -c "[E]ND="')"; done
  ;;
*) echo "usage: $0 start [minutes] | stop | status"; exit 1 ;;
esac
