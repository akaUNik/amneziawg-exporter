#!/usr/bin/env python3

import logging
import sys
import os
import time
import subprocess
import signal
import argparse
import http.client
import json
import redis
import socket
from urllib.parse import quote
from decouple import Config, RepositoryEnv, RepositoryEmpty
from datetime import datetime, timedelta
from prometheus_client import start_http_server, CollectorRegistry, Gauge, write_to_textfile
from dataclasses import dataclass, asdict


class MyLogger:
    """Custom logger that outputs INFO messages to stdout and ERROR messages to stderr."""

    def __init__(self, name: str, level=logging.INFO):
        """
        Initialize the logger.

        Args:
            name (str): Name of the logger.
            level (int): Logging level (default is logging.INFO).
        """
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        if not self.logger.hasHandlers():
            formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s')
            stdout_handler = logging.StreamHandler(sys.stdout)
            stdout_handler.setLevel(logging.INFO)
            stdout_handler.setFormatter(formatter)
            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler.setLevel(logging.ERROR)
            stderr_handler.setFormatter(formatter)
            self.logger.addHandler(stdout_handler)
            self.logger.addHandler(stderr_handler)


class ReConfig(Config):
    """Extended Config class to support environment variable discovery by prefix."""

    def find(self, regex):
        """
        Find environment variables starting with a specific prefix.

        Args:
            regex (str): Prefix to filter environment variables.

        Returns:
            dict: Filtered environment variables.
        """
        return {k: v for k, v in os.environ.items() if k.startswith(regex)}


class Decouwrapper:
    """Wrapper for Decouple's Config to support dynamic discovery and loading."""

    def __init__(self, envfile: str = None):
        """
        Initialize the wrapper with an optional .env file.

        Args:
            envfile (str): Path to the .env file.
        """
        repository = RepositoryEnv(envfile) if envfile else RepositoryEmpty()
        self.__config = ReConfig(repository)

    def discovery(self, regex):
        """
        Discover environment variables with a given prefix, returning them in lowercase without prefix.

        Args:
            regex (str): Prefix string to search.

        Returns:
            dict: Discovered key-value pairs with prefix removed.
        """
        discovered = self.__config.find(regex)
        prefix_len = len(regex)
        return {key[prefix_len:].lower(): value for key, value in discovered.items()}

    def get(self, key, default=None):
        """
        Get a configuration value.

        Args:
            key (str): Configuration key.
            default: Default value if the key is not found.

        Returns:
            str or default: Retrieved value or default.
        """
        return self.__config.get(key, default)


@dataclass
class ExporterConfig:
    """Dataclass for holding configuration options for the exporter."""

    scrape_interval: int
    http_port: int
    addr: str
    metrics_file: str
    ops_mode: str
    awg_executable: str
    docker_containers: list
    docker_socket: str
    redis_host: str
    redis_port: int
    redis_db: int
    extra_labels: dict


class PersistenceWrapper:
    """Handles Redis-based persistence for tracking peer activity over time."""

    FIVE_MINUTES = timedelta(minutes=5)
    ONE_DAY = timedelta(days=1)
    ONE_MONTH = timedelta(days=30)

    def __init__(self, host: str, port: int, db: int):
        """
        Initialize Redis connection and set up metrics counters.

        Args:
            host (str): Redis host.
            port (int): Redis port.
            db (int): Redis database number.
        """
        self.log = MyLogger(self.__class__.__name__).logger
        self.connection = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        try:
            self.connection.ping()
        except redis.ConnectionError:
            self.log.error("Redis connection failed.")
            raise
        self.dau = self.mau = self.mau_abs = self.online = 0
        self.current_month = ''

    @staticmethod
    def _peer_key(peer: str, namespace: str = None) -> str:
        if namespace:
            return f"container:{namespace}:peer:{peer}"
        return peer

    def update_peer(self, peer: str, handshake_time: int, namespace: str = None):
        """
        Update a peer's last handshake time in Redis.

        Args:
            peer (str): Peer identifier.
            handshake_time (int): Timestamp of the latest handshake.
            namespace (str): Optional peer namespace, such as a Docker container.
        """
        key = self._peer_key(peer, namespace)
        try:
            stored = self.connection.get(key)
            if stored and float(stored) > float(handshake_time):
                return
            self.connection.set(key, handshake_time)
        except redis.RedisError as e:
            self.log.error(f"Error updating peer {peer}: {e}")

    def recalculate(self, namespace: str = None) -> dict:
        """
        Recalculate activity metrics (DAU, MAU, online peers) based on stored timestamps.
        """
        now = datetime.now()
        five_minutes_ago = (now - self.FIVE_MINUTES).timestamp()
        day_ago = (now - self.ONE_DAY).timestamp()
        month_ago = (now - self.ONE_MONTH).timestamp()
        first_day_of_month = datetime(now.year, now.month, 1).timestamp()
        counts = dict(mau_abs=0, mau=0, dau=0, online=0)
        try:
            pattern = self._peer_key('*', namespace) if namespace else '*'
            for peer in self.connection.keys(pattern):
                if namespace is None and str(peer).startswith('container:'):
                    continue
                ts = self.connection.get(peer)
                if ts:
                    ts = float(ts)
                    if ts >= first_day_of_month:
                        counts['mau_abs'] += 1
                    if ts >= month_ago:
                        counts['mau'] += 1
                    if ts >= day_ago:
                        counts['dau'] += 1
                    if ts >= five_minutes_ago:
                        counts['online'] += 1
            self.mau_abs = counts['mau_abs']
            self.mau = counts['mau']
            self.dau = counts['dau']
            self.online = counts['online']
            self.current_month = f"{now:%Y-%m}"
        except redis.RedisError as e:
            self.log.error(f"Error during recalculation: {e}")
        return counts

    def __getitem__(self, key):
        """
        Allow dictionary-style access to internal metric attributes.

        Args:
            key (str): One of 'dau', 'mau', 'mau_abs', 'online'.

        Returns:
            int: Corresponding metric value.
        """
        if key in ['dau', 'mau', 'mau_abs', 'online']:
            return getattr(self, key)
        raise KeyError(f"Invalid key: {key}")


class AwgShowWrapper:
    """Handles execution and parsing of the AWG binary output."""

    @staticmethod
    def parse(text_block: str) -> list:
        """
        Parse AWG output text to extract peer info.

        Args:
            text_block (str): Output from AWG binary.

        Returns:
            list: List of peers with handshake info.
        """
        lines = text_block.strip().splitlines()
        peers = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 6:
                peers.append({'peer': parts[4], 'latest_handshake': parts[5]})
        return peers

    @staticmethod
    def run_bin(command: list) -> str:
        """
        Execute a shell command and return the output.

        Args:
            command (list): Command to run as a list.

        Returns:
            str: Standard output from the command.
        """
        log = MyLogger('AwgShowWrapper').logger
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            log.error(f"Subprocess failed: {e.stderr.strip()}")
        except FileNotFoundError:
            log.error("WireGuard binary not found.")
        except Exception as e:
            log.error(f"Unexpected error: {e}")
        return ''


class DockerUnixHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that talks to the Docker daemon over a Unix socket."""

    def __init__(self, socket_path: str):
        super().__init__('localhost')
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class DockerExecWrapper:
    """Runs commands inside Docker containers using the Docker Engine API."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.log = MyLogger(self.__class__.__name__).logger

    def _request(self, method: str, path: str, payload: dict = None):
        body = json.dumps(payload).encode('utf-8') if payload is not None else None
        headers = {'Content-Type': 'application/json'} if payload is not None else {}
        connection = DockerUnixHTTPConnection(self.socket_path)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data
        finally:
            connection.close()

    @staticmethod
    def _decode_stream(data: bytes) -> tuple:
        stdout = []
        stderr = []
        index = 0
        while index + 8 <= len(data):
            stream_type = data[index]
            size = int.from_bytes(data[index + 4:index + 8], byteorder='big')
            index += 8
            chunk = data[index:index + size]
            index += size
            if stream_type == 1:
                stdout.append(chunk)
            elif stream_type == 2:
                stderr.append(chunk)
        if index < len(data):
            stdout.append(data[index:])
        return (
            b''.join(stdout).decode('utf-8', errors='replace'),
            b''.join(stderr).decode('utf-8', errors='replace')
        )

    def run_container(self, container: str, command: list) -> str:
        """
        Execute a command inside a Docker container and return stdout.

        Args:
            container (str): Docker container name or ID.
            command (list): Command to run inside the container.

        Returns:
            str: Standard output from the command.
        """
        container_ref = quote(container, safe='')
        create_payload = {
            'AttachStdout': True,
            'AttachStderr': True,
            'Cmd': command
        }
        try:
            status, data = self._request('POST', f'/containers/{container_ref}/exec', create_payload)
            if status < 200 or status >= 300:
                self.log.error(f"Docker exec create failed for {container}: {data.decode('utf-8', errors='replace')}")
                return ''
            exec_id = json.loads(data.decode('utf-8'))['Id']
            status, data = self._request('POST', f'/exec/{exec_id}/start', {'Detach': False, 'Tty': False})
            if status < 200 or status >= 300:
                self.log.error(f"Docker exec start failed for {container}: {data.decode('utf-8', errors='replace')}")
                return ''
            stdout, stderr = self._decode_stream(data)
            status, inspect_data = self._request('GET', f'/exec/{exec_id}/json')
            if status >= 200 and status < 300:
                exit_code = json.loads(inspect_data.decode('utf-8')).get('ExitCode')
                if exit_code:
                    self.log.error(f"Docker exec failed in {container} with exit code {exit_code}: {stderr.strip()}")
                    return ''
            elif stderr.strip():
                self.log.error(f"Docker exec stderr in {container}: {stderr.strip()}")
            return stdout.strip()
        except FileNotFoundError:
            self.log.error(f"Docker socket not found: {self.socket_path}")
        except OSError as e:
            self.log.error(f"Docker socket error: {e}")
        except Exception as e:
            self.log.error(f"Unexpected Docker exec error for {container}: {e}")
        return ''

    def run_containers(self, containers: list, command: list) -> dict:
        return {
            container: self.run_container(container, command)
            for container in containers
        }


class Exporter:
    """Prometheus exporter that collects and exposes metrics based on AWG output."""

    def __init__(self, config: ExporterConfig):
        """
        Initialize the exporter with configuration and setup metrics.

        Args:
            config (ExporterConfig): Exporter configuration object.
        """
        self.config = config
        self.log = MyLogger(self.__class__.__name__).logger
        self.registry = CollectorRegistry()
        self.storage = PersistenceWrapper(config.redis_host, config.redis_port, config.redis_db)
        self.awg_show_command = config.awg_executable.split()
        self.docker = DockerExecWrapper(config.docker_socket) if config.docker_containers else None
        labels = list(config.extra_labels.keys())
        if self.docker and 'container' not in labels:
            labels = ['container'] + labels
        self.has_labels = True if len(labels) > 0 else False
        self.metrics = {
            'online': Gauge('awg_current_online', 'Online users', labels, registry=self.registry),
            'dau': Gauge('awg_dau', 'Daily Active Users', labels, registry=self.registry),
            'mau': Gauge('awg_mau', 'Monthly Active Users', labels, registry=self.registry),
            'mau_abs': Gauge('awg_mau_abs', 'Absolute Monthly Active Users', ['month'] + labels, registry=self.registry),
            'status': Gauge('awg_status', 'Exporter status', labels, registry=self.registry)
        }
        self.log.info("Exporter initialized")

    def metric_labels(self, container: str = None) -> dict:
        labels = dict(self.config.extra_labels)
        if self.docker:
            labels['container'] = container
        return labels

    def set_metric(self, name, value=None, container: str = None):
        if value is None:
            if name != 'status':
                try:
                    value = self.storage[name]
                except Exception:
                    value = 0
            else:
                value = 1
        labels = self.metric_labels(container)
        if self.has_labels:
            if name == 'mau_abs':
                self.metrics[name].labels(month=self.storage.current_month, **labels).set(value)
            else:
                self.metrics[name].labels(**labels).set(value)
        else:
            if name == 'mau_abs':
                self.metrics[name].labels(month=self.storage.current_month).set(value)
            else:
                self.metrics[name].set(value)

    def update_metrics(self):
        """
        Fetch the latest peer data, update Redis, and export metrics.
        """
        if self.docker:
            outputs = self.docker.run_containers(self.config.docker_containers, self.awg_show_command)
            for container, output in outputs.items():
                peers = AwgShowWrapper.parse(output)
                for peer in peers:
                    if peer.get('latest_handshake') != '0':
                        self.storage.update_peer(peer['peer'], peer['latest_handshake'], namespace=container)
                counts = self.storage.recalculate(namespace=container)
                status = 1 if output and peers else 0
                self.set_metric('online', counts['online'], container=container)
                self.set_metric('dau', counts['dau'], container=container)
                self.set_metric('mau', counts['mau'], container=container)
                self.set_metric('mau_abs', counts['mau_abs'], container=container)
                self.set_metric('status', status, container=container)
            return

        output = AwgShowWrapper.run_bin(self.awg_show_command)
        peers = AwgShowWrapper.parse(output)
        for peer in peers:
            if peer.get('latest_handshake') != '0':
                self.storage.update_peer(peer['peer'], peer['latest_handshake'])
        self.storage.recalculate()
        for metric in self.metrics.keys():
            self.set_metric(metric)

    def run(self):
        """
        Start the exporter based on the configured operational mode.
        Supports 'http', 'metricsfile', and 'oneshot'.
        """
        self.log.info("Exporter running")
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        if self.config.ops_mode == 'http':
            start_http_server(self.config.http_port, addr=self.config.addr, registry=self.registry)
        while True:
            self.update_metrics()
            if self.config.ops_mode in ['metricsfile', 'oneshot']:
                write_to_textfile(self.config.metrics_file, self.registry)
            if self.config.ops_mode == 'oneshot':
                break
            time.sleep(self.config.scrape_interval)


def main():
    """
    Entry point for the exporter script. Parses arguments, loads config,
    initializes and runs the exporter.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--envfile', type=str, help='Path to env file')
    args = parser.parse_args()
    raw = Decouwrapper(args.envfile)
    docker_containers = parse_docker_containers(raw.get('AWG_EXPORTER_DOCKER_CONTAINERS', ''))
    config = ExporterConfig(
        scrape_interval=int(raw.get('AWG_EXPORTER_SCRAPE_INTERVAL', 60)),
        http_port=int(raw.get('AWG_EXPORTER_HTTP_PORT', 9351)),
        addr=raw.get('AWG_EXPORTER_LISTEN_ADDR', '0.0.0.0'),
        metrics_file=raw.get('AWG_EXPORTER_METRICS_FILE', '/tmp/prometheus/awg.prom'),
        ops_mode=raw.get('AWG_EXPORTER_OPS_MODE', 'http'),
        awg_executable=raw.get('AWG_EXPORTER_AWG_SHOW_EXEC', 'wg show all dump'),
        docker_containers=docker_containers,
        docker_socket=raw.get('AWG_EXPORTER_DOCKER_SOCKET', '/var/run/docker.sock'),
        redis_host=raw.get('AWG_EXPORTER_REDIS_HOST', 'localhost'),
        redis_port=int(raw.get('AWG_EXPORTER_REDIS_PORT', 6379)),
        redis_db=int(raw.get('AWG_EXPORTER_REDIS_DB', 0)),
        extra_labels=raw.discovery('AWG_EXPORTER_EXTRA_LABEL_')
    )

    logger = MyLogger("Main").logger
    logger.info("Starting Exporter")
    logger.info('Exporter config:')
    for key, value in asdict(config).items():
        if key == 'metrics_file' and config.ops_mode != 'metricsfile':
            continue
        logger.info(f"--> {key}: {value}")
    exporter = Exporter(config)
    exporter.run()


def parse_docker_containers(value: str) -> list:
    return [
        container.strip()
        for container in value.split(',')
        if container.strip()
    ]


if __name__ == '__main__':
    main()
