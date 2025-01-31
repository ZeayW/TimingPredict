import torch
import torch.nn.functional as F
import dgl
import dgl.function as fn
import functools
import pdb

class MLP(torch.nn.Module):
    def __init__(self, *sizes, batchnorm=False, dropout=False):
        super().__init__()
        fcs = []
        for i in range(1, len(sizes)):
            fcs.append(torch.nn.Linear(sizes[i - 1], sizes[i]))
            if i < len(sizes) - 1:
                fcs.append(torch.nn.LeakyReLU(negative_slope=0.2))
                if dropout: fcs.append(torch.nn.Dropout(p=0.2))
                if batchnorm: fcs.append(torch.nn.BatchNorm1d(sizes[i]))
        self.layers = torch.nn.Sequential(*fcs)

    def forward(self, x):
        return self.layers(x)

class NetConv(torch.nn.Module):
    def __init__(self, in_nf, in_ef, out_nf, h1=32, h2=32):
        super().__init__()
        self.in_nf = in_nf      # input node feature dim 
        self.in_ef = in_ef      # input edge feature dim
        self.out_nf = out_nf    # output node feature dim
        self.h1 = h1            # hidden dim
        self.h2 = h2
        # (cell) inputs are net sink pins, and outputs are net drive pins
        # e.g., in figure 2, a,b,c are inputs, X is output
        # broadcast mlp in figure 2: aggregate messages from input (sinks) to output (drive)
        self.MLP_msg_i2o = MLP(self.in_nf * 2 + self.in_ef, 64, 64, 64, 1 + self.h1 + self.h2)
        # reduce mlp: used to reduce messages at output
        self.MLP_reduce_o = MLP(self.in_nf + self.h1 + self.h2, 64, 64, 64, self.out_nf)
        # message mlp: deal with messages from output to input
        self.MLP_msg_o2i = MLP(self.in_nf * 2 + self.in_ef, 64, 64, 64, 64, self.out_nf)

    def edge_msg_i(self, edges):
        # figure 2 broadcast X->a: 
        # concat(X.f,a.f, distance(X,a))
        x = torch.cat([edges.src['nf'], edges.dst['nf'], edges.data['ef']], dim=1)
        # pass through broadcast mlp to generate a's new node feature
        x = self.MLP_msg_o2i(x)
        return {'efi': x}

    def edge_msg_o(self, edges):
        # figure 2 reduce: a,b,c ->X
        # concat(X.f,a.f, distance(X,a))
        x = torch.cat([edges.src['nf'], edges.dst['nf'], edges.data['ef']], dim=1)
        # pass through message mlp to generate messages at sink pins a,b,c
        x = self.MLP_msg_i2o(x)
        # split the sink pins' message 
        k, f1, f2 = torch.split(x, [1, self.h1, self.h2], dim=1)
        k = torch.sigmoid(k)
        return {'efo1': f1 * k, 'efo2': f2 * k}

    def node_reduce_o(self, nodes):
        # concat(X's old feature, sum-reduced message of sinks, max-reduced message of sinks)
        x = torch.cat([nodes.data['nf'], nodes.data['nfo1'], nodes.data['nfo2']], dim=1)
        # pass through reduce mlp to generate X's new feature
        x = self.MLP_reduce_o(x)
        return {'new_nf': x}
        
    def forward(self, g, ts, nf):
        with g.local_scope():
            g.ndata['nf'] = nf
            # input nodes
            # broadcast from drive to sink to generate the new feature of sink pins' (a,b,c)
            g.update_all(self.edge_msg_i, fn.sum('efi', 'new_nf'), etype='net_out')
            # output nodes
            # reduce from sink to drive
            g.apply_edges(self.edge_msg_o, etype='net_in')
            # reduce (sink messages) by sum
            g.update_all(fn.copy_e('efo1', 'efo1'), fn.sum('efo1', 'nfo1'), etype='net_in')
            # reduce by max
            g.update_all(fn.copy_e('efo2', 'efo2'), fn.max('efo2', 'nfo2'), etype='net_in')
            # generate drive node X's new feature
            g.apply_nodes(self.node_reduce_o, ts['output_nodes'])
            
            return g.ndata['new_nf']

class SignalProp(torch.nn.Module):
    def __init__(self, in_nf, in_cell_num_luts, in_cell_lut_sz, out_nf, out_cef, h1=32, h2=32, lut_dup=4):
        super().__init__()
        self.in_nf = in_nf                          # input node feature dim
        self.in_cell_num_luts = in_cell_num_luts    
        self.in_cell_lut_sz = in_cell_lut_sz
        self.out_nf = out_nf                        # output node feature dim
        self.out_cef = out_cef                      # output cell feature dim
        self.h1 = h1
        self.h2 = h2
        self.lut_dup = lut_dup
        
        self.MLP_netprop = MLP(self.out_nf + 2 * self.in_nf, 64, 64, 64, 64, self.out_nf)
        self.MLP_lut_query = MLP(self.out_nf + 2 * self.in_nf, 64, 64, 64, self.in_cell_num_luts * lut_dup * 2)
        self.MLP_lut_attention = MLP(1 + 2 + self.in_cell_lut_sz * 2, 64, 64, 64, self.in_cell_lut_sz * 2)
        self.MLP_cellarc_msg = MLP(self.out_nf + 2 * self.in_nf + self.in_cell_num_luts * self.lut_dup, 64, 64, 64, 1 + self.h1 + self.h2 + self.out_cef)
        self.MLP_cellreduce = MLP(self.in_nf + self.h1 + self.h2, 64, 64, 64, self.out_nf)

    def edge_msg_net(self, edges, groundtruth=False):
        # net propogation in figure 3, e.g., X->c
        # X is drive pin, c is sink pin
        if groundtruth:
            last_nf = edges.src['n_atslew']
        else:
            last_nf = edges.src['new_nf']
        # concat(drive pin at/slew prediction, drive pin feature, sink pin feature)
        x = torch.cat([last_nf, edges.src['nf'], edges.dst['nf']], dim=1)
        # pass through mlp
        x = self.MLP_netprop(x)
        return {'efn': x}

    def edge_msg_cell(self, edges, groundtruth=False):
        # cell propogation in figure 3, e.g., c,d->Z
        # c,d are input pins, Z is output pin
        # generate lut axis query
        if groundtruth:
            last_nf = edges.src['n_atslew']
        else:
            last_nf = edges.src['new_nf']
        # concat(input pin at/slew prediction, input pin feature, output pin feature)
        q = torch.cat([last_nf, edges.src['nf'], edges.dst['nf']], dim=1)
        # pass through NLDM Query mlp
        q = self.MLP_lut_query(q)
        # separate into two channels (corresponding to the two inputs of LUT)
        #   c->Z Query LUT.x
        #   c->Z Query LUT.y
        q = q.reshape(-1, 2)
        
        # answer lut axis query
        axis_len = self.in_cell_num_luts * (1 + 2 * self.in_cell_lut_sz)
        axis = edges.data['ef'][:, :axis_len]
        axis = axis.reshape(-1, 1 + 2 * self.in_cell_lut_sz)
        axis = axis.repeat(1, self.lut_dup).reshape(-1, 1 + 2 * self.in_cell_lut_sz)
        # pass through LUT Mask MLP
        a = self.MLP_lut_attention(torch.cat([q, axis], dim=1))
        
        # transform answer to answer mask matrix
        a = a.reshape(-1, 2, self.in_cell_lut_sz)
        ax, ay = torch.split(a, [1, 1], dim=1)
        a = torch.matmul(ax.reshape(-1, self.in_cell_lut_sz, 1), ay.reshape(-1, 1, self.in_cell_lut_sz))  # batch tensor product

        # look up answer matrix in lut
        tables_len = self.in_cell_num_luts * self.in_cell_lut_sz ** 2
        tables = edges.data['ef'][:, axis_len:axis_len + tables_len]
        r = torch.matmul(tables.reshape(-1, 1, 1, self.in_cell_lut_sz ** 2), a.reshape(-1, 4, self.in_cell_lut_sz ** 2, 1))   # batch dot product

        # construct final msg
        r = r.reshape(len(edges), self.in_cell_num_luts * self.lut_dup)
        # concat (c's AT/Slew Prediction, c's net embedding, Z's Net Embedding, LUT Result)
        x = torch.cat([last_nf, edges.src['nf'], edges.dst['nf'], r], dim=1)
        # pass through Cellprop MLP
        x = self.MLP_cellarc_msg(x)
        # like net embedding, split into three channels
        #   k: key
        #   f1: to be reduced by sum
        #   f2: to be reduced by max
        #   cef: precited cell delay
        k, f1, f2, cef = torch.split(x, [1, self.h1, self.h2, self.out_cef], dim=1)
        k = torch.sigmoid(k)
        return {'efc1': f1 * k, 'efc2': f2 * k, 'efce': cef}

    def node_reduce_o(self, nodes):
        x = torch.cat([nodes.data['nf'], nodes.data['nfc1'], nodes.data['nfc2']], dim=1)
        x = self.MLP_cellreduce(x)
        return {'new_nf': x}

    def node_skip_level_o(self, nodes):
        return {'new_nf': nodes.data['n_atslew']}
        
    def forward(self, g, ts, nf, groundtruth=False):
        # g: graph, ts: information about graph, nf: node features
        assert len(ts['topo']) % 2 == 0, 'The number of logic levels must be even (net, cell, net)'
        
        with g.local_scope():
            # init level 0 with ground truth features
            g.ndata['nf'] = nf
            # initialize new node features
            g.ndata['new_nf'] = torch.zeros(g.num_nodes(), self.out_nf, device='cuda', dtype=nf.dtype)
            # skip the first level
            g.apply_nodes(self.node_skip_level_o, ts['pi_nodes'])

            def prop_net(nodes, groundtruth):
                # net propogation in figure 3
                # new_nf are the predicted arrival time/slew
                g.pull(nodes, functools.partial(self.edge_msg_net, groundtruth=groundtruth), fn.sum('efn', 'new_nf'), etype='net_out')

            def prop_cell(nodes, groundtruth):
                # cell propogation in figure 3
                # es are the cell edges
                es = g.in_edges(nodes, etype='cell_out')
                g.apply_edges(functools.partial(self.edge_msg_cell, groundtruth=groundtruth), es, etype='cell_out')
                g.send_and_recv(es, fn.copy_e('efc1', 'efc1'), fn.sum('efc1', 'nfc1'), etype='cell_out')
                g.send_and_recv(es, fn.copy_e('efc2', 'efc2'), fn.max('efc2', 'nfc2'), etype='cell_out')
                g.apply_nodes(self.node_reduce_o, nodes)
            
            if groundtruth:
                # don't need to propagate.
                prop_net(ts['input_nodes'], groundtruth)
                prop_cell(ts['output_nodes_nonpi'], groundtruth)

            else:
                # propagate
                for i in range(1, len(ts['topo'])):
                    if i % 2 == 1:
                        # propogate from net drive pin to sink pins
                        prop_net(ts['topo'][i], groundtruth)
                    else:
                        # propogate from cell input pins to output pins
                        prop_cell(ts['topo'][i], groundtruth)
            
            # new_nf: predicted arrival time/slew
            return g.ndata['new_nf'], g.edges['cell_out'].data['efce']

class TimingGCN(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.nc1 = NetConv(10, 2, 32)
        self.nc2 = NetConv(32, 2, 32)
        self.nc3 = NetConv(32, 2, 16)  # 16 = 4x delay + 12x arbitrary (might include cap, beta)
        self.prop = SignalProp(10 + 16, 8, 7, 8, 4)

    def forward(self, g, ts, groundtruth=False):
        # nf0: initial node feature
        nf0 = g.ndata['nf']
        # figure 2: use 3-layer net embedding model;
        x = self.nc1(g, ts, nf0)
        x = self.nc2(g, ts, x)
        x = self.nc3(g, ts, x)
        # predicted net delays are the first 4 bits of the new node feature
        """
            but why include the drive pins?
        """
        net_delays = x[:, :4]
        # nf1: concat initial node feature and new node feature
        nf1 = torch.cat([nf0, x], dim=1)
        # figure 3: delay propogation model
        # nf2: predicted arrival time/slew 
        nf2, cell_delays = self.prop(g, ts, nf1, groundtruth=groundtruth)
        return net_delays, cell_delays, nf2

# {AllConv, DeepGCNII}: Simple and Deep Graph Convolutional Networks, arxiv 2007.02133 (GCNII)
class AllConv(torch.nn.Module):
    def __init__(self, in_nf, out_nf, in_ef=12, h1=10, h2=10):
        super().__init__()
        self.h1 = h1
        self.h2 = h2
        self.MLP_msg = MLP(in_nf * 2 + in_ef, 32, 32, 32, 1 + h1 + h2)
        self.MLP_reduce = MLP(in_nf + h1 + h2, 32, 32, 32, out_nf)

    def edge_udf(self, edges):
        x = self.MLP_msg(torch.cat([edges.src['nf'], edges.dst['nf'], edges.data['ef']], dim=1))
        k, f1, f2 = torch.split(x, [1, self.h1, self.h2], dim=1)
        k = torch.sigmoid(k)
        return {'ef1': f1 * k, 'ef2': f2 * k}

    def forward(self, g, nf):   # assume edata is in ef
        with g.local_scope():
            g.ndata['nf'] = nf
            g.apply_edges(self.edge_udf)
            g.update_all(fn.copy_e('ef1', 'ef1'), fn.sum('ef1', 'nf1'))
            g.update_all(fn.copy_e('ef2', 'ef2'), fn.max('ef2', 'nf2'))
            x = torch.cat([g.ndata['nf'], g.ndata['nf1'], g.ndata['nf2']], dim=1)
            x = self.MLP_reduce(x)
            return x

class DeepGCNII(torch.nn.Module):
    def __init__(self, n_layers=60, out_nf=8):
        super().__init__()
        self.n_layers = n_layers
        self.out_nf = out_nf
        self.layer0 = AllConv(10, 16)
        self.layers = [AllConv(26, 16) for i in range(n_layers - 2)]
        self.layern = AllConv(16, out_nf)
        self.layers_store = torch.nn.Sequential(*self.layers)

    def forward(self, g):
        x = self.layer0(g, g.ndata['nf'])
        for layer in self.layers:
            x = layer(g, torch.cat([x, g.ndata['nf']], dim=1)) + x   # both two tricks are mimicked here.
        x = self.layern(g, x)
        return x
