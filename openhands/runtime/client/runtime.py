import os
import tempfile
import threading
import time
import uuid
from zipfile import ZipFile

import docker
import requests
import tenacity

from openhands.core.config import AppConfig
from openhands.core.logger import openhands_logger as logger
from openhands.events import EventStream
from openhands.events.action import (
    ActionConfirmationStatus,
    BrowseInteractiveAction,
    BrowseURLAction,
    CmdRunAction,
    FileReadAction,
    FileWriteAction,
    IPythonRunCellAction,
)
from openhands.events.action.action import Action
from openhands.events.observation import (
    ErrorObservation,
    NullObservation,
    Observation,
    UserRejectObservation,
)
from openhands.events.serialization import event_to_dict, observation_from_dict
from openhands.events.serialization.action import ACTION_TYPE_TO_CLASS
from openhands.runtime.builder import DockerRuntimeBuilder
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.runtime import Runtime
from openhands.runtime.utils import find_available_tcp_port
from openhands.runtime.utils.runtime_build import build_runtime_image


class LogBuffer:
    """
    Synchronous buffer for Docker container logs.

    This class provides a thread-safe way to collect, store, and retrieve logs
    from a Docker container. It uses a list to store log lines and provides methods
    for appending, retrieving, and clearing logs.
    """

    def __init__(self, container: docker.models.containers.Container):
        self.client_ready = False
        self.init_msg = 'Runtime client initialized.'

        self.buffer: list[str] = []
        self.lock = threading.Lock()
        self.log_generator = container.logs(stream=True, follow=True)
        self.log_stream_thread = threading.Thread(target=self.stream_logs)
        self.log_stream_thread.daemon = True
        self.log_stream_thread.start()
        self._stop_event = threading.Event()

    def append(self, log_line: str):
        with self.lock:
            self.buffer.append(log_line)

    def get_and_clear(self) -> list[str]:
        with self.lock:
            logs = list(self.buffer)
            self.buffer.clear()
            return logs

    def stream_logs(self):
        """
        Stream logs from the Docker container in a separate thread.

        This method runs in its own thread to handle the blocking
        operation of reading log lines from the Docker SDK's synchronous generator.
        """
        try:
            for log_line in self.log_generator:
                if self._stop_event.is_set():
                    break
                if log_line:
                    decoded_line = log_line.decode('utf-8').rstrip()
                    self.append(decoded_line)
                    if self.init_msg in decoded_line:
                        self.client_ready = True
        except Exception as e:
            logger.error(f'Error streaming docker logs: {e}')

    def __del__(self):
        if self.log_stream_thread.is_alive():
            logger.warn(
                "LogBuffer was not properly closed. Use 'log_buffer.close()' for clean shutdown."
            )
            self.close(timeout=5)

    def close(self, timeout: float = 10.0):
        self._stop_event.set()
        self.log_stream_thread.join(timeout)


class EventStreamRuntime(Runtime):
    """This runtime will subscribe the event stream.
    When receive an event, it will send the event to runtime-client which run inside the docker environment.
    """

    container_name_prefix = 'openhands-sandbox-'

    def __init__(
        self,
        config: AppConfig,
        event_stream: EventStream,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
    ):
        self.config = config
        self._port = find_available_tcp_port()
        self.api_url = f'http://{self.config.sandbox.api_hostname}:{self._port}'
        self.session = requests.Session()

        self.instance_id = (
            sid + '_' + str(uuid.uuid4()) if sid is not None else str(uuid.uuid4())
        )
        self.docker_client: docker.DockerClient = self._init_docker_client()
        self.base_container_image = self.config.sandbox.base_container_image
        self.runtime_container_image = self.config.sandbox.runtime_container_image
        self.container_name = self.container_name_prefix + self.instance_id

        self.container = None
        self.action_semaphore = threading.Semaphore(1)  # Ensure one action at a time

        self.runtime_builder = DockerRuntimeBuilder(self.docker_client)
        logger.debug(f'EventStreamRuntime `{sid}`')

        # Buffer for container logs
        self.log_buffer: LogBuffer | None = None

        if self.config.sandbox.runtime_extra_deps:
            logger.info(
                f'Installing extra user-provided dependencies in the runtime image: {self.config.sandbox.runtime_extra_deps}'
            )

        if self.runtime_container_image is None:
            if self.base_container_image is None:
                raise ValueError(
                    'Neither runtime container image nor base container image is set'
                )
            self.runtime_container_image = build_runtime_image(
                self.base_container_image,
                self.runtime_builder,
                extra_deps=self.config.sandbox.runtime_extra_deps,
            )
        self.container = self._init_container(
            self.sandbox_workspace_dir,
            mount_dir=self.config.workspace_mount_path,
            plugins=plugins,
        )
        # will initialize both the event stream and the env vars
        super().__init__(config, event_stream, sid, plugins, env_vars)

        logger.info(
            f'Container initialized with plugins: {[plugin.name for plugin in self.plugins]}'
        )
        logger.info(f'Container initialized with env vars: {env_vars}')

    @staticmethod
    def _init_docker_client() -> docker.DockerClient:
        try:
            return docker.from_env()
        except Exception as ex:
            logger.error(
                'Launch docker client failed. Please make sure you have installed docker and started docker desktop/daemon.'
            )
            raise ex

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=60),
    )
    def _init_container(
        self,
        sandbox_workspace_dir: str,
        mount_dir: str | None = None,
        plugins: list[PluginRequirement] | None = None,
    ):
        try:
            logger.info(
                f'Starting container with image: {self.runtime_container_image} and name: {self.container_name}'
            )
            plugin_arg = ''
            if plugins is not None and len(plugins) > 0:
                plugin_arg = (
                    f'--plugins {" ".join([plugin.name for plugin in plugins])} '
                )

            network_mode: str | None = None
            port_mapping: dict[str, int] | None = None
            if self.config.sandbox.use_host_network:
                network_mode = 'host'
                logger.warn(
                    'Using host network mode. If you are using MacOS, please make sure you have the latest version of Docker Desktop and enabled host network feature: https://docs.docker.com/network/drivers/host/#docker-desktop'
                )
            else:
                port_mapping = {f'{self._port}/tcp': self._port}

            if mount_dir is not None:
                volumes = {mount_dir: {'bind': sandbox_workspace_dir, 'mode': 'rw'}}
                logger.info(f'Mount dir: {sandbox_workspace_dir}')
            else:
                logger.warn(
                    'Mount dir is not set, will not mount the workspace directory to the container.'
                )
                volumes = None

            if self.config.sandbox.browsergym_eval_env is not None:
                browsergym_arg = (
                    f'--browsergym-eval-env {self.config.sandbox.browsergym_eval_env}'
                )
            else:
                browsergym_arg = ''
            container = self.docker_client.containers.run(
                self.runtime_container_image,
                command=(
                    f'/openhands/miniforge3/bin/mamba run --no-capture-output -n base '
                    'PYTHONUNBUFFERED=1 poetry run '
                    f'python -u -m openhands.runtime.client.client {self._port} '
                    f'--working-dir {sandbox_workspace_dir} '
                    f'{plugin_arg}'
                    f'--username {"openhands" if self.config.run_as_openhands else "root"} '
                    f'--user-id {self.config.sandbox.user_id} '
                    f'{browsergym_arg}'
                ),
                network_mode=network_mode,
                ports=port_mapping,
                working_dir='/openhands/code/',
                name=self.container_name,
                detach=True,
                environment={'DEBUG': 'true'} if self.config.debug else None,
                volumes=volumes,
            )
            self.log_buffer = LogBuffer(container)
            logger.info(f'Container started. Server url: {self.api_url}')
            return container
        except Exception as e:
            logger.error('Failed to start container')
            logger.exception(e)
            self.close(close_client=False)
            raise e

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(10),
        wait=tenacity.wait_exponential(multiplier=2, min=10, max=60),
        reraise=(ConnectionRefusedError,),
    )
    def _wait_until_alive(self):
        logger.debug('Getting container logs...')

        # Print and clear the log buffer
        assert (
            self.log_buffer is not None
        ), 'Log buffer is expected to be initialized when container is started'

        # Always process logs, regardless of client_ready status
        logs = self.log_buffer.get_and_clear()
        if logs:
            formatted_logs = '\n'.join([f'    |{log}' for log in logs])
            logger.info(
                '\n'
                + '-' * 35
                + 'Container logs:'
                + '-' * 35
                + f'\n{formatted_logs}'
                + '\n'
                + '-' * 80
            )

        if not self.log_buffer.client_ready:
            attempts = 0
            while not self.log_buffer.client_ready and attempts < 5:
                attempts += 1
                time.sleep(1)
                logs = self.log_buffer.get_and_clear()
                if logs:
                    formatted_logs = '\n'.join([f'    |{log}' for log in logs])
                    logger.info(
                        '\n'
                        + '-' * 35
                        + 'Container logs:'
                        + '-' * 35
                        + f'\n{formatted_logs}'
                        + '\n'
                        + '-' * 80
                    )

        response = self.session.get(f'{self.api_url}/alive')
        if response.status_code == 200:
            return
        else:
            msg = f'Action execution API is not alive. Response: {response}'
            logger.error(msg)
            raise RuntimeError(msg)

    @property
    def sandbox_workspace_dir(self):
        return self.config.workspace_mount_path_in_sandbox

    def close(self, close_client: bool = True):
        if self.log_buffer:
            self.log_buffer.close()

        if self.session:
            self.session.close()

        containers = self.docker_client.containers.list(all=True)
        for container in containers:
            try:
                if container.name.startswith(self.container_name_prefix):
                    logs = container.logs(tail=1000).decode('utf-8')
                    logger.debug(
                        f'==== Container logs ====\n{logs}\n==== End of container logs ===='
                    )
                    container.remove(force=True)
            except docker.errors.NotFound:
                pass
        if close_client:
            self.docker_client.close()

    def run_action(self, action: Action) -> Observation:
        # set timeout to default if not set
        if action.timeout is None:
            action.timeout = self.config.sandbox.timeout

        with self.action_semaphore:
            if not action.runnable:
                return NullObservation('')
            if (
                hasattr(action, 'is_confirmed')
                and action.is_confirmed
                == ActionConfirmationStatus.AWAITING_CONFIRMATION
            ):
                return NullObservation('')
            action_type = action.action  # type: ignore[attr-defined]
            if action_type not in ACTION_TYPE_TO_CLASS:
                return ErrorObservation(f'Action {action_type} does not exist.')
            if not hasattr(self, action_type):
                return ErrorObservation(
                    f'Action {action_type} is not supported in the current runtime.'
                )
            if (
                hasattr(action, 'is_confirmed')
                and action.is_confirmed == ActionConfirmationStatus.REJECTED
            ):
                return UserRejectObservation(
                    'Action has been rejected by the user! Waiting for further user input.'
                )

            logger.info('Awaiting session')
            self._wait_until_alive()

            assert action.timeout is not None

            try:
                response = self.session.post(
                    f'{self.api_url}/execute_action',
                    json={'action': event_to_dict(action)},
                    timeout=action.timeout,
                )
                if response.status_code == 200:
                    output = response.json()
                    obs = observation_from_dict(output)
                    obs._cause = action.id  # type: ignore[attr-defined]
                    return obs
                else:
                    error_message = response.text
                    logger.error(f'Error from server: {error_message}')
                    obs = ErrorObservation(f'Command execution failed: {error_message}')
            except requests.Timeout:
                logger.error('No response received within the timeout period.')
                obs = ErrorObservation('Command execution timed out')
            except Exception as e:
                logger.error(f'Error during command execution: {e}')
                obs = ErrorObservation(f'Command execution failed: {str(e)}')
            return obs

    def run(self, action: CmdRunAction) -> Observation:
        return self.run_action(action)

    def run_ipython(self, action: IPythonRunCellAction) -> Observation:
        return self.run_action(action)

    def read(self, action: FileReadAction) -> Observation:
        return self.run_action(action)

    def write(self, action: FileWriteAction) -> Observation:
        return self.run_action(action)

    def browse(self, action: BrowseURLAction) -> Observation:
        return self.run_action(action)

    def browse_interactive(self, action: BrowseInteractiveAction) -> Observation:
        return self.run_action(action)

    # ====================================================================
    # Implement these methods (for file operations) in the subclass
    # ====================================================================

    def copy_to(
        self, host_src: str, sandbox_dest: str, recursive: bool = False
    ) -> None:
        if not os.path.exists(host_src):
            raise FileNotFoundError(f'Source file {host_src} does not exist')

        self._wait_until_alive()
        try:
            if recursive:
                # For recursive copy, create a zip file
                with tempfile.NamedTemporaryFile(
                    suffix='.zip', delete=False
                ) as temp_zip:
                    temp_zip_path = temp_zip.name

                with ZipFile(temp_zip_path, 'w') as zipf:
                    for root, _, files in os.walk(host_src):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(
                                file_path, os.path.dirname(host_src)
                            )
                            zipf.write(file_path, arcname)

                upload_data = {'file': open(temp_zip_path, 'rb')}
            else:
                # For single file copy
                upload_data = {'file': open(host_src, 'rb')}

            params = {'destination': sandbox_dest, 'recursive': str(recursive).lower()}

            response = self.session.post(
                f'{self.api_url}/upload_file', files=upload_data, params=params
            )
            if response.status_code == 200:
                return
            else:
                error_message = response.text
                raise Exception(f'Copy operation failed: {error_message}')

        except requests.Timeout:
            raise TimeoutError('Copy operation timed out')
        except Exception as e:
            raise RuntimeError(f'Copy operation failed: {str(e)}')
        finally:
            if recursive:
                os.unlink(temp_zip_path)
            logger.info(f'Copy completed: host:{host_src} -> runtime:{sandbox_dest}')

    def list_files(self, path: str | None = None) -> list[str]:
        """List files in the sandbox.

        If path is None, list files in the sandbox's initial working directory (e.g., /workspace).
        """
        self._wait_until_alive()
        try:
            data = {}
            if path is not None:
                data['path'] = path

            response = self.session.post(f'{self.api_url}/list_files', json=data)
            if response.status_code == 200:
                response_json = response.json()
                assert isinstance(response_json, list)
                return response_json
            else:
                error_message = response.text
                raise Exception(f'List files operation failed: {error_message}')
        except requests.Timeout:
            raise TimeoutError('List files operation timed out')
        except Exception as e:
            raise RuntimeError(f'List files operation failed: {str(e)}')
