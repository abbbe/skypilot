"""Scaleway configuration bootstrapping."""

from sky import sky_logging
from sky.provision import common
from sky.provision.scaleway import utils

logger = sky_logging.init_logger(__name__)


def bootstrap_instances(
        region: str, cluster_name_on_cloud: str,
        config: common.ProvisionConfig) -> common.ProvisionConfig:
    """Creates the cluster's security group before any instance is launched.

    Scaleway's project-default security group accepts all inbound traffic, so
    a cluster left on it would be reachable on every port. Create a dedicated
    group that only admits SSH plus whatever the task asked to expose.
    """
    del region  # The Instances API is zone-scoped.
    zone = config.provider_config.get('availability_zone')
    if not zone:
        raise ValueError(
            'Scaleway provider config has no availability_zone; cannot create '
            'the cluster security group.')

    ports = [int(p) for p in (config.ports_to_open_on_launch or [])]
    security_group_id = utils.create_security_group(zone, cluster_name_on_cloud,
                                                    ports)
    config.provider_config['security_group_id'] = security_group_id
    return config
