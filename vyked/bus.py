import asyncio
from collections import defaultdict
from functools import partial
import json
import logging
import random
import uuid

from again.utils import unique_hex
import aiohttp
from retrial.retrial import retry

from .services import TCPServiceClient, HTTPServiceClient
from .pubsub import PubSub
from .packet import ControlPacket, MessagePacket
from .protocol_factory import get_vyked_protocol
from .utils.jsonencoder import VykedEncoder
from .exceptions import ClientNotFoundError, ClientDisconnected

HTTP = 'http'
TCP = 'tcp'


def _retry_for_pub(result):
    return not result


def _retry_for_exception(_):
    return True


class HTTPBus:
    def __init__(self, registry_client):
        self._registry_client = registry_client

    def send_http_request(self, app: str, service: str, version: str, method: str, entity: str, params: dict):
        """
        A convenience method that allows you to send a well formatted http request to another service
        """
        host, port, node_id, service_type = self._registry_client.resolve(service, version, entity, HTTP)

        url = 'http://{}:{}{}'.format(host, port, params.pop('path'))

        http_keys = ['data', 'headers', 'cookies', 'auth', 'allow_redirects', 'compress', 'chunked']
        kwargs = {k: params[k] for k in http_keys if k in params}

        query_params = params.pop('params', {})

        if app is not None:
            query_params['app'] = app

        query_params['version'] = version
        query_params['service'] = service

        response = yield from aiohttp.request(method, url, params=query_params, **kwargs)
        return response


class TCPBus:
    def __init__(self, registry_client):
        registry_client.conn_handler = self
        self._registry_client = registry_client
        self._client_protocols = {}
        self._pingers = {}
        self._node_clients = {}
        self._service_clients = []
        self.hosts = {'tcp_host': None, 'http_host': None}
        self._host_id = unique_hex()
        self._ronin = False
        self._registered = False
        self._logger = logging.getLogger(__name__)

    def _create_service_clients(self):
        futures = []
        for sc in self._service_clients:
            for host, port, node_id, service_type in self._registry_client.get_all_addresses(*sc.properties):
                if service_type == 'tcp':
                    self._node_clients[node_id] = sc
                    future = self._connect_to_client(host, node_id, port, service_type, sc)
                    futures.append(future)
        return asyncio.gather(*futures, return_exceptions=False)

    def connect(self):
        clients = []
        for host in self.hosts.values():
            if host:
                clients = host.clients
        for client in clients:
            if isinstance(client, (TCPServiceClient, HTTPServiceClient)):
                client.bus = self
        self._service_clients = clients
        yield from self._registry_client.connect()

    def register(self):
        for key, host in self.hosts.items():
            if host:
                self._registry_client.register(host.host, host.port, host.name, host.version, host.node_id,
                                               host.clients, key.split('_')[0])

    def registration_complete(self):
        if not self._registered:
            self._create_service_clients()
            self._registered = True

    def new_instance(self, service, version, host, port, node_id, type):
        sc = next(sc for sc in self._service_clients if sc.name == service and sc.version == version)
        if type == 'tcp':
            self._node_clients[node_id] = sc
            asyncio.async(self._connect_to_client(host, node_id, port, type, sc))

    def send(self, packet: dict):
        packet['from'] = self._host_id
        func = getattr(self, '_' + packet['type'] + '_sender')
        asyncio.async(func(packet))

    @retry(should_retry_for_result=lambda x: not x, should_retry_for_exception=lambda x: True, timeout=None,
           max_attempts=5, multiplier=2)
    def _request_sender(self, packet: dict):
        """
        Sends a request to a server from a ServiceClient
        auto dispatch method called from self.send()
        """
        node_id = self._get_node_id_for_packet(packet)
        client_protocol = self._client_protocols.get(node_id)

        if node_id and client_protocol:
            if client_protocol.is_connected():
                packet['to'] = node_id
                client_protocol.send(packet)
                return True
            else:
                self._logger.error('Client protocol is not connected for packet %s', packet)
                raise ClientDisconnected()
        else:
            # No node found to send request
            self._logger.error('Out of %s, Client Not found for packet %s', self._client_protocols.keys(), packet)
            raise ClientNotFoundError()

    def _connect_to_client(self, host, node_id, port, service_type, service_client):

        future = asyncio.async(
            asyncio.get_event_loop().create_connection(partial(get_vyked_protocol, service_client), host, port,
                                                       ssl=service_client._ssl_context))
        future.add_done_callback(
            partial(self._service_client_connection_callback, self._node_clients[node_id], node_id, service_type))
        return future

    def _service_client_connection_callback(self, sc, node_id, service_type, future):
        _, protocol = future.result()
        # TODO : handle pinging
        # if service_type == TCP:
        #     pinger = Pinger(self, asyncio.get_event_loop())
        #     self._pingers[node_id] = pinger
        #     pinger.register_tcp_service(protocol, node_id)
        #     asyncio.async(pinger.start_ping())
        self._client_protocols[node_id] = protocol

    @staticmethod
    def _create_json_service_name(app, service, version):
        return {'app': app, 'name': service, 'version': version}

    @staticmethod
    def _handle_ping(packet, protocol):
        protocol.send(ControlPacket.pong(packet['node_id']))

    def _handle_pong(self, node_id, count):
        pinger = self._pingers[node_id]
        asyncio.async(pinger.pong_received(count))

    def _get_node_id_for_packet(self, packet):
        service, version, entity = packet['name'], packet['version'], packet['entity']
        node = self._registry_client.resolve(service, version, entity, TCP)
        return node[2] if node else None

    def handle_ping_timeout(self, node_id):
        self._logger.info("Service client connection timed out {}".format(node_id))
        self._pingers.pop(node_id, None)
        service_props = self._registry_client.get_for_node(node_id)
        self._logger.info('service client props {}'.format(service_props))
        if service_props is not None:
            host, port, _node_id, _type = service_props
            asyncio.async(self._connect_to_client(host, _node_id, port, _type))

    def receive(self, packet: dict, protocol, transport):
        if packet['type'] == 'ping':
            self._handle_ping(packet, protocol)
        elif packet['type'] == 'pong':
            self._handle_pong(packet['node_id'], packet['count'])
        else:
            if self.hosts['tcp_host'].is_for_me(packet['name'], packet['version']):
                func = getattr(self, '_' + packet['type'] + '_receiver')
                func(packet, protocol)
            else:
                self._logger.warn('wrongly routed packet: ', packet)

    def _request_receiver(self, packet, protocol):
        api_fn = getattr(self.hosts['tcp_host'], packet['endpoint'])
        if api_fn.is_api:
            from_node_id = packet['from']
            entity = packet['entity']
            future = asyncio.async(api_fn(from_id=from_node_id, entity=entity, **packet['payload']))

            def send_result(f):
                result_packet = f.result()
                protocol.send(result_packet)

            future.add_done_callback(send_result)
        else:
            print('no api found for packet: ', packet)

    def handle_connected(self):
        for host in self.hosts.values():
            if host:
                yield from host.initiate()


class PubSubBus:
    PUBSUB_DELAY = 5

    def __init__(self, pubsub_host, pubsub_port, registry_client, ssl_context=None):
        self._host = pubsub_host
        self._port = pubsub_port
        self._pubsub_handler = None
        self._registry_client = registry_client
        self._clients = None
        self._pending_publishes = {}
        self._ssl_context = ssl_context

    def create_pubsub_handler(self):
        self._pubsub_handler = PubSub(self._host, self._port)
        yield from self._pubsub_handler.connect()

    def register_for_subscription(self, host, port, node_id, clients):
        self._clients = clients
        subs_list = []
        xsubs_list4registry = []
        xsubs_list4redis = []
        for client in clients:
            if isinstance(client, TCPServiceClient):
                for each in dir(client):
                    fn = getattr(client, each)
                    if callable(fn) and getattr(fn, 'is_subscribe', False):
                        subs_list.append(self._get_pubsub_key(client.name, client.version, fn.__name__))
                    elif callable(fn) and getattr(fn, 'is_xsubscribe', False):
                        xsubs_list4registry.append((client.name, client.version, fn.__name__, getattr(fn, 'strategy')))
                        xsubs_list4redis.append(self._get_pubsub_key(client.name, client.version, fn.__name__,
                                                                     node_id=node_id))
        self._registry_client.x_subscribe(host, port, node_id, xsubs_list4registry)
        yield from self._pubsub_handler.subscribe(subs_list + xsubs_list4redis, handler=self.subscription_handler)

    def publish(self, service, version, endpoint, payload):
        endpoint_key = self._get_pubsub_key(service, version, endpoint)
        asyncio.async(self._pubsub_handler.publish(endpoint_key, json.dumps(payload, cls=VykedEncoder)))
        asyncio.async(self.xpublish(service, version, endpoint, payload))

    def xpublish(self, service, version, endpoint, payload):
        subscribers = yield from self._registry_client.get_subscribers(service, version, endpoint)
        strategies = defaultdict(list)
        for subscriber in subscribers:
            strategies[(subscriber['name'], subscriber['version'])].append(
                (subscriber['host'], subscriber['port'], subscriber['node_id'], subscriber['strategy']))
        for key, value in strategies.items():
            if value[0][3] == 'LEADER':
                node_id = value[0][2]
            else:
                random_metadata = random.choice(value)
                node_id = random_metadata[2]
            endpoint_key = self._get_pubsub_key(service, version, endpoint, node_id=node_id)
            asyncio.async(self._pubsub_handler.publish(endpoint_key, json.dumps(payload, cls=VykedEncoder)))

    def subscription_handler(self, endpoint, payload):
        elements = endpoint.split('/')
        if len(elements) > 3:
            service, version, endpoint, node_id = elements
        else:
            service, version, endpoint = elements
        client = [sc for sc in self._clients if (sc.name == service and sc.version == version)][0]
        func = getattr(client, endpoint)
        asyncio.async(func(**json.loads(payload)))

    @staticmethod
    def _get_pubsub_key(service, version, endpoint, node_id=None):
        if node_id:
            return '/'.join((service, str(version), endpoint, node_id))
        return '/'.join((service, str(version), endpoint))
