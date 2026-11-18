# Labs to try it on

NAVIGATOR is tested on two containerlab topologies. Both are ready to download: one zip each, everything
inside one folder, every path relative. Unzip it anywhere and start it.

| Lab | What | Nodes | RAM, about |
|---|---|---|---|
| [Arista EVPN Fabric](LAB_EVPN_E2E.md) | Arista EVPN: two fabrics, a WAN border, firewalls, service nodes | 22 × cEOS | 32 GB |
| [Palo Alto Firewall Lab](LAB_PALAB1.md) | three PAN-OS firewalls, Arista routing, Linux test hosts with real traffic | 3 × PAN-OS, 3 × cEOS, 6 × Linux | 24 GB |

Every node logs in with **admin / admin**. These are labs: never reuse these logins anywhere else.

## What you need

- A Linux host with Docker and [containerlab](https://containerlab.dev):

    ```bash
    bash -c "$(curl -sL https://get.containerlab.dev)"
    ```

- The vendor images. They are not in the zips: each vendor's licence requires you to download them yourself.

    | Image | Where | Load it as |
    |---|---|---|
    | Arista cEOS | [arista.com](https://www.arista.com/en/support/software-download) (free account), file `cEOS64-lab-<version>.tar.xz` | `docker import cEOS64-lab-<version>.tar.xz ceos:4336m` |
    | Palo Alto PA-VM (palab1 only) | Palo Alto support portal (needs an account), file `PA-VM-KVM-11.2.8.qcow2` | built with [vrnetlab](https://github.com/srl-labs/vrnetlab): `vrnetlab/paloalto_pa-vm:11.2.8` |

    Use the image tag the lab's `topology.yml` names (`image:`), or change that line to your tag.

## Download and start

```bash
curl -LO https://navigator.atomidea.com/downloads/evpn_e2e.zip      # or palab1.zip
unzip evpn_e2e.zip
cd evpn_e2e
sudo containerlab deploy -t topology.yml
```

- `sudo containerlab deploy -t topology.yml --reconfigure` (`-c`) starts again from the startup configs.
- `sudo containerlab inspect -t topology.yml` lists the nodes and their management addresses.
- `sudo containerlab destroy -t topology.yml` stops the lab.
- Management addresses: 172.31.11.0/24 (evpn_e2e) and 172.31.12.0/24 (palab1). If that range is in use on
  your host, change `ipv4-subnet` and the `mgmt-ipv4` lines in `topology.yml`.

## Add the lab to NAVIGATOR

Follow [Day 1](DAY1.md) with the lab's node table: one inventory per lab, a credential tag with
admin / admin for the cEOS nodes (and one for the firewalls), then *"show version on all devices in the lab"*.
Mark the inventory as a lab, never as production.
