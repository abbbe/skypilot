"""Scaleway instance provisioning."""

from typing import Any, Dict, List, Optional, Tuple

from sky import exceptions
from sky import sky_logging
from sky.provision import common
from sky.provision.scaleway import utils
from sky.resources import DEFAULT_DISK_SIZE_GB
from sky.utils import status_lib
from sky.utils import ux_utils

logger = sky_logging.init_logger(__name__)

_PROVIDER_NAME = 'scaleway'

# Scaleway GPU instances can take a few minutes to be allocated and booted.
_BOOT_TIMEOUT_SECONDS = 15 * 60
_STOP_TIMEOUT_SECONDS = 10 * 60
_TERMINATE_TIMEOUT_SECONDS = 10 * 60

# Scaleway's error bodies for capacity problems.
_OUT_OF_STOCK_MARKERS = (
    'out_of_stock',
    'out of stock',
    'no available',
    'not available',
    'insufficient',
    'quota',
    'resource_exhausted',
)

_RUNNING_STATES = (utils.STATE_RUNNING,)
_STOPPED_STATES = (utils.STATE_STOPPED, utils.STATE_STOPPED_IN_PLACE)

_STATUS_MAP = {
    utils.STATE_RUNNING: status_lib.ClusterStatus.UP,
    utils.STATE_STARTING: status_lib.ClusterStatus.INIT,
    utils.STATE_STOPPING: status_lib.ClusterStatus.STOPPED,
    utils.STATE_STOPPED: status_lib.ClusterStatus.STOPPED,
    utils.STATE_STOPPED_IN_PLACE: status_lib.ClusterStatus.STOPPED,
    utils.STATE_LOCKED: status_lib.ClusterStatus.INIT,
}


def _zone_of(provider_config: Optional[Dict[str, Any]]) -> str:
    """Returns the zone a cluster lives in.

    Every Scaleway Instances call is zone-scoped, so a missing zone must fail
    here rather than be silently defaulted to somewhere the cluster is not.
    """
    assert provider_config is not None, 'provider_config is required'
    zone = provider_config.get('availability_zone')
    if not zone:
        raise ValueError(
            'Scaleway provider config has no availability_zone; cannot '
            'address the cluster.')
    return zone


def _is_capacity_error(e: Exception) -> bool:
    message = str(e).lower()
    return any(marker in message for marker in _OUT_OF_STOCK_MARKERS)


def _head_first(servers: List[Any]) -> List[Any]:
    return sorted(servers, key=lambda s: 0 if utils.is_head(s) else 1)


def run_instances(region: str, cluster_name: str, cluster_name_on_cloud: str,
                  config: common.ProvisionConfig) -> common.ProvisionRecord:
    """Creates or resumes the instances of a cluster. See provision/__init__."""
    del cluster_name  # unused
    zone = _zone_of(config.provider_config)

    servers = utils.list_cluster_servers(zone, cluster_name_on_cloud)
    alive = [s for s in servers if str(s.state) != utils.STATE_STOPPING]
    if len(alive) > config.count:
        raise RuntimeError(
            f'Cluster {cluster_name_on_cloud} already has {len(alive)} '
            f'instances in {zone}, but only {config.count} were requested.')

    stopped = [s for s in alive if str(s.state) in _STOPPED_STATES]
    resumed_instance_ids: List[str] = []
    if stopped and not config.resume_stopped_nodes:
        # Counting a stopped node towards `count` while refusing to start it
        # would leave the cluster permanently short of nodes, reported as a
        # successful launch.
        raise RuntimeError(
            f'Cluster {cluster_name_on_cloud} has {len(stopped)} stopped '
            f'instance(s) in {zone} and resuming them was not requested. '
            'Terminate the cluster with `sky down` before relaunching it.')
    for server in _head_first(stopped):
        logger.info(f'Starting stopped Scaleway server {server.id}.')
        utils.power_on(zone, server.id)
        resumed_instance_ids.append(server.id)

    head_instance_id: Optional[str] = None
    for server in alive:
        if utils.is_head(server):
            head_instance_id = server.id
            break

    security_group_id = config.provider_config.get('security_group_id')
    if security_group_id is None:
        security_group_id = utils.find_security_group(zone,
                                                      cluster_name_on_cloud)

    node_config = config.node_config
    instance_type = node_config['InstanceType']
    image_label = node_config['ImageId']
    disk_size = int(node_config.get('DiskSize') or DEFAULT_DISK_SIZE_GB)

    created_instance_ids: List[str] = []
    to_start_count = config.count - len(alive)
    for _ in range(to_start_count):
        node_kind = (utils.HEAD_NODE_KIND
                     if head_instance_id is None else utils.WORKER_NODE_KIND)
        name = f'{cluster_name_on_cloud}-{node_kind}'
        tags = [
            utils.TAG_SKYPILOT,
            utils.cluster_tag(cluster_name_on_cloud),
            utils.node_kind_tag(node_kind),
        ]
        for key, value in (config.tags or {}).items():
            tags.append(utils.make_tag(key, value))

        try:
            server = utils.create_server(
                zone=zone,
                name=name,
                commercial_type=instance_type,
                image_label=image_label,
                disk_size_gb=disk_size,
                tags=tags,
                security_group_id=security_group_id,
                cluster_name_on_cloud=cluster_name_on_cloud,
            )
        except Exception as e:  # pylint: disable=broad-except
            message = (
                f'Failed to launch a {instance_type} instance on Scaleway in '
                f'{zone}: {e}')
            with ux_utils.print_exception_no_traceback():
                if _is_capacity_error(e):
                    raise exceptions.ResourcesUnavailableError(
                        f'Scaleway has no {instance_type} capacity in {zone} '
                        f'right now. Try another zone or instance type. '
                        f'Details: {e}') from e
                raise exceptions.ResourcesUnavailableError(message) from e

        logger.info(f'Launched Scaleway server {server.id} ({name}) in {zone}.')
        created_instance_ids.append(server.id)
        if head_instance_id is None:
            head_instance_id = server.id

    # Wait for everything to be running before handing the cluster over.
    for instance_id in created_instance_ids + resumed_instance_ids:
        try:
            server = utils.wait_for_state(zone, instance_id, _RUNNING_STATES,
                                          _BOOT_TIMEOUT_SECONDS)
        except TimeoutError as e:
            with ux_utils.print_exception_no_traceback():
                raise exceptions.ResourcesUnavailableError(str(e)) from e
        if server is None:
            with ux_utils.print_exception_no_traceback():
                raise exceptions.ResourcesUnavailableError(
                    f'Scaleway server {instance_id} disappeared while it was '
                    'being provisioned.')

    assert head_instance_id is not None, (
        f'No head instance for cluster {cluster_name_on_cloud}.')
    return common.ProvisionRecord(
        provider_name=_PROVIDER_NAME,
        region=region,
        zone=zone,
        cluster_name=cluster_name_on_cloud,
        head_instance_id=head_instance_id,
        resumed_instance_ids=resumed_instance_ids,
        created_instance_ids=created_instance_ids,
    )


def wait_instances(region: str, cluster_name_on_cloud: str,
                   state: Optional[status_lib.ClusterStatus]) -> None:
    """See sky/provision/__init__.py"""
    # run_instances() and stop_instances() already block until the instances
    # reach the requested state.
    del region, cluster_name_on_cloud, state


def stop_instances(
    cluster_name_on_cloud: str,
    provider_config: Optional[Dict[str, Any]] = None,
    worker_only: bool = False,
) -> None:
    """See sky/provision/__init__.py"""
    zone = _zone_of(provider_config)
    servers = utils.list_cluster_servers(zone, cluster_name_on_cloud)

    stopping: List[str] = []
    for server in servers:
        if worker_only and utils.is_head(server):
            continue
        if str(server.state) in _STOPPED_STATES:
            continue
        logger.debug(f'Powering off Scaleway server {server.id}.')
        utils.power_off(zone, server.id)
        stopping.append(server.id)

    for instance_id in stopping:
        utils.wait_for_state(zone, instance_id, _STOPPED_STATES,
                             _STOP_TIMEOUT_SECONDS)


def terminate_instances(
    cluster_name_on_cloud: str,
    provider_config: Optional[Dict[str, Any]] = None,
    worker_only: bool = False,
) -> None:
    """See sky/provision/__init__.py"""
    zone = _zone_of(provider_config)
    servers = utils.list_cluster_servers(zone, cluster_name_on_cloud)

    leftover_volume_ids: List[str] = []
    terminating: List[str] = []
    for server in servers:
        if worker_only and utils.is_head(server):
            continue
        leftover_volume_ids.extend(utils.server_volume_ids(server))
        logger.debug(f'Terminating Scaleway server {server.id}.')
        utils.terminate_server(zone, server.id)
        terminating.append(server.id)

    if worker_only:
        return

    # The terminate action removes the server and the local volumes it owns.
    # Block volumes and the flexible IPs outlive it and are billed until they
    # are reclaimed, and a block volume cannot be deleted while it is still
    # attached -- so wait for the servers to actually go first.
    still_there = utils.wait_until_gone(zone, terminating,
                                        _TERMINATE_TIMEOUT_SECONDS)
    if still_there:
        logger.warning(
            f'Scaleway servers {still_there} are still being deleted; their '
            'volumes and IPs will be reclaimed on the next teardown.')
    utils.delete_volumes(zone, leftover_volume_ids)
    utils.release_cluster_ips(zone, cluster_name_on_cloud)
    utils.delete_security_group(zone, cluster_name_on_cloud)


def get_cluster_info(
    region: str,
    cluster_name_on_cloud: str,
    provider_config: Optional[Dict[str, Any]] = None,
) -> common.ClusterInfo:
    """See sky/provision/__init__.py"""
    del region  # unused
    zone = _zone_of(provider_config)
    servers = utils.list_cluster_servers(zone, cluster_name_on_cloud)

    instances: Dict[str, List[common.InstanceInfo]] = {}
    head_instance_id: Optional[str] = None
    for server in servers:
        if str(server.state) != utils.STATE_RUNNING:
            continue
        external_ip = utils.external_ip(server)
        internal_ip = utils.internal_ip(server) or external_ip
        instances[server.id] = [
            common.InstanceInfo(
                instance_id=server.id,
                internal_ip=internal_ip or '',
                external_ip=external_ip,
                ssh_port=22,
                tags={'provider': _PROVIDER_NAME},
                node_name=server.name,
            )
        ]
        if utils.is_head(server):
            head_instance_id = server.id

    return common.ClusterInfo(
        instances=instances,
        head_instance_id=head_instance_id,
        provider_name=_PROVIDER_NAME,
        provider_config=provider_config,
        # Scaleway's Ubuntu images, including the GPU ones, only provision the
        # root account with the project's SSH keys.
        ssh_user='root',
    )


def query_instances(
    cluster_name: str,
    cluster_name_on_cloud: str,
    provider_config: Optional[Dict[str, Any]] = None,
    non_terminated_only: bool = True,
    retry_if_missing: bool = False,
) -> Dict[str, Tuple[Optional[status_lib.ClusterStatus], Optional[str]]]:
    """See sky/provision/__init__.py"""
    del cluster_name, retry_if_missing  # unused
    zone = _zone_of(provider_config)
    servers = utils.list_cluster_servers(zone, cluster_name_on_cloud)

    statuses: Dict[str, Tuple[Optional[status_lib.ClusterStatus],
                              Optional[str]]] = {}
    for server in servers:
        state = str(server.state)
        if state not in _STATUS_MAP:
            # An unknown state must not be reported as a healthy cluster.
            logger.warning(
                f'Unknown Scaleway server state {state!r} for {server.id}; '
                'reporting it as INIT.')
            status: Optional[status_lib.ClusterStatus] = (
                status_lib.ClusterStatus.INIT)
        else:
            status = _STATUS_MAP[state]
        if non_terminated_only and status is None:
            continue
        statuses[server.id] = (status, getattr(server, 'state_detail', None) or
                               None)
    return statuses


def open_ports(
    cluster_name_on_cloud: str,
    ports: List[str],
    provider_config: Optional[Dict[str, Any]] = None,
) -> None:
    """See sky/provision/__init__.py

    Scaleway is registered as OPEN_PORTS_VERSION.LAUNCH_ONLY: the ports are
    baked into the cluster's security group when it is created.
    """
    del cluster_name_on_cloud, ports, provider_config


def cleanup_ports(
    cluster_name_on_cloud: str,
    ports: List[str],
    provider_config: Optional[Dict[str, Any]] = None,
) -> None:
    """See sky/provision/__init__.py"""
    del ports  # The whole security group goes away with the cluster.
    if provider_config is None:
        return
    zone = provider_config.get('availability_zone')
    if not zone:
        return
    utils.delete_security_group(zone, cluster_name_on_cloud)
