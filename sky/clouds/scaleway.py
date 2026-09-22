"""Scaleway Instances."""

import typing
from typing import Dict, Iterator, List, Optional, Tuple, Union

from sky import catalog
from sky import clouds
from sky.adaptors import scaleway as scaleway_adaptor
from sky.utils import registry
from sky.utils import resources_utils

if typing.TYPE_CHECKING:
    from sky import resources as resources_lib
    from sky.utils import volume as volume_lib

# Scaleway marketplace image *labels*, not image ids. A label resolves to a
# different image id in every zone, and Scaleway republishes the underlying
# image regularly, so resolving the label at provision time is what keeps this
# working; a pinned id goes stale silently.
SCALEWAY_DEFAULT_GPU_IMAGE = 'ubuntu_noble_gpu_os_12'
SCALEWAY_DEFAULT_CPU_IMAGE = 'ubuntu_noble'


@registry.CLOUD_REGISTRY.register
class Scaleway(clouds.Cloud):
    """Scaleway Instances.

    _REPR | The string representation for the Scaleway cloud object.
    """

    _REPR = 'Scaleway'

    _CLOUD_UNSUPPORTED_FEATURES = {
        clouds.CloudImplementationFeatures.SPOT_INSTANCE:
            (f'Spot instances are not offered on {_REPR} Instances.'),
        clouds.CloudImplementationFeatures.MULTI_NODE:
            ('Multi-node clusters are not supported yet on '
             f'{_REPR}, as it requires setting up a Private Network for the '
             'nodes to reach each other over internal IPs.'),
        clouds.CloudImplementationFeatures.CLONE_DISK_FROM_CLUSTER:
            (f'Migrating disk is not supported on {_REPR}.'),
        clouds.CloudImplementationFeatures.DOCKER_IMAGE:
            (f'Docker images are not supported yet on {_REPR}. '
             'Use the `image_id` field to pick a Scaleway image label '
             'instead.'),
        clouds.CloudImplementationFeatures.CUSTOM_DISK_TIER:
            (f'Customizing disk tier is not supported yet on {_REPR}.'),
        clouds.CloudImplementationFeatures.CUSTOM_NETWORK_TIER:
            (f'Customizing network tier is not supported yet on {_REPR}.'),
        clouds.CloudImplementationFeatures.STORAGE_MOUNTING:
            (f'Mounting object stores is not supported on {_REPR}. To read '
             f'data from object stores on {_REPR}, use `mode: COPY` to copy '
             'the data to local disk.'),
        clouds.CloudImplementationFeatures.HOST_CONTROLLERS:
            (f'Host controllers are not supported yet on {_REPR}.'),
        clouds.CloudImplementationFeatures.HIGH_AVAILABILITY_CONTROLLERS:
            (f'High availability controllers are not supported on {_REPR}.'),
        clouds.CloudImplementationFeatures.CUSTOM_MULTI_NETWORK:
            ('Customized multiple network interfaces are not supported on '
             f'{_REPR}.'),
        clouds.CloudImplementationFeatures.LOCAL_DISK:
            (f'Local disk is not supported on {_REPR}.'),
    }

    # Scaleway server names must be at most 64 characters. Leave room for the
    # '-head'/'-worker' suffix SkyPilot appends.
    _MAX_CLUSTER_NAME_LEN_LIMIT = 50

    PROVISIONER_VERSION = clouds.ProvisionerVersion.SKYPILOT
    STATUS_VERSION = clouds.StatusVersion.SKYPILOT
    # Scaleway security group rules are attached to the group at creation and
    # SkyPilot only needs the launch-time ports.
    OPEN_PORTS_VERSION = clouds.OpenPortsVersion.LAUNCH_ONLY

    @classmethod
    def _unsupported_features_for_resources(
        cls,
        resources: 'resources_lib.Resources',
        region: Optional[str] = None,
    ) -> Dict[clouds.CloudImplementationFeatures, str]:
        del resources, region  # unused
        return cls._CLOUD_UNSUPPORTED_FEATURES

    @classmethod
    def _max_cluster_name_length(cls) -> Optional[int]:
        return cls._MAX_CLUSTER_NAME_LEN_LIMIT

    @classmethod
    def regions_with_offering(
        cls,
        instance_type: str,
        accelerators: Optional[Dict[str, int]],
        use_spot: bool,
        region: Optional[str],
        zone: Optional[str],
        resources: Optional['resources_lib.Resources'] = None,
    ) -> List[clouds.Region]:
        del accelerators, resources  # unused
        if use_spot:
            return []
        regions = catalog.get_region_zones_for_instance_type(
            instance_type, use_spot, 'scaleway')
        if region is not None:
            regions = [r for r in regions if r.name == region]
        if zone is not None:
            filtered = []
            for r in regions:
                assert r.zones is not None, r
                zones = [z for z in r.zones if z.name == zone]
                if zones:
                    r.set_zones(zones)
                    filtered.append(r)
            regions = filtered
        return regions

    @classmethod
    def zones_provision_loop(
        cls,
        *,
        region: str,
        num_nodes: int,
        instance_type: str,
        accelerators: Optional[Dict[str, int]] = None,
        use_spot: bool = False,
    ) -> Iterator[Optional[List[clouds.Zone]]]:
        del num_nodes  # unused
        regions = cls.regions_with_offering(instance_type,
                                            accelerators,
                                            use_spot,
                                            region=region,
                                            zone=None)
        for r in regions:
            assert r.zones is not None, r
            for zone in r.zones:
                # Every Scaleway Instances API call is scoped to a single
                # zone, so retry one zone at a time.
                yield [zone]

    @classmethod
    def get_vcpus_mem_from_instance_type(
        cls,
        instance_type: str,
    ) -> Tuple[Optional[float], Optional[float]]:
        return catalog.get_vcpus_mem_from_instance_type(instance_type,
                                                        clouds='scaleway')

    def instance_type_to_hourly_cost(
        self,
        instance_type: str,
        use_spot: bool,
        region: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> float:
        return catalog.get_hourly_cost(
            instance_type,
            use_spot=use_spot,
            region=region,
            zone=zone,
            clouds='scaleway',
        )

    def accelerators_to_hourly_cost(
        self,
        accelerators: Dict[str, int],
        use_spot: bool,
        region: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> float:
        del accelerators, use_spot, region, zone  # unused
        # Scaleway bundles the GPU into the instance price.
        return 0.0

    def get_egress_cost(self, num_gigabytes: float) -> float:
        del num_gigabytes  # unused
        # Scaleway Instances include a monthly egress allowance and do not
        # meter per-GB egress the way the hyperscalers do.
        return 0.0

    @classmethod
    def get_default_instance_type(
        cls,
        cpus: Optional[str] = None,
        memory: Optional[str] = None,
        disk_tier: Optional[resources_utils.DiskTier] = None,
        local_disk: Optional[str] = None,
        region: Optional[str] = None,
        zone: Optional[str] = None,
        use_spot: bool = False,
        max_hourly_cost: Optional[float] = None,
    ) -> Optional[str]:
        return catalog.get_default_instance_type(
            cpus=cpus,
            memory=memory,
            disk_tier=disk_tier,
            local_disk=local_disk,
            region=region,
            zone=zone,
            use_spot=use_spot,
            max_hourly_cost=max_hourly_cost,
            clouds='scaleway',
        )

    @classmethod
    def get_accelerators_from_instance_type(
            cls, instance_type: str) -> Optional[Dict[str, Union[int, float]]]:
        return catalog.get_accelerators_from_instance_type(instance_type,
                                                           clouds='scaleway')

    @classmethod
    def get_zone_shell_cmd(cls) -> Optional[str]:
        return None

    def make_deploy_resources_variables(
        self,
        resources: 'resources_lib.Resources',
        cluster_name: resources_utils.ClusterName,
        region: 'clouds.Region',
        zones: Optional[List['clouds.Zone']],
        num_nodes: int,
        dryrun: bool = False,
        volume_mounts: Optional[List['volume_lib.VolumeMount']] = None,
    ) -> Dict[str, Optional[Union[str, bool]]]:
        del cluster_name, dryrun, volume_mounts  # unused

        if num_nodes > 1:
            raise ValueError(
                f'Scaleway currently supports one node per cluster, but '
                f'{num_nodes} nodes were requested.')
        assert zones is not None, 'Scaleway requires a zone to provision in.'
        zone = zones[0].name

        resources = resources.assert_launchable()
        acc_dict = self.get_accelerators_from_instance_type(
            resources.instance_type)
        custom_resources = resources_utils.make_ray_custom_resources_str(
            acc_dict)

        image_id = self._get_image_label(resources, acc_dict)

        return {
            'instance_type': resources.instance_type,
            'custom_resources': custom_resources,
            'region': region.name,
            'zone': zone,
            'image_id': image_id,
        }

    @classmethod
    def _get_image_label(
            cls, resources: 'resources_lib.Resources',
            acc_dict: Optional[Dict[str, Union[int, float]]]) -> str:
        """Returns the Scaleway image label to boot from."""
        default_image = (SCALEWAY_DEFAULT_GPU_IMAGE
                         if acc_dict else SCALEWAY_DEFAULT_CPU_IMAGE)
        if resources.image_id is None:
            return default_image
        if isinstance(resources.image_id, dict):
            if None in resources.image_id:
                return resources.image_id[None]
            if resources.region in resources.image_id:
                return resources.image_id[resources.region]
            return default_image
        return resources.image_id

    def _get_feasible_launchable_resources(
        self, resources: 'resources_lib.Resources'
    ) -> 'resources_utils.FeasibleResources':
        if resources.use_spot:
            # Scaleway Instances have no spot tier; fail fast rather than
            # quietly handing back on-demand instances at on-demand prices.
            return resources_utils.FeasibleResources([], [], None)

        if resources.instance_type is not None:
            assert resources.is_launchable(), resources
            resources = resources.copy(accelerators=None)
            return resources_utils.FeasibleResources([resources], [], None)

        def _make(instance_list):
            resource_list = []
            for instance_type in instance_list:
                r = resources.copy(
                    cloud=Scaleway(),
                    instance_type=instance_type,
                    accelerators=None,
                    cpus=None,
                )
                resource_list.append(r)
            return resource_list

        accelerators = resources.accelerators
        if accelerators is None:
            default_instance_type = Scaleway.get_default_instance_type(
                cpus=resources.cpus,
                memory=resources.memory,
                disk_tier=resources.disk_tier,
                region=resources.region,
                zone=resources.zone,
                use_spot=resources.use_spot,
                max_hourly_cost=resources.max_hourly_cost,
            )
            if default_instance_type is None:
                return resources_utils.FeasibleResources([], [], None)
            return resources_utils.FeasibleResources(
                _make([default_instance_type]), [], None)

        assert len(accelerators) == 1, resources
        acc, acc_count = list(accelerators.items())[0]
        (instance_list,
         fuzzy_candidate_list) = catalog.get_instance_type_for_accelerator(
             acc,
             acc_count,
             use_spot=resources.use_spot,
             cpus=resources.cpus,
             memory=resources.memory,
             region=resources.region,
             zone=resources.zone,
             max_hourly_cost=resources.max_hourly_cost,
             clouds='scaleway',
         )
        if instance_list is None:
            return resources_utils.FeasibleResources([], fuzzy_candidate_list,
                                                     None)
        return resources_utils.FeasibleResources(_make(instance_list),
                                                 fuzzy_candidate_list, None)

    @classmethod
    def check_credentials(
        cls, cloud_capability: clouds.CloudCapability
    ) -> Tuple[bool, Optional[Union[str, Dict[str, str]]]]:
        del cloud_capability  # unused
        return scaleway_adaptor.check_credentials()

    @property
    def name(self) -> str:
        return 'scaleway'

    def get_credential_file_mounts(self) -> Dict[str, str]:
        return scaleway_adaptor.get_credential_file_mounts()

    @classmethod
    def get_user_identities(cls) -> Optional[List[List[str]]]:
        return None

    def instance_type_exists(self, instance_type: str) -> bool:
        return catalog.instance_type_exists(instance_type, 'scaleway')

    def validate_region_zone(self, region: Optional[str], zone: Optional[str]):
        return catalog.validate_region_zone(region, zone, clouds='scaleway')

    @classmethod
    def get_image_size(cls, image_id: str, region: Optional[str]) -> float:
        del image_id, region  # unused
        # Scaleway sizes the root volume from the image, and SkyPilot only
        # uses this to reject a disk smaller than the image.
        return 0.0
