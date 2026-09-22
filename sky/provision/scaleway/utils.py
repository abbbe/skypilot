"""Helpers wrapping the Scaleway Instances API.

Everything in the Instances API is scoped to a single zone, so every helper
here takes an explicit ``zone``.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from sky import sky_logging
from sky.adaptors import scaleway as scaleway_adaptor

logger = sky_logging.init_logger(__name__)

# Tags SkyPilot writes onto every server it creates. Scaleway tags are free
# form strings, so use a 'key=value' convention.
TAG_CLUSTER_NAME = 'skypilot-cluster'
TAG_NODE_KIND = 'skypilot-node'
TAG_SKYPILOT = 'skypilot'

HEAD_NODE_KIND = 'head'
WORKER_NODE_KIND = 'worker'

# Scaleway states, from the ServerState enum.
STATE_RUNNING = 'running'
STATE_STOPPED = 'stopped'
STATE_STOPPED_IN_PLACE = 'stopped_in_place'
STATE_STARTING = 'starting'
STATE_STOPPING = 'stopping'
STATE_LOCKED = 'locked'

# The marketplace publishes one image variant per root-volume technology; the
# variant has to match the volume the instance type can actually boot from.
_IMAGE_TYPE_LOCAL = 'instance_local'
_IMAGE_TYPE_SBS = 'instance_sbs'

_VOLUME_TYPE_LOCAL = 'l_ssd'
_VOLUME_TYPE_SBS = 'sbs_volume'

# Scaleway expresses every volume size in bytes and counts in decimal GB: the
# l_ssd constraint of a RENDER-S reads 800000000000 for its documented 800 GB.
_BYTES_PER_GB = 1000**3

# Scaleway's smallest block volume.
_MIN_SBS_VOLUME_SIZE_GB = 10

_POLL_INTERVAL_SECONDS = 5


def make_tag(key: str, value: str) -> str:
    return f'{key}={value}'


def cluster_tag(cluster_name_on_cloud: str) -> str:
    return make_tag(TAG_CLUSTER_NAME, cluster_name_on_cloud)


def node_kind_tag(node_kind: str) -> str:
    return make_tag(TAG_NODE_KIND, node_kind)


def get_tag_value(tags: List[str], key: str) -> Optional[str]:
    prefix = f'{key}='
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return None


def node_kind_of(server: Any) -> Optional[str]:
    return get_tag_value(list(server.tags or []), TAG_NODE_KIND)


def is_head(server: Any) -> bool:
    return node_kind_of(server) == HEAD_NODE_KIND


def _is_not_found(e: Exception) -> bool:
    """Whether a Scaleway SDK exception is a 404.

    Teardown has to be idempotent: a resource somebody already removed is a
    success, not a failure.
    """
    status = getattr(getattr(e, 'response', None), 'status_code', None)
    if status == 404:
        return True
    return 'not_found' in str(e) or '404' in str(e)


def list_cluster_servers(zone: str, cluster_name_on_cloud: str) -> List[Any]:
    """Returns the servers of a cluster in a zone.

    The API-side tag filter is only a hint: re-check the tag locally so that a
    change in its matching semantics cannot silently hand us another cluster's
    servers.
    """
    api = scaleway_adaptor.instance_api()
    wanted = cluster_tag(cluster_name_on_cloud)
    try:
        servers = api.list_servers_all(zone=zone, tags=[wanted])
    except Exception as e:  # pylint: disable=broad-except
        if _is_not_found(e):
            return []
        raise
    return [s for s in servers if wanted in (s.tags or [])]


def get_server(zone: str, server_id: str) -> Optional[Any]:
    api = scaleway_adaptor.instance_api()
    try:
        return api.get_server(zone=zone, server_id=server_id).server
    except Exception as e:  # pylint: disable=broad-except
        if _is_not_found(e):
            return None
        raise


def get_server_type(zone: str, commercial_type: str) -> Optional[Any]:
    """Returns the ServerType description for an instance type in a zone."""
    api = scaleway_adaptor.instance_api()
    page = 1
    while True:
        response = api.list_servers_types(zone=zone, per_page=100, page=page)
        batch = response.servers
        if not batch:
            return None
        if commercial_type in batch:
            return batch[commercial_type]
        if len(batch) < 100:
            return None
        page += 1


def root_volume_spec(zone: str, commercial_type: str) -> Tuple[str, int, int]:
    """Returns (volume_type, min_size_bytes, max_size_bytes) for a root volume.

    Scaleway's newer GPU instances (H100, L4, L40S, ...) report an ``l_ssd``
    constraint of max_size=0, meaning they cannot use local SSD at all and
    must boot from a block (SBS) volume. The older local-SSD instance types
    are the other way round, so pick from what the API reports rather than
    from a hardcoded list that would rot as the offering changes.

    For a local-SSD type the reported constraint is a hard limit, so return it
    and let the caller clamp; an over-sized request is rejected at create time.
    """
    server_type = get_server_type(zone, commercial_type)
    if server_type is None:
        raise ValueError(
            f'Scaleway instance type {commercial_type!r} is not offered in '
            f'zone {zone}.')
    per_volume = getattr(server_type, 'per_volume_constraint', None)
    local_ssd = getattr(per_volume, 'l_ssd', None) if per_volume else None
    max_size = getattr(local_ssd, 'max_size', 0) or 0
    min_size = getattr(local_ssd, 'min_size', 0) or 0
    if max_size > 0:
        return _VOLUME_TYPE_LOCAL, min_size, max_size
    return _VOLUME_TYPE_SBS, _MIN_SBS_VOLUME_SIZE_GB * _BYTES_PER_GB, 0


def root_volume_type(zone: str, commercial_type: str) -> str:
    """Returns the volume type an instance type boots from."""
    return root_volume_spec(zone, commercial_type)[0]


def resolve_image_id(zone: str, image_label: str, volume_type: str) -> str:
    """Resolves a marketplace image *label* to a zone-local image id.

    A label such as ``ubuntu_noble_gpu_os_12`` maps to a different id in every
    zone, and Scaleway republishes the image behind it regularly. Resolving at
    provision time is what keeps this from going stale.
    """
    # A caller may pass an image id straight through; ids are UUIDs and labels
    # never look like one.
    if _looks_like_uuid(image_label):
        return image_label

    api = scaleway_adaptor.marketplace_api()
    wanted_type = (_IMAGE_TYPE_LOCAL
                   if volume_type == _VOLUME_TYPE_LOCAL else _IMAGE_TYPE_SBS)
    try:
        local_images = api.list_local_images_all(zone=zone,
                                                 arch='x86_64',
                                                 image_label=image_label)
    except Exception as e:  # pylint: disable=broad-except
        raise ValueError(
            f'Failed to resolve Scaleway image label {image_label!r} in zone '
            f'{zone}: {e}') from e

    for image in local_images:
        if str(image.type_) == wanted_type:
            return image.id
    available = sorted({str(i.type_) for i in local_images})
    raise ValueError(
        f'Scaleway image label {image_label!r} has no {wanted_type} variant '
        f'in zone {zone}, which is what a {volume_type} root volume needs. '
        f'Available variants: {available}.')


def _looks_like_uuid(value: str) -> bool:
    parts = value.split('-')
    return (len(parts) == 5 and len(value) == 36 and
            all(c in '0123456789abcdef-' for c in value.lower()))


def find_security_group(zone: str, cluster_name_on_cloud: str) -> Optional[str]:
    """Returns the id of the cluster's security group, if it exists."""
    api = scaleway_adaptor.instance_api()
    wanted = cluster_tag(cluster_name_on_cloud)
    try:
        groups = api.list_security_groups_all(zone=zone, tags=[wanted])
    except Exception as e:  # pylint: disable=broad-except
        if _is_not_found(e):
            return None
        raise
    for group in groups:
        if wanted in (group.tags or []):
            return group.id
    return None


def create_security_group(zone: str, cluster_name_on_cloud: str,
                          ports: List[int]) -> str:
    """Creates a security group allowing SSH plus ``ports``.

    Scaleway's project-default security group accepts all inbound traffic, so
    creating an explicit one is what keeps a SkyPilot cluster from being
    reachable on every port.
    """
    api = scaleway_adaptor.instance_api()
    # pylint: disable=import-outside-toplevel
    from scaleway.instance.v1 import SecurityGroupPolicy
    from scaleway.instance.v1 import SecurityGroupRuleAction
    from scaleway.instance.v1 import SecurityGroupRuleDirection
    from scaleway.instance.v1 import SecurityGroupRuleProtocol

    existing = find_security_group(zone, cluster_name_on_cloud)
    if existing is not None:
        return existing

    group = api.create_security_group(
        zone=zone,
        name=f'skypilot-{cluster_name_on_cloud}',
        description=f'SkyPilot cluster {cluster_name_on_cloud}',
        project=scaleway_adaptor.project_id(),
        stateful=True,
        inbound_default_policy=SecurityGroupPolicy.DROP,
        outbound_default_policy=SecurityGroupPolicy.ACCEPT,
        tags=[TAG_SKYPILOT, cluster_tag(cluster_name_on_cloud)],
    ).security_group

    wanted_ports = [22] + [p for p in ports if p != 22]
    for position, port in enumerate(wanted_ports, start=1):
        api.create_security_group_rule(
            zone=zone,
            security_group_id=group.id,
            protocol=SecurityGroupRuleProtocol.TCP,
            direction=SecurityGroupRuleDirection.INBOUND,
            action=SecurityGroupRuleAction.ACCEPT,
            ip_range='0.0.0.0/0',
            dest_port_from=port,
            dest_port_to=port,
            position=position,
            editable=True,
        )
    logger.debug(f'Created Scaleway security group {group.id} in {zone} '
                 f'for ports {wanted_ports}.')
    return group.id


def delete_security_group(zone: str, cluster_name_on_cloud: str) -> None:
    group_id = find_security_group(zone, cluster_name_on_cloud)
    if group_id is None:
        return
    api = scaleway_adaptor.instance_api()
    try:
        api.delete_security_group(zone=zone, security_group_id=group_id)
    except Exception as e:  # pylint: disable=broad-except
        if _is_not_found(e):
            return
        # A group still attached to a server that is mid-deletion cannot be
        # removed yet; leaving it is harmless and it will be reused on the
        # next launch of the same cluster name.
        logger.debug(f'Could not delete security group {group_id}: {e}')


def create_flexible_ip(zone: str, cluster_name_on_cloud: str) -> Any:
    """Creates a routed IPv4 address for a cluster node.

    A flexible IP survives a power cycle, which is what lets `sky stop` and
    `sky start` keep the same address. A dynamic IP would be released on
    poweroff.
    """
    api = scaleway_adaptor.instance_api()
    # pylint: disable=import-outside-toplevel
    from scaleway.instance.v1 import IpType
    return api.create_ip(
        zone=zone,
        project=scaleway_adaptor.project_id(),
        type_=IpType.ROUTED_IPV4,
        tags=[TAG_SKYPILOT, cluster_tag(cluster_name_on_cloud)],
    ).ip


def delete_ip(zone: str, ip_id: str) -> None:
    api = scaleway_adaptor.instance_api()
    try:
        api.delete_ip(zone=zone, ip=ip_id)
    except Exception as e:  # pylint: disable=broad-except
        if not _is_not_found(e):
            raise


def release_cluster_ips(zone: str, cluster_name_on_cloud: str) -> None:
    """Releases every flexible IP SkyPilot created for a cluster."""
    api = scaleway_adaptor.instance_api()
    wanted = cluster_tag(cluster_name_on_cloud)
    try:
        ips = api.list_ips_all(zone=zone, tags=[wanted])
    except Exception as e:  # pylint: disable=broad-except
        if _is_not_found(e):
            return
        raise
    for ip in ips:
        if wanted not in (ip.tags or []):
            continue
        delete_ip(zone, ip.id)


def create_server(
    zone: str,
    name: str,
    commercial_type: str,
    image_label: str,
    disk_size_gb: int,
    tags: List[str],
    security_group_id: Optional[str],
    cluster_name_on_cloud: str,
) -> Any:
    """Creates and powers on one server. Returns the created Server."""
    api = scaleway_adaptor.instance_api()
    # pylint: disable=import-outside-toplevel
    from scaleway.instance.v1 import VolumeServerTemplate
    from scaleway.instance.v1 import VolumeVolumeType

    volume_type, min_size, max_size = root_volume_spec(zone, commercial_type)
    image_id = resolve_image_id(zone, image_label, volume_type)

    size_bytes = disk_size_gb * _BYTES_PER_GB
    if min_size and size_bytes < min_size:
        size_bytes = min_size
    if max_size and size_bytes > max_size:
        # A local-SSD type cannot exceed its reported size; asking for more is
        # a create-time rejection, so clamp and say so.
        logger.info(
            f'Scaleway {commercial_type} allows at most '
            f'{max_size // _BYTES_PER_GB} GB of local SSD; using that instead '
            f'of the requested {disk_size_gb} GB.')
        size_bytes = max_size
    root_volume = VolumeServerTemplate(
        volume_type=(VolumeVolumeType.SBS_VOLUME if volume_type
                     == _VOLUME_TYPE_SBS else VolumeVolumeType.L_SSD),
        size=size_bytes,
    )

    ip = create_flexible_ip(zone, cluster_name_on_cloud)
    try:
        # The SDK exposes server creation only as a private method; the public
        # surface is the `scw` CLI. The Go/Python SDKs both call this.
        server = api._create_server(  # pylint: disable=protected-access
            zone=zone,
            name=name,
            commercial_type=commercial_type,
            image=image_id,
            volumes={
                '0': root_volume
            },
            public_ips=[ip.id],
            protected=False,
            project=scaleway_adaptor.project_id(),
            tags=tags,
            security_group=security_group_id,
        ).server
    except Exception:
        # Do not leak a billed address if the server never came up.
        delete_ip(zone, ip.id)
        raise

    try:
        power_on(zone, server.id)
    except Exception:
        terminate_server(zone, server.id)
        delete_ip(zone, ip.id)
        raise
    return server


def power_on(zone: str, server_id: str) -> None:
    _server_action(zone, server_id, 'poweron')


def power_off(zone: str, server_id: str) -> None:
    _server_action(zone, server_id, 'poweroff')


def terminate_server(zone: str, server_id: str) -> None:
    """Terminates a server, deleting it and its attached volumes."""
    _server_action(zone, server_id, 'terminate')


def _server_action(zone: str, server_id: str, action: str) -> None:
    api = scaleway_adaptor.instance_api()
    # pylint: disable=import-outside-toplevel
    from scaleway.instance.v1 import ServerAction
    try:
        api.server_action(zone=zone,
                          server_id=server_id,
                          action=ServerAction(action))
    except Exception as e:  # pylint: disable=broad-except
        if _is_not_found(e):
            return
        raise


def delete_volumes(zone: str, volume_ids: List[str]) -> None:
    api = scaleway_adaptor.instance_api()
    for volume_id in volume_ids:
        try:
            api.delete_volume(zone=zone, volume_id=volume_id)
        except Exception as e:  # pylint: disable=broad-except
            if _is_not_found(e):
                continue
            # A volume still detaching is not a teardown failure; the terminate
            # action removes the ones it owns.
            logger.debug(f'Could not delete volume {volume_id}: {e}')


def external_ip(server: Any) -> Optional[str]:
    """Returns a server's public IPv4 address, if it has one."""
    public_ip = getattr(server, 'public_ip', None)
    if public_ip is not None and getattr(public_ip, 'address', None):
        return str(public_ip.address)
    for ip in getattr(server, 'public_ips', None) or []:
        address = getattr(ip, 'address', None)
        if address:
            return str(address)
    return None


def internal_ip(server: Any) -> Optional[str]:
    private_ip = getattr(server, 'private_ip', None)
    return str(private_ip) if private_ip else None


def wait_for_state(
    zone: str,
    server_id: str,
    target_states: Tuple[str, ...],
    timeout_seconds: int,
) -> Optional[Any]:
    """Polls a server until it reaches one of ``target_states``.

    Returns the server, or None if it disappeared. Raises TimeoutError if the
    deadline passes, so that a stuck provision fails loudly rather than being
    reported as a cluster that is merely slow.
    """
    deadline = time.time() + timeout_seconds
    last_state = None
    while time.time() < deadline:
        server = get_server(zone, server_id)
        if server is None:
            return None
        last_state = str(server.state)
        if last_state in target_states:
            return server
        time.sleep(_POLL_INTERVAL_SECONDS)
    wanted = '/'.join(target_states)
    raise TimeoutError(
        f'Scaleway server {server_id} in {zone} did not reach {wanted} within '
        f'{timeout_seconds}s; last observed state was {last_state}.')


def wait_until_gone(zone: str, server_ids: List[str],
                    timeout_seconds: int) -> List[str]:
    """Waits for servers to disappear. Returns the ids still present.

    Block volumes outlive the terminate action and cannot be deleted while
    they are still attached, so teardown has to wait for the server to go
    before reclaiming them; otherwise the volume is silently left behind and
    billed.
    """
    remaining = list(server_ids)
    deadline = time.time() + timeout_seconds
    while remaining and time.time() < deadline:
        remaining = [
            server_id for server_id in remaining
            if get_server(zone, server_id) is not None
        ]
        if not remaining:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    return remaining


def server_volume_ids(server: Any) -> List[str]:
    volumes: Dict[str, Any] = getattr(server, 'volumes', None) or {}
    ids = []
    for volume in volumes.values():
        volume_id = getattr(volume, 'id', None)
        if volume_id:
            ids.append(volume_id)
    return ids
