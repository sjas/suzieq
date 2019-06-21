#!/usr/bin/env python3

# Copyright (c) Dinesh G Dutt
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
#

from .engineobj import SQEngineObject


class macsObj(SQEngineObject):

    def get(self, **kwargs):
        if not self.iobj._table:
            raise NotImplementedError

        if self.ctxt.sort_fields is None:
            sort_fields = None
        else:
            sort_fields = self.iobj._sort_fields

        remoteOnly = False
        if kwargs.get('remoteVtepIp', []):
            if kwargs['remoteVtepIp'] == ['any']:
                remoteOnly = True
                del kwargs['remoteVtepIp']

        df = self.get_valid_df(self.iobj._table, sort_fields, **kwargs)
        if remoteOnly:
            return df.query("remoteVtepIp != ''")

        return df

    def summarize(self, **kwargs):
        if not self.iobj._table:
            raise NotImplementedError

        if self.ctxt.sort_fields is None:
            sort_fields = None
        else:
            sort_fields = self.iobj._sort_fields

        remoteOnly = False
        if kwargs.get('remoteVtepIp', []):
            if kwargs['remoteVtepIp'] == ['any']:
                remoteOnly = True
                del kwargs['remoteVtepIp']

        df = self.get_valid_df(self.iobj._table, sort_fields, **kwargs)

        if not df.empty:
            if remoteOnly:
                df = df.query("remoteVtepIp != ''")

            if kwargs.get("groupby"):
                return df.groupby(kwargs["groupby"]) \
                         .agg(lambda x: x.unique().tolist())
            else:
                for i in self.iobj._cat_fields:
                    if (kwargs.get(i, []) or
                            "default" in kwargs.get("columns", [])):
                        df[i] = df[i].astype("category", copy=False)
                return df.describe(include="all").fillna("-")

        return df