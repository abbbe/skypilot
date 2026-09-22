"""Tests for the Scaleway provider."""

import textwrap
from unittest import mock

import pytest

from sky import clouds
from sky.adaptors import scaleway as scaleway_adaptor
from sky.clouds import scaleway as scaleway_cloud
from sky.provision.scaleway import instance as scaleway_instance
from sky.provision.scaleway import utils
from sky.utils import status_lib


@pytest.fixture(autouse=True)
def _clear_client_cache():
    scaleway_adaptor._reset_client_cache()  # pylint: disable=protected-access
    yield
    scaleway_adaptor._reset_client_cache()  # pylint: disable=protected-access


def _write_config(tmp_path, body: str):
    path = tmp_path / 'config.yaml'
    path.write_text(textwrap.dedent(body), encoding='utf-8')
    return path


class TestProfileResolution:
    """The scw CLI honours `active_profile`; the Python SDK does not."""

    def test_active_profile_is_used(self, tmp_path, monkeypatch):
        path = _write_config(
            tmp_path, '''
            active_profile: newprofile
            profiles:
              newprofile:
                access_key: SCWXXXXXXXXXXXXXXXXX
            ''')
        monkeypatch.delenv('SCW_PROFILE', raising=False)
        monkeypatch.setenv('SCW_CONFIG_PATH', str(path))
        assert scaleway_adaptor.resolve_profile_name() == 'newprofile'

    def test_env_profile_wins(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path, 'active_profile: fromfile\n')
        monkeypatch.setenv('SCW_CONFIG_PATH', str(path))
        monkeypatch.setenv('SCW_PROFILE', 'fromenv')
        assert scaleway_adaptor.resolve_profile_name() == 'fromenv'

    def test_config_without_active_profile_falls_back_to_default(
            self, tmp_path, monkeypatch):
        path = _write_config(tmp_path, 'profiles:\n  default:\n    a: b\n')
        monkeypatch.delenv('SCW_PROFILE', raising=False)
        monkeypatch.setenv('SCW_CONFIG_PATH', str(path))
        assert scaleway_adaptor.resolve_profile_name() == 'default'

    def test_missing_config_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv('SCW_PROFILE', raising=False)
        monkeypatch.setenv('SCW_CONFIG_PATH', str(tmp_path / 'absent.yaml'))
        assert scaleway_adaptor.resolve_profile_name() is None

    def test_malformed_config_does_not_raise(self, tmp_path, monkeypatch):
        path = tmp_path / 'config.yaml'
        path.write_text('this: [is: not: valid', encoding='utf-8')
        monkeypatch.delenv('SCW_PROFILE', raising=False)
        monkeypatch.setenv('SCW_CONFIG_PATH', str(path))
        assert scaleway_adaptor.resolve_profile_name() is None

    def test_missing_credentials_reported_not_raised(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.delenv('SCW_ACCESS_KEY', raising=False)
        monkeypatch.delenv('SCW_SECRET_KEY', raising=False)
        monkeypatch.setenv('SCW_CONFIG_PATH', str(tmp_path / 'absent.yaml'))
        ok, hint = scaleway_adaptor.check_credentials()
        assert not ok
        assert 'does not exist' in hint


def test_zone_to_region():
    assert scaleway_adaptor.zone_to_region('fr-par-2') == 'fr-par'
    assert scaleway_adaptor.zone_to_region('pl-waw-2') == 'pl-waw'
    assert scaleway_adaptor.zone_to_region('nl-ams-1') == 'nl-ams'


def test_public_key_matches_ignores_comment():
    stored = mock.Mock(public_key='ssh-ed25519 AAAAC3Nz someone@host')
    assert scaleway_adaptor.public_key_matches(stored,
                                               'ssh-ed25519 AAAAC3Nz other@x')
    assert not scaleway_adaptor.public_key_matches(
        stored, 'ssh-ed25519 DIFFERENT other@x')
    assert not scaleway_adaptor.public_key_matches(mock.Mock(public_key=None),
                                                   'ssh-ed25519 AAAAC3Nz')


class TestCloud:

    def test_basics(self):
        cloud = scaleway_cloud.Scaleway()
        assert cloud.name == 'scaleway'
        assert repr(cloud) == 'Scaleway'
        assert cloud.PROVISIONER_VERSION == clouds.ProvisionerVersion.SKYPILOT
        assert cloud.STATUS_VERSION == clouds.StatusVersion.SKYPILOT

    def test_spot_is_not_offered(self):
        cloud = scaleway_cloud.Scaleway()
        assert cloud.regions_with_offering('H100-1-80G',
                                           None,
                                           use_spot=True,
                                           region=None,
                                           zone=None) == []

    def test_accelerator_cost_is_bundled(self):
        cloud = scaleway_cloud.Scaleway()
        assert cloud.accelerators_to_hourly_cost({'H100': 1}, False) == 0.0

    def test_image_label_defaults_by_accelerator(self):
        resources = mock.Mock(image_id=None)
        assert scaleway_cloud.Scaleway._get_image_label(
            resources, {'H100': 1}) == scaleway_cloud.SCALEWAY_DEFAULT_GPU_IMAGE
        assert scaleway_cloud.Scaleway._get_image_label(
            resources, None) == scaleway_cloud.SCALEWAY_DEFAULT_CPU_IMAGE

    def test_image_label_honours_user_override(self):
        resources = mock.Mock(image_id='ubuntu_jammy', region='fr-par')
        assert scaleway_cloud.Scaleway._get_image_label(resources,
                                                        None) == 'ubuntu_jammy'

        region_scoped = mock.Mock(image_id={'fr-par': 'custom'},
                                  region='fr-par')
        assert scaleway_cloud.Scaleway._get_image_label(region_scoped,
                                                        None) == 'custom'

        region_agnostic = mock.Mock(image_id={None: 'anywhere'},
                                    region='fr-par')
        assert scaleway_cloud.Scaleway._get_image_label(region_agnostic,
                                                        None) == 'anywhere'


class TestTags:

    def test_roundtrip(self):
        tags = [
            utils.TAG_SKYPILOT,
            utils.cluster_tag('my-cluster'),
            utils.node_kind_tag(utils.HEAD_NODE_KIND),
        ]
        assert utils.get_tag_value(tags, utils.TAG_CLUSTER_NAME) == 'my-cluster'
        assert utils.get_tag_value(tags, utils.TAG_NODE_KIND) == 'head'
        assert utils.get_tag_value(tags, 'absent') is None

    def test_head_detection(self):
        head = mock.Mock(tags=[utils.node_kind_tag(utils.HEAD_NODE_KIND)])
        worker = mock.Mock(tags=[utils.node_kind_tag(utils.WORKER_NODE_KIND)])
        untagged = mock.Mock(tags=[])
        assert utils.is_head(head)
        assert not utils.is_head(worker)
        assert not utils.is_head(untagged)


class TestImageAndVolumeSelection:

    @staticmethod
    def _server_type(local_ssd_max_size):
        return mock.Mock(per_volume_constraint=mock.Mock(
            l_ssd=mock.Mock(max_size=local_ssd_max_size, min_size=0)))

    def test_modern_gpu_types_require_block_storage(self):
        with mock.patch.object(utils,
                               'get_server_type',
                               return_value=self._server_type(0)):
            assert utils.root_volume_type('fr-par-2',
                                          'H100-1-80G') == 'sbs_volume'

    def test_legacy_types_use_local_ssd(self):
        with mock.patch.object(utils,
                               'get_server_type',
                               return_value=self._server_type(800 * 1024**3)):
            assert utils.root_volume_type('fr-par-2', 'RENDER-S') == 'l_ssd'

    def test_sizes_are_decimal_gb_like_scaleway_reports_them(self):
        # Scaleway reports a RENDER-S local SSD limit of 800000000000 for its
        # documented 800 GB, i.e. decimal GB, not GiB. Reading it as GiB would
        # overshoot the limit and be rejected at create time.
        with mock.patch.object(utils,
                               'get_server_type',
                               return_value=self._server_type(800 * 1000**3)):
            volume_type, _, max_size = utils.root_volume_spec(
                'fr-par-2', 'RENDER-S')
        assert volume_type == 'l_ssd'
        assert max_size // utils._BYTES_PER_GB == 800  # pylint: disable=protected-access

    def test_block_storage_types_report_a_minimum(self):
        with mock.patch.object(utils,
                               'get_server_type',
                               return_value=self._server_type(0)):
            volume_type, min_size, max_size = utils.root_volume_spec(
                'fr-par-2', 'H100-1-80G')
        assert volume_type == 'sbs_volume'
        assert min_size > 0
        assert max_size == 0

    def test_unknown_instance_type_fails_loudly(self):
        with mock.patch.object(utils, 'get_server_type', return_value=None):
            with pytest.raises(ValueError, match='is not offered in zone'):
                utils.root_volume_type('fr-par-3', 'H100-1-80G')

    def test_uuid_is_passed_through(self):
        image_id = '9947dae7-d886-46d1-801a-fedf772916b3'
        assert utils.resolve_image_id('fr-par-2', image_id,
                                      'sbs_volume') == image_id

    def test_label_resolves_to_matching_variant(self):
        api = mock.Mock()
        api.list_local_images_all.return_value = [
            mock.Mock(id='local-id', type_='instance_local'),
            mock.Mock(id='sbs-id', type_='instance_sbs'),
        ]
        with mock.patch.object(scaleway_adaptor,
                               'marketplace_api',
                               return_value=api):
            assert utils.resolve_image_id('fr-par-2', 'ubuntu_noble',
                                          'sbs_volume') == 'sbs-id'
            assert utils.resolve_image_id('fr-par-2', 'ubuntu_noble',
                                          'l_ssd') == 'local-id'

    def test_missing_variant_fails_loudly(self):
        api = mock.Mock()
        api.list_local_images_all.return_value = [
            mock.Mock(id='local-id', type_='instance_local'),
        ]
        with mock.patch.object(scaleway_adaptor,
                               'marketplace_api',
                               return_value=api):
            with pytest.raises(ValueError, match='has no instance_sbs'):
                utils.resolve_image_id('fr-par-2', 'ubuntu_noble', 'sbs_volume')


class TestServerFields:

    def test_external_ip_prefers_public_ip(self):
        server = mock.Mock(public_ip=mock.Mock(address='1.2.3.4'),
                           public_ips=[mock.Mock(address='5.6.7.8')])
        assert utils.external_ip(server) == '1.2.3.4'

    def test_external_ip_falls_back_to_list(self):
        server = mock.Mock(public_ip=None,
                           public_ips=[mock.Mock(address='5.6.7.8')])
        assert utils.external_ip(server) == '5.6.7.8'

    def test_external_ip_absent(self):
        server = mock.Mock(public_ip=None, public_ips=[])
        assert utils.external_ip(server) is None

    def test_volume_ids(self):
        server = mock.Mock(volumes={
            '0': mock.Mock(id='vol-a'),
            '1': mock.Mock(id='vol-b'),
        })
        assert sorted(utils.server_volume_ids(server)) == ['vol-a', 'vol-b']


class TestProvisionHelpers:

    def test_zone_is_required(self):
        with pytest.raises(ValueError, match='no availability_zone'):
            scaleway_instance._zone_of({})  # pylint: disable=protected-access

    def test_zone_is_read_from_provider_config(self):
        # pylint: disable=protected-access
        assert scaleway_instance._zone_of({'availability_zone': 'fr-par-2'
                                          }) == 'fr-par-2'

    def test_capacity_errors_are_recognised(self):
        # pylint: disable=protected-access
        assert scaleway_instance._is_capacity_error(
            Exception('{"type":"out_of_stock"}'))
        assert scaleway_instance._is_capacity_error(
            Exception('quota exceeded for this resource'))
        assert not scaleway_instance._is_capacity_error(
            Exception('invalid_arguments'))

    def test_status_map_covers_every_scaleway_state(self):
        # Every state the Instances API can report must map to a SkyPilot
        # status; an unmapped one would surface as a silently wrong cluster
        # status.
        api_states = {
            utils.STATE_RUNNING,
            utils.STATE_STOPPED,
            utils.STATE_STOPPED_IN_PLACE,
            utils.STATE_STARTING,
            utils.STATE_STOPPING,
            utils.STATE_LOCKED,
        }
        # pylint: disable=protected-access
        assert api_states == set(scaleway_instance._STATUS_MAP)
        assert (scaleway_instance._STATUS_MAP[utils.STATE_RUNNING] ==
                status_lib.ClusterStatus.UP)

    def test_query_instances_reports_unknown_state_as_init(self):
        server = mock.Mock(id='srv-1', state='some-new-state')
        server.state_detail = ''
        with mock.patch.object(utils,
                               'list_cluster_servers',
                               return_value=[server]):
            statuses = scaleway_instance.query_instances(
                'cluster', 'cluster-abc', {'availability_zone': 'fr-par-2'})
        assert statuses['srv-1'][0] == status_lib.ClusterStatus.INIT

    def test_cleanup_ports_without_zone_is_a_noop(self):
        with mock.patch.object(utils, 'delete_security_group') as deleter:
            scaleway_instance.cleanup_ports('cluster-abc', ['8080'], None)
            scaleway_instance.cleanup_ports('cluster-abc', ['8080'], {})
            deleter.assert_not_called()


class TestNotFoundHandling:
    """Teardown must treat an already-removed resource as success."""

    def test_404_response_is_detected(self):
        # pylint: disable=protected-access
        err = Exception('boom')
        err.response = mock.Mock(status_code=404)
        assert utils._is_not_found(err)

    def test_not_found_body_is_detected(self):
        # pylint: disable=protected-access
        assert utils._is_not_found(Exception('{"type":"not_found"}'))

    def test_other_errors_are_not_swallowed(self):
        # pylint: disable=protected-access
        err = Exception('permission denied')
        err.response = mock.Mock(status_code=403)
        assert not utils._is_not_found(err)
