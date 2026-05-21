import fnmatch
import os
import sys
import time

from prometheus_client import generate_latest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import exporter


AWG_OUTPUT = """awg0 private public 51820 off
awg0 peer-a psk 1.2.3.4:1234 10.8.0.2/32 1893456000 10 20 off
"""


class FakeRedisConnection:
    def __init__(self):
        self.data = {}

    def ping(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def keys(self, pattern='*'):
        return [
            key
            for key in self.data.keys()
            if fnmatch.fnmatch(key, pattern)
        ]


class FakeStorage:
    def __init__(self):
        self.current_month = '2026-05'
        self.updated_peers = []
        self.counts = {
            None: dict(online=1, dau=1, mau=1, mau_abs=1),
            'amnezia-wg': dict(online=1, dau=1, mau=1, mau_abs=1),
            'amnezia-wg2': dict(online=0, dau=0, mau=0, mau_abs=0),
        }

    def update_peer(self, peer, handshake_time, namespace=None):
        self.updated_peers.append((peer, handshake_time, namespace))

    def recalculate(self, namespace=None):
        return self.counts[namespace]

    def __getitem__(self, key):
        return self.counts[None][key]


def make_config(docker_containers=None):
    return exporter.ExporterConfig(
        scrape_interval=60,
        http_port=9351,
        addr='127.0.0.1',
        metrics_file='/tmp/awg.prom',
        ops_mode='http',
        awg_executable='wg show all dump',
        docker_containers=docker_containers or [],
        docker_socket='/var/run/docker.sock',
        redis_host='localhost',
        redis_port=6379,
        redis_db=0,
        extra_labels={},
    )


def docker_stream(stream_type, payload):
    return bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, 'big') + payload


def test_parse_single_awg_dump_output():
    assert exporter.AwgShowWrapper.parse(AWG_OUTPUT) == [
        {'peer': '10.8.0.2/32', 'latest_handshake': '1893456000'}
    ]


def test_parse_multiple_awg_dump_outputs():
    second_output = """awg1 private public 51821 off
awg1 peer-b psk 1.2.3.5:1234 10.8.0.3/32 1893456001 11 21 off
"""
    assert exporter.AwgShowWrapper.parse(f"{AWG_OUTPUT}\n{second_output}") == [
        {'peer': '10.8.0.2/32', 'latest_handshake': '1893456000'},
        {'peer': '10.8.0.3/32', 'latest_handshake': '1893456001'},
    ]


def test_parse_docker_containers_trims_empty_entries():
    assert exporter.parse_docker_containers(' amnezia-wg, ,amnezia-wg2,, ') == [
        'amnezia-wg',
        'amnezia-wg2',
    ]


def test_persistence_namespaces_container_peer_keys(monkeypatch):
    redis_connection = FakeRedisConnection()
    monkeypatch.setattr(exporter.redis, 'Redis', lambda **kwargs: redis_connection)

    storage = exporter.PersistenceWrapper('localhost', 6379, 0)
    storage.update_peer('peer-a', 200, namespace='amnezia-wg')
    storage.update_peer('peer-a', 100, namespace='amnezia-wg')
    storage.update_peer('peer-a', 300, namespace='amnezia-wg2')

    assert redis_connection.data == {
        'container:amnezia-wg:peer:peer-a': 200,
        'container:amnezia-wg2:peer:peer-a': 300,
    }


def test_persistence_recalculates_only_requested_container(monkeypatch):
    redis_connection = FakeRedisConnection()
    monkeypatch.setattr(exporter.redis, 'Redis', lambda **kwargs: redis_connection)
    now = int(time.time())
    redis_connection.set('legacy-peer', now)
    redis_connection.set('container:amnezia-wg:peer:peer-a', now)
    redis_connection.set('container:amnezia-wg2:peer:peer-a', now)

    storage = exporter.PersistenceWrapper('localhost', 6379, 0)

    assert storage.recalculate(namespace='amnezia-wg')['online'] == 1
    assert storage.recalculate(namespace='amnezia-wg2')['online'] == 1
    assert storage.recalculate()['online'] == 1


def test_local_metrics_do_not_include_container_label(monkeypatch):
    fake_storage = FakeStorage()
    monkeypatch.setattr(exporter, 'PersistenceWrapper', lambda *args: fake_storage)
    monkeypatch.setattr(exporter.AwgShowWrapper, 'run_bin', staticmethod(lambda command: AWG_OUTPUT))

    instance = exporter.Exporter(make_config())
    instance.update_metrics()
    metrics = generate_latest(instance.registry).decode()

    assert 'container=' not in metrics
    assert 'awg_current_online 1.0' in metrics
    assert fake_storage.updated_peers == [('10.8.0.2/32', '1893456000', None)]


def test_docker_metrics_include_container_label_and_status(monkeypatch):
    fake_storage = FakeStorage()

    class FakeDockerExecWrapper:
        def __init__(self, socket_path):
            self.socket_path = socket_path

        def run_containers(self, containers, command):
            assert containers == ['amnezia-wg', 'amnezia-wg2']
            assert command == ['wg', 'show', 'all', 'dump']
            return {
                'amnezia-wg': AWG_OUTPUT,
                'amnezia-wg2': '',
            }

    monkeypatch.setattr(exporter, 'PersistenceWrapper', lambda *args: fake_storage)
    monkeypatch.setattr(exporter, 'DockerExecWrapper', FakeDockerExecWrapper)

    instance = exporter.Exporter(make_config(docker_containers=['amnezia-wg', 'amnezia-wg2']))
    instance.update_metrics()
    metrics = generate_latest(instance.registry).decode()

    assert 'awg_current_online{container="amnezia-wg"} 1.0' in metrics
    assert 'awg_current_online{container="amnezia-wg2"} 0.0' in metrics
    assert 'awg_status{container="amnezia-wg"} 1.0' in metrics
    assert 'awg_status{container="amnezia-wg2"} 0.0' in metrics
    assert fake_storage.updated_peers == [('10.8.0.2/32', '1893456000', 'amnezia-wg')]


def test_docker_exec_wrapper_returns_decoded_stdout(monkeypatch):
    wrapper = exporter.DockerExecWrapper('/var/run/docker.sock')
    responses = [
        (201, b'{"Id": "exec-id"}'),
        (200, docker_stream(1, b'ok\n')),
        (200, b'{"ExitCode": 0}'),
    ]

    monkeypatch.setattr(wrapper, '_request', lambda *args: responses.pop(0))

    assert wrapper.run_container('amnezia-wg', ['wg', 'show']) == 'ok'


def test_docker_exec_wrapper_returns_empty_on_nonzero_exit(monkeypatch):
    wrapper = exporter.DockerExecWrapper('/var/run/docker.sock')
    responses = [
        (201, b'{"Id": "exec-id"}'),
        (200, docker_stream(2, b'failed\n')),
        (200, b'{"ExitCode": 1}'),
    ]

    monkeypatch.setattr(wrapper, '_request', lambda *args: responses.pop(0))

    assert wrapper.run_container('amnezia-wg', ['wg', 'show']) == ''


def test_docker_exec_wrapper_missing_socket_does_not_crash(monkeypatch):
    wrapper = exporter.DockerExecWrapper('/var/run/docker.sock')

    def raise_missing_socket(*args):
        raise FileNotFoundError()

    monkeypatch.setattr(wrapper, '_request', raise_missing_socket)

    assert wrapper.run_container('amnezia-wg', ['wg', 'show']) == ''
