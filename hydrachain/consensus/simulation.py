# Copyright (c) 2015 Heiko Hees
import time
import simpy
import random
import copy
from ethereum.db import EphemDB
from collections import Counter
from pyethapp.accounts import Account, AccountsService
from hydrachain.hdc_service import ChainService
from hydrachain.consensus.utils import num_colors, phx
import tempfile
from ethereum import slogging
from hydrachain import hdc_service
from hydrachain.consensus import protocol as hdc_protocol
from hydrachain.consensus.base import Block
from hydrachain.consensus.manager import RoundManager
from ethereum.utils import big_endian_to_int, sha3, privtoaddr
import ethereum.keys
import gevent
log = slogging.get_logger('hdc.sim')
slogging.configure(config_string=':debug')

# reduce key derivation iterations
ethereum.keys.PBKDF2_CONSTANTS['c'] = 100

privkeys = [chr(i) * 32 for i in range(1, 11)]
validators = [(p) for p in privkeys]


empty = object()

random.seed(42)


def mk_privkeys(num):
    "make privkeys that support coloring, see utils.cstr"
    privkeys = []
    assert num <= num_colors
    for i in range(num):
        j = 0
        while True:
            k = sha3(str(j))
            a = privtoaddr(k)
            an = big_endian_to_int(a)
            if an % num_colors == i:
                break
            j += 1
        privkeys.append(k)
    return privkeys


class Transport(object):

    def __init__(self, simenv=None):
        self.simenv = simenv

    def delay(self, sender, receiver, packet, add_delay=0):
        """
        bandwidths are inaccurate, as we don't account for parallel transfers here
        """
        bw = min(sender.ul_bandwidth, receiver.dl_bandwidth)
        delay = sender.base_latency + receiver.base_latency
        delay += len(packet) / bw
        delay += add_delay
        return delay

    def deliver(self, sender, receiver, packet, add_delay=0):
        if self.simenv:
            self.simenv_deliver(sender, receiver, packet, add_delay)
        else:
            self.gevent_deliver(sender, receiver, packet, add_delay)

    def gevent_deliver(self, sender, receiver, packet, add_delay=0):
        assert sender != receiver

        def transfer():
            gevent.sleep(self.delay(sender, receiver, packet, add_delay))
            receiver.receive_packet(sender, packet)
        gevent.spawn(transfer)

    def simenv_deliver(self, sender, receiver, packet, add_delay=0):
        def transfer():
            yield self.simenv.timeout(self.delay(sender, receiver, packet, add_delay))
            receiver.receive_packet(sender, packet)

        self.simenv.process(transfer())


class NoTransport(Transport):

    def deliver(self, sender, receiver, packet):
        pass


class SlowTransport(Transport):

    def deliver(self, sender, receiver, packet):
        "deliver on edge of timeout_window"
        to = sender.app.services.chainservice.consensus_manager.active_round.timeout
        assert to > 0
        print "in slow transport deliver"
        super(SlowTransport, self).deliver(sender, receiver, packet, add_delay=to)


class PeerMock(object):

    ul_bandwidth = 1 * 10**6  # bytes/s net bandwidth
    dl_bandwidth = 1 * 10**6  # bytes/s net bandwidth
    base_latency = 0.05  # secs
    ingress_bytes = 0
    egress_bytes = 0

    def __init__(self, app, transport):
        self.app = app
        self.config = app.config
        self.remote_client_version = empty
        self.peer = None
        self.protocol = None
        self.transport = transport

    def __repr__(self):
        return "<PeerMock(A:%s > R:%s)>" % (phx(self.coinbase), phx(self.peer.coinbase))

    @property
    def coinbase(self):
        return self.app.services.chainservice.chain.coinbase

    def send_packet(self, packet):
        assert self.peer
        log.debug('send_packet', sender=self, receiver=self.peer)
        self.egress_bytes += len(packet)
        self.transport.deliver(self, self.peer, packet)

    def receive_packet(self, sender, packet):
        assert self.app.isactive
        assert sender != self
        log.debug('receive_packet', sender=sender, receiver=self)
        self.ingress_bytes += len(packet)
        self.protocol.receive_packet(packet)

    def __eq__(self, other):
        return repr(self) == repr(other)

    def __hash__(self):
        return hash(repr(self))


class PeerManagerMock(object):

    def __init__(self, app):
        self.app = app
        self.peers = []

    def __repr__(self):
        return "<PeerManager(A:%s)>" % phx(self.coinbase)

    @property
    def coinbase(self):
        return self.app.services.chainservice.chain.coinbase

    def broadcast(self, protocol, command_name, args=[], kargs={},
                  num_peers=None, exclude_peers=[]):
        for p in self.peers:
            assert p.coinbase == self.coinbase
            assert p.peer.coinbase != self.coinbase
            assert isinstance(p.protocol, protocol)
            if not exclude_peers or p not in exclude_peers:
                log.debug('broadcasting', sender=self, receiver=p, obj=args)
                func = getattr(p.protocol, 'send_' + command_name)
                func(*args, **kargs)


class SimChainService(ChainService):

    processing_time = 0.1  # time to validate block

    def __init__(self, *args, **kargs):
        self.simenv = kargs.pop('simenv')
        super(SimChainService, self).__init__(*args, **kargs)

    @property
    def now(self):
        assert self.simenv
        return self.simenv.now

    def setup_alarm(self, delay, cb, *args):
        assert self.simenv

        def _trigger():
            yield self.simenv.timeout(delay)
            cb(*args)
        self.simenv.process(_trigger())

    def on_receive_blockproposal(self, proto, proposal):

        def process():
            yield self.simenv.timeout(self.processing_time)
            super(SimChainService, self).on_receive_blockproposal(proto, proposal)

        self.simenv.process(process())


class AppMock(object):

    class Services(dict):
        pass

    def __init__(self, privkey, validators, simenv=None):
        self.config = copy.deepcopy(hdc_service.ChainService.default_config)
        self.config['db'] = dict(path='_db')
        self.config['data_dir'] = tempfile.mkdtemp()
        self.config['hdc']['validators'] = validators

        self.simenv = simenv
        self.services = self.Services()
        self.services.db = EphemDB()
        self.services.accounts = AccountsService(self)
        self.services.peermanager = PeerManagerMock(self)
        account = Account.new(password='', key=privkey)
        self.services.accounts.add_account(account, store=False)
        if simenv:
            self.services.chainservice = SimChainService(self, simenv=simenv)
        else:
            self.services.chainservice = hdc_service.ChainService(self)
        self.isactive = True

    def __repr__(self):
        return '<AppMock(%s)>' % phx(self.services.chainservice.chain.coinbase)

    def add_peer(self, peer):
        if peer in self.services.peermanager.peers:
            return
        self.services.peermanager.peers.append(peer)
        proto = hdc_protocol.HDCProtocol(peer, self.services.chainservice)
        peer.protocol = proto
        return True

    def connect_app(self, other):
        log.DEV('connecting', node=self, other=other)
        transport = Transport(self.simenv)
        p = PeerMock(self, transport)
        op = PeerMock(other, transport)
        p.peer = op
        op.peer = p
        if self.add_peer(p):
            self.services.chainservice.on_wire_protocol_start(p.protocol)
        if other.add_peer(op):
            other.services.chainservice.on_wire_protocol_start(op.protocol)


class Network(object):

    starttime = None

    def __init__(self, num_nodes=2, simenv=None):
        if simenv:
            self.simenv = simpy.Environment()
        else:
            self.simenv = None
        privkeys = mk_privkeys(num_nodes)
        validators = [privtoaddr(p) for p in privkeys]
        self.nodes = []
        for i in range(num_nodes):
            app = AppMock(privkeys[i], validators, self.simenv)
            self.nodes.append(app)

    def connect_nodes(self):
        # connect nodes
        for i, n in enumerate(self.nodes):
            for o in self.nodes[i + 1:]:
                if n.isactive and o.isactive:
                    n.connect_app(o)

    def start(self):
        # start nodes
        for n in self.nodes:
            if n.isactive:
                n.services.chainservice.consensus_manager.process()

    def run(self, duration):
        if self.simenv:
            self.simenv.run(until=self.elapsed + duration)
        else:
            if not self.starttime:
                self.starttime = time.time()
            gevent.sleep(duration)

    @property
    def elapsed(self):
        if self.simenv:
            return self.simenv.now
        else:
            return time.time() - self.starttime

    def disable_validators(self, num):
        assert num <= len(self.nodes)
        for i in range(num):
            n = self.nodes[i]
            for p in n.services.peermanager.peers:
                p.transport = NoTransport(self.simenv)

    def throttle_validators(self, num):
        assert num <= len(self.nodes)
        for i in reversed(range(num)):
            n = self.nodes[i]
            for p in n.services.peermanager.peers:
                p.transport = SlowTransport(self.simenv)

    def normvariate_base_latencies(self, sigma_factor=0.5, base_latency=None):
        min_latency = 0.001
        for n in self.nodes:
            for p in n.services.peermanager.peers:
                p.base_latency = base_latency or p.base_latency
                sigma = p.base_latency * sigma_factor
                p.base_latency = max(min_latency, random.normalvariate(p.base_latency, sigma))
                assert p.base_latency > 0

    def consensus_managers(self):
        return [n.services.chainservice.consensus_manager for n in self.nodes]

    def check_consistency(self):
        print 'checking consistency'
        cs = self.consensus_managers()
        # check they are all on the same block or the previous one
        s = Counter(c.chain.head.number for c in cs)
        if len(s) > 1:
            print 'nodes on different heights (H:num_nodes)', s
            print 'but note: byzantine nodes might have no chance to sync'
        else:
            print 'all nodes on same height', s
        max_height = height = max(s)

        # check they are all using the same block
        while height > 0:
            bs = list(set(c.chain.index.get_block_by_number(height) for c in cs
                          if c.chain.index.has_block_by_number(height)))
            assert len(bs) == 1 or (len(bs) == 2 and None in bs), bs
            height -= 1

        # highest round seen (i.e. number of failed proposers)
        max_rounds = 0
        for c in cs:
            blk = c.chain.head
            while blk.number > 0:
                p = c.load_proposal(blk.hash)
                max_rounds = max(max_rounds, p.signing_lockset.round)
                bh = c.chain.index.get_block_by_number(blk.number - 1)
                blk = c.chain.get(bh)
                assert isinstance(blk, Block)
        print 'max height', max_height, 'max rounds', max_rounds + 1

        # messages
        ingress_bytes_transfered = 0
        egress_bytes_transfered = 0

        for n in self.nodes:
            for p in n.services.peermanager.peers:
                ingress_bytes_transfered += p.ingress_bytes
                egress_bytes_transfered += p.egress_bytes

        print ingress_bytes_transfered, 'bytes received (note this is filtered)'
        print ingress_bytes_transfered / max_height / len(self.nodes), 'bytes per height and node'

        print egress_bytes_transfered, 'bytes sent'
        print egress_bytes_transfered / max_height / len(self.nodes), 'bytes per height and node'

        # print
        # elapsed = self.nodes[0].env.network.last_delivery
        # print 'elapsed', elapsed
        # print 'avg/block time', elapsed / max_height


def main(num_nodes=10, sim_duration=10, timeout=0.5,
         base_latency=0.05, latency_sigma_factor=0.5,
         num_faulty_nodes=3, num_slow_nodes=0):

    network = Network(num_nodes)
    network.connect_nodes()
    network.normvariate_base_latencies(latency_sigma_factor, base_latency)
    network.disable_validators(num_faulty_nodes)
    network.throttle_validators(num_slow_nodes)
    network.start()
    network.run(sim_duration)
    network.check_consistency()
    RoundManager.timeout = timeout

    return network

if __name__ == '__main__':
    num_nodes = 10
    faulty_fraction = 1 / 3. * 0  # nodes not sending anything
    # nodes sending votes and proposals at the edge of the timeout window
    slow_fraction = 1 / 3. * 0

    network = main(num_nodes=num_nodes,
                   sim_duration=1,
                   timeout=0.5,
                   base_latency=0.05,
                   latency_sigma_factor=0.5,
                   num_faulty_nodes=int(num_nodes * faulty_fraction),
                   num_slow_nodes=int(num_nodes * slow_fraction)
                   )
