from suzieq.poller.services.service import Service
import re
import numpy as np


class ArpndService(Service):
    """arpnd service. Different class because minor munging of output"""

    def __init__(self, name, defn, period, stype, keys, ignore_fields, schema,
                 queue, run_once="forever"):

        super().__init__(name, defn, period, stype, keys, ignore_fields, schema,
                         queue, run_once)
        # Change the partition columns to not include the IPAddress
        # We don't want millions of directories, one per prefix
        self.partition_cols.remove("ipAddress")

    def clean_data(self, processed_data, raw_data):

        devtype = self._get_devtype_from_input(raw_data)

        if any([devtype == x for x in ["cumulus", "linux"]]):
            processed_data = self._clean_linux_data(processed_data, raw_data)
        elif devtype == 'eos':
            processed_data = self._clean_eos_data(processed_data, raw_data)
        elif devtype == 'junos':
            processed_data = self._clean_junos_data(processed_data, raw_data)
        elif devtype == 'nxos':
            processed_data = self._clean_nxos_data(processed_data, raw_data)
        else:
            for entry in processed_data:
                entry['oif'] = entry['oif'].replace('/', '-')

        return super().clean_data(processed_data, raw_data)

    def _clean_linux_data(self, processed_data, raw_data):
        for entry in processed_data:
            entry["remote"] = entry["remote"] == "offload"
            entry["state"] = entry["state"].lower()
            if entry["state"] == "stale" or entry["state"] == "delay":
                entry["state"] = "reachable"
            entry['origIfname'] = entry['oif']
        return processed_data

    def _clean_eos_data(self, processed_data, raw_data):
        for entry in processed_data:
            entry['macaddr'] = ':'.join(
                [f'{x[:2]}:{x[2:]}' for x in entry['macaddr'].split('.')])
            if ',' in entry['oif']:
                entry['oif'] = entry['oif'].split(',')[0].strip()
                entry['origIfname'] = entry['oif']
            entry['oif'] = entry['oif'].replace('/', '-')

        return processed_data

    def _clean_junos_data(self, processed_data, raw_data):
        for entry in processed_data:
            if '[vtep.' in entry['oif']:
                entry['remote'] = True
            if entry['oif']:
                entry['oif'] = re.sub(r' \[.*\]', '', entry['oif'])
                entry['oif'] = entry['oif'].replace('/', '-')
            entry['state'] = 'reachable'

        return processed_data

    def _clean_nxos_data(self, processed_data, raw_data):

        drop_indices = []
        for i, entry in enumerate(processed_data):
            if not entry['ipAddress']:
                drop_indices.append(i)
                continue
            if entry['oif']:
                entry['oif'] = entry['oif'].replace('/', '-')
            entry['macaddr'] = ':'.join(
                [f'{x[:2]}:{x[2:]}' for x in entry['macaddr'].split('.')])

        processed_data = np.delete(processed_data,
                                   drop_indices).tolist()
        return processed_data
