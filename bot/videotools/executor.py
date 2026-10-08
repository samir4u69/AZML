import re
from os import path as ospath, listdir
from time import monotonic

from aiofiles.os import makedirs as aio_makedirs, path as aiopath, rename as aio_rename
from natsort import natsorted

from .. import (
    LOGGER,
    bot,
    config_dict,
    download_dict as task_dict,
    download_dict_lock as task_dict_lock,
)
from ..helper.ext_utils.bot_utils import sync_to_async
from ..helper.ext_utils.leech_utils import get_document_type
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from .ffmpeg import VideoFFmpeg, get_streams
from .menus import MergeGroupSelector, _main_menu, _stream_menu
from .session import (
    VT_FUNCS,
    VTSession,
    _collect_files,
    _is_sub,
    _uv,
    _RES_PRESETS,
    _sessions,
    _register_menu_msg,
    _unregister_menu_msg,
)
from .status import VTStatus


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_lang(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return "und"
    if " - " in raw:
        raw = raw.split(" - ", 1)[0].strip()
    aliases = {
        "jp": "jpn",
        "zh": "zho",
        "en": "eng",
        "hi": "hin",
    }
    return aliases.get(raw, raw)


async def _get_media_type(sess: VTSession, file_path: str):
    cached = sess.media_type_cache.get(file_path)
    if cached is None:
        cached = await get_document_type(file_path)
        sess.media_type_cache[file_path] = cached
    return cached


def _require_same_count(label: str, *groups: list[str]) -> bool:
    sizes = {len(group) for group in groups}
    if len(sizes) > 1:
        LOGGER.error(
            f"[VT] {label}: mismatched file counts: {' / '.join(str(len(group)) for group in groups)}"
        )
        return False
    return True


def _stream_counts(streams: list) -> tuple[int, int]:
    audio_count = 0
    subtitle_count = 0
    seen = set()
    for stream in streams or []:
        raw_index = stream.get("index")
        if raw_index is not None and raw_index in seen:
            continue
        if raw_index is not None:
            seen.add(raw_index)
        ctype = stream.get("codec_type")
        if ctype == "audio":
            audio_count += 1
        elif ctype == "subtitle":
            subtitle_count += 1
    return audio_count, subtitle_count


async def _run_processing(sess: VTSession, ff: VideoFFmpeg) -> list:
    fkey = sess.func_key
    files = sess.files
    uid = sess.user_id

    def uv(k):
        return _uv(uid, k)

    results = []

    if fkey == "encode":
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            r = await ff.encode(
                f,
                vcodec=uv("VT_VCODEC"),
                acodec=uv("VT_ACODEC"),
                crf=_safe_int(uv("VT_CRF"), 23),
                preset=uv("VT_PRESET"),
                resolution=uv("VT_RES") or None,
                fps=_safe_int(uv("VT_FPS"), None) if uv("VT_FPS") else None,
                out_ext=uv("VT_EXT") or "mkv",
            )
            if r:
                results.append(r)

    elif fkey == "convert":
        for f in files:
            is_v, is_a, _ = await _get_media_type(sess, f)
            if not (is_v or is_a):
                continue
            r = await ff.convert(
                f,
                out_ext=uv("VT_CONV_EXT") or "mkv",
                vcodec=uv("VT_CONV_VCODEC") or "copy",
                acodec=uv("VT_CONV_ACODEC") or "copy",
            )
            if r:
                results.append(r)

    elif fkey == "multi_res":
        sel_str = uv("VT_MRES_LIST")
        selected = [x.strip() for x in sel_str.split(",") if x.strip()]
        res_list = [(sc, lb) for lb, sc in _RES_PRESETS.items() if lb in selected]
        if not res_list:
            res_list = [
                ("1920:1080", "1080p"),
                ("1280:720", "720p"),
                ("854:480", "480p"),
            ]
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            mode = uv("VT_MRES_MODE") or "quality"
            rs = await ff.multi_resolution(
                f, res_list, out_ext=uv("VT_EXT") or "mkv", mode=mode
            )
            if rs:
                results.extend(rs)
            else:
                LOGGER.warning(f"[VT] multi_res produced no output for: {f}")

    elif fkey == "merge_video":
        video_files = []
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if is_v:
                video_files.append(f)
        groups = sess.merge_groups or [video_files]
        multi_group = len(groups) > 1
        for idx, grp in enumerate(groups):
            grp = natsorted(grp)
            if sess.merge_name and multi_group:
                out_name = f"{sess.merge_name} Part {idx + 1}"
            elif sess.merge_name:
                out_name = sess.merge_name
            else:
                out_name = None

            if len(grp) >= 2:
                r = await ff.merge_videos(grp, output_name=out_name)
                if r:
                    results.append(r)
            elif grp:
                src = grp[0]
                if out_name:
                    ext = ospath.splitext(src)[1]
                    dest = ospath.join(ospath.dirname(src), f"{out_name}{ext}")
                    try:
                        await aio_rename(src, dest)
                        results.append(dest)
                    except Exception as e:
                        LOGGER.warning(f"[VT] merge_video rename failed: {e}")
                        results.append(src)
                else:
                    results.append(src)

    elif fkey == "mux_audio":
        video_files = []
        audio_files = []
        for f in files:
            if _is_sub(f):
                continue
            is_v, is_a, _ = await _get_media_type(sess, f)
            if is_v:
                video_files.append(f)
            elif is_a:
                audio_files.append(f)

        if not video_files or not audio_files:
            LOGGER.warning(
                "[VT] mux_audio: need at least 1 video file and 1 audio file"
            )
            return results
        if not _require_same_count("mux_audio", video_files, audio_files):
            return results
        for vid, aud in zip(video_files, audio_files):
            r = await ff.mux_audio(
                vid,
                aud,
                replace=(uv("VT_MUX_REPLACE") or "true") == "true",
                lang=_normalize_lang(uv("VT_ALL_LANG")),
            )
            if r:
                results.append(r)

    elif fkey == "softsub":
        vf = [f for f in files if not _is_sub(f)]
        sf = [f for f in files if _is_sub(f)]
        if not _require_same_count("softsub", vf, sf):
            return results
        for vid, sub in zip(vf, sf):
            r = await ff.softsub(vid, sub, lang=_normalize_lang(uv("VT_SUB_LANG")))
            if r:
                results.append(r)

    elif fkey == "mux_all":
        sf = [f for f in files if _is_sub(f)]
        non_sub = [f for f in files if not _is_sub(f)]
        video_files = []
        audio_files = []
        for f in non_sub:
            is_v, is_a, _ = await _get_media_type(sess, f)
            if is_v:
                video_files.append(f)
            elif is_a:
                audio_files.append(f)
        if not video_files or not audio_files or not sf:
            LOGGER.error(
                f"[VT] mux_all: Need 1 video, 1 audio, 1 subtitle. Got: {len(video_files)}V / {len(audio_files)}A / {len(sf)}S"
            )
        elif not _require_same_count("mux_all", video_files, audio_files, sf):
            return results
        else:
            for vid, aud, sub in zip(video_files, audio_files, sf):
                r = await ff.mux_all(
                    vid,
                    aud,
                    sub,
                    lang=_normalize_lang(uv("VT_ALL_LANG")),
                    sub_lang=_normalize_lang(uv("VT_ALL_SUB_LANG")),
                )
                if r:
                    results.append(r)

    elif fkey == "introsub":
        vf = [f for f in files if not _is_sub(f)]
        _txt = uv("VT_ISUB_TEXT") or "Intro"
        _lang = uv("VT_ISUB_LANG") or "und"
        _dur = _safe_int(uv("VT_ISUB_DURATION"), 90)
        _size = _safe_int(uv("VT_ISUB_SIZE"), 20)
        _color = uv("VT_ISUB_COLOR") or "white"
        _pos = uv("VT_ISUB_POS") or "bottom_center"
        for vid in vf:
            r = await ff.introsub(
                vid,
                text=_txt,
                duration=_dur,
                lang=_lang,
                font_size=_size,
                font_color=_color,
                position=_pos,
            )
            if r:
                results.append(r)

    elif fkey == "hardsub":
        vf = [f for f in files if not _is_sub(f)]
        sf = [f for f in files if _is_sub(f)]
        if not _require_same_count("hardsub", vf, sf):
            return results
        for vid, sub in zip(vf, sf):
            r = await ff.hardsub(
                vid,
                sub,
                font_size=_safe_int(uv("VT_HS_SIZE"), 18),
                font_color=uv("VT_HS_COLOR") or "white",
                out_ext=uv("VT_HS_EXT") or "mkv",
            )
            if r:
                results.append(r)

    elif fkey == "rm_sub":
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            r = await ff.remove_subtitles(f)
            if r:
                results.append(r)

    elif fkey == "rm_audio":
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            r = await ff.remove_audio(f)
            if r:
                results.append(r)

    elif fkey == "rm_streams":
        ref_audio_count, ref_sub_count = _stream_counts(sess.streams_info)
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            streams = await get_streams(f)
            if not streams:
                continue
            audio_count, sub_count = _stream_counts(streams)
            if (audio_count, sub_count) != (ref_audio_count, ref_sub_count):
                LOGGER.warning(
                    f"[VT] rm_streams: skipping {f} because its stream layout differs from the selected reference file"
                )
                continue
            ai = si = 0
            rm_audio_idx = []
            rm_sub_idx = []
            for s in streams:
                ct = s.get("codec_type", "")
                if ct == "audio":
                    if ai in sess.remove_audio_idx:
                        rm_audio_idx.append(s["index"])
                    ai += 1
                elif ct == "subtitle":
                    if si in sess.remove_sub_idx:
                        rm_sub_idx.append(s["index"])
                    si += 1
            r = await ff.remove_streams(f, rm_audio_idx, rm_sub_idx, streams)
            if r:
                results.append(r)

    elif fkey == "strip_metadata":
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            r = await ff.strip_metadata(f)
            if r:
                results.append(r)

    elif fkey == "extract":
        ref_audio_count, ref_sub_count = _stream_counts(sess.streams_info)
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            if sess.extract_use_selection:
                streams = await get_streams(f)
                if not streams:
                    continue
                audio_count, sub_count = _stream_counts(streams)
                if (audio_count, sub_count) != (ref_audio_count, ref_sub_count):
                    LOGGER.warning(
                        f"[VT] extract: skipping {f} because its stream layout differs from the selected reference file"
                    )
                    continue
                if not sess.extract_audio_idx and not sess.extract_sub_idx:
                    LOGGER.warning("[VT] extract: no streams selected by user.")
                    continue
                rs = await ff.extract_streams(
                    f,
                    audio_indices=sorted(sess.extract_audio_idx),
                    subtitle_indices=sorted(sess.extract_sub_idx),
                )
                if rs:
                    results.extend(rs)
                continue

            extracted = []
            rs = await ff.extract_audio(f)
            if rs:
                extracted.append(rs)
            ss = await ff.extract_subtitles(f)
            if ss:
                extracted.extend(ss)
            if extracted:
                results.extend(extracted)

    elif fkey == "swap_audio":
        a_idx = sess.swap_a_idx
        b_idx = sess.swap_b_idx
        if a_idx is None or b_idx is None:
            LOGGER.error("[VT] swap_audio: indices not set")
            return results
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            streams = await get_streams(f)
            if not streams:
                continue
            audio_count, _ = _stream_counts(streams)
            if max(a_idx, b_idx) >= audio_count:
                LOGGER.warning(
                    f"[VT] swap_audio: skipping {f} because it has only {audio_count} audio track(s)"
                )
                continue
            r = await ff.swap_audio(f, a_idx, b_idx, streams)
            if r:
                results.append(r)

    elif fkey == "watermark":
        wt = uv("VT_WM_TYPE") or "text"
        wm_user_img = uv("VT_WM_IMAGE") or ""
        wm_user_img = (
            wm_user_img if wm_user_img and await aiopath.exists(wm_user_img) else ""
        )
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            if wt == "image":
                img = wm_user_img
                if not img:
                    base_dir = ospath.dirname(f)
                    img = next(
                        (
                            ospath.join(base_dir, fn)
                            for fn in listdir(base_dir)
                            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                        ),
                        None,
                    )
                r = (
                    await ff.watermark_image(
                        f,
                        img,
                        position=uv("VT_WM_POS") or "bottom_right",
                        opacity=_safe_float(uv("VT_WM_OPACITY"), 0.8),
                    )
                    if img
                    else False
                )
            else:
                r = await ff.watermark_text(
                    f,
                    text=uv("VT_WM_TEXT") or "Downloader Zone",
                    font_file=uv("VT_WM_FONT") or None,
                    position=uv("VT_WM_POS") or "bottom_right",
                    font_size=_safe_int(uv("VT_WM_SIZE"), 24),
                    font_color=uv("VT_WM_COLOR") or "white",
                    opacity=_safe_float(uv("VT_WM_OPACITY"), 0.8),
                )
            if r:
                results.append(r)

    elif fkey == "convert_audio":
        ext = uv("VT_CONV_AUDIO_EXT") or "mp3"
        bitrate = uv("VT_CONV_AUDIO_BITRATE") or "192k"
        codec_map = {
            "mp3": "libmp3lame",
            "aac": "aac",
            "flac": "flac",
            "ogg": "libvorbis",
            "opus": "libopus",
            "wav": "pcm_s16le",
            "m4a": "aac",
            "wma": "wmav2",
        }
        acodec = codec_map.get(ext, "libmp3lame")
        if bitrate == "lossless":
            for f in files:
                is_v, is_a, _ = await _get_media_type(sess, f)
                if not is_a or is_v:
                    continue
                r = await ff.convert_audio(f, out_ext=ext, acodec=acodec, bitrate="320k")
                if r:
                    results.append(r)
        else:
            for f in files:
                is_v, is_a, _ = await _get_media_type(sess, f)
                if not is_a or is_v:
                    continue
                r = await ff.convert_audio(f, out_ext=ext, acodec=acodec, bitrate=bitrate)
                if r:
                    results.append(r)

    elif fkey == "aspect_ratio":
        ratio = uv("VT_ASPECT_RATIO_VALUE") or "16:9"
        for f in files:
            is_v, _, _ = await _get_media_type(sess, f)
            if not is_v:
                continue
            r = await ff.aspect_ratio(f, ratio=ratio)
            if r:
                results.append(r)

    return results


async def video_tool_pre_config(listener) -> bool:
    if config_dict.get("DISABLE_VT", False):
        return False

    sess = VTSession(listener, None, None)
    sess.pre_config = True
    sess.selection_deadline = monotonic() + 600
    _sessions[listener.mid] = sess

    try:
        text, btns = await _main_menu(sess)
        sess.menu_msg = await send_message(listener.message, text, btns)
        _register_menu_msg(sess.menu_msg.id, listener.mid)
        ok = await sess.wait(600)
        if not ok:
            listener.is_cancelled = True
            u = listener.message.from_user
            tag = u.mention(style="html") if u else "User"
            await edit_message(
                sess.menu_msg,
                f"⌬ <b>Video Tools</b>\n<b>├</b> {tag}\n<b>╰</b> Selection timed out.",
            )
            LOGGER.warning(f"[VT] pre-config timed out for {listener.name}")
            return False
        if listener.is_cancelled or not sess.func_key:
            await edit_message(
                sess.menu_msg, "⌬ <b>Video Tools</b>\n\nCancelled.", None
            )
            return False
        listener.vt_func_key = sess.func_key
        await delete_message(sess.menu_msg)
        return True
    finally:
        if sess.menu_msg:
            _unregister_menu_msg(sess.menu_msg.id)
        _sessions.pop(listener.mid, None)


async def proceed_video_tool(listener, dl_path: str, gid: str) -> str:
    if config_dict.get("DISABLE_VT", False):
        return dl_path

    func_key = getattr(listener, "vt_func_key", None)
    if not func_key:
        LOGGER.warning("[VT] proceed_video_tool: vt_func_key not set.")
        return dl_path

    if not listener.message or not getattr(listener.message, "chat", None):
        LOGGER.error(
            "[VT] proceed_video_tool: listener.message or message.chat is None — skipping VT."
        )
        return dl_path

    async def _abort_after_prompt(text: str) -> str:
        await send_message(listener.message, text)
        listener.is_cancelled = True
        return dl_path

    requested_name = (getattr(listener, "requested_name", "") or "").strip()
    folder_default = (getattr(listener, "folder_name", "") or "").strip("/")
    preferred_base = (
        ospath.splitext(ospath.basename(requested_name))[0]
        if requested_name
        else (ospath.basename(folder_default) if folder_default else "")
    )

    async def _rename_outputs(out_paths: list[str]) -> list[str]:
        if not preferred_base:
            return out_paths
        renamed = []
        total = len(out_paths)
        for idx, path in enumerate(out_paths, start=1):
            if not path or not await aiopath.exists(path):
                continue
            dirp = ospath.dirname(path)
            stem, ext = ospath.splitext(ospath.basename(path))
            dst = ""
            if func_key == "multi_res":
                res_labels = sorted(_RES_PRESETS, key=len, reverse=True)
                pat = (
                    r"(?:^|(?<=[._\s-]))("
                    + "|".join(re.escape(r) for r in res_labels)
                    + r")(?=[._\s-]|$)"
                )
                m = re.search(pat, stem)
                if m:
                    new_res = m.group(1)
                    bm = re.search(pat, preferred_base)
                    if bm:
                        new_base = (
                            preferred_base[: bm.start(1)]
                            + new_res
                            + preferred_base[bm.end(1) :]
                        )
                        dst = ospath.join(dirp, f"{new_base}{ext}")
                    else:
                        dst = ospath.join(dirp, f"{preferred_base}_{new_res}{ext}")
                else:
                    for q in res_labels:
                        if stem.endswith(f"_{q}"):
                            dst = ospath.join(dirp, f"{preferred_base}_{q}{ext}")
                            break
                    if not dst:
                        dst = ospath.join(dirp, f"{preferred_base}{ext}")
            elif total > 1:
                dst = ospath.join(dirp, f"{preferred_base}_{idx}{ext}")
            else:
                dst = ospath.join(dirp, f"{preferred_base}{ext}")
            if ospath.normpath(dst) == ospath.normpath(path):
                renamed.append(path)
                continue
            if await aiopath.exists(dst):
                mid_part = f"_{listener.mid}"
                base_dst, dst_ext = ospath.splitext(dst)
                dst = f"{base_dst}{mid_part}{dst_ext}"
            await aio_rename(path, dst)
            renamed.append(dst)
        return renamed if renamed else out_paths

    sess = VTSession(listener, dl_path, gid)
    sess.func_key = func_key
    sess.files = await _collect_files(dl_path)
    sess.selection_deadline = monotonic() + 600
    for f in sess.files:
        if f not in sess.media_type_cache:
            sess.media_type_cache[f] = await get_document_type(f)
    _sessions[listener.mid] = sess

    if not sess.files:
        LOGGER.warning(f"[VT] No media files found in: {dl_path}")
        return dl_path

    LOGGER.info(f"[VT] {func_key} | {len(sess.files)} file(s) | {dl_path}")

    if (
        func_key == "watermark"
        and (_uv(listener.user_id, "VT_WM_TYPE") or "text") == "image"
    ):
        user_img = _uv(listener.user_id, "VT_WM_IMAGE") or ""
        has_user_img = bool(user_img and await aiopath.exists(user_img))
        has_local_img = False
        if not has_user_img:
            for f in sess.files:
                base_dir = ospath.dirname(f)
                has_local_img = any(
                    fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                    for fn in await sync_to_async(listdir, base_dir)
                )
                if has_local_img:
                    break
        if not has_user_img and not has_local_img:
            return await _abort_after_prompt(
                "⌬ <b>Video Tools</b>\n\nPlease set the following settings first:\n• Wm Image\n\nGo to Settings → Video Tools Settings → Watermark Settings to configure."
            )

    try:
        video_files = [
            f
            for f in sess.files
            if sess.media_type_cache.get(f, (False, False, False))[0]
        ]
        if func_key == "rm_streams":
            if not video_files:
                LOGGER.error("[VT] rm_streams: no video files found.")
                return dl_path
            streams = await get_streams(video_files[0])
            if not streams:
                LOGGER.error("[VT] rm_streams: cannot read streams.")
                return dl_path
            sess.streams_info = streams
            sess.menu_msg = await send_message(
                listener.message, "⌬ <b>Remove Streams</b>\n\nLoading Stream Info…"
            )
            _register_menu_msg(sess.menu_msg.id, listener.mid)
            await _stream_menu(sess, mode="rm")
            ok = await sess.wait(600)
            if not ok:
                listener.is_cancelled = True
                LOGGER.warning(f"[VT] rm_streams timed out for {listener.name}")
                await listener.on_upload_error(
                    "Selection timed out.", is_cancelled=True
                )
                return dl_path
            if listener.is_cancelled or not sess.func_key:
                await listener.on_upload_error("Cancelled.", is_cancelled=True)
                return dl_path

        elif func_key == "extract":
            if not video_files:
                LOGGER.error("[VT] extract: no video files found.")
                return dl_path
            streams = await get_streams(video_files[0])
            if not streams:
                LOGGER.error("[VT] extract: cannot read streams.")
                return dl_path
            sess.streams_info = streams
            sess.extract_use_selection = True
            sess.extract_audio_idx = set()
            sess.extract_sub_idx = set()
            extract_mode = _uv(listener.user_id, "VT_EXTRACT_MODE")
            sess.extract_mode = extract_mode
            sess.menu_msg = await send_message(
                listener.message,
                "⌬ <b>Extract Subs/Audio</b>\n\nSelect tracks to extract, then confirm.",
            )
            _register_menu_msg(sess.menu_msg.id, listener.mid)
            await _stream_menu(sess, mode="extract", extract_mode=extract_mode)
            ok = await sess.wait(600)
            if not ok:
                listener.is_cancelled = True
                LOGGER.warning(f"[VT] extract timed out for {listener.name}")
                await listener.on_upload_error(
                    "Selection timed out.", is_cancelled=True
                )
                return dl_path
            if listener.is_cancelled or not sess.func_key:
                await listener.on_upload_error("Cancelled.", is_cancelled=True)
                return dl_path

        elif func_key == "swap_audio":
            if not video_files:
                LOGGER.error("[VT] swap_audio: no video files found.")
                return dl_path
            streams = await get_streams(video_files[0])
            if not streams:
                LOGGER.error("[VT] swap_audio: cannot read streams.")
                return dl_path
            audio_count = sum(1 for s in streams if s.get("codec_type") == "audio")
            if audio_count < 2:
                return await _abort_after_prompt(
                    f"⚠️ <b>Swap Audio Failed</b>\n\nFile Has Only <b>{audio_count}</b> Audio Track.\nNeed At Least <b>2 Audio Tracks</b> To Swap."
                )
            sess.streams_info = streams
            sess.menu_msg = await send_message(
                listener.message,
                "⌬ <b>Swap Audio Tracks</b>\n\nTap Two Audio Tracks Then Confirm.",
            )
            _register_menu_msg(sess.menu_msg.id, listener.mid)
            await _stream_menu(sess, mode="swap")
            ok = await sess.wait(600)
            if not ok:
                listener.is_cancelled = True
                LOGGER.warning(f"[VT] swap_audio timed out for {listener.name}")
                await listener.on_upload_error(
                    "Selection timed out.", is_cancelled=True
                )
                return dl_path
            if listener.is_cancelled or not sess.func_key:
                await listener.on_upload_error("Cancelled.", is_cancelled=True)
                return dl_path

        elif func_key == "merge_video":
            if len(sess.files) < 2:
                LOGGER.warning(
                    f"[VT] merge_video: only {len(sess.files)} file(s) — need 2+"
                )
                return dl_path
            if len(video_files) < 2:
                LOGGER.warning(
                    f"[VT] merge_video: only {len(video_files)} video file(s) — need 2+"
                )
                return dl_path
            if len(video_files) >= 3:
                selector = MergeGroupSelector(bot, listener.message, video_files)
                groups = await selector.get_groups()
                if groups is None:
                    return dl_path
                sess.merge_groups = groups
            else:
                sess.merge_groups = [natsorted(video_files)]
            sess.merge_name = None

        label = VT_FUNCS[func_key][0]
        LOGGER.info(
            f"[VT] Starting: {label} — {listener.name} ({len(sess.files)} files)"
        )

        _orig_name = listener.name
        ff = VideoFFmpeg(listener)

        async with task_dict_lock:
            task_dict[listener.mid] = VTStatus(listener, gid, func_key, obj=ff)

        listener.progress = True
        try:
            results = await _run_processing(sess, ff)
        except Exception as e:
            LOGGER.error(f"[VT] Processing error [{func_key}]: {e}")
            async with task_dict_lock:
                task_dict.pop(listener.mid, None)
            listener.is_cancelled = True
            return dl_path

        if not results:
            if listener.is_cancelled:
                LOGGER.info(
                    f"[VT] {func_key} cancelled by user — skipping no-output prompt."
                )
                return dl_path
            LOGGER.error(f"[VT] No results: {func_key} / {listener.name}")
            async with task_dict_lock:
                vt_status = task_dict.get(listener.mid)
            if vt_status and hasattr(vt_status, "cancel_task"):
                await vt_status.cancel_task(
                    "No output generated from the selected settings."
                )
                return dl_path
            return await _abort_after_prompt(
                "⌬ <b>Video Tools</b>\n\nOperation produced no output.\nPlease check your selected settings and try again."
            )

        LOGGER.info(f"[VT] {label} done — {len(results)} output(s)")
        if func_key == "extract":
            listener.name = _orig_name
            extract_dir = ospath.join(
                ospath.dirname(results[0]), f"extracted_{listener.mid}"
            )
            await aio_makedirs(extract_dir, exist_ok=True)
            final_paths = []
            for out_file in results:
                if not out_file:
                    continue
                dst = ospath.join(extract_dir, ospath.basename(out_file))
                if out_file != dst and await aiopath.exists(out_file):
                    await aio_rename(out_file, dst)
                final_paths.append(dst)
            return extract_dir if final_paths else dl_path

        results = await _rename_outputs(results)
        return results[0] if len(results) == 1 else ospath.dirname(results[0])
    finally:
        if sess.menu_msg:
            _unregister_menu_msg(sess.menu_msg.id)
            await delete_message(sess.menu_msg)
        _sessions.pop(listener.mid, None)
