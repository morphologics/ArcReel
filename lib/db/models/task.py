"""Task queue ORM models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from lib.db.base import Base, UserOwnedMixin


class Task(UserOwnedMixin, Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.batch_id"), index=True)
    project_name: Mapped[str] = mapped_column(String, nullable=False)
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    media_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    # Only written by image_edit tasks (other task types already distinguish resource kinds via task_type, no need for this column):
    # included in the dedupe key so active tasks of different asset types with the same name (e.g. a character and a prop both called "jade pendant") are not mistaken as duplicates.
    # 仅 image_edit 任务写入（其余任务类型 task_type 本身已按资源种类区分，无需此列）：
    # 纳入去重键，避免不同资产类型同名（如角色和道具都叫「玉佩」）时活动任务互相误判去重。
    resource_type: Mapped[str | None] = mapped_column(String)
    script_file: Mapped[str | None] = mapped_column(String)
    payload_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String, nullable=False, server_default="webui")
    dependency_task_id: Mapped[str | None] = mapped_column(String)
    dependency_group: Mapped[str | None] = mapped_column(String)
    dependency_index: Mapped[int | None] = mapped_column(Integer)
    cancelled_by: Mapped[str | None] = mapped_column(String)
    provider_id: Mapped[str | None] = mapped_column(String)
    provider_job_id: Mapped[str | None] = mapped_column(String)
    # Protocol identifier (protocol dimension) used when submitting this provider job; only custom providers have this dimension, always NULL for built-in providers.
    # Written together with provider_job_id, recording which protocol this provider job was submitted with, for attribution
    # when troubleshooting. Does not store the request domain. The resume protocol check does not read this column: that
    # gate's authority is endpoint_guard in execution_checkpoint_json.
    # 提交该供应商任务时所用的协议标识（协议维度），只有自定义供应商有这个维度，内置供应商恒 NULL。
    # 与 provider_job_id 同一次写入落地，记录这笔供应商任务按哪套协议提交，供排障时归因。不存请求
    # 域名。续跑的协议比对不读这一列——那道闸的权威是 execution_checkpoint_json 里的 endpoint_guard。
    provider_endpoint: Mapped[str | None] = mapped_column(String)
    # Domain actually requested when submitting this provider job (connection dimension), for both kinds of providers, written together with provider_job_id.
    # The domain changes with user config; resume replays polling against the original domain so old jobs are not polled on a new domain and not found.
    # 提交该供应商任务时实际请求的域名（连接维度），两类供应商通用，与 provider_job_id 同一次
    # 写入落地。域名随用户配置变化，续跑据此回放原域名轮询，避免按新域名轮旧任务查无。
    submitted_base_url: Mapped[str | None] = mapped_column(String)
    # Strict execution facts frozen before the first reference-to-video submission. A separate column avoids mixing with the
    # mutable enqueue payload and lets the checkpoint/job combination branch unambiguously on restart; consumed only inside the worker, not part of the tasks API contract.
    # 参考生视频首次提交前冻结的严格执行事实。独立列避免与可变 enqueue payload 混合，且让
    # checkpoint/job 组合在重启时可无歧义分流；只由 worker 内部消费，不属于 tasks API 契约。
    execution_checkpoint_json: Mapped[str | None] = mapped_column(Text)
    # The inputs the task was submitted with, taken at enqueue for media generation tasks
    # (``lib.generation.request_snapshot``): the editable prompt, the declared options and model, the reference inputs
    # with their versions. Kept so an attempt refused or cancelled before any provider call still has its inputs; the
    # prompt actually sent is in ``api_calls.prompt``. NULL for tasks queued before the column existed, for text /
    # render tasks, and when taking the snapshot failed (enqueue never fails for it).
    # 任务提交时的输入，媒体生成任务在入队时取下（``lib.generation.request_snapshot``）：可编辑提示词、声明的选项与模型、
    # 参考输入及其版本。留着它，在任何供应商调用之前被拒绝或取消的尝试也有输入；实际发出的提示词在 ``api_calls.prompt``。
    # 该列出现之前入队的任务、文本 / 渲染任务、以及取快照失败时为 NULL（入队绝不因它失败）。
    request_snapshot_json: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_tasks_status_queued_at", "status", "queued_at"),
        Index("idx_tasks_project_updated_at", "project_name", "updated_at"),
        # Changed-since reads across projects (``GET /tasks?updated_since=``, generation records).
        # 跨项目的「自某时刻以来的变化」读取（``GET /tasks?updated_since=``、生成记录）。
        Index("idx_tasks_updated_at", "updated_at"),
        # A project's generation records, newest first. / 一个项目的生成记录，新的在前。
        Index("idx_tasks_project_queued_at", "project_name", "queued_at"),
        Index("idx_tasks_dependency_task_id", "dependency_task_id"),
        Index("idx_tasks_status_provider_queued", "status", "provider_id", "queued_at"),
        Index(
            "idx_tasks_dedupe_active",
            "project_name",
            "user_id",
            "task_type",
            "resource_id",
            text("COALESCE(script_file, '')"),
            text("COALESCE(resource_type, '')"),
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


class GenerationBatch(UserOwnedMixin, Base):
    __tablename__ = "batches"

    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_name: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    requested_json: Mapped[str] = mapped_column(Text, nullable=False)
    blocked_json: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BatchTask(Base):
    __tablename__ = "batch_tasks"

    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.batch_id", ondelete="CASCADE"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), primary_key=True)
    unit_id: Mapped[str] = mapped_column(String, primary_key=True)
    deduped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (UniqueConstraint("batch_id", "unit_id", name="uq_batch_tasks_batch_unit"),)


class WorkerLease(Base):
    __tablename__ = "worker_lease"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
