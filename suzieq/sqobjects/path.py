import typing
from collections import OrderedDict
from itertools import repeat

import numpy as np
import pandas as pd

from suzieq.sqobjects import interfaces, lldp, routes, arpnd, macs, basicobj
from suzieq.exceptions import NoLLdpError, EmptyDataframeError, PathLoopError


# TODO: Handle EVPN
# TODO: Handle MLAG
# TODO: What timestamp to use (arpND, mac, interface, route..)
class PathObj(basicobj.SqObject):
    def __init__(
            self,
            engine: str = "",
            hostname: typing.List[str] = [],
            start_time: str = "",
            end_time: str = "",
            view: str = "latest",
            namespace: typing.List[str] = [],
            columns: typing.List[str] = ["default"],
            context=None,
    ) -> None:
        super().__init__(
            engine,
            hostname,
            start_time,
            end_time,
            view,
            namespace,
            columns,
            context=context,
            table=None,
        )
        self._sort_fields = ["namespace", "hostname", "pathid"]
        self._cat_fields = []

    def _init_dfs(self, namespace, source, dest):
        """Initialize the dataframes used in this path hunt"""

        self._if_df = interfaces.IfObj(context=self.ctxt) \
                                .get(namespace=namespace)
        if self._if_df.empty:
            raise EmptyDataframeError(f"No interface found for {namespace}")

        self._lldp_df = lldp.LldpObj(context=self.ctxt).get(
            namespace=namespace, columns=self.columns)
        if self._lldp_df.empty:
            raise NoLLdpError(f"No LLDP information found for {namespace}")

        self._rdf = routes.RoutesObj(context=self.ctxt) \
                          .lpm(namespace=namespace, address=dest)
        if self._rdf.empty:
            raise EmptyDataframeError("No Routes information found for {}".
                                      format(dest))

        # We ignore the lack of ARPND for now
        self._arpnd_df = arpnd.ArpndObj(
            context=self.ctxt).get(namespace=namespace)

        self._macsobj = macs.MacsObj(context=self.ctxt, namespace=namespace)

        if ':' in source:
            self._src_df = self._if_df[self._if_df.ip6AddressList.astype(str)
                                       .str.contains(source + "/")]
        else:
            self._src_df = self._if_df[self._if_df.ipAddressList.astype(str)
                                       .str.contains(source + "/")]

        if self._src_df.empty:
            raise AttributeError(f"Invalid src {source}")

        if ':' in dest:
            self._dest_df = self._if_df[self._if_df.ip6AddressList.astype(str)
                                        .str.contains(dest + "/")]
        else:
            self._dest_df = self._if_df[self._if_df.ipAddressList.astype(str)
                                        .str.contains(dest + "/")]

        if self._dest_df.empty:
            raise AttributeError(f"Invalid dest {dest}")

        self.dest_device = self._dest_df["hostname"].unique()[0]
        self.src_device = self._src_df["hostname"].unique()[0]

        # Start with the source host and find its route to the destination
        if self._rdf[self._rdf["hostname"] == self.src_device].empty:
            raise EmptyDataframeError(f"No routes found for {self.src_device}")

    def _get_vrf(self, hostname, ifname) -> str:
        return (self._if_df[(self._if_df["hostname"] == hostname) &
                            (self._if_df["ifname"] == ifname)]["master"]
                .to_string(index=False)
                .strip())

    def _is_mtu_match(self, device, iface, peer, peerif) -> bool:
        return (
            self._if_df[(self._if_df["hostname"] == peer) &
                        (self._if_df["ifname"] == peerif)].iloc[0].mtu
            ==
            self._if_df[(self._if_df["hostname"] == device) &
                        (self._if_df["ifname"] == iface)].iloc[0].mtu
        )

    def _get_if_vlan(self, device: str, ifname: str) -> int:
        oif_df = self._if_df[(self._if_df["hostname"] == device) &
                             (self._if_df["ifname"] == ifname)]

        if oif_df.empty:
            return []

        return oif_df.iloc[0]["vlan"]

    def _get_l2_nexthop(self, device: str, dest: str) -> list:
        """Get the bridged/tunnel nexthops"""

        if self._arpnd_df.empty:
            return []

        rslt = self._arpnd_df[(self._arpnd_df['hostname'] == device) &
                              (self._arpnd_df['ipAddress'] == dest)]
        # the end of knowledge
        if rslt.empty:
            return []

        macaddr = rslt.iloc[0]["macaddr"]
        oif = rslt.iloc[0]["oif"]
        # This is to handle the VRR interface on Cumulus Linux machines
        if oif.endswith("-v0"):
            oif = oif.split("-v0")[0]

        vlan = self._get_if_vlan(device, oif)

        if macaddr:
            mac_df = self._macsobj.get(
                hostname=device, macaddr=macaddr, vlan=vlan)

            if mac_df.empty:
                return []

            overlay = mac_df.iloc[0].remoteVtepIp or False
            return ([(mac_df.iloc[0].remoteVtepIp, mac_df.iloc[0].oif,
                      overlay, True)])
        return []

    def _get_nexthops(self, device: str, vrf: str, dest: str) -> list:
        """Get nexthops (oif + IP + overlay) or just oif for given host/vrf.

        The overlay is a bit indicating we're getting into overlay or not.
        """

        rslt = self._rdf.query('hostname == "{}" and vrf == "{}"'
                               .format(device, vrf))

        if not rslt.empty and (len(rslt.nexthopIps.iloc[0]) != 0 and
                               rslt.nexthopIps.iloc[0][0] != ''):
            return zip(rslt.nexthopIps.iloc[0].tolist(),
                       rslt.oifs.iloc[0].tolist(),
                       repeat(False), repeat(False))

        # We've either reached the end of routing or the end of knowledge
        # Look for L2 nexthop
        return self._get_l2_nexthop(device, dest)

    def _get_nh_with_peer(self, device: str, vrf: str, dest: str) -> list:
        """Get the nexthops & peer node for each nexthop for a given device/vrf
        This uses the cached route lpm DF to get the nexthops. It
        also handles vlan subinterfaces, MLAG and plain bonds to get the
        valid nexthop peers.

        :param device: devicename to query in lpm DF for nexthop
        :type device: str

        :param vrf: VRF to qualify lpm DF for nexthop
        :type vrf: str

        :param dest: Destination IP we're searching forever, needed for arp/nd
        :type dest: str

        :rtype: list
        :return:
        list of tuples where each tuple is (oif, peerdevice, peerif, overlay)
        """

        nexthops = []

        # TODO: Can we have ECMP nexthops, one with NH IP and one without?
        nexthop_list = self._get_nexthops(device, vrf, dest)

        # Convert each OIF into its actual physical list
        is_l2 = False
        for nhip, iface, overlay, is_l2 in nexthop_list:
            vlan = 0
            # Remove VLAN subinterfaces
            if "." in iface:
                raw_iface, vlan = iface.split(".")
            else:
                raw_iface = iface

            # Replace bonds with their individual ports
            slaveoifs = self._if_df[(self._if_df["hostname"] == device) &
                                    (self._if_df["master"] == raw_iface)] \
                            .ifname.tolist()

            if not slaveoifs:
                slaveoifs = [raw_iface]

            peer_device = None
            for slave in slaveoifs:
                df = self._lldp_df[(self._lldp_df["hostname"] == device) &
                                   (self._lldp_df["ifname"] == slave)]

                if df.empty or df['peerHostname'].iloc[0] == '':
                    continue

                this_peerh = df["peerHostname"].to_string(index=False).strip()
                this_peerif = df["peerIfname"].to_string(index=False).strip()

                peer_if_master = (
                    self._if_df[(self._if_df["hostname"] == this_peerh) &
                                (self._if_df["ifname"] == this_peerif)]
                    ["master"].to_string(index=False).strip()
                )
                if peer_if_master:
                    this_peerif = peer_if_master

                if peer_device != this_peerh:
                    # MLAG case, add peer for each of 2 slave interfaces
                    if vlan:
                        this_peerif += ".{}".format(vlan)
                        slave = f"{slave}.{vlan}"
                    nexthops.append((slave, this_peerh, this_peerif, overlay,
                                     is_l2))
                    peer_device = this_peerh

            if not peer_device and (nhip and nhip != '169.254.0.1'):
                # We found not a single nbr via LLDP. Try another approach
                df = self._if_df.loc[self._if_df.ipAddressList.astype(str)
                                     .str.contains(nhip + "/")]
                if df.empty:
                    continue

                df.apply(lambda x, nexthops:
                         nexthops.append((iface, x['hostname'],
                                          x['ifname'],  overlay, is_l2))
                         if (x['namespace'] in self.namespace) else None,
                         args=(nexthops,), axis=1)

        if not nexthops and is_l2:
            return [(None, None, None, False, is_l2)]

        return nexthops

    def get(self, **kwargs) -> pd.DataFrame:
        """return a pandas dataframe with the paths between src and dest
        :param kwargs:
        :return:
        :rtype: pd.DataFrame
        """

        if not self.ctxt.engine:
            raise AttributeError(
                "Specify an analysis engine using set engine " "command"
            )

        namespaces = kwargs.get("namespace", self.ctxt.namespace)
        if not namespaces:
            raise AttributeError("Must specify namespace to run the trace in")

        namespace = namespaces[0]
        src = kwargs.get("source", None)
        dest = kwargs.get("dest", None)
        dvrf = kwargs.get("vrf", "default")
        if not dvrf:
            dvrf = 'default'
        if not src or not dest:
            raise AttributeError("Must specify trace source and dest")

        # All exceptions in the initial data gathering will happen in this init
        # After this, at least we know we have the data to work on
        self._init_dfs(namespace, src, dest)

        devices_iifs = OrderedDict()
        for i in range(len(self._src_df)):
            item = self._src_df.iloc[i]
            devices_iifs[item.hostname] = {
                "iif": item["ifname"],
                "mtu": item["mtu"],
                "overlay": False,
                "timestamp": item["timestamp"],
            }

        dest_device_iifs = OrderedDict()
        for i in range(len(self._dest_df)):
            item = self._dest_df.iloc[i]
            dest_device_iifs[item.hostname] = {
                "iif": item["ifname"],
                "vrf": item["master"] or "default",
                "mtu": item["mtu"],
                "overlay": False,
                "timestamp": item["timestamp"],
            }

        paths = []
        for x in devices_iifs:
            paths.append([OrderedDict({x: devices_iifs[x]})])
        visited_devices = set()

        # The logic is to loop through the nexthops till you reach the dest
        # device The topmost while is this looping. The next loop within handles
        # one nexthop at a time.The paths are constructed as a list of lists,
        # where each element of the outermost loop is one complete path and
        # each inner list represents one hop in that path. Each hop is the
        # list of devicename and incoming interface. loops are detected by
        # ensuring that no device is visited twice in the same VRF. The VRF
        # qualification is required to ensure packets coming back from a
        # firewall or load balancer are not tagged as duplicates.
        is_l2 = False
        while devices_iifs:
            nextdevices_iifs = OrderedDict()
            newpaths = []
            devices_this_round = set()

            for device in devices_iifs:
                iif = devices_iifs[device]["iif"]
                ivrf = None
                if iif:
                    ivrf = self._get_vrf(device, iif)

                if not ivrf or ivrf == "bridge":
                    ivrf = dvrf

                devices_iifs[device]["vrf"] = ivrf
                skey = device + ivrf

                if skey in visited_devices:
                    # This is a loop
                    raise PathLoopError(f"Loop detected on node {device}")

                devices_this_round.add(skey)

                # We've reached the destination, so stop this loop
                if device in dest_device_iifs:
                    continue

                newdevices_iifs = {}  # NHs from this NH to add to the next round
                rslt = self._rdf.query('hostname == "{}" and vrf == "{}"'
                                       .format(device, ivrf))
                if not rslt.empty:
                    timestamp = str(rslt["timestamp"].max())
                is_l2 = False
                for nexthop in self._get_nh_with_peer(device, ivrf, dest):
                    iface, peer_device, peer_if, overlay, is_l2 = nexthop
                    if iface is not None:
                        try:
                            mtu_match = self._is_mtu_match(device, iface,
                                                           peer_device,
                                                           peer_if)
                        except Exception:
                            mtu_match = np.NaN

                        newdevices_iifs[peer_device] = {
                            "iif": peer_if,
                            "vrf": ivrf,
                            "overlay": overlay,
                            "mtu": self._if_df[
                                (self._if_df["hostname"] == peer_device) &
                                (self._if_df["ifname"] == peer_if)].iloc[-1].mtu,
                            "mtuMatch": mtu_match,
                            "timestamp": timestamp,
                        }

                if not newdevices_iifs:
                    break

                for x in paths:
                    if x[-1].keys().isdisjoint([device]):
                        continue
                    for y in newdevices_iifs:
                        z = x + [OrderedDict({y: newdevices_iifs[y]})]
                        if z not in newpaths:
                            newpaths.append(z)

                for x in newdevices_iifs:
                    if x not in nextdevices_iifs:
                        nextdevices_iifs[x] = newdevices_iifs[x]

            if newpaths:
                paths = newpaths

            visited_devices = visited_devices.union(devices_this_round)
            devices_iifs = nextdevices_iifs

        # Add the final destination to all paths
        for x in paths:
            for device in dest_device_iifs:
                if not x[-1].keys().isdisjoint([device]) or is_l2:
                    x.append(OrderedDict({device: dest_device_iifs[device]}))
        # Construct the pandas dataframe.
        # Constructing the dataframe in one shot here as that's more efficient
        # for pandas
        df_plist = []
        for i, path in enumerate(paths):
            prev_device = None
            prev_hopid = 0
            for j, ele in enumerate(path):
                item = list(ele)[0]
                if item == prev_device:
                    hopid = prev_hopid
                else:
                    hopid = j
                    prev_device = item
                    prev_hopid = hopid
                df_plist.append(
                    {
                        "pathid": i + 1,
                        "hopCount": hopid,
                        "namespace": (namespace[0]
                                      if isinstance(namespace, list)
                                      else namespace),
                        "hostname": item,
                        "iif": ele[item]["iif"],
                        "vrf": ele[item]["vrf"],
                        "overlay": ele[item]["overlay"],
                        "mtuMatch": ele[item].get("mtuMatch", np.nan),
                        "mtu": ele[item].get("mtu", 0),
                        "timestamp": ele[item].get("timestamp", np.nan)
                    }
                )
        paths_df = pd.DataFrame(df_plist)
        return paths_df

    def summarize(self, **kwargs):
        """return a pandas dataframe summarizing the path info between src/dest
        :param kwargs:
        :return:
        :rtype: pd.DataFrame
        """

        path_df = self.get(**kwargs)

        if path_df.empty:
            return pd.DataFrame()

        namespace = self.namespace[0]  # Its stored as a list
        ns = {}
        ns[namespace] = {}

        perhopEcmp = path_df.groupby(by=['hopCount'])['hostname']
        ns[namespace]['totalPaths'] = path_df['pathid'].max()
        ns[namespace]['perHopEcmp'] = perhopEcmp.nunique().tolist()
        ns[namespace]['maxPathLength'] = path_df.groupby(by=['pathid'])[
            'hopCount'].max().max()
        ns[namespace]['avgPathLength'] = path_df.groupby(by=['pathid'])[
            'hopCount'].max().mean()
        ns[namespace]['uniqueDevices'] = path_df['hostname'].nunique()
        ns[namespace]['mtuMismatch'] = not all(path_df['mtuMatch'])
        ns[namespace]['usesOverlay'] = any(path_df['overlay'])
        ns[namespace]['pathMtu'] = path_df.query('iif != "lo"')['mtu'].min()

        summary_fields = ['totalPaths', 'perHopEcmp', 'maxPathLength',
                          'avgPathLength', 'uniqueDevices', 'pathMtu',
                          'usesOverlay', 'mtuMismatch']
        return pd.DataFrame(ns).reindex(summary_fields, axis=0) \
                               .convert_dtypes()
