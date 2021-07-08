# This file is a part of IntelOwl https://github.com/intelowlproject/IntelOwl
# See the file 'LICENSE' for copying permission.

import logging
from celery import uuid
from django.core.cache import cache

from intel_owl.settings import CELERY_QUEUES
from intel_owl.celery import app as celery_app

from ..models import Job
from ..helpers import generate_sha256, get_now
from ..exceptions import AlreadyFailedJobException
from .serializers import AnalyzerConfigSerializer
from .models import AnalyzerReport


logger = logging.getLogger(__name__)

# constants
CELERY_TASK_NAME = "run_analyzer"
ALL_ANALYZERS = "__all__"
DEFAULT_QUEUE = "default"
DEFAULT_SOFT_TIME_LIMIT = 300


def build_import_path(cls_path: str, observable_analyzer=True) -> str:
    if observable_analyzer:
        return f"api_app.analyzers_manager.observable_analyzers.{cls_path}"
    else:
        return f"api_app.analyzers_manager.file_analyzers.{cls_path}"


def build_cache_key(job_id: int):
    return f"job.{job_id}.analyzers_manager.task_ids"


def start_analyzers(
    job_id: int,
    analyzers_to_execute: list,
    runtime_configuration: dict = {},
    celery_kwargs: dict = {},
) -> dict:
    # mapping of analyzer name and task_id
    analyzer_task_id_map = {}

    # get analyzer config
    analyzers_config = AnalyzerConfigSerializer.read_and_verify_config()

    # get job
    job = Job.objects.get(pk=job_id)
    job.update_status("running")  # set job status to running

    # loop over and fire the analyzers in a celery task
    for analyzer_name in analyzers_to_execute:
        ac = analyzers_config[analyzer_name]

        if ac["disabled"] or not ac["verification"]["configured"]:
            # if disabled or unconfigured
            continue

        # get runtime_configuration if any specified for this analyzer
        runtime_conf = runtime_configuration.get(analyzer_name, {})
        # merge ac["config"] with runtime_configuration
        config_params = {
            **ac["config"],
            **runtime_conf,
        }
        # get celery queue
        queue = config_params.get("queue", DEFAULT_QUEUE)
        if queue not in CELERY_QUEUES:
            logger.warning(
                f"Analyzer {analyzers_to_execute} has a wrong queue."
                f" Setting to `{DEFAULT_QUEUE}`"
            )
            queue = DEFAULT_QUEUE
        # get soft time limit
        stl = config_params.get("soft_time_limit", DEFAULT_SOFT_TIME_LIMIT)
        # construct arguments
        args = [
            job_id,
            {
                **ac,
                "name": analyzer_name,
                "config": config_params,
            },
        ]
        # gen new task_id
        task_id = uuid()
        # add to map
        analyzer_task_id_map[analyzer_name] = task_id
        # run analyzer with a celery task asynchronously
        celery_app.send_task(
            CELERY_TASK_NAME,
            args=args,
            kwargs={"job_id": job_id, "runtime_conf": runtime_conf, **celery_kwargs},
            queue=queue,
            soft_time_limit=stl,
            task_id=task_id,
        )

    # cache the task ids
    cache.set(build_cache_key(job_id), analyzer_task_id_map.values())

    return analyzer_task_id_map


def job_cleanup(job_id: int):
    job: Job = Job.objects.get(pk=job_id)
    logger.info(f"STARTING set_report_and_cleanup for <-- {repr(job)}.")
    status_to_set = "failed"

    try:
        if job.status == "failed":
            raise AlreadyFailedJobException()

        analysis_reports = job.analyzer_reports.all()
        num_analysis_reports = len(analysis_reports)
        num_analyzers_to_execute = len(job.analyzers_to_execute)
        logger.info(
            f"REPORT: num analysis reports:{num_analysis_reports}, "
            f"num analyzer to execute:{num_analyzers_to_execute}"
            f" <-- {repr(job)}."
        )

        # check if it was the last analysis...
        # ..In case, set the analysis as "reported" or "failed"
        if num_analysis_reports == num_analyzers_to_execute:
            status_to_set = "reported_without_fails"
            # set status "failed" in case all analyzers failed
            failed_analyzers = 0
            for analysis_report in analysis_reports:
                if analysis_report.status != analysis_report.Statuses.SUCCESS.name:
                    failed_analyzers += 1

            if failed_analyzers == num_analysis_reports:
                status_to_set = "failed"
            elif failed_analyzers >= 1:
                status_to_set = "reported_with_fails"

    except AlreadyFailedJobException:
        logger.error(f"job_id {job_id} status failed. Do not process the report")

    except Exception as e:
        logger.exception(f"job_id: {job_id}, Error: {e}")
        job.append_error(str(e), save=False)

    finally:
        if not (job.status == "failed" and job.finished_analysis_time):
            job.finished_analysis_time = get_now()
        job.status = status_to_set
        job.save(update_fields=["status", "errros", "finished_analysis_time"])


def set_failed_analyzer(job_id: int, analyzer_name: str, err_msg):
    status = AnalyzerReport.Statuses.FAILED.name
    logger.warning(
        f"({analyzer_name}, job_id #{job_id}) -> set as {status}. ",
        f" Error: {err_msg}",
    )
    report = AnalyzerReport.objects.create(
        job_id=job_id,
        analyzer_name=analyzer_name,
        report={},
        errors=[err_msg],
        status=status,
    )
    job_cleanup(job_id)
    return report


def kill_running_analysis(job_id: int) -> None:
    key = build_cache_key(job_id)
    task_ids = cache.get(key)
    if isinstance(task_ids, list):
        celery_app.control.revoke(task_ids)
        cache.delete(key)
