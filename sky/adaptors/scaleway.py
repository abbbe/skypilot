"""Scaleway cloud adaptors."""

import os
import threading
from typing import Any, List, Optional, Tuple

from sky.adaptors import common

_IMPORT_ERROR_MESSAGE = ('Failed to import dependencies for Scaleway. '
                         'Try pip install "skypilot[scaleway]"')

scaleway = common.LazyImport('scaleway',
                             import_error_message=_IMPORT_ERROR_MESSAGE)
_LAZY_MODULES = (scaleway,)

CREDENTIAL_FILE = '~/.config/scw/config.yaml'

# The name Scaleway's own tooling uses when no profile has been selected.
_DEFAULT_PROFILE = 'default'

# Environment variables honoured by the official SDK. If either of the key
# variables is set we let the SDK resolve everything from the environment,
# which is also how CI and containers are expected to be configured.
_ENV_ACCESS_KEY = 'SCW_ACCESS_KEY'
_ENV_SECRET_KEY = 'SCW_SECRET_KEY'
_ENV_PROFILE = 'SCW_PROFILE'

_client = None
_client_lock = threading.RLock()


def _credential_path() -> str:
    """Returns the path of the scw config file, honouring SCW_CONFIG_PATH."""
    path = os.environ.get('SCW_CONFIG_PATH')
    if path:
        return os.path.expanduser(path)
    return os.path.expanduser(CREDENTIAL_FILE)


def resolve_profile_name() -> Optional[str]:
    """Returns the scw profile name to load from the config file.

    The Scaleway SDK's ``from_config_file_and_env`` defaults to the profile
    literally named ``default`` and, unlike the official ``scw`` CLI, ignores
    the ``active_profile`` key that ``scw init`` writes. Users who named their
    profile therefore end up with a config file the CLI reads happily and the
    SDK silently refuses. Resolve it the way the CLI does.

    Returns None when the config file cannot be read, in which case the caller
    should fall back to the SDK's own default handling.
    """
    env_profile = os.environ.get(_ENV_PROFILE)
    if env_profile:
        return env_profile

    path = _credential_path()
    if not os.path.isfile(path):
        return None
    try:
        # yaml is a core SkyPilot dependency, so this import is always safe.
        import yaml  # pylint: disable=import-outside-toplevel
        with open(path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError):
        return None
    except Exception:  # pylint: disable=broad-except
        # A malformed config file should surface as a credential error from
        # check_credentials(), not as a crash here.
        return None
    if not isinstance(config, dict):
        return None
    active_profile = config.get('active_profile')
    if isinstance(active_profile, str) and active_profile:
        return active_profile
    return _DEFAULT_PROFILE


@common.load_lazy_modules(modules=_LAZY_MODULES)
def client():
    """Returns a cached Scaleway API client.

    Raises:
        ValueError: if no usable credentials were found.
    """
    global _client
    with _client_lock:
        if _client is None:
            profile_name = resolve_profile_name()
            if profile_name is None:
                _client = scaleway.Client.from_env()
            else:
                _client = scaleway.Client.from_config_file_and_env(
                    profile_name=profile_name)
        return _client


def _reset_client_cache() -> None:
    """Drops the cached client. Only used by tests."""
    global _client
    with _client_lock:
        _client = None


@common.load_lazy_modules(modules=_LAZY_MODULES)
def instance_api():
    """Returns the Instance (compute) API bound to the resolved client."""
    # pylint: disable=import-outside-toplevel
    from scaleway.instance.v1 import InstanceV1API
    return InstanceV1API(client())


@common.load_lazy_modules(modules=_LAZY_MODULES)
def marketplace_api():
    """Returns the Marketplace (images) API bound to the resolved client."""
    # pylint: disable=import-outside-toplevel
    from scaleway.marketplace.v2 import MarketplaceV2API
    return MarketplaceV2API(client())


@common.load_lazy_modules(modules=_LAZY_MODULES)
def iam_api():
    """Returns the IAM API bound to the resolved client."""
    # pylint: disable=import-outside-toplevel
    from scaleway.iam.v1alpha1 import IamV1Alpha1API
    return IamV1Alpha1API(client())


@common.load_lazy_modules(modules=_LAZY_MODULES)
def scaleway_exception():
    """Returns the base Scaleway SDK exception class."""
    return scaleway.ScalewayException


@common.load_lazy_modules(modules=_LAZY_MODULES)
def all_zones() -> List[str]:
    """Returns every zone the SDK knows about."""
    return list(scaleway.ALL_ZONES)


def zone_to_region(zone: str) -> str:
    """Maps a Scaleway zone onto its region, e.g. fr-par-2 -> fr-par.

    Scaleway zone names are always ``<region>-<index>``.
    """
    return zone.rsplit('-', 1)[0]


def project_id() -> Optional[str]:
    """Returns the default project id from the resolved profile."""
    return client().default_project_id


def check_credentials() -> Tuple[bool, Optional[str]]:
    """Checks whether Scaleway credentials are usable.

    Returns:
        A (ok, hint) tuple. ``hint`` is None when ok is True.
    """
    hint = ('Failed to access Scaleway with the provided credentials. '
            'To set up credentials, generate an API key at\n    '
            '  https://console.scaleway.com/iam/api-keys\n    '
            'and run `scw init`, or set the SCW_ACCESS_KEY, SCW_SECRET_KEY and '
            'SCW_DEFAULT_PROJECT_ID environment variables.')

    has_env_credentials = bool(
        os.environ.get(_ENV_ACCESS_KEY) and os.environ.get(_ENV_SECRET_KEY))
    if not has_env_credentials and not os.path.isfile(_credential_path()):
        return False, (f'{_credential_path()} does not exist. ' + hint)

    try:
        api = instance_api()
    except ImportError as e:
        return False, str(e)
    except Exception as e:  # pylint: disable=broad-except
        return False, f'{hint}\n    Details: {e}'

    if not client().default_project_id:
        return False, ('No default Scaleway project id is configured. '
                       'Set `default_project_id` in '
                       f'{_credential_path()} or the '
                       'SCW_DEFAULT_PROJECT_ID environment variable.')

    try:
        # A cheap, read-only call that exercises authentication.
        api.list_servers(zone=_probe_zone(), per_page=1)
    except Exception as e:  # pylint: disable=broad-except
        return False, f'{hint}\n    Details: {e}'
    return True, None


def _probe_zone() -> str:
    """Returns a zone to use for credential probing."""
    default_zone = client().default_zone
    if default_zone:
        return str(default_zone)
    return 'fr-par-1'


def get_credential_file_mounts() -> dict:
    """Returns the credential files to mount onto the cluster."""
    path = _credential_path()
    if os.path.isfile(path):
        return {CREDENTIAL_FILE: path}
    return {}


def public_key_matches(ssh_key: Any, public_key: str) -> bool:
    """Compares an IAM SSH key against a public key string.

    Scaleway normalises the trailing comment and whitespace of uploaded keys,
    so compare only the algorithm and the key material.
    """
    stored = getattr(ssh_key, 'public_key', None)
    if not stored:
        return False
    return _key_material(stored) == _key_material(public_key)


def _key_material(public_key: str) -> Tuple[str, ...]:
    parts = public_key.strip().split()
    # 'ssh-ed25519 AAAA... comment' -> ('ssh-ed25519', 'AAAA...')
    return tuple(parts[:2])
