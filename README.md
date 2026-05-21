![License](https://img.shields.io/github/license/amnezia-vpn/amneziawg-exporter)  
![Docker Latest Version](https://img.shields.io/docker/v/amneziavpn/amneziawg-exporter)  
![Docker Image Size](https://img.shields.io/docker/image-size/amneziavpn/amneziawg-exporter)  
![Docker Pulls](https://img.shields.io/docker/pulls/amneziavpn/amneziawg-exporter)

# AmneziaWG exporter

AmneziaWG exporter is a Prometheus exporter for gathering AmneziaWG client connection metrics.

## Features and limitations

### Client identification

amneziawg-exporter can optionally identify WireGuard clients using a client table. If this feature is enabled, clients are identified by their names; otherwise, they are marked as "unidentified."

### Operating modes

amneziawg-exporter has three operating modes (`AWG_EXPORTER_OPS_MODE` environment variable):

*   `http` - Run an HTTP server on `AWG_EXPORTER_HTTP_PORT` to make metrics accessible, like most exporters. _Default_
*   `metricsfile` - Write metrics to `AWG_EXPORTER_METRICS_FILE` instead of serving them on an HTTP port.
*   `oneshot` - Same as in `metricsfile` mode, but the service creates a metrics file and then shuts down. In Docker, you can use a volume to save the file on disk. It can then be used by [node-exporter](https://github.com/prometheus/node_exporter) to serve your exporter metrics.
*   `grafana_cloud` - Sends metrics directly to Grafana Cloud using the provided API URL and token.

> \[!TIP\]  
> Open [this link](https://github.com/prometheus/node_exporter#textfile-collector) to read more about the textfile collector.

## Configuration

The following environment variables can be used to configure amneziawg-exporter.

| Variable Name | Default Value | Description |
| --- | --- | --- |
| AWG\_EXPORTER\_SCRAPE\_INTERVAL | 60 | Interval for scraping WireGuard metrics (for the `http` mode). |
| AWG\_EXPORTER\_HTTP\_PORT | 9351 | Port for HTTP service. |
| AWG\_EXPORTER\_LISTEN\_ADDR | 0.0.0.0 | Listen address for HTTP service. |
| AWG\_EXPORTER\_METRICS\_FILE | /tmp/prometheus/awg.prom | Path to the metrics file for Node exporter textfile collector. |
| AWG\_EXPORTER\_OPS\_MODE | http | Operation mode for the exporter (`http`, `metricsfile`, `oneshot` or `grafana_cloud`). |
| AWG\_EXPORTER\_AWG\_SHOW\_EXEC | "awg show all dump" | Command to run the `awg show` command. |
| AWG\_EXPORTER\_DOCKER\_CONTAINERS |   | Comma-separated Docker container names or IDs to run `AWG_EXPORTER_AWG_SHOW_EXEC` in. |
| AWG\_EXPORTER\_DOCKER\_SOCKET | /var/run/docker.sock | Docker Engine Unix socket used when `AWG_EXPORTER_DOCKER_CONTAINERS` is set. |
| AWG\_EXPORTER\_EXTRA\_LABEL\_\* |   | Additional labels to add to each metric (`AWG_EXPORTER_EXTRA_LABEL_(.*)` - lowercase key by this regexp) |
| AWG\_EXPORTER\_REDIS\_HOST | localhost | Redis server host to store peers data |
| AWG\_EXPORTER\_REDIS\_PORT | 6379 | Redis server port to store peers data |
| AWG\_EXPORTER\_REDIS\_DB | 0 | Redis server db number to store peers data |

### Docker container collection mode

When `AWG_EXPORTER_DOCKER_CONTAINERS` is set, the exporter uses the Docker Engine API to execute `AWG_EXPORTER_AWG_SHOW_EXEC` inside each listed container. For example:

```
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
environment:
  AWG_EXPORTER_REDIS_HOST: amneziawg-exporter-redis
  AWG_EXPORTER_DOCKER_CONTAINERS: amnezia-wg,amnezia-wg2
```

In this mode, `awg` must be available inside each target AmneziaWG container. Mounting `/var/run/docker.sock` gives the exporter privileged access to the host Docker daemon, so only use it with trusted images and configuration.

Docker collection mode adds a `container` label to the exported metrics, for example `awg_current_online{container="amnezia-wg"}` and `awg_status{container="amnezia-wg2"}`.

## Metrics

| Metric name | Labels | Description |
| --- | --- | --- |
| awg\_current\_online | container\* | Current number of online users. |
| awg\_dau | container\* | Daily active users. |
| awg\_mau | container\* | Monthly active users. |
| awg\_mau\_abs | month, container\* | Absolute monthly active users. |
| awg\_status | container\* | Exporter status. 1 - OK, 0 - not OK |

`container` is present only when Docker container collection mode is enabled.

## Docker image

The Docker image is built using the [Dockerfile](Dockerfile) available in this repository. You can easily obtain it from [DockerHub](https://hub.docker.com/r/amneziavpn/amneziawg-exporter) by running the command `docker pull amneziavpn/amneziawg-exporter.`

## Example usage

You can use example [docker-compose.yml](docker-compose.yml) with Docker Compose v2 to run AmneziaWG exporter:

```
# docker compose up -d
[+] Running 3/3
 ✔ Network amneziawg-exporter_default  Created          0.2s
 ✔ Container amneziawg-exporter-redis  Started          0.1s
 ✔ Container amneziawg-exporter        Started          0.1s
# docker compose ps
NAME                 IMAGE                                          COMMAND                         SERVICE              CREATED          STATUS          PORTS
amneziawg-exporter         amneziavpn/amneziawg-exporter:latest   "/exporter.py"           amneziawg-exporter         15 seconds ago   Up 14 seconds   0.0.0.0:9351->9351/tcp, :::9351->9351/tcp
amneziawg-exporter-redis   redis:alpine                           "docker-entrypoint.s…"   amneziawg-exporter-redis   15 seconds ago   Up 14 seconds   6379/tcp
```

> \[!TIP\]  
> Run `docker compose build` before, if you want to build image by yourself.