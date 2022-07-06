import asyncio
import concurrent.futures
import dataclasses
from datetime import datetime
import hashlib
import json
from typing import Any, Dict, List, Optional

import aiohttp.web

import ray
import ray.dashboard.optional_utils as dashboard_optional_utils
import ray.dashboard.utils as dashboard_utils
from ray._private import ray_constants
from ray.core.generated import gcs_pb2, gcs_service_pb2, gcs_service_pb2_grpc
from ray.dashboard.modules.job.common import JOB_ID_METADATA_KEY, JobInfoStorageClient
from ray.experimental.internal_kv import (
    _internal_kv_get,
    _internal_kv_initialized,
    _internal_kv_list,
)
from ray.job_submission import JobInfo
from ray.runtime_env import RuntimeEnv

routes = dashboard_optional_utils.ClassMethodRouteTable


@dataclasses.dataclass
class RayActivityResponse:
    """
    Dataclass used to inform if a particular Ray component can be considered
    active, and metadata about observation.
    """

    # Whether the corresponding Ray component is considered active
    is_active: bool
    # Reason if Ray component is considered active
    reason: Optional[str] = None
    # Timestamp of when this observation about the Ray component was made
    timestamp: Optional[float] = None


class APIHead(dashboard_utils.DashboardHeadModule):
    def __init__(self, dashboard_head):
        super().__init__(dashboard_head)
        self._gcs_job_info_stub = None
        self._gcs_actor_info_stub = None
        self._dashboard_head = dashboard_head
        assert _internal_kv_initialized()
        self._job_info_client = JobInfoStorageClient()
        # For offloading CPU intensive work.
        self._thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="api_head"
        )

    @routes.get("/api/actors/kill")
    async def kill_actor_gcs(self, req) -> aiohttp.web.Response:
        actor_id = req.query.get("actor_id")
        force_kill = req.query.get("force_kill", False) in ("true", "True")
        no_restart = req.query.get("no_restart", False) in ("true", "True")
        if not actor_id:
            return dashboard_optional_utils.rest_response(
                success=False, message="actor_id is required."
            )

        request = gcs_service_pb2.KillActorViaGcsRequest()
        request.actor_id = bytes.fromhex(actor_id)
        request.force_kill = force_kill
        request.no_restart = no_restart
        await self._gcs_actor_info_stub.KillActorViaGcs(request, timeout=5)

        message = (
            f"Force killed actor with id {actor_id}"
            if force_kill
            else f"Requested actor with id {actor_id} to terminate. "
            + "It will exit once running tasks complete"
        )

        return dashboard_optional_utils.rest_response(success=True, message=message)

    @routes.get("/api/snapshot")
    async def snapshot(self, req):
        (
            job_info,
            job_submission_data,
            actor_data,
            serve_data,
            session_name,
        ) = await asyncio.gather(
            self.get_job_info(),
            self.get_job_submission_info(),
            self.get_actor_info(),
            self.get_serve_info(),
            self.get_session_name(),
        )
        snapshot = {
            "jobs": job_info,
            "job_submission": job_submission_data,
            "actors": actor_data,
            "deployments": serve_data,
            "session_name": session_name,
            "ray_version": ray.__version__,
            "ray_commit": ray.__commit__,
        }
        return dashboard_optional_utils.rest_response(
            success=True, message="hello", snapshot=snapshot
        )

    @routes.get("/api/component_activities")
    async def get_component_activities(self, req) -> aiohttp.web.Response:
        # Get activity information for driver
        timeout = req.query.get("timeout", None)
        if timeout and timeout.isdigit():
            timeout = int(timeout)
        else:
            timeout = 5

        driver_activity_info = await self._get_job_activity_info(timeout=timeout)

        resp = {"driver": dataclasses.asdict(driver_activity_info)}
        return aiohttp.web.Response(
            text=json.dumps(resp),
            content_type="application/json",
            status=aiohttp.web.HTTPOk.status_code,
        )

    async def _get_job_activity_info(self, timeout: int) -> RayActivityResponse:
        # Returns if there is Ray activity from drivers (job).
        # Drivers in namespaces that start with _ray_internal_job_info_ are not
        # considered activity.
        request = gcs_service_pb2.GetAllJobInfoRequest()
        reply = await self._gcs_job_info_stub.GetAllJobInfo(request, timeout=timeout)

        num_active_drivers = 0
        for job_table_entry in reply.job_info_list:
            is_dead = bool(job_table_entry.is_dead)
            in_internal_namespace = job_table_entry.config.ray_namespace.startswith(
                JobInfoStorageClient.JOB_DATA_KEY_PREFIX
            )
            if not is_dead and not in_internal_namespace:
                num_active_drivers += 1

        return RayActivityResponse(
            is_active=num_active_drivers > 0,
            reason=f"Number of active drivers: {num_active_drivers}"
            if num_active_drivers
            else None,
            timestamp=datetime.now().timestamp(),
        )

    def _get_job_info(self, metadata: Dict[str, str]) -> Optional[JobInfo]:
        # If a job submission ID has been added to a job, the status is
        # guaranteed to be returned.
        job_submission_id = metadata.get(JOB_ID_METADATA_KEY)
        return self._job_info_client.get_info(job_submission_id)

    async def get_job_info(self):
        """Return info for each job.  Here a job is a Ray driver."""
        request = gcs_service_pb2.GetAllJobInfoRequest()
        reply = await self._gcs_job_info_stub.GetAllJobInfo(request, timeout=5)

        jobs = {}
        for job_table_entry in reply.job_info_list:
            job_id = job_table_entry.job_id.hex()
            metadata = dict(job_table_entry.config.metadata)
            config = {
                "namespace": job_table_entry.config.ray_namespace,
                "metadata": metadata,
                "runtime_env": RuntimeEnv.deserialize(
                    job_table_entry.config.runtime_env_info.serialized_runtime_env
                ),
            }
            info = self._get_job_info(metadata)
            entry = {
                "status": None if info is None else info.status,
                "status_message": None if info is None else info.message,
                "is_dead": job_table_entry.is_dead,
                "start_time": job_table_entry.start_time,
                "end_time": job_table_entry.end_time,
                "config": config,
            }
            jobs[job_id] = entry

        return jobs

    async def get_job_submission_info(self):
        """Info for Ray job submission.  Here a job can have 0 or many drivers."""

        jobs = {}

        for job_submission_id, job_info in self._job_info_client.get_all_jobs().items():
            if job_info is not None:
                entry = {
                    "job_submission_id": job_submission_id,
                    "status": job_info.status,
                    "message": job_info.message,
                    "error_type": job_info.error_type,
                    "start_time": job_info.start_time,
                    "end_time": job_info.end_time,
                    "metadata": job_info.metadata,
                    "runtime_env": job_info.runtime_env,
                    "entrypoint": job_info.entrypoint,
                }
                jobs[job_submission_id] = entry
        return jobs

    async def get_actor_info(self):
        # TODO (Alex): GCS still needs to return actors from dead jobs.
        request = gcs_service_pb2.GetAllActorInfoRequest()
        request.show_dead_jobs = True
        reply = await self._gcs_actor_info_stub.GetAllActorInfo(request, timeout=5)
        actors = {}
        for actor_table_entry in reply.actor_table_data:
            actor_id = actor_table_entry.actor_id.hex()
            runtime_env = json.loads(actor_table_entry.serialized_runtime_env)
            entry = {
                "job_id": actor_table_entry.job_id.hex(),
                "state": gcs_pb2.ActorTableData.ActorState.Name(
                    actor_table_entry.state
                ),
                "name": actor_table_entry.name,
                "namespace": actor_table_entry.ray_namespace,
                "runtime_env": runtime_env,
                "start_time": actor_table_entry.start_time,
                "end_time": actor_table_entry.end_time,
                "is_detached": actor_table_entry.is_detached,
                "resources": dict(actor_table_entry.required_resources),
                "actor_class": actor_table_entry.class_name,
                "current_worker_id": actor_table_entry.address.worker_id.hex(),
                "current_raylet_id": actor_table_entry.address.raylet_id.hex(),
                "ip_address": actor_table_entry.address.ip_address,
                "port": actor_table_entry.address.port,
                "metadata": dict(),
            }
            actors[actor_id] = entry

            deployments = await self.get_serve_info()
            for _, deployment_info in deployments.items():
                for replica_actor_id, actor_info in deployment_info["actors"].items():
                    if replica_actor_id in actors:
                        serve_metadata = dict()
                        serve_metadata["replica_tag"] = actor_info["replica_tag"]
                        serve_metadata["deployment_name"] = deployment_info["name"]
                        serve_metadata["version"] = actor_info["version"]
                        actors[replica_actor_id]["metadata"]["serve"] = serve_metadata
        return actors

    async def get_serve_info(self) -> Dict[str, Any]:
        # Conditionally import serve to prevent ModuleNotFoundError from serve
        # dependencies when only ray[default] is installed (#17712)
        try:
            from ray.serve.constants import SERVE_CONTROLLER_NAME
            from ray.serve.controller import SNAPSHOT_KEY as SERVE_SNAPSHOT_KEY
        except Exception:
            return {}

        # Serve wraps Ray's internal KV store and specially formats the keys.
        # These are the keys we are interested in:
        # SERVE_CONTROLLER_NAME(+ optional random letters):SERVE_SNAPSHOT_KEY
        # TODO: Convert to async GRPC, if CPU usage is not a concern.
        def get_deployments():
            serve_keys = _internal_kv_list(
                SERVE_CONTROLLER_NAME, namespace=ray_constants.KV_NAMESPACE_SERVE
            )
            serve_snapshot_keys = filter(
                lambda k: SERVE_SNAPSHOT_KEY in str(k), serve_keys
            )

            deployments_per_controller: List[Dict[str, Any]] = []
            for key in serve_snapshot_keys:
                val_bytes = _internal_kv_get(
                    key, namespace=ray_constants.KV_NAMESPACE_SERVE
                ) or "{}".encode("utf-8")
                deployments_per_controller.append(json.loads(val_bytes.decode("utf-8")))
            # Merge the deployments dicts of all controllers.
            deployments: Dict[str, Any] = {
                k: v for d in deployments_per_controller for k, v in d.items()
            }
            # Replace the keys (deployment names) with their hashes to prevent
            # collisions caused by the automatic conversion to camelcase by the
            # dashboard agent.
            return {
                hashlib.sha1(name.encode()).hexdigest(): info
                for name, info in deployments.items()
            }

        return await asyncio.get_event_loop().run_in_executor(
            executor=self._thread_pool, func=get_deployments
        )

    async def get_session_name(self):
        # TODO(yic): Convert to async GRPC.
        def get_session():
            return ray.experimental.internal_kv._internal_kv_get(
                "session_name", namespace=ray_constants.KV_NAMESPACE_SESSION
            ).decode()

        return await asyncio.get_event_loop().run_in_executor(
            executor=self._thread_pool, func=get_session
        )

    async def run(self, server):
        self._gcs_job_info_stub = gcs_service_pb2_grpc.JobInfoGcsServiceStub(
            self._dashboard_head.aiogrpc_gcs_channel
        )
        self._gcs_actor_info_stub = gcs_service_pb2_grpc.ActorInfoGcsServiceStub(
            self._dashboard_head.aiogrpc_gcs_channel
        )

    @staticmethod
    def is_minimal_module():
        return False
