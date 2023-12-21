# stdlib
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union
from typing import cast

# third party
import docker
from docker.models.containers import Container

# relative
from ...serde.serializable import serializable
from ...store.document_store import DocumentStore
from ...store.linked_obj import LinkedObject
from ...types.uid import UID
from ..context import AuthedServiceContext
from ..response import SyftError
from ..response import SyftSuccess
from ..service import AbstractService
from ..service import TYPE_TO_SERVICE
from ..service import service_method
from ..user.user_roles import DATA_OWNER_ROLE_LEVEL
from .utils import DEFAULT_WORKER_POOL_NAME
from .utils import backend_container_name
from .utils import get_container
from .utils import run_containers
from .utils import run_workers_in_threads
from .worker_image_stash import SyftWorkerImageStash
from .worker_pool import ContainerSpawnStatus
from .worker_pool import SyftWorker
from .worker_pool import WorkerOrchestrationType
from .worker_pool import WorkerPool
from .worker_pool_stash import SyftWorkerPoolStash
from .worker_service import WorkerService
from .worker_stash import WorkerStash


@serializable()
class SyftWorkerPoolService(AbstractService):
    store: DocumentStore
    stash: SyftWorkerPoolStash

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self.stash = SyftWorkerPoolStash(store=store)
        self.image_stash = SyftWorkerImageStash(store=store)
        self.worker_stash = WorkerStash(store=store)

    @service_method(
        path="worker_pool.create",
        name="create",
        roles=DATA_OWNER_ROLE_LEVEL,
    )
    def create_pool(
        self,
        context: AuthedServiceContext,
        name: str,
        image_uid: Optional[UID],
        number: int,
    ) -> Union[List[ContainerSpawnStatus], SyftError]:
        """Creates a pool of workers from the given SyftWorkerImage.

        - Retrieves the image for the given UID
        - Use docker to launch containers for given image
        - For each successful container instantiation create a SyftWorker object
        - Creates a SyftWorkerPool object

        Args:
            context (AuthedServiceContext): context passed to the service
            name (str): name of the pool
            image_id (UID): UID of the SyftWorkerImage against which the pool should be created
            number (int): number of SyftWorker that needs to be created in the pool
        """

        result = self.stash.get_by_name(context.credentials, pool_name=name)

        if result.is_err():
            return SyftError(message=f"{result.err()}")

        if result.ok() is not None:
            return SyftError(message=f"Worker Pool with name: {name} already exists !!")

        if image_uid is None:
            result = self.stash.get_by_name(
                context.credentials, pool_name=DEFAULT_WORKER_POOL_NAME
            )
            default_worker_pool = result.ok()
            image_uid = default_worker_pool.syft_worker_image_id

        result = self.image_stash.get_by_uid(
            credentials=context.credentials, uid=image_uid
        )
        if result.is_err():
            return SyftError(
                message=f"Failed to retrieve Worker Image with id: {image_uid}. Error: {result.err()}"
            )

        worker_image = result.ok()

        queue_port = context.node.queue_config.client_config.queue_port

        # Check if workers needs to be run in memory or as containers
        existing_backend_container = get_container(
            docker_client=docker.from_env(),
            container_name=backend_container_name(),
        )
        start_workers_in_memory = (
            existing_backend_container is None or context.node.in_memory_workers
        )

        if start_workers_in_memory:
            # Run in-memory workers in threads
            container_statuses: List[ContainerSpawnStatus] = run_workers_in_threads(
                node=context.node,
                pool_name=name,
                number=number,
            )
        else:
            container_statuses: List[ContainerSpawnStatus] = run_containers(
                pool_name=name,
                worker_image=worker_image,
                number=number,
                orchestration=WorkerOrchestrationType.DOCKER,
                queue_port=queue_port,
                dev_mode=context.node.dev_mode,
            )

        worker_list = []

        for container_status in container_statuses:
            worker = container_status.worker
            if worker is None:
                continue
            result = self.worker_stash.set(
                credentials=context.credentials,
                obj=worker,
            )

            if result.is_ok():
                worker_obj = LinkedObject.from_obj(
                    obj=result.ok(),
                    service_type=WorkerService,
                    node_uid=context.node.id,
                )
                worker_list.append(worker_obj)
            else:
                container_status.error = result.err()

        worker_pool = WorkerPool(
            name=name,
            syft_worker_image_id=image_uid,
            max_count=number,
            worker_list=worker_list,
        )
        result = self.stash.set(credentials=context.credentials, obj=worker_pool)

        if result.is_err():
            return SyftError(message=f"Failed to save Worker Pool: {result.err()}")

        return container_statuses

    @service_method(
        path="worker_pool.get_all",
        name="get_all",
        roles=DATA_OWNER_ROLE_LEVEL,
    )
    def get_all(
        self, context: AuthedServiceContext
    ) -> Union[List[WorkerPool], SyftError]:
        # TODO: During get_all, we should dynamically make a call to docker to get the status of the containers
        # and update the status of the workers in the pool.
        result = self.stash.get_all(credentials=context.credentials)
        if result.is_err():
            return SyftError(message=f"{result.err()}")

        return result.ok()

    @service_method(
        path="worker_pool.delete_worker",
        name="delete_worker",
        roles=DATA_OWNER_ROLE_LEVEL,
    )
    def delete_worker(
        self,
        context: AuthedServiceContext,
        worker_pool_id: UID,
        worker_id: UID,
        force: bool = False,
    ) -> Union[SyftSuccess, SyftError]:
        worker_pool_worker = self._get_worker_pool_and_worker(
            context, worker_pool_id, worker_id
        )
        if isinstance(worker_pool_worker, SyftError):
            return worker_pool_worker

        worker_pool, linked_worker = worker_pool_worker

        result = linked_worker.resolve_with_context(context=context)

        if result.is_err():
            return SyftError(
                message=f"Failed to retrieve Linked SyftWorker {linked_worker.object_uid}"
            )

        worker = result.ok()

        if not context.node.in_memory_workers:
            # delete the worker using docker client sdk
            docker_container = _get_worker_container(worker)
            if isinstance(docker_container, SyftError):
                return docker_container

            try:
                # stop the container
                docker_container.stop()
                # Remove the container and its volumes
                docker_container.remove(force=force, v=True)
            except docker.errors.APIError as e:
                if "removal of container" in str(e) and "is already in progress" in str(
                    e
                ):
                    # If the container is already being removed, ignore the error
                    pass
                else:
                    # If it's a different error, return it
                    return SyftError(
                        message=f"Failed to delete worker with id: {worker_id}. Error: {e}"
                    )
            except Exception as e:
                return SyftError(
                    message=f"Failed to delete worker with id: {worker_id}. Error: {e}"
                )

        # remove the worker from the pool
        worker_pool.worker_list.remove(linked_worker)

        # Delete worker from worker stash
        result = self.worker_stash.delete_by_uid(
            credentials=context.credentials, uid=worker.id
        )
        if result.is_err():
            return SyftError(message=f"Failed to delete worker with uid: {worker.id}")

        # Update worker pool
        result = self.stash.update(context.credentials, obj=worker_pool)
        if result.is_err():
            return SyftError(message=f"Failed to update worker pool: {result.err()}")

        return SyftSuccess(
            message=f"Worker with id: {worker_id} deleted successfully from pool: {worker_pool.name}"
        )

    @service_method(
        path="worker_pool.filter_by_image_id",
        name="filter_by_image_id",
        roles=DATA_OWNER_ROLE_LEVEL,
    )
    def filter_by_image_id(
        self, context: AuthedServiceContext, image_uid: UID
    ) -> Union[List[WorkerPool], SyftError]:
        result = self.stash.get_by_image_uid(context.credentials, image_uid)

        if result.is_err():
            return SyftError(message=f"Failed to get worker pool for uid: {image_uid}")

        return result.ok()

    @service_method(
        path="worker_pool.worker_logs",
        name="worker_logs",
        roles=DATA_OWNER_ROLE_LEVEL,
    )
    def worker_logs(
        self,
        context: AuthedServiceContext,
        worker_pool_id: UID,
        worker_id: UID,
        raw: bool = False,
    ) -> Union[bytes, str, SyftError]:
        worker_pool_worker = self._get_worker_pool_and_worker(
            context, worker_pool_id, worker_id
        )
        if isinstance(worker_pool_worker, SyftError):
            return worker_pool_worker

        _, linked_worker = worker_pool_worker

        result = linked_worker.resolve_with_context(context)

        if result.is_err():
            return SyftError(
                message=f"Failed to retrieve Linked SyftWorker {linked_worker.object_uid}"
            )

        worker = result.ok()

        if context.node.in_memory_workers:
            logs = b"Logs not implemented for In Memory Workers"
        else:
            docker_container = _get_worker_container(worker)
            if isinstance(docker_container, SyftError):
                return docker_container

            try:
                logs = cast(bytes, docker_container.logs())
            except docker.errors.APIError as e:
                return SyftError(
                    f"Failed to get worker {worker.id} container logs. Error {e}"
                )

        return logs if raw else logs.decode(errors="ignore")

    def _get_worker_pool(
        self,
        context: AuthedServiceContext,
        worker_pool_id: UID,
    ) -> Union[WorkerPool, SyftError]:
        worker_pool = self.stash.get_by_uid(
            credentials=context.credentials, uid=worker_pool_id
        )

        return (
            SyftError(message=f"{worker_pool.err()}")
            if worker_pool.is_err()
            else cast(WorkerPool, worker_pool.ok())
        )

    def _get_worker_pool_and_worker(
        self, context: AuthedServiceContext, worker_pool_id: UID, worker_id: UID
    ) -> Union[Tuple[WorkerPool, LinkedObject], SyftError]:
        worker_pool = self._get_worker_pool(context, worker_pool_id)
        if isinstance(worker_pool, SyftError):
            return worker_pool

        worker = _get_worker(worker_pool, worker_id)
        if isinstance(worker, SyftError):
            return worker

        return worker_pool, worker


def _get_worker_opt(worker_pool: WorkerPool, worker_id: UID) -> Optional[SyftWorker]:
    try:
        return next(
            worker
            for worker in worker_pool.worker_list
            if worker.object_uid == worker_id
        )
    except StopIteration:
        return None


def _get_worker(
    worker_pool: WorkerPool, worker_id: UID
) -> Union[LinkedObject, SyftError]:
    linked_worker = _get_worker_opt(worker_pool, worker_id)
    return (
        linked_worker
        if linked_worker is not None
        else SyftError(
            message=f"Worker with id: {worker_id} not found in pool: {worker_pool.name}"
        )
    )


def _get_worker_container(
    worker: SyftWorker, docker_client: Optional[docker.DockerClient] = None
) -> Union[Container, SyftError]:
    docker_client = docker_client if docker_client is not None else docker.from_env()
    try:
        return cast(Container, docker_client.containers.get(worker.container_id))
    except docker.errors.NotFound as e:
        return SyftError(f"Worker {worker.id} container not found. Error {e}")
    except docker.errors.APIError as e:
        return SyftError(
            f"Unable to access worker {worker.id} container. "
            + f"Container server error {e}"
        )


TYPE_TO_SERVICE[WorkerPool] = SyftWorkerPoolService
