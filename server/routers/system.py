"""System-level endpoints: diagnostic log bundle download.

系统级端点：诊断日志打包下载。"""

from __future__ import annotations

import tempfile
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from lib.infra.async_thread import run_sync_transaction
from lib.infra.data_root_layout import DataRootLayout
from server.i18n import Translator
from server.services.system.diagnostics import collect_diagnostics

router = APIRouter()

_MAX_FILE_BYTES = 100 * 1024 * 1024
_SPOOL_MAX = 50 * 1024 * 1024
_LOG_GLOB = "arcreel.log*"


def _write_diagnostics_zip(spooled: IO[bytes], log_dir: Path) -> None:
    """Pack the logs and diagnostic info under logs/ into a zip written to ``spooled``, and seek back to the start when
    done.

    把 logs/ 下的日志与诊断信息打成 zip 写入 ``spooled``，写完回到开头。"""
    diagnostics_lines: list[str] = []
    with zipfile.ZipFile(spooled, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if log_dir.exists():
            for path in sorted(log_dir.glob(_LOG_GLOB)):
                # Skip symlinks: prevents someone placing a symlink under logs/ that points to a sensitive file
                # outside the directory and leaking it through the diagnostic bundle.
                # 跳过 symlink：防止有人在 logs/ 下放符号链接指向目录外的敏感文件，
                # 通过诊断包外泄。
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                if size > _MAX_FILE_BYTES:
                    diagnostics_lines.append(f"[skipped: too large: {path.name} ({size} bytes)]")
                    continue
                zf.write(path, arcname=f"logs/{path.name}")

        diagnostics_text = collect_diagnostics()
        if diagnostics_lines:
            diagnostics_text += "\n" + "\n".join(diagnostics_lines) + "\n"
        zf.writestr("diagnostics.txt", diagnostics_text)

    spooled.seek(0)


@router.get("/system/logs/download")
async def download_logs(_t: Translator) -> StreamingResponse:
    """打包返回 logs/ 目录所有文件 + diagnostics.txt。"""
    log_dir = DataRootLayout.current().log_dir

    # The handle must outlive this function and is closed only after StreamingResponse finishes streaming it, so it
    # cannot go into a with block
    spooled = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX)  # noqa: SIM115 -- 句柄要活过本函数、随 StreamingResponse 流式读完才关，不能收进 with
    try:
        # Packing and diagnostic collection (subprocesses, reading logs) run in a thread; on cancellation, wait for
        # the thread to finish writing before closing the handle
        # 打包与诊断采集（子进程、读日志）在线程里完成；取消时等线程写完再关句柄
        await run_sync_transaction(_write_diagnostics_zip, spooled, log_dir)
    except BaseException:
        spooled.close()
        raise

    ts = datetime.now(UTC).strftime("%Y-%m-%d-%H%MZ")
    filename = f"arcreel-diagnostics-{ts}.zip"

    def _iter() -> Iterator[bytes]:
        try:
            while chunk := spooled.read(64 * 1024):
                yield chunk
        finally:
            spooled.close()

    return StreamingResponse(
        _iter(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
