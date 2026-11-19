# Arista EVPN Fabric

An Arista EVPN design end to end: a production fabric (PD), a QA fabric, a WAN border fabric that joins them
and reaches the "internet", a firewall per fabric, and service nodes that act as hosts. 22 cEOS nodes.

[:material-download: Download evpn_e2e.zip](downloads/evpn_e2e.zip){ .md-button .md-button--primary }

![Arista EVPN Fabric topology](lab-evpn-e2e.svg#only-light)![Arista EVPN Fabric topology](lab-evpn-e2e-dark.svg#only-dark)

## Start it

```bash
curl -LO https://navigator.atomidea.com/downloads/evpn_e2e.zip
unzip evpn_e2e.zip && cd evpn_e2e
sudo containerlab deploy -t topology.yml
```

Needs Docker, containerlab and a cEOS image tagged `ceos:4336m` ([Labs](LABS.md#what-you-need)). The nodes take
a few minutes to bring up BGP and EVPN.

## What is in the zip

```
evpn_e2e/
├── topology.yml                    containerlab topology, mgmt 172.31.11.0/24
├── topology.yml.annotations.json   node positions for the containerlab graph
├── evpn_e2e.html                   lab guide (open in a browser): topology, devices, EVPN services, flows, test questions
├── IP_MAPPING.txt                  VLAN and VNI mapping between the fabrics
├── configs/                        one startup config per node
├── templates/base__ceos.tmpl       the cEOS base template (management, logins, APIs)
└── flows.sh                        17 flow checks with ping
```

## Nodes

| Group | Nodes | Mgmt (172.31.11.x) |
|---|---|---|
| PD spines | PDSPINE1, PDSPINE2 | .10, .20 |
| PD leaf pairs (MLAG) | PDLEAF1A/B, PDLEAF2A/B | .11, .12, .21, .22 |
| PD service leaves | PDSVCL1A/B | .31, .32 |
| PD firewall | PDFW1 (cEOS) | .41 |
| QA spines | QASPINE1, QASPINE2 | .110, .120 |
| QA leaf pair | QALEAF1A/B | .111, .112 |
| QA service leaves | QASVCL1A/B | .131, .132 |
| QA firewall | QAFW1 (cEOS) | .141 |
| WAN border | WANFAB1A/B | .61, .62 |
| Service nodes (hosts, one VRF each) | SERVICE1–4 | .51–.54 |

Login: **admin / admin**. eAPI and SSH listen in the management VRF `M`.

## Check it

```bash
bash flows.sh
```

17 flows: L2 VNIs across leaf pairs, routing in the tenant VRFs, PD ↔ QA through the firewalls, routes from
the "ISP", and three flows that must fail by design. Each line prints PASS or FAIL against the design.

## Try with NAVIGATOR

- *"show bgp evpn summary on all spines"*
- *"are all EVPN peers up on the PD fabric?"* (STATE_VALIDATOR)
- *"add Loopback99 on QALEAF1A with description test"* (a ticket, your `/pushok`, applied and verified)
