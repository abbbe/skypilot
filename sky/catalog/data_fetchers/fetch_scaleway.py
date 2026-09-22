"""A script that generates the Scaleway catalog.

Usage:
    python fetch_scaleway.py [-o <output-dir>]

Credentials are resolved exactly as the `scw` CLI resolves them: from
SCW_ACCESS_KEY / SCW_SECRET_KEY, or from the profile named by `active_profile`
in ~/.config/scw/config.yaml.
"""

import argparse
import csv
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import scaleway
from scaleway.instance.v1 import InstanceV1API

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Scaleway reports the GPU model in `gpu_info.gpu_name`. This maps every model
# Scaleway currently offers onto SkyPilot's canonical accelerator name; the
# identity entries are listed explicitly so that a model Scaleway adds later
# shows up as a warning rather than being passed through silently.
_GPU_NAME_TO_ACCELERATOR_NAME = {
    # Spellings that differ from SkyPilot's.
    'H100-PCIe': 'H100',
    'B300-SXM': 'B300',
    # Spellings that already match. SkyPilot keeps '-SXM' as a distinct
    # accelerator (see fetch_fluidstack.py), so H100-SXM is not folded into
    # H100.
    'H100-SXM': 'H100-SXM',
    'L4': 'L4',
    'L40S': 'L40S',
    'P100': 'P100',
}

# SkyPilot images are x86_64 only; Scaleway also sells ARM instances.
_SUPPORTED_ARCH = 'x86_64'

_PER_PAGE = 100

_COLUMNS = [
    'InstanceType',
    'AcceleratorName',
    'AcceleratorCount',
    'vCPUs',
    'MemoryGiB',
    'GpuInfo',
    'Region',
    'AvailabilityZone',
    'Price',
    'SpotPrice',
]


def _normalize_accelerator_name(gpu_name: str) -> str:
    """Maps a Scaleway GPU model onto SkyPilot's accelerator name."""
    mapped = _GPU_NAME_TO_ACCELERATOR_NAME.get(gpu_name)
    if mapped is not None:
        return mapped
    # Scaleway spells PCIe parts '<model>-PCIe' and SXM parts '<model>-SXM'.
    # SkyPilot keeps the '-SXM' suffix (see fetch_fluidstack.py) and drops the
    # PCIe one, which is the default form.
    if gpu_name.endswith('-PCIe'):
        return gpu_name[:-len('-PCIe')]
    logger.warning(
        'Unrecognized Scaleway GPU model %r; using it verbatim as the '
        'accelerator name. Add it to _GPU_NAME_TO_ACCELERATOR_NAME if '
        'SkyPilot spells it differently.', gpu_name)
    return gpu_name


def _build_gpu_info(accelerator_name: str, count: int,
                    memory_bytes_per_gpu: int) -> str:
    """Builds the GpuInfo JSON blob, matching the other fetchers' schema."""
    memory_mib_per_gpu = int(memory_bytes_per_gpu / (1024**2))
    return json.dumps({
        'Gpus': [{
            'Name': accelerator_name,
            'Manufacturer': 'NVIDIA',
            'Count': count,
            'MemoryInfo': {
                'SizeInMiB': memory_mib_per_gpu
            },
        }],
        'TotalGpuMemoryInMiB': memory_mib_per_gpu * count,
    })


def _list_all_server_types(api: InstanceV1API, zone: str) -> Dict[str, Any]:
    """Returns every server type in a zone, following pagination.

    Scaleway caps `per_page` at 100 and several zones offer more types than
    that, so a single un-paginated call silently truncates the catalog.
    """
    servers: Dict[str, Any] = {}
    page = 1
    while True:
        response = api.list_servers_types(zone=zone,
                                          per_page=_PER_PAGE,
                                          page=page)
        batch = response.servers
        if not batch:
            break
        servers.update(batch)
        if len(batch) < _PER_PAGE:
            break
        page += 1
    return servers


def _row_for_server_type(zone: str, name: str,
                         server_type: Any) -> Optional[List[Any]]:
    """Builds one catalog row, or None if the type should be skipped."""
    if str(server_type.arch) != _SUPPORTED_ARCH:
        return None
    if server_type.end_of_service:
        return None

    accelerator_name: Optional[str] = None
    accelerator_count: Optional[int] = None
    gpu_info = ''
    gpu_count = server_type.gpu or 0
    if gpu_count > 0:
        info = server_type.gpu_info
        if info is None or not info.gpu_name:
            # A GPU instance whose model we cannot name would be offered to
            # users as an un-requestable accelerator. Skip it loudly instead.
            logger.warning(
                'Skipping %s in %s: it reports %d GPU(s) but no gpu_info.',
                name, zone, gpu_count)
            return None
        accelerator_name = _normalize_accelerator_name(info.gpu_name)
        accelerator_count = gpu_count
        gpu_info = _build_gpu_info(accelerator_name, gpu_count, info.gpu_memory)

    return [
        name,
        accelerator_name if accelerator_name is not None else '',
        accelerator_count if accelerator_count is not None else '',
        server_type.ncpus,
        round(server_type.ram / (1024**3), 2),
        gpu_info,
        zone.rsplit('-', 1)[0],
        zone,
        server_type.hourly_price,
        # Scaleway Instances have no spot/preemptible tier.
        '',
    ]


def _make_client() -> scaleway.Client:
    """Builds a client the way the `scw` CLI resolves credentials."""
    profile_name = os.environ.get('SCW_PROFILE')
    if profile_name is None:
        config_path = os.path.expanduser(
            os.environ.get('SCW_CONFIG_PATH', '~/.config/scw/config.yaml'))
        if os.path.isfile(config_path):
            import yaml  # pylint: disable=import-outside-toplevel
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            profile_name = config.get('active_profile') or 'default'
    if profile_name is None:
        return scaleway.Client.from_env()
    return scaleway.Client.from_config_file_and_env(profile_name=profile_name)


def create_catalog(output_path: str) -> Tuple[int, int]:
    """Writes the Scaleway catalog CSV. Returns (rows, gpu_rows)."""
    api = InstanceV1API(_make_client())

    rows: List[List[Any]] = []
    gpu_rows = 0
    for zone in scaleway.ALL_ZONES:
        try:
            server_types = _list_all_server_types(api, zone)
        except Exception as e:  # pylint: disable=broad-except
            # A zone the account cannot see must not be mistaken for a zone
            # with nothing on offer, so make the difference visible.
            logger.error('Failed to list server types in %s: %s', zone, e)
            raise
        zone_rows = 0
        for name, server_type in sorted(server_types.items()):
            row = _row_for_server_type(zone, name, server_type)
            if row is None:
                continue
            rows.append(row)
            zone_rows += 1
            if row[1]:
                gpu_rows += 1
        logger.info('%s: %d instance types kept (of %d offered)', zone,
                    zone_rows, len(server_types))

    if not rows:
        raise RuntimeError(
            'Scaleway returned no usable instance types. Refusing to write an '
            'empty catalog, which would look like a cloud with no capacity.')

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(_COLUMNS)
        writer.writerows(rows)
    return len(rows), gpu_rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-o',
                        '--output-dir',
                        default='scaleway',
                        help='Directory to write vms.csv into.')
    args = parser.parse_args()
    out = os.path.join(args.output_dir, 'vms.csv')
    total, gpus = create_catalog(out)
    logger.info('Wrote %s: %d rows, %d with accelerators.', out, total, gpus)
    sys.exit(0)
