from contextlib import suppress
from math import isfinite
import os
import signal

from .. import LOGGER
from ..helper.ext_utils.bot_utils import (
    EngineStatus,
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)

ENGINE_NAME = "DzoneVT V1.0"


class VTStatus:
    def __init__(self, listener, gid: str, func_key: str, obj=None):
        self.listener = listener
        self._gid = gid
        self._func_key = func_key
        self._obj = obj
        self.upload_details = listener.upload_details
        self.message = listener.message
        self.engine = ENGINE_NAME
        self.cancel_reason = None

    def gid(self):
        return self._gid

    def name(self):
        return self.listener.name

    def size(self):
        return get_readable_file_size(self.listener.size)

    def status(self):
        return MirrorStatus.STATUS_FFMPEG

    def processed_bytes(self):
        if self._obj and hasattr(self._obj, "processed_bytes"):
            return get_readable_file_size(self._obj.processed_bytes)
        return "0B"

    def progress(self):
        if self._obj and hasattr(self._obj, "progress_raw"):
            try:
                progress = float(self._obj.progress_raw)
            except (TypeError, ValueError):
                return "0%"
            if isfinite(progress):
                return f"{max(0, min(100, progress)):.2f}%"
        return "0%"

    def speed(self):
        if self._obj and hasattr(self._obj, "speed_raw"):
            try:
                speed = max(0, int(self._obj.speed_raw or 0))
            except (TypeError, ValueError):
                speed = 0
            return f"{get_readable_file_size(speed)}/s"
        return "0B/s"

    def eta(self):
        if self._obj and getattr(self._obj, "eta_raw", 0):
            try:
                eta = int(self._obj.eta_raw)
            except (TypeError, ValueError):
                return "-"
            return get_readable_time(eta) if eta >= 0 else "-"
        return "-"

    def download(self):
        return self

    def eng(self):
        return EngineStatus().STATUS_SPLIT_MERGE

    async def cancel_task(self, reason=None):
        if self.listener.is_cancelled:
            return
        LOGGER.info(f"[VT] Cancelling {self._func_key}: {self.listener.name}")
        self.listener.is_cancelled = True
        self.cancel_reason = reason or f"Video Tool ({self._func_key}) stopped by user!"
        proc = getattr(self._obj, "subproc", None)
        if proc is not None and proc.returncode is None:
            try:
                if proc.pid:
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except Exception as error:
                LOGGER.warning(f"[VT] Failed to kill process: {error}")
                with suppress(Exception):
                    proc.kill()
        self.listener.suproc = "cancelled"
        await self.listener.onUploadError(self.cancel_reason)

    async def cancel_download(self):
        await self.cancel_task()
