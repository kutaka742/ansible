"""Functions for accessing docker via the docker cli."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import os
import time

from .io import (
    open_binary_file,
    read_text_file,
)

from .util import (
    ApplicationError,
    common_environment,
    display,
    find_executable,
    SubprocessError,
)

from .util_common import (
    run_command,
)

from .config import (
    EnvironmentConfig,
)

BUFFER_SIZE = 256 * 256


def docker_available():
    """
    :rtype: bool
    """
    return find_executable('docker', required=False)


def get_docker_container_id():
    """
    :rtype: str | None
    """
    path = '/proc/self/cgroup'

    if not os.path.exists(path):
        return None

    contents = read_text_file(path)

    paths = [line.split(':')[2] for line in contents.splitlines()]
    container_ids = set(path.split('/')[2] for path in paths if path.startswith('/docker/'))

    if not container_ids:
        return None

    if len(container_ids) == 1:
        return container_ids.pop()

    raise ApplicationError('Found multiple container_id candidates: %s\n%s' % (sorted(container_ids), contents))


def get_docker_container_ip(args, container_id):
    """
    :type args: EnvironmentConfig
    :type container_id: str
    :rtype: str
    """
    results = docker_inspect(args, container_id)
    ipaddress = results[0]['NetworkSettings']['IPAddress']
    return ipaddress


def get_docker_networks(args, container_id):
    """
    :param args: EnvironmentConfig
    :param container_id: str
    :rtype: list[str]
    """
    results = docker_inspect(args, container_id)
    # podman doesn't return Networks- just silently return None if it's missing...
    networks = results[0]['NetworkSettings'].get('Networks')
    if networks is None:
        return None
    return sorted(networks)


def docker_pull(args, image):
    """
    :type args: EnvironmentConfig
    :type image: str
    """
    if ('@' in image or ':' in image) and docker_images(args, image):
        display.info('Skipping docker pull of existing image with tag or digest: %s' % image, verbosity=2)
        return

    if not args.docker_pull:
        display.warning('Skipping docker pull for "%s". Image may be out-of-date.' % image)
        return

    for _iteration in range(1, 10):
        try:
            docker_command(args, ['pull', image])
            return
        except SubprocessError:
            display.warning('Failed to pull docker image "%s". Waiting a few seconds before trying again.' % image)
            time.sleep(3)

    raise ApplicationError('Failed to pull docker image "%s".' % image)


def docker_put(args, container_id, src, dst):
    """
    :type args: EnvironmentConfig
    :type container_id: str
    :type src: str
    :type dst: str
    """
    # avoid 'docker cp' due to a bug which causes 'docker rm' to fail
    with open_binary_file(src) as src_fd:
        docker_exec(args, container_id, ['dd', 'of=%s' % dst, 'bs=%s' % BUFFER_SIZE],
                    options=['-i'], stdin=src_fd, capture=True)


def docker_get(args, container_id, src, dst):
    """
    :type args: EnvironmentConfig
    :type container_id: str
    :type src: str
    :type dst: str
    """
    # avoid 'docker cp' due to a bug which causes 'docker rm' to fail
    with open_binary_file(dst, 'wb') as dst_fd:
        docker_exec(args, container_id, ['dd', 'if=%s' % src, 'bs=%s' % BUFFER_SIZE],
                    options=['-i'], stdout=dst_fd, capture=True)


def docker_run(args, image, options, cmd=None, create_only=False):
    """
    :type args: EnvironmentConfig
    :type image: str
    :type options: list[str] | None
    :type cmd: list[str] | None
    :type create_only[bool] | False
    :rtype: str | None, str | None
    """
    if not options:
        options = []

    if not cmd:
        cmd = []

    if create_only:
        command = 'create'
    else:
        command = 'run'

    for _iteration in range(1, 3):
        try:
            return docker_command(args, [command] + options + [image] + cmd, capture=True)
        except SubprocessError as ex:
            display.error(ex)
            display.warning('Failed to run docker image "%s". Waiting a few seconds before trying again.' % image)
            time.sleep(3)

    raise ApplicationError('Failed to run docker image "%s".' % image)


def docker_start(args, container_id, options):  # type: (EnvironmentConfig, str, t.List[str]) -> (t.Optional[str], t.Optional[str])
    """
    Start a docker container by name or ID
    """
    if not options:
        options = []

    for _iteration in range(1, 3):
        try:
            return docker_command(args, ['start'] + options + [container_id], capture=True)
        except SubprocessError as ex:
            display.error(ex)
            display.warning('Failed to start docker container "%s". Waiting a few seconds before trying again.' % container_id)
            time.sleep(3)

    raise ApplicationError('Failed to run docker container "%s".' % container_id)


def docker_images(args, image):
    """
    :param args: CommonConfig
    :param image: str
    :rtype: list[dict[str, any]]
    """
    try:
        stdout, _dummy = docker_command(args, ['images', image, '--format', '{{json .}}'], capture=True, always=True)
    except SubprocessError as ex:
        if 'no such image' in ex.stderr:
            return []  # podman does not handle this gracefully, exits 125

        if 'function "json" not defined' in ex.stderr:
            # podman > 2 && < 2.2.0 breaks with --format {{json .}}, and requires --format json
            # So we try this as a fallback. If it fails again, we just raise the exception and bail.
            stdout, _dummy = docker_command(args, ['images', image, '--format', 'json'], capture=True, always=True)
        else:
            raise ex

    if stdout.startswith('['):
        # modern podman outputs a pretty-printed json list. Just load the whole thing.
        return json.loads(stdout)

    # docker outputs one json object per line (jsonl)
    return [json.loads(line) for line in stdout.splitlines()]


def docker_rm(args, container_id):
    """
    :type args: EnvironmentConfig
    :type container_id: str
    """
    try:
        docker_command(args, ['rm', '-f', container_id], capture=True)
    except SubprocessError as ex:
        if 'no such container' in ex.stderr:
            pass  # podman does not handle this gracefully, exits 1
        else:
            raise ex


def docker_inspect(args, container_id):
    """
    :type args: EnvironmentConfig
    :type container_id: str
    :rtype: list[dict]
    """
    if args.explain:
        return []

    try:
        stdout = docker_command(args, ['inspect', container_id], capture=True)[0]
        return json.loads(stdout)
    except SubprocessError as ex:
        if 'no such image' in ex.stderr:
            return []  # podman does not handle this gracefully, exits 125
        try:
            return json.loads(ex.stdout)
        except Exception:
            raise ex


def docker_network_disconnect(args, container_id, network):
    """
    :param args: EnvironmentConfig
    :param container_id: str
    :param network: str
    """
    docker_command(args, ['network', 'disconnect', network, container_id], capture=True)


def docker_network_inspect(args, network):
    """
    :type args: EnvironmentConfig
    :type network: str
    :rtype: list[dict]
    """
    if args.explain:
        return []

    try:
        stdout = docker_command(args, ['network', 'inspect', network], capture=True)[0]
        return json.loads(stdout)
    except SubprocessError as ex:
        try:
            return json.loads(ex.stdout)
        except Exception:
            raise ex


def docker_exec(args, container_id, cmd, options=None, capture=False, stdin=None, stdout=None):
    """
    :type args: EnvironmentConfig
    :type container_id: str
    :type cmd: list[str]
    :type options: list[str] | None
    :type capture: bool
    :type stdin: BinaryIO | None
    :type stdout: BinaryIO | None
    :rtype: str | None, str | None
    """
    if not options:
        options = []

    return docker_command(args, ['exec'] + options + [container_id] + cmd, capture=capture, stdin=stdin, stdout=stdout)


def docker_info(args):
    """
    :param args: CommonConfig
    :rtype: dict[str, any]
    """
    stdout, _dummy = docker_command(args, ['info', '--format', '{{json .}}'], capture=True, always=True)
    return json.loads(stdout)


def docker_version(args):
    """
    :param args: CommonConfig
    :rtype: dict[str, any]
    """
    stdout, _dummy = docker_command(args, ['version', '--format', '{{json .}}'], capture=True, always=True)
    return json.loads(stdout)


def docker_command(args, cmd, capture=False, stdin=None, stdout=None, always=False, data=None):
    """
    :type args: CommonConfig
    :type cmd: list[str]
    :type capture: bool
    :type stdin: file | None
    :type stdout: file | None
    :type always: bool
    :type data: str | None
    :rtype: str | None, str | None
    """
    env = docker_environment()
    return run_command(args, ['docker'] + cmd, env=env, capture=capture, stdin=stdin, stdout=stdout, always=always, data=data)


def docker_environment():
    """
    :rtype: dict[str, str]
    """
    env = common_environment()
    env.update(dict((key, os.environ[key]) for key in os.environ if key.startswith('DOCKER_')))
    return env
