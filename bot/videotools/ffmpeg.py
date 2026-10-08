import gc
import json
import re
from asyncio import create_subprocess_exec, create_task
from asyncio.subprocess import PIPE
from contextlib import suppress
from os import cpu_count, path as ospath
from time import monotonic
from typing import Any

from aiofiles.os import remove, path as aiopath
from aioshutil import move
from natsort import natsorted

from .. import LOGGER
from ..helper.ext_utils.bot_utils import cmd_exec
from ..helper.ext_utils.leech_utils import get_media_info
from .session import _RES_PRESETS

threads = max(1, cpu_count() or 1)


async def get_streams(file_path: str) -> list:
    stdout, stderr, code = await cmd_exec(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            file_path,
        ]
    )
    if code != 0:
        LOGGER.warning(f"[VT] ffprobe failed for {file_path}: {stderr}")
        return []
    try:
        return json.loads(stdout).get("streams", [])
    except (TypeError, ValueError):
        LOGGER.warning(f"[VT] Invalid ffprobe output for {file_path}")
        return []


class FFMpeg:
    def __init__(self, listener):
        self._listener = listener
        self.subproc = None
        self.subsize = 0
        self._processed_bytes = 0
        self._speed_raw = 0
        self._progress_raw = 0
        self._total_time = 0
        self._eta_raw = 0
        self._started = monotonic()

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def speed_raw(self):
        return self._speed_raw

    @property
    def progress_raw(self):
        return self._progress_raw

    @property
    def eta_raw(self):
        return self._eta_raw

    def clear(self):
        self._processed_bytes = 0
        self._speed_raw = 0
        self._progress_raw = 0
        self._eta_raw = 0
        self._started = monotonic()

    async def _ffmpeg_progress(self):
        while self.subproc and self.subproc.returncode is None:
            line = await self.subproc.stdout.readline()
            if not line:
                break
            key, _, value = line.decode(errors="ignore").strip().partition("=")
            try:
                if key == "total_size" and value != "N/A":
                    self._processed_bytes = int(value)
                    elapsed = max(monotonic() - self._started, 0.001)
                    self._speed_raw = int(self._processed_bytes / elapsed)
                elif key in ("out_time_us", "out_time_ms") and value != "N/A":
                    seconds = int(value) / 1_000_000
                    if self._total_time:
                        self._progress_raw = min(100, seconds * 100 / self._total_time)
                        if seconds > 0:
                            elapsed = max(monotonic() - self._started, 0.001)
                            rate = seconds / elapsed
                            self._eta_raw = max(
                                0, int((self._total_time - seconds) / rate)
                            )
            except (TypeError, ValueError, ZeroDivisionError):
                continue

TEXT_CODECS = [
    "subrip",
    "ass",
    "ssa",
    "mov_text",
    "webvtt",
    "mpl2",
    "vplayer",
    "pjs",
    "microdvd",
    "text",
]


class VideoFFmpeg(FFMpeg):
    @staticmethod
    def _normalize_audio_codec(acodec: str) -> str:
        if acodec == "opus":
            return "libopus"
        return acodec

    @staticmethod
    def _opus_channelmap_filter(streams: list) -> str:
        for stream in streams or []:
            if stream.get("codec_type") != "audio":
                continue
            channels = stream.get("channels")
            try:
                channels = int(channels) if channels is not None else None
            except Exception:
                channels = None
            layout = (
                stream.get("channel_layout")
                or stream.get("tags", {}).get("CHANNEL_LAYOUT")
                or ""
            ).lower()
            if channels in (6, 8) or layout in ("5.1(side)", "5.1", "7.1(side)", "7.1"):
                target = "5.1" if channels == 6 or "5.1" in layout else "7.1"
                return f"channelmap=channel_layout={target}"
            break
        return ""

    def _ff(self, *args) -> list:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            *args,
        ]

    @staticmethod
    def _mp4_mux_args() -> list:
        return ["-video_track_timescale", "90000", "-movflags", "+faststart"]

    async def _run(self, cmd: list, src: str) -> bool:
        self.clear()

        try:
            self._total_time = (await get_media_info(src))[0] or 0
        except Exception:
            self._total_time = 0

        self.subsize = ospath.getsize(src) if src and ospath.isfile(src) else 0

        if self._listener.is_cancelled:
            return False

        self.subproc = await create_subprocess_exec(
            *cmd, stdout=PIPE, stderr=PIPE, start_new_session=True
        )

        stderr_log = []

        async def log_stderr():
            async for line in self.subproc.stderr:
                stderr_log.append(line.decode().strip())

        stderr_task = create_task(log_stderr())
        await self._ffmpeg_progress()

        if self._listener.is_cancelled and self.subproc.returncode is None:
            with suppress(Exception):
                self.subproc.kill()

        await stderr_task

        return_code = await self.subproc.wait()

        if return_code == 0:
            return True
        if return_code == -9:
            self._listener.is_cancelled = True
            return False

        if stderr_log:
            LOGGER.error(
                f"[VT] FFmpeg Error ({return_code}): {chr(10).join(stderr_log)}"
            )
        else:
            LOGGER.error(f"[VT] FFmpeg failed with code {return_code} — no stderr")
        return False

    @staticmethod
    async def _del(*paths):
        for p in paths:
            with suppress(Exception):
                if p and await aiopath.exists(p):
                    await remove(p)

    @staticmethod
    def _is_cover_art(stream: dict) -> bool:
        if stream.get("codec_type") != "video":
            return False
        codec = stream.get("codec_name", "").lower()
        if codec in ("mjpeg", "png", "bmp", "gif"):
            return True
        disp = stream.get("disposition", {})
        if disp.get("attached_pic", 0) == 1:
            return True
        return False

    @staticmethod
    def _safe_out(src: str, out_ext: str) -> tuple:
        base, src_ext = ospath.splitext(src)
        out = f"{base}.{out_ext}"
        if src_ext.lower() == f".{out_ext}".lower():
            tmp = f"{base}.__vttmp.{out_ext}"
            return out, tmp
        return out, None

    @classmethod
    def _resolve_output(cls, src: str, out_ext: str) -> tuple[str, str | None, str]:
        out, tmp = cls._safe_out(src, out_ext)
        return out, tmp, tmp if tmp else out

    @staticmethod
    def _safe_labeled_out(src: str, label: str, out_ext: str) -> tuple[str, str]:
        base, _ = ospath.splitext(src)
        _known = sorted(_RES_PRESETS, key=len, reverse=True)
        _pat = (
            r"(?:^|(?<=[._\s-]))("
            + "|".join(re.escape(r) for r in _known)
            + r")(?=[._\s-]|$)"
        )
        m = re.search(_pat, base)
        if m:
            new_base = base[: m.start(1)] + label + base[m.end(1) :]
            out = f"{new_base}.{out_ext}"
            tmp = f"{new_base}.__vttmp.{out_ext}"
            return out, tmp
        out = f"{base}_{label}.{out_ext}"
        tmp = f"{base}.__vttmp_{label}.{out_ext}"
        return out, tmp

    async def _video_codec_args(
        self, src: str, force_crf: int = None, preset: str = "medium"
    ) -> list:
        streams = await get_streams(src)
        video_codec = None
        bitrate = None
        profile = None
        level = None
        for stream in streams or []:
            if stream.get("codec_type") == "video" and not self._is_cover_art(stream):
                video_codec = (stream.get("codec_name") or "").lower()
                br = stream.get("bit_rate") or stream.get("tags", {}).get("BPS")
                if br:
                    try:
                        bitrate = int(br)
                    except Exception:
                        bitrate = None
                profile = stream.get("profile")
                level = stream.get("level")
                break

        target_kbps = int(bitrate // 1000) if bitrate else None

        # Select encoder matching source codec when possible
        if video_codec in ("h264", "avc1"):
            args = ["-c:v", "libx264"]
        elif video_codec in ("hevc", "h265", "x265", "libx265"):
            args = ["-c:v", "libx265"]
        elif video_codec in ("vp9", "libvpx-vp9"):
            args = ["-c:v", "libvpx-vp9"]
        elif video_codec in ("av1", "libaom-av1", "aom"):
            args = ["-c:v", "libaom-av1"]
        else:
            args = ["-c:v", "libx264"]

        # Apply profile/level if present for compatible encoders
        if profile and args[1] in ("libx264", "libx265"):
            args += ["-profile:v", profile]
        if level and args[1] in ("libx264", "libx265"):
            args += ["-level", str(level)]

        if force_crf is not None:
            # Force CRF mode for consistent quality across resolutions (multi_res)
            if args[1] == "libx264":
                args += ["-crf", str(force_crf), "-preset", preset]
            elif args[1] == "libx265":
                args += ["-x265-params", f"crf={force_crf}", "-preset", preset]
            elif args[1] == "libvpx-vp9":
                args += ["-crf", str(force_crf), "-b:v", "0"]
            else:
                args += ["-crf", str(force_crf)]
        elif target_kbps:
            # Prefer matching source bitrate to avoid quality drops after filtering.
            args += [
                "-b:v",
                f"{target_kbps}k",
                "-maxrate",
                f"{target_kbps}k",
                "-bufsize",
                f"{max(1000, target_kbps * 2)}k",
            ]
            if args[1] in ("libx264", "libx265"):
                args += ["-preset", "medium"]
        else:
            # Fallback to higher quality defaults when bitrate metadata is unavailable.
            if args[1] == "libx264":
                args += ["-crf", "18", "-preset", "medium"]
            elif args[1] == "libx265":
                args += ["-x265-params", "crf=18", "-preset", "medium"]
            elif args[1] == "libvpx-vp9":
                args += ["-crf", "18", "-b:v", "0"]
            else:
                args += ["-crf", "18"]

        return args

    async def encode(
        self,
        src: str,
        vcodec="libx264",
        acodec="aac",
        crf=23,
        preset="medium",
        resolution=None,
        fps=None,
        out_ext="mkv",
    ) -> "str | bool":
        out, tmp, write_to = self._resolve_output(src, out_ext)
        acodec = self._normalize_audio_codec(acodec)
        streams = await get_streams(src)
        opus_filter = (
            self._opus_channelmap_filter(streams) if acodec == "libopus" else ""
        )

        vf = []
        if resolution:
            vf.append(f"scale={resolution}")
        if fps:
            vf.append(f"fps={fps}")

        cmd = self._ff("-i", src, "-y", "-c:v", vcodec, "-crf", str(crf))
        if vcodec in ("libx264", "libx265"):
            cmd += ["-preset", preset]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        if opus_filter:
            cmd += ["-af", opus_filter]
        cmd += [
            "-c:a",
            acodec,
            "-map",
            "0:V",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
        ]
        cmd += [*(self._mp4_mux_args() if out_ext == "mp4" else [])]
        cmd += ["-threads", f"{threads}", write_to]

        if await self._run(cmd, src):
            await self._del(src)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def convert(
        self, src: str, out_ext="mkv", vcodec="copy", acodec="copy"
    ) -> "str | bool":
        out, tmp, write_to = self._resolve_output(src, out_ext)
        acodec = self._normalize_audio_codec(acodec)
        streams = await get_streams(src)
        has_subtitles = any(s.get("codec_type") == "subtitle" for s in (streams or []))
        needs_mov_text = has_subtitles and out_ext.lower() in ("mp4", "m4v", "mov")

        if vcodec == "copy" and acodec == "copy" and not needs_mov_text:
            cmd = self._ff(
                "-i",
                src,
                "-y",
                "-map",
                "0:V",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                "-c",
                "copy",
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                *(self._mp4_mux_args() if out_ext == "mp4" else []),
                "-threads",
                f"{threads}",
                write_to,
            )
        else:
            subtitle_args = (
                ["-c:s", "mov_text"]
                if needs_mov_text
                else (["-c:s", "copy"] if has_subtitles else [])
            )
            opus_filter = (
                self._opus_channelmap_filter(streams) if acodec == "libopus" else ""
            )
            cmd = self._ff(
                "-i",
                src,
                "-y",
                "-c:v",
                vcodec,
                "-c:a",
                acodec,
                "-map",
                "0:V",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                *subtitle_args,
                *(["-af", opus_filter] if opus_filter else []),
                *(self._mp4_mux_args() if out_ext == "mp4" else []),
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-threads",
                f"{threads}",
                write_to,
            )

        if await self._run(cmd, src):
            await self._del(src)
            if tmp:
                await move(tmp, out)
            return out

        if needs_mov_text:
            retry_cmd = self._ff(
                "-i",
                src,
                "-y",
                "-c:v",
                vcodec,
                "-c:a",
                acodec,
                "-map",
                "0:V",
                "-map",
                "0:a?",
                "-c:s",
                "none",
                "-sn",
                *(["-af", opus_filter] if opus_filter else []),
                *(self._mp4_mux_args() if out_ext == "mp4" else []),
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-threads",
                f"{threads}",
                write_to,
            )
            if await self._run(retry_cmd, src):
                await self._del(src)
                if tmp:
                    await move(tmp, out)
                return out

        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def multi_resolution(
        self,
        src: str,
        resolutions: list[tuple[str, str]],
        out_ext="mkv",
        mode="quality",
    ) -> "list[str] | bool":
        if not resolutions:
            return False
        crf = 20 if mode == "quality" else 28
        preset = "medium" if mode == "quality" else "slow"
        video_args = await self._video_codec_args(src, force_crf=crf, preset=preset)
        outputs: list[str] = []

        for res, label in resolutions:
            if self._listener.is_cancelled:
                break
            out, tmp = self._safe_labeled_out(src, label, out_ext)
            cmd = self._ff(
                "-i",
                src,
                "-y",
                "-vf",
                f"scale={res}:flags=lanczos",
                *video_args,
                "-c:a",
                "copy",
                "-c:s",
                "copy",
                "-map",
                "0:V:0",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                *(self._mp4_mux_args() if out_ext == "mp4" else []),
                "-threads",
                f"{threads}",
                tmp,
            )
            if await self._run(cmd, src):
                if await aiopath.exists(out):
                    await remove(out)
                await move(tmp, out)
                outputs.append(out)
            else:
                LOGGER.warning(f"[VT] multi_res failed: {label}")
                with suppress(Exception):
                    if await aiopath.exists(tmp):
                        await remove(tmp)

        if outputs:
            await self._del(src)
        return outputs or False

    async def _run_merge(self, cmd: list, out: str, total_time: float = 0) -> bool:
        self.clear()

        if total_time > 0:
            self._total_time = total_time

        if self._listener.is_cancelled:
            return False

        self.subproc = await create_subprocess_exec(
            *cmd, stdout=PIPE, stderr=PIPE, start_new_session=True
        )

        stderr_log = []

        async def _drain_stderr():
            async for line in self.subproc.stderr:
                decoded = line.decode().strip()
                if decoded:
                    stderr_log.append(decoded)

        stderr_task = create_task(_drain_stderr())

        await self._ffmpeg_progress()

        if self._listener.is_cancelled and self.subproc.returncode is None:
            with suppress(Exception):
                self.subproc.kill()

        await stderr_task

        return_code = await self.subproc.wait()

        if return_code == 0:
            return True
        if return_code == -9:
            self._listener.is_cancelled = True
            return False

        if stderr_log:
            LOGGER.error(
                f"[VT] Merge Error ({return_code}): {chr(10).join(stderr_log)}"
            )
        return False

    async def convert_audio(
        self, src: str, out_ext="mp3", acodec="libmp3lame", bitrate="192k"
    ) -> "str | bool":
        out, tmp, write_to = self._resolve_output(src, out_ext)
        cmd = self._ff(
            "-i",
            src,
            "-y",
            "-vn",
            "-c:a",
            acodec,
            "-b:a",
            bitrate,
            "-map_metadata",
            "0",
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(cmd, src):
            await self._del(src)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def aspect_ratio(
        self, src: str, ratio: str, out_ext: str | None = None
    ) -> "str | bool":
        ext = (out_ext or ospath.splitext(src)[1].lstrip(".") or "mkv").lstrip(".")
        out, tmp, write_to = self._resolve_output(src, ext)

        # Try copy-mode first (fast)
        copy_cmd = self._ff(
            "-i",
            src,
            "-y",
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map",
            "0:t?",
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-aspect",
            ratio,
            *(self._mp4_mux_args() if ext == "mp4" else []),
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(copy_cmd, src):
            await self._del(src)
            if tmp:
                await move(tmp, out)
            return out

        # Fallback with re-encode
        LOGGER.warning(f"[VT] Aspect ratio copy mode failed for {src}, retrying with re-encode")
        fallback_cmd = self._ff(
            "-i",
            src,
            "-y",
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map",
            "0:t?",
            "-c",
            "copy",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-map_metadata",
            "0",
            "-aspect",
            ratio,
            *(self._mp4_mux_args() if ext == "mp4" else []),
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(fallback_cmd, src):
            await self._del(src)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def merge_videos(
        self, clips: list, out_ext="mkv", output_name=None
    ) -> "str | bool":
        from aiofiles import open as aiopen

        if len(clips) < 2:
            return False

        clips = natsorted(clips)
        base_dir = ospath.dirname(clips[0])

        raw_name = output_name or ospath.splitext(ospath.basename(clips[0]))[0]
        name = (
            ospath.splitext(raw_name)[0]
            if ospath.splitext(raw_name)[1].lstrip(".").lower() == out_ext.lower()
            else raw_name
        )
        out = ospath.join(base_dir, f"{name}.{out_ext}")
        out_abs = ospath.normcase(ospath.abspath(out))
        clip_abs = {ospath.normcase(ospath.abspath(c)) for c in clips}
        write_to = (
            ospath.join(base_dir, f"{name}.__vttmp__.{out_ext}")
            if out_abs in clip_abs
            else out
        )
        list_file = ospath.join(base_dir, f"concat_list_{hash(tuple(clips)) % 100000}.txt")

        async with aiopen(list_file, "w") as f:
            lines = []
            for c in clips:
                esc = c.replace("\\", "\\\\").replace("'", "\\'")
                lines.append(f"file '{esc}'")
            concat_content = "\n".join(lines)
            await f.write(concat_content + "\n")

        LOGGER.info(f"[VT] Concat file: {list_file} with {len(clips)} clips")
        LOGGER.debug(f"[VT] Concat content:\n{concat_content}")

        total_time = 0
        for clip in clips:
            try:
                total_time += (await get_media_info(clip))[0] or 0
            except Exception:
                continue

        clip_fps = set()
        for clip in clips:
            try:
                streams = await get_streams(clip)
                for s in (streams or []):
                    if s.get("codec_type") == "video" and not self._is_cover_art(s):
                        clip_fps.add(s.get("avg_frame_rate"))
                        break
            except Exception:
                continue
        if len(clip_fps) > 1:
            LOGGER.warning(
                f"[VT] Merge: clips have mixed frame rates {sorted(clip_fps)}. "
                "Output keeps each clip's original frame durations (VFR), but the "
                "container fps flag is taken from the first clip, so players and "
                "uploaders may report the fps as changed. Convert clips to a common "
                "fps first if this matters."
            )

        if self._listener.is_cancelled:
            with suppress(Exception):
                await remove(list_file)
            return False

        # Force GC before merge to free memory
        gc.collect()

        # Use fewer threads for merge to reduce memory pressure
        merge_threads = max(1, threads // 2) if threads and threads > 1 else threads

        def _build_cmd(map_all: bool):
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-ignore_unknown",
                "-fflags",
                "+genpts+discardcorrupt",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file,
                "-y",
            ]
            if map_all:
                cmd.extend(["-map", "0", "-c", "copy"])
            else:
                cmd.extend(["-map", "0:v?", "-map", "0:a?", "-c", "copy"])
            if merge_threads:
                cmd.extend(["-threads", str(merge_threads)])
            cmd.append(write_to)
            return cmd

        try:
            # First attempt: try with all streams (-map 0)
            cmd = _build_cmd(map_all=True)
            ok = await self._run_merge(cmd, out, total_time)

            # Fallback: retry with video/audio only if full map fails
            if not ok and not self._listener.is_cancelled and await aiopath.exists(write_to):
                with suppress(Exception):
                    await remove(write_to)
                LOGGER.warning("[VT] Merge failed with all streams, retrying with video/audio only...")
                self.clear()
                self._total_time = total_time
                cmd = _build_cmd(map_all=False)
                ok = await self._run_merge(cmd, out, total_time)
                if ok:
                    LOGGER.info("[VT] Merge retry succeeded with video/audio only mapping")
        finally:
            with suppress(Exception):
                if await aiopath.exists(list_file):
                    await remove(list_file)
            gc.collect()

        if ok:
            await self._del(
                *[c for c in clips if ospath.normcase(ospath.abspath(c)) != out_abs]
            )
            if write_to != out:
                await self._del(out)
                await move(write_to, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def mux_audio(
        self, video: str, audio: str, replace=True, lang="und"
    ) -> "str | bool":
        base, ext = ospath.splitext(video)
        ext = ext or ".mkv"

        # Mobile-safe output: prefer MP4 when audio is AAC and video is H.264
        v_streams = await get_streams(video)
        video_codec = ""
        for s in (v_streams or []):
            if s.get("codec_type") == "video" and not self._is_cover_art(s):
                video_codec = (s.get("codec_name") or "").lower()
                break
        a_streams = await get_streams(audio)
        audio_codec = ""
        for s in (a_streams or []):
            if s.get("codec_type") == "audio":
                audio_codec = (s.get("codec_name") or "").lower()
                break
        prefer_mp4 = audio_codec in ("aac",) and video_codec in ("h264", "avc1", "hevc", "h265")
        out_ext = "mp4" if (prefer_mp4 and ext.lower() in (".mkv", ".mp4")) else ext.lstrip(".") or "mkv"
        out, tmp, write_to = self._resolve_output(video, out_ext)

        is_mp4 = out_ext == "mp4"
        sub_codec = "mov_text" if is_mp4 else None

        if replace:
            cmd = self._ff(
                "-i",
                video,
                "-i",
                audio,
                "-y",
                "-map",
                "0:V",
                "-map",
                "1:a",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                "-map_metadata",
                "0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                *(["-c:s", sub_codec] if sub_codec else ["-c:s", "copy"]),
                "-disposition:a:0",
                "default",
                *(self._mp4_mux_args() if is_mp4 else []),
                "-threads",
                f"{threads}",
                write_to,
            )
            # insert metadata args before the -threads/write_to items
            try:
                ti = cmd.index("-threads")
            except ValueError:
                ti = len(cmd)
            meta_args = []
            if lang:
                meta_args += ["-metadata:s:a:0", f"language={lang}"]
            if meta_args:
                cmd[ti:ti] = meta_args
        else:
            cmd = self._ff(
                "-i",
                video,
                "-i",
                audio,
                "-y",
                "-map",
                "0:V",
                "-map",
                "0:a?",
                "-map",
                "1:a",
                "-map",
                "0:s?",
                "-c",
                "copy",
                *(["-c:s", sub_codec, *self._mp4_mux_args()] if sub_codec else []),
                *(self._mp4_mux_args() if (is_mp4 and not sub_codec) else []),
                "-threads",
                f"{threads}",
                write_to,
            )

        if await self._run(cmd, video):
            await self._del(video, audio)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def _add_subtitle_track(
        self, video: str, subtitle: str, lang: str = "und"
    ) -> "str | bool":
        sub_ext = ospath.splitext(subtitle)[1].lower()
        out, tmp, write_to = self._resolve_output(video, "mkv")

        sub_ext_codec_map = {
            ".ass": "ass",
            ".ssa": "ass",
            ".vtt": "webvtt",
            ".srt": "subrip",
        }
        new_sub_codec = sub_ext_codec_map.get(sub_ext, "subrip")

        streams: list[dict[str, Any]] = await get_streams(video)
        existing_sub_count = sum(
            1 for s in (streams or []) if s.get("codec_type") == "subtitle"
        )
        new_sub_idx = existing_sub_count

        cmd = self._ff(
            "-i",
            video,
            "-i",
            subtitle,
            "-y",
            "-map",
            "0:V",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map",
            "1:s",
            "-c",
            "copy",
            f"-c:s:{new_sub_idx}",
            new_sub_codec,
            f"-metadata:s:s:{new_sub_idx}",
            f"language={lang}",
            f"-disposition:s:{new_sub_idx}",
            "default",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(cmd, video):
            await self._del(video, subtitle)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def softsub(self, video: str, subtitle: str, lang="und") -> "str | bool":
        return await self._add_subtitle_track(video, subtitle, lang)

    async def mux_all(
        self,
        video: str,
        audio: str,
        subtitle: str,
        replace_audio=True,
        lang="und",
        sub_lang="und",
    ) -> "str | bool":
        base, ext = ospath.splitext(video)
        ext = ext or ".mkv"
        out_ext = ext.lstrip(".") or "mkv"
        out, tmp, write_to = self._resolve_output(video, out_ext)

        if replace_audio:
            maps = ["-map", "0:V", "-map", "1:a", "-map", "0:a?", "-map", "2:s"]
        else:
            maps = ["-map", "0:V", "-map", "0:a?", "-map", "1:a", "-map", "2:s"]
        cmd = self._ff(
            "-i",
            video,
            "-i",
            audio,
            "-i",
            subtitle,
            "-y",
            *maps,
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-metadata:s:a:0",
            f"language={lang}",
            "-metadata:s:s:0",
            f"language={sub_lang}",
            "-disposition:a:0",
            "default",
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(cmd, video):
            await self._del(video, audio, subtitle)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    _COLOR_NAMES = {
        "white": "FFFFFF",
        "yellow": "00FFFF",
        "cyan": "FFFF00",
        "orange": "00A5FF",
        "red": "0000FF",
        "black": "000000",
    }

    _ALIGN_MAP = {
        "bottom_left": 1,
        "bottom_center": 2,
        "bottom_right": 3,
        "middle_left": 4,
        "middle_center": 5,
        "middle_right": 6,
        "top_left": 7,
        "top_center": 8,
        "top_right": 9,
    }

    async def introsub(
        self,
        video: str,
        text: str = "Intro",
        duration: int = 90,
        lang: str = "und",
        font_size: int = 20,
        font_color: str = "white",
        position: str = "bottom_center",
    ) -> "str | bool":
        base = ospath.splitext(video)[0]
        tmp_ass = f"{base}.__introsub.ass"

        hours = duration // 3600
        minutes = (duration % 3600) // 60
        secs = duration % 60
        end = f"{hours}:{minutes:02d}:{secs:02d}.00"
        color = self._COLOR_NAMES.get(font_color, "FFFFFF")
        align = self._ALIGN_MAP.get(position, 2)

        ass_content = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1920\n"
            "PlayResY: 1080\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Intro,Arial,{font_size},&H00{color},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,{align},10,10,10,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            f"Dialogue: 0,0:00:00.00,{end},Intro,,0,0,0,,{text}\n"
        )

        with open(tmp_ass, "w", encoding="utf-8") as f:
            f.write(ass_content)

        try:
            return await self._add_subtitle_track(video, tmp_ass, lang=lang)
        finally:
            with suppress(Exception):
                if await aiopath.exists(tmp_ass):
                    await remove(tmp_ass)

    async def hardsub(
        self, video: str, subtitle: str, font_size=18, font_color="white", out_ext="mkv"
    ) -> "str | bool":
        sub_ext = ospath.splitext(subtitle)[1].lower()
        out, tmp, write_to = self._resolve_output(video, out_ext)

        escaped = (
            subtitle.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace("{", "\\{")
            .replace("}", "\\}")
        )
        if sub_ext in (".ass", ".ssa"):
            vf = f"ass='{escaped}'"
        else:
            color_hex = self._COLOR_NAMES.get(font_color, "FFFFFF")
            vf = (
                f"subtitles='{escaped}'"
                f":force_style='FontSize={font_size},PrimaryColour=&H00{color_hex}'"
            )

        cmd = self._ff(
            "-i",
            video,
            "-y",
            "-vf",
            vf,
            "-map",
            "0:V:0",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            *await self._video_codec_args(video),
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(cmd, video):
            await self._del(video, subtitle)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    async def remove_subtitles(self, video: str) -> "str | bool":
        base, ext = ospath.splitext(video)
        tmp = f"{base}.__vttmp{ext}"
        cmd = self._ff(
            "-i",
            video,
            "-y",
            "-map",
            "0:V",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-threads",
            f"{threads}",
            tmp,
        )

        if await self._run(cmd, video):
            await move(tmp, video)
            return video
        with suppress(Exception):
            if await aiopath.exists(tmp):
                await remove(tmp)
        return False

    async def remove_audio(self, video: str) -> "str | bool":
        base, ext = ospath.splitext(video)
        tmp = f"{base}.__vttmp{ext}"
        cmd = self._ff(
            "-i",
            video,
            "-y",
            "-map",
            "0:V",
            "-map",
            "0:s?",
            "-map_metadata",
            "0",
            "-c",
            "copy",
            *(self._mp4_mux_args() if ext.lower() == ".mp4" else []),
            "-threads",
            f"{threads}",
            tmp,
        )

        if await self._run(cmd, video):
            await move(tmp, video)
            return video
        with suppress(Exception):
            if await aiopath.exists(tmp):
                await remove(tmp)
        return False

    async def remove_streams(
        self,
        video: str,
        remove_audio_indices: list,
        remove_sub_indices: list,
        all_streams: list,
    ) -> "str | bool":
        base, ext = ospath.splitext(video)
        tmp = f"{base}.__vttmp{ext}"

        maps = []
        for s in all_streams:
            idx = s.get("index")
            if idx is None:
                continue
            if self._is_cover_art(s):
                continue
            ct = s.get("codec_type", "")
            if ct == "audio" and idx in remove_audio_indices:
                continue
            if ct == "subtitle" and idx in remove_sub_indices:
                continue
            maps += ["-map", f"0:{idx}"]

        if not maps:
            LOGGER.error("[VT] remove_streams: nothing left to map")
            return False

        cmd = self._ff(
            "-i",
            video,
            "-y",
            *maps,
            "-c",
            "copy",
            "-map_metadata",
            "0",
            *(self._mp4_mux_args() if ext.lower() == ".mp4" else []),
            "-threads",
            f"{threads}",
            tmp,
        )

        if await self._run(cmd, video):
            await move(tmp, video)
            return video
        with suppress(Exception):
            if await aiopath.exists(tmp):
                await remove(tmp)
        return False

    async def strip_metadata(self, video: str) -> "str | bool":
        base, ext = ospath.splitext(video)
        tmp = f"{base}.__vttmp{ext}"
        cmd = self._ff(
            "-i",
            video,
            "-y",
            "-map",
            "0:V",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-c",
            "copy",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            *(self._mp4_mux_args() if ext.lower() == ".mp4" else []),
            "-threads",
            f"{threads}",
            tmp,
        )

        if await self._run(cmd, video):
            await move(tmp, video)
            return video
        with suppress(Exception):
            if await aiopath.exists(tmp):
                await remove(tmp)
        return False

    async def extract_audio(self, video: str) -> "str | bool":
        base, _ = ospath.splitext(video)
        out = f"{base}.mka"
        cmd = self._ff(
            "-i",
            video,
            "-y",
            "-vn",
            "-sn",
            "-map",
            "0:a",
            "-c",
            "copy",
            "-threads",
            f"{threads}",
            out,
        )

        if await self._run(cmd, video):
            return out
        with suppress(Exception):
            if await aiopath.exists(out):
                await remove(out)
        return False

    async def extract_subtitles(self, video: str) -> "list | bool":
        streams = await get_streams(video)
        if not streams:
            return False

        ext_map = {
            "ass": "ass",
            "ssa": "ass",
            "subrip": "srt",
            "webvtt": "vtt",
            "mov_text": "srt",
            "hdmv_pgs_subtitle": "sup",
            "dvd_subtitle": "sub",
        }
        base, _ = ospath.splitext(video)
        outputs = []
        lang_counter: dict = {}

        for s in streams:
            if s.get("codec_type") != "subtitle":
                continue
            codec = s.get("codec_name", "subrip").lower()
            lang = s.get("tags", {}).get("language", "und")
            sub_ext = ext_map.get(codec, "srt")

            key = (lang, sub_ext)
            count = lang_counter.get(key, 0)
            lang_counter[key] = count + 1
            suffix = f".{count + 1}" if count > 0 else ""
            out = f"{base}_{lang}{suffix}.{sub_ext}"

            cmd = self._ff(
                "-i",
                video,
                "-y",
                "-map",
                f"0:{s['index']}",
                "-c:s",
                "copy",
                "-threads",
                f"{threads}",
                out,
            )

            if await self._run(cmd, video):
                outputs.append(out)
            else:
                with suppress(Exception):
                    if await aiopath.exists(out):
                        await remove(out)

        return outputs or False

    async def extract_both(self, video: str) -> "list | bool":
        results = []
        a = await self.extract_audio(video)
        if a:
            results.append(a)
        s = await self.extract_subtitles(video)
        if s:
            results.extend(s)
        return results or False

    async def extract_streams(
        self, video: str, audio_indices=None, subtitle_indices=None
    ) -> "list | bool":
        audio_indices = audio_indices or []
        subtitle_indices = subtitle_indices or []
        streams = await get_streams(video)
        if not streams:
            return False

        base, _ = ospath.splitext(video)
        outputs = []

        seen_audio = set()
        seen_subs = set()
        audio_streams = []
        sub_streams = []
        for s in streams:
            ctype = s.get("codec_type")
            sidx = s.get("index")
            if ctype == "audio":
                if sidx in seen_audio:
                    continue
                seen_audio.add(sidx)
                audio_streams.append(s)
            elif ctype == "subtitle":
                if sidx in seen_subs:
                    continue
                seen_subs.add(sidx)
                sub_streams.append(s)

        audio_ext_map = {
            "aac": "aac",
            "mp3": "mp3",
            "m4a": "m4a",
            "opus": "opus",
            "flac": "flac",
            "alac": "flac",
            "ac3": "ac3",
            "eac3": "eac3",
            "dts": "dts",
            "pcm_s16le": "wav",
            "pcm_s24le": "wav",
        }
        sub_ext_map = {
            "subrip": "srt",
            "srt": "srt",
            "ass": "ass",
            "ssa": "ass",
            "webvtt": "vtt",
            "dvd_subtitle": "sup",
            "dvbsub": "sup",
            "hdmv_pgs_subtitle": "sup",
        }

        for track_idx in sorted(set(audio_indices)):
            if track_idx < 0 or track_idx >= len(audio_streams):
                continue
            stream = audio_streams[track_idx]
            stream_idx = stream.get("index")
            if stream_idx is None:
                continue
            tags = stream.get("tags") or {}
            lang = tags.get("language", "und")
            codec = (stream.get("codec_name") or "").lower()
            ext = audio_ext_map.get(codec, "mka")
            out = f"{base}.audio{track_idx}.{lang}.{ext}"
            cmd = self._ff(
                "-i",
                video,
                "-y",
                "-map",
                f"0:{stream_idx}",
                "-c",
                "copy",
                out,
            )
            if await self._run(cmd, video):
                outputs.append(out)
            else:
                # Fallback to Matroska Audio container for codec/container mismatches.
                mka_out = f"{base}.audio{track_idx}.{lang}.mka"
                mka_cmd = self._ff(
                    "-i",
                    video,
                    "-y",
                    "-map",
                    f"0:{stream_idx}",
                    "-c",
                    "copy",
                    mka_out,
                )
                if await self._run(mka_cmd, video):
                    outputs.append(mka_out)
                    with suppress(Exception):
                        if await aiopath.exists(out):
                            await remove(out)
                    continue
                with suppress(Exception):
                    if await aiopath.exists(out):
                        await remove(out)
                    if await aiopath.exists(mka_out):
                        await remove(mka_out)

        for track_idx in sorted(set(subtitle_indices)):
            if track_idx < 0 or track_idx >= len(sub_streams):
                continue
            stream = sub_streams[track_idx]
            stream_idx = stream.get("index")
            if stream_idx is None:
                continue
            tags = stream.get("tags") or {}
            lang = tags.get("language", "und")
            codec = (stream.get("codec_name") or "").lower()
            ext = sub_ext_map.get(codec, "srt")
            out = f"{base}.subtitle{track_idx}.{lang}.{ext}"
            cmd = self._ff(
                "-i",
                video,
                "-y",
                "-map",
                f"0:{stream_idx}",
                "-c",
                "copy",
                out,
            )
            if await self._run(cmd, video):
                outputs.append(out)
            else:
                with suppress(Exception):
                    if await aiopath.exists(out):
                        await remove(out)

        return outputs or False

    async def swap_audio(
        self, video: str, audio_index_a: int, audio_index_b: int, all_streams: list
    ) -> "str | bool":
        base, ext = ospath.splitext(video)
        ext_lower = ext.lower()
        tmp = f"{base}.__vttmp{ext}"

        if ext_lower in (".mp4", ".m4v", ".mov"):
            sub_codec_args = ["-c:s", "mov_text"]
        else:
            sub_codec_args = []

        order = {}
        ai = 0
        for s in all_streams:
            if s.get("codec_type") == "audio":
                order[ai] = s["index"]
                ai += 1

        if audio_index_a not in order or audio_index_b not in order:
            LOGGER.error("[VT] swap_audio: invalid audio indices")
            return False

        maps = []
        ai = 0
        for s in all_streams:
            ct = s.get("codec_type", "")
            if self._is_cover_art(s):
                continue
            if ct == "audio":
                if ai == audio_index_a:
                    maps += ["-map", f"0:{order[audio_index_b]}"]
                elif ai == audio_index_b:
                    maps += ["-map", f"0:{order[audio_index_a]}"]
                else:
                    maps += ["-map", f"0:{s['index']}"]
                ai += 1
            else:
                maps += ["-map", f"0:{s['index']}"]

        cmd = self._ff(
            "-i",
            video,
            "-y",
            *maps,
            "-c",
            "copy",
            "-map_metadata",
            "0",
            *sub_codec_args,
            *(self._mp4_mux_args() if ext_lower == ".mp4" else []),
            "-threads",
            f"{threads}",
            tmp,
        )

        if await self._run(cmd, video):
            await move(tmp, video)
            return video
        with suppress(Exception):
            if await aiopath.exists(tmp):
                await remove(tmp)
        return False

    _WM_POS = {
        "top_left": "10:10",
        "top_right": "main_w-overlay_w-10:10",
        "bottom_left": "10:main_h-overlay_h-10",
        "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
        "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
    }

    async def watermark_image(
        self, video: str, wm_image: str, position="bottom_right", opacity=0.7
    ) -> "str | bool":
        base, ext = ospath.splitext(video)
        ext_lower = ext.lower()
        out_ext = ext_lower.lstrip(".")
        out, tmp, write_to = self._resolve_output(video, out_ext)

        xy = self._WM_POS.get(position, self._WM_POS["bottom_right"])
        fc = (
            f"[1:v]scale=iw*0.15:-1,format=rgba,"
            f"colorchannelmixer=aa={opacity}[wm];"
            f"[0:v][wm]overlay={xy}[out]"
        )

        cmd = self._ff(
            "-i",
            video,
            "-i",
            wm_image,
            "-y",
            "-filter_complex",
            fc,
            "-map",
            "[out]",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            *await self._video_codec_args(video),
            *(self._mp4_mux_args() if out_ext == "mp4" else []),
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(cmd, video):
            await self._del(video)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False

    _POS_MAP = {
        "top_left": "x=10:y=10",
        "top_right": "x=w-tw-10:y=10",
        "bottom_left": "x=10:y=h-th-10",
        "bottom_right": "x=w-tw-10:y=h-th-10",
        "center": "x=(w-tw)/2:y=(h-th)/2",
    }

    async def watermark_text(
        self,
        video: str,
        text: str,
        position="bottom_right",
        font_size=24,
        font_color="white",
        opacity=0.8,
        font_file=None,
    ) -> "str | bool":
        base, ext = ospath.splitext(video)
        ext_lower = ext.lower()
        out_ext = ext_lower.lstrip(".")
        out, tmp, write_to = self._resolve_output(video, out_ext)

        xy = self._POS_MAP.get(position, self._POS_MAP["bottom_right"])
        safe = (
            text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace(",", "\\,")
            .replace("{", "\\{")
            .replace("}", "\\}")
        )
        fp = f":fontfile='{font_file}'" if font_file else ""
        vf = (
            f"drawtext=text='{safe}'{fp}:{xy}:"
            f"fontsize={font_size}:fontcolor={font_color}@{opacity}:"
            f"box=1:boxcolor=black@0.3:boxborderw=5"
        )

        cmd = self._ff(
            "-i",
            video,
            "-y",
            "-vf",
            vf,
            "-map",
            "0:V:0",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            *await self._video_codec_args(video),
            *(self._mp4_mux_args() if out_ext == "mp4" else []),
            "-threads",
            f"{threads}",
            write_to,
        )

        if await self._run(cmd, video):
            await self._del(video)
            if tmp:
                await move(tmp, out)
            return out
        with suppress(Exception):
            if await aiopath.exists(write_to):
                await remove(write_to)
        return False
