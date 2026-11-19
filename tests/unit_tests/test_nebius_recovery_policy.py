"""nebius.recovery_policy and nebius.post_provision_runcmd.

Both are opt-in: with neither set, the launch request carries no recovery
policy for an on-demand instance and the rendered cluster YAML has no trace
of either key -- which is what keeps existing clusters' config unchanged.
"""
import importlib.util
import os
from unittest import mock

import jinja2
import jsonschema
import pytest
import yaml

import sky
from sky import resources as resources_lib
from sky import skypilot_config
from sky.adaptors import nebius as nebius_adaptor
from sky.clouds import nebius as nebius_cloud
from sky.provision import common as provision_common
from sky.provision.nebius import instance as nebius_instance
from sky.provision.nebius import utils as nebius_utils
from sky.utils import common_utils
from sky.utils import resources_utils
from sky.utils import schemas

_HAS_NEBIUS_SDK = importlib.util.find_spec('nebius') is not None
nebius_sdk_required = pytest.mark.skipif(not _HAS_NEBIUS_SDK,
                                         reason='Nebius SDK is not installed')


# Through SkyPilot's own validator, as a config file is: plain jsonschema
# does not know `case_insensitive_enum` and would accept any string.
@pytest.mark.parametrize('value', ['fail', 'recover', 'FAIL'])
def test_recovery_policy_config_accepts(value):
    common_utils.validate_schema({'nebius': {
        'recovery_policy': value
    }}, schemas.get_config_schema(), 'Invalid config: ')


@pytest.mark.parametrize('value', ['stop', '', True, 1])
def test_recovery_policy_config_rejects(value):
    with pytest.raises(ValueError):
        common_utils.validate_schema({'nebius': {
            'recovery_policy': value
        }}, schemas.get_config_schema(), 'Invalid config: ')


def test_post_provision_runcmd_config():
    jsonschema.validate(
        instance={'nebius': {
            'post_provision_runcmd': ['echo hi', 'true']
        }},
        schema=schemas.get_config_schema())
    for bad in ('echo hi', [['echo', 'hi']], [1]):
        with pytest.raises(jsonschema.exceptions.ValidationError):
            jsonschema.validate(
                instance={'nebius': {
                    'post_provision_runcmd': bad
                }},
                schema=schemas.get_config_schema())


def _launch_spec(**kwargs):
    """Build a Nebius launch request and return its instance spec."""
    compute = nebius_adaptor.compute()
    service = mock.MagicMock()
    instance = mock.MagicMock()
    instance.metadata.id = 'instance-id'
    instance.status.state.name = 'STARTING'

    with (
            mock.patch.object(nebius_utils,
                              'get_project_by_region',
                              return_value='project-id'),
            mock.patch.object(nebius_utils,
                              'get_subnet_id',
                              return_value='subnet-id'),
            mock.patch.object(nebius_adaptor, 'sdk'),
            mock.patch.object(compute,
                              'InstanceServiceClient',
                              return_value=service),
            mock.patch.object(nebius_adaptor,
                              'sync_call',
                              side_effect=[None, instance]),
    ):
        nebius_utils.launch(cluster_name_on_cloud='cluster',
                            node_type='head',
                            platform='gpu-l40s',
                            preset='1gpu-16vcpu-64gb',
                            region='us-central1',
                            image_id_or_family='computeimage-test',
                            disk_size=93,
                            user_data='',
                            associate_public_ip_address=False,
                            filesystems=[],
                            disk_tier=resources_utils.DiskTier.MEDIUM,
                            **kwargs)

    return service.create.call_args.args[0].spec


@nebius_sdk_required
def test_on_demand_sends_no_recovery_policy_by_default():
    """Unset is Nebius's own default (RECOVER, the enum's zero value): the
    request is what it was before the key existed."""
    compute = nebius_adaptor.compute()
    spec = _launch_spec()
    assert spec.recovery_policy == compute.InstanceRecoveryPolicy.RECOVER
    assert _launch_spec(recovery_policy=None).recovery_policy == (
        compute.InstanceRecoveryPolicy.RECOVER)


@nebius_sdk_required
@pytest.mark.parametrize('value', ['fail', 'FAIL'])
def test_on_demand_fail_when_asked(value):
    compute = nebius_adaptor.compute()
    spec = _launch_spec(recovery_policy=value)
    assert spec.recovery_policy == compute.InstanceRecoveryPolicy.FAIL
    # Not a preemptible instance for it.
    assert not spec.preemptible.on_preemption


@nebius_sdk_required
def test_on_demand_recover_when_asked():
    compute = nebius_adaptor.compute()
    spec = _launch_spec(recovery_policy='recover')
    assert spec.recovery_policy == compute.InstanceRecoveryPolicy.RECOVER


@nebius_sdk_required
@pytest.mark.parametrize('value', [None, 'recover', 'fail'])
def test_spot_is_always_fail(value):
    compute = nebius_adaptor.compute()
    spec = _launch_spec(use_spot=True, recovery_policy=value)
    assert spec.recovery_policy == compute.InstanceRecoveryPolicy.FAIL
    assert spec.preemptible.on_preemption == (
        compute.PreemptibleSpec.PreemptionPolicy.STOP)


@nebius_sdk_required
def test_unknown_recovery_policy_is_refused():
    with pytest.raises(ValueError, match='recovery_policy'):
        _launch_spec(recovery_policy='stop')


def _render(**overrides):
    """Render nebius-ray.yml.j2 the way write_cluster_config does, with just
    enough variables, and return (raw text, parsed YAML)."""
    path = os.path.join(os.path.dirname(sky.__file__), 'templates',
                        'nebius-ray.yml.j2')
    with open(path, 'r', encoding='utf-8') as f:
        template = jinja2.Template(f.read())
    variables = {
        'cluster_name_on_cloud': 'c-abcd',
        'num_nodes': 1,
        'region': 'eu-north1',
        'project_id': 'project-x',
        'use_internal_ips': False,
        'use_static_ip_address': False,
        'security_group': 'sg',
        'security_group_managed_by_skypilot': 'true',
        'docker_image': None,
        'ssh_private_key': '~/.ssh/sky-key',
        'ssh_proxy_command': None,
        'instance_type': 'gpu-l40s_1gpu-16vcpu-64gb',
        'image_id': 'ubuntu24.04-cuda12',
        'disk_size': 100,
        'use_spot': False,
        'network_tier': None,
        'disk_tier': 'network_ssd',
        'disk_encrypted': False,
        'filesystems': [],
        'recovery_policy': None,
        'runcmd': None,
        'initial_setup_commands': [],
        'conda_installation_commands': 'true;',
        'uv_installation_commands': 'true;',
        'ray_skypilot_installation_commands': 'true;',
        'copy_skypilot_templates_commands': 'true;',
        'credentials': {},
        'labels': {},
        'sky_ray_yaml_remote_path': '~/.sky/sky_ray.yml',
        'sky_ray_yaml_local_path': '/tmp/sky_ray.yml',
        'sky_local_path': '/tmp/wheel',
        'sky_remote_path': '~/.sky/wheels',
        'dump_port_command': 'true',
        'sky_version': '0',
        'sky_wheel_hash': '0',
        'high_availability': False,
    }
    variables.update(overrides)
    text = template.render(**variables)
    return text, yaml.safe_load(text)


def _node_config(parsed):
    return parsed['available_node_types']['ray_head_default']['node_config']


def test_template_unchanged_when_neither_is_set():
    text, parsed = _render()
    assert 'recovery_policy' not in text
    assert 'runcmd' not in text
    user_data = yaml.safe_load(_node_config(parsed)['UserData'])
    assert list(user_data) == ['users']


def test_template_carries_recovery_policy():
    _, parsed = _render(recovery_policy='fail')
    node_config = _node_config(parsed)
    assert node_config['recovery_policy'] == 'fail'
    # The keys around it are still there.
    assert node_config['disk_encrypted'] is False
    assert node_config['filesystems'] is None


def test_template_carries_runcmd_verbatim():
    """A command with quotes, a backslash escape, a `#` and a `: ` must reach
    cloud-init as written: it is a shell command."""
    commands = [
        'systemd-run --on-active=30min --unit=x poweroff',
        'printf \'%s\\n\' "[Unit]" \'a: b # c\' > /etc/x.conf',
    ]
    _, parsed = _render(runcmd=commands)
    user_data = yaml.safe_load(_node_config(parsed)['UserData'])
    assert user_data['runcmd'] == commands
    # The users block before it is intact.
    assert user_data['users'][0]['sudo'] == 'ALL=(ALL) NOPASSWD:ALL'
    assert user_data['users'][0]['ssh_authorized_keys'] == [
        'skypilot:ssh_public_key_content'
    ]


def _deploy_vars():
    """make_deploy_resources_variables for an on-demand Nebius instance, with
    the project lookup (a Nebius IAM call) stubbed."""
    resources = resources_lib.Resources(
        cloud=nebius_cloud.Nebius(),
        accelerators={'H200': 8},
        instance_type='gpu-h200-sxm_8gpu-128vcpu-1600gb').assert_launchable()
    region = mock.MagicMock()
    region.name = 'us-central1'
    with mock.patch.object(nebius_utils,
                           'get_project_by_region',
                           return_value='project-id'):
        return nebius_cloud.Nebius().make_deploy_resources_variables(
            resources=resources,
            cluster_name=resources_utils.ClusterName(
                display_name='test-cluster', name_on_cloud='test-cluster'),
            region=region,
            zones=None,
            num_nodes=1)


def test_cloud_reads_no_recovery_policy_by_default():
    assert _deploy_vars()['recovery_policy'] is None


@pytest.mark.parametrize(('configured', 'expected'), [('fail', 'fail'),
                                                      ('FAIL', 'fail'),
                                                      ('recover', 'recover')])
def test_cloud_reads_recovery_policy_from_config(configured, expected):
    """Through a config override, which is how `sky launch --config` and a
    config file both arrive."""
    with skypilot_config.override_skypilot_config(
        {'nebius': {
            'recovery_policy': configured
        }}):
        assert _deploy_vars()['recovery_policy'] == expected
    assert _deploy_vars()['recovery_policy'] is None


class _Launched(Exception):
    """Stops run_instances once utils.launch has been called."""


@pytest.mark.parametrize('node_config_extra', [{}, {
    'recovery_policy': 'fail'
}])
def test_run_instances_hands_recovery_policy_to_launch(node_config_extra):
    """From the cluster YAML's node_config to utils.launch: absent stays
    None (a cluster YAML written before the key existed), present arrives."""
    node_config = {
        'InstanceType': 'gpu-l40s_1gpu-16vcpu-64gb',
        'ImageId': 'ubuntu24.04-cuda12',
        'DiskSize': 100,
        'UserData': '',
        'use_spot': False,
        'disk_tier': resources_utils.DiskTier.MEDIUM,
    }
    node_config.update(node_config_extra)
    config = provision_common.ProvisionConfig(
        provider_config={
            'project_id': 'project-id',
            'use_internal_ips': False
        },
        authentication_config={},
        docker_config={},
        node_config=node_config,
        count=1,
        tags={},
        resume_stopped_nodes=True,
        ports_to_open_on_launch=None)
    with (mock.patch.object(nebius_instance, '_wait_until_no_pending'),
          mock.patch.object(nebius_instance,
                            '_filter_instances',
                            return_value={}),
          mock.patch.object(nebius_utils, 'launch',
                            side_effect=_Launched) as launch):
        with pytest.raises(_Launched):
            nebius_instance.run_instances('us-central1', 'cluster',
                                          'cluster-on-cloud', config)
    assert launch.call_args.kwargs['recovery_policy'] == (
        node_config_extra.get('recovery_policy'))
    assert launch.call_args.kwargs['use_spot'] is False
