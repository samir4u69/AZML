from functools import partial
from os import getcwd, path as ospath
from contextlib import suppress

from aiofiles.os import makedirs as aio_makedirs, remove as aio_remove, path as aiopath
from html import escape
from PIL import Image

from .. import LOGGER, user_data
from ..helper.ext_utils.bot_utils import new_task, update_user_ldata
from ..helper.ext_utils.db_handler import DbManger
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
)
from .menus import (
    _fn_menu,
    _main_menu,
    _stream_menu,
    _sub_menu,
    _text_event_handler,
    _text_msg_handler,
)
from aiofiles.os import rename as aio_rename
from .session import (
    VT_FUNCS,
    VT_MRES_DEFAULT,
    _FUNC_KEYS,
    _RES_PRESETS,
    _get_vt_session_for_callback,
    _func_enabled,
    _selector_futures,
    _sessions,
    _unregister_menu_msg,
    handler_token_active,
    clear_handler_token,
)


class _DatabaseProxy:
    async def update_user_data(self, user_id):
        await DbManger().update_user_data(user_id)

    async def update_user_doc(self, user_id, key, path=""):
        await DbManger().update_user_doc(user_id, key, path)


database = _DatabaseProxy()
WATERMARK_DIR = ospath.join(getcwd(), "watermarks")
ALLOWED_WM_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_watermark_path(path: str) -> bool:
    try:
        return bool(
            path
            and ospath.commonpath([ospath.abspath(path), WATERMARK_DIR])
            == ospath.abspath(WATERMARK_DIR)
        )
    except Exception:
        return False


def _normalize_wm_ext(message) -> str:
    if message.photo:
        return ".jpg"
    file_name = (
        getattr(getattr(message, "document", None), "file_name", "") or "watermark.png"
    )
    ext = ospath.splitext(file_name)[1].lower().strip()
    return ext if ext in ALLOWED_WM_EXTS else ".png"


async def _upload_wm_image(_, message, rfunc, token=None):
    """Handle watermark image upload from VT menu"""
    uid = message.from_user.id
    if not handler_token_active(uid, token):
        return
    clear_handler_token(uid, token)

    try:
        await aio_makedirs(WATERMARK_DIR, exist_ok=True)

        ext = _normalize_wm_ext(message)
        final_path = ospath.join(WATERMARK_DIR, f"{uid}{ext}")
        tmp_path = ospath.join(WATERMARK_DIR, f".{uid}.uploading{ext}")

        old_img = user_data.get(uid, {}).get("VT_WM_IMAGE")

        if await aiopath.exists(tmp_path):
            await aio_remove(tmp_path)

        downloaded = await message.download(file_name=tmp_path)
        if not downloaded or not await aiopath.exists(tmp_path):
            raise ValueError("download failed")
        if (await aiopath.getsize(tmp_path)) <= 0:
            raise ValueError("empty file")

        with Image.open(tmp_path) as img:
            img.verify()

        await aio_rename(tmp_path, final_path)

        if (
            old_img
            and old_img != final_path
            and _safe_watermark_path(old_img)
            and await aiopath.exists(old_img)
        ):
            await aio_remove(old_img)

        update_user_ldata(uid, "VT_WM_IMAGE", final_path)
        await delete_message(message)
        await database.update_user_doc(uid, "VT_WM_IMAGE", final_path)
        await rfunc()
    except Exception as e:
        LOGGER.error(f"[VT] Failed to upload watermark image: {e}")
        with suppress(Exception):
            if await aiopath.exists(tmp_path):
                await aio_remove(tmp_path)
        await delete_message(message)
        await rfunc()


async def _prompt_wm_image_upload(client, query, sess, owner, msg, fkey):
    await query.answer()

    async def _rfunc():
        if sess and sess.menu_msg:
            t, b = await _fn_menu(sess)
            sess.menu_msg = await edit_message(sess.menu_msg, t, b)

    btns = ButtonMaker()
    btns.data_button("Stop", f"vtmenu {owner} sub_stop {fkey} VT_WM_IMAGE")
    btns.data_button("Back", f"vtmenu {owner} fn {fkey}", "footer")
    btns.data_button("Close", f"vtmenu {owner} cancel", "footer")

    current = user_data.get(owner, {}).get("VT_WM_IMAGE", "")
    current_label = escape(ospath.basename(current)) if current else "Not Set"
    text = (
        "⌬ <b>Set Watermark Image</b>\n\n"
        f"<b>├</b> Current : <code>{current_label}</code>\n"
        "<b>╰</b> Send a photo or document to save it as watermark image.\n"
        "<b>╰</b> <b>Time Left :</b> <code>60 sec</code>"
    )
    await edit_message(msg, text, btns.build_menu(1))
    pfunc = partial(_upload_wm_image, rfunc=_rfunc)
    await _text_event_handler(client, query, pfunc, _rfunc, photo=True, document=True)


async def _prompt_wm_text_input(client, query, sess, owner, msg, fkey):
    await query.answer()

    async def _rfunc():
        if sess and sess.menu_msg:
            t, b = await _fn_menu(sess)
            sess.menu_msg = await edit_message(sess.menu_msg, t, b)

    needs_text = await _sub_menu(owner, fkey, "VT_WM_TEXT", msg)
    if not needs_text:
        return
    pfunc = partial(_text_msg_handler, skey="VT_WM_TEXT", rfunc=_rfunc)
    await _text_event_handler(client, query, pfunc, _rfunc)


@new_task
async def vt_callback(client, query):
    data = query.data.split()
    if len(data) < 3:
        await query.answer()
        return

    owner = _safe_int(data[1])
    if owner is None:
        await query.answer("Invalid callback data.", show_alert=True)
        return
    action = data[2]

    if query.from_user.id != owner:
        await query.answer("Not Yours!", show_alert=True)
        return

    msg = query.message
    sess, sess_err = _get_vt_session_for_callback(owner, msg_id=msg.id)

    if action == "cancel":
        await query.answer()
        await edit_message(msg, "⌬ <b>Video Tools</b>\n\nCancelled.", None)
        if sess:
            sess.listener.is_cancelled = True
            sess.func_key = None
            if sess.menu_msg:
                _unregister_menu_msg(sess.menu_msg.id)
            sess.signal()
            _sessions.pop(sess.mid, None)
        return

    if action == "main":
        await query.answer()
        if not sess:
            if sess_err:
                await send_message(msg, f"⚠️ {sess_err}")
            return
        text, btns = await _main_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "fn":
        fkey = data[3] if len(data) > 3 else None
        if not sess or not fkey or fkey not in VT_FUNCS:
            await query.answer()
            return
        if not _func_enabled(fkey):
            func_label = VT_FUNCS[fkey][0]
            await query.answer(
                f"⚠️ {func_label} Is Currently Disabled.", show_alert=True
            )
            return
        await query.answer()
        sess.browsed_func = fkey
        text, btns = await _fn_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    await query.answer()

    if action == "fn_enable":
        fkey = data[3] if len(data) > 3 else None
        if not sess or not fkey:
            if not sess and sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        sess.func_key = fkey
        sess.browsed_func = fkey
        text, btns = await _main_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "fn_disable":
        if not sess:
            if sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        sess.func_key = None
        text, btns = await _main_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "proceed":
        if not sess:
            if sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        if not sess.func_key:
            await query.answer("Select a function first!", show_alert=True)
            return
        sess.signal()
        return

    if action == "sub":
        if not sess or len(data) < 5:
            if not sess and sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        fkey = data[3]
        skey = data[4]

        # Special handling for watermark image upload
        if skey == "VT_WM_IMAGE":
            await _prompt_wm_image_upload(client, query, sess, owner, msg, fkey)
            return
        if skey == "VT_WM_TEXT":
            await _prompt_wm_text_input(client, query, sess, owner, msg, fkey)
            return

        needs_text = await _sub_menu(owner, fkey, skey, msg)
        if needs_text:

            async def _rfunc():
                if sess and sess.menu_msg:
                    t, b = await _fn_menu(sess)
                    sess.menu_msg = await edit_message(sess.menu_msg, t, b)

            pfunc = partial(_text_msg_handler, skey=skey, rfunc=_rfunc)
            await _text_event_handler(client, query, pfunc, _rfunc)
        return

    if action == "sub_stop":
        if not sess:
            if sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        text, btns = await _fn_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "setval":
        if not sess or len(data) < 5:
            if not sess and sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        fkey = data[3]
        skey = data[4]
        val = data[5] if len(data) > 5 else ""
        update_user_ldata(owner, skey, val)
        await database.update_user_data(owner)
        text, btns = await _fn_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "wm_view":
        wm_path = user_data.get(owner, {}).get("VT_WM_IMAGE", "")
        if not wm_path or not await aiopath.exists(wm_path):
            await query.answer("Watermark image not found!", show_alert=True)
            return
        await send_file(msg, wm_path, "Watermark image")
        return

    if action == "wm_remove":
        wm_path = user_data.get(owner, {}).get("VT_WM_IMAGE", "")
        if _safe_watermark_path(wm_path) and await aiopath.exists(wm_path):
            await aio_remove(wm_path)
        update_user_ldata(owner, "VT_WM_IMAGE", "")
        await database.update_user_doc(owner, "VT_WM_IMAGE")
        await database.update_user_data(owner)
        if sess and sess.menu_msg:
            text, btns = await _fn_menu(sess)
            sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "fn_reset":
        if not sess or len(data) < 4:
            if not sess and sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        fkey = data[3]
        if fkey not in VT_FUNCS:
            return
        ud = user_data.get(owner, {})
        keys = _FUNC_KEYS.get(fkey, [])
        if fkey == "watermark":
            wm_path = ud.get("VT_WM_IMAGE", "")
            if _safe_watermark_path(wm_path) and await aiopath.exists(wm_path):
                await aio_remove(wm_path)
            await database.update_user_doc(owner, "VT_WM_IMAGE")
        for k in keys:
            ud.pop(k, None)
        await database.update_user_data(owner)
        if sess.browsed_func != fkey:
            sess.browsed_func = fkey
        text, btns = await _fn_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "res_tog":
        if not sess or len(data) < 4:
            if not sess and sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        rlbl = data[3]
        ud = user_data.get(owner, {})
        sel = {
            x.strip()
            for x in ud.get("VT_MRES_LIST", VT_MRES_DEFAULT).split(",")
            if x.strip()
        }
        sel.discard(rlbl) if rlbl in sel else sel.add(rlbl)
        ordered = [k for k in _RES_PRESETS if k in sel]
        update_user_ldata(owner, "VT_MRES_LIST", ",".join(ordered))
        await database.update_user_data(owner)
        text, btns = await _fn_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "wm_type":
        if not sess or len(data) < 4:
            if not sess and sess_err:
                await query.answer(sess_err, show_alert=True)
            return
        choice = data[3]
        current = user_data.get(owner, {}).get("VT_WM_TYPE", "text")
        if current == choice:
            if choice == "image":
                await _prompt_wm_image_upload(
                    client, query, sess, owner, msg, "watermark"
                )
            else:
                await _prompt_wm_text_input(
                    client, query, sess, owner, msg, "watermark"
                )
            return

        update_user_ldata(owner, "VT_WM_TYPE", choice)
        await database.update_user_data(owner)
        text, btns = await _fn_menu(sess)
        sess.menu_msg = await edit_message(msg, text, btns)
        return

    if action == "aud_page":
        mid = _safe_int(data[3])
        page = _safe_int(data[4])
        if mid is None or page is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        ts.audio_page = page
        mode = (
            "rm"
            if ts.func_key == "rm_streams"
            else ("extract" if ts.func_key == "extract" else "swap")
        )
        extract_mode = ts.extract_mode if hasattr(ts, "extract_mode") else None
        await _stream_menu(ts, mode=mode, extract_mode=extract_mode)
        return

    if action == "sub_page":
        mid = _safe_int(data[3])
        page = _safe_int(data[4])
        if mid is None or page is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        ts.sub_page = page
        mode = (
            "rm"
            if ts.func_key == "rm_streams"
            else ("extract" if ts.func_key == "extract" else "swap")
        )
        extract_mode = ts.extract_mode if hasattr(ts, "extract_mode") else None
        await _stream_menu(ts, mode=mode, extract_mode=extract_mode)
        return

    if action == "tog_a":
        mid = _safe_int(data[3])
        idx = _safe_int(data[4])
        if mid is None or idx is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        (
            ts.remove_audio_idx.discard(idx)
            if idx in ts.remove_audio_idx
            else ts.remove_audio_idx.add(idx)
        )
        await _stream_menu(ts, mode="rm")
        return

    if action == "tog_s":
        mid = _safe_int(data[3])
        idx = _safe_int(data[4])
        if mid is None or idx is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        (
            ts.remove_sub_idx.discard(idx)
            if idx in ts.remove_sub_idx
            else ts.remove_sub_idx.add(idx)
        )
        await _stream_menu(ts, mode="rm")
        return

    if action == "ext_a":
        mid = _safe_int(data[3])
        idx = _safe_int(data[4])
        if mid is None or idx is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        await query.answer()
        (
            ts.extract_audio_idx.discard(idx)
            if idx in ts.extract_audio_idx
            else ts.extract_audio_idx.add(idx)
        )
        await _stream_menu(ts, mode="extract", extract_mode=ts.extract_mode)
        return

    if action == "ext_s":
        mid = _safe_int(data[3])
        idx = _safe_int(data[4])
        if mid is None or idx is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        await query.answer()
        (
            ts.extract_sub_idx.discard(idx)
            if idx in ts.extract_sub_idx
            else ts.extract_sub_idx.add(idx)
        )
        await _stream_menu(ts, mode="extract", extract_mode=ts.extract_mode)
        return

    if action == "ext_all_a":
        mid = _safe_int(data[3])
        if mid is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        await query.answer()
        all_audio = set()
        seen_stream_indices = set()
        ai = 0
        for s in ts.streams_info:
            raw_index = s.get("index")
            if raw_index in seen_stream_indices:
                continue
            seen_stream_indices.add(raw_index)
            if s.get("codec_type") == "audio":
                all_audio.add(ai)
                ai += 1
        ts.extract_audio_idx = set() if ts.extract_audio_idx == all_audio else all_audio
        await _stream_menu(ts, mode="extract", extract_mode=ts.extract_mode)
        return

    if action == "ext_all_s":
        mid = _safe_int(data[3])
        if mid is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        await query.answer()
        all_subs = set()
        seen_stream_indices = set()
        si = 0
        for s in ts.streams_info:
            raw_index = s.get("index")
            if raw_index in seen_stream_indices:
                continue
            seen_stream_indices.add(raw_index)
            if s.get("codec_type") == "subtitle":
                all_subs.add(si)
                si += 1
        ts.extract_sub_idx = set() if ts.extract_sub_idx == all_subs else all_subs
        await _stream_menu(ts, mode="extract", extract_mode=ts.extract_mode)
        return

    if action == "swap_pick":
        mid = _safe_int(data[3])
        idx = _safe_int(data[4])
        if mid is None or idx is None:
            await query.answer("Invalid callback data.", show_alert=True)
            return
        ts, ts_err = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer(ts_err or "Session not found!", show_alert=True)
            return
        if ts.swap_a_idx is None:
            ts.swap_a_idx = idx
        elif ts.swap_b_idx is None and idx != ts.swap_a_idx:
            ts.swap_b_idx = idx
        else:
            ts.swap_a_idx = idx
            ts.swap_b_idx = None
        await _stream_menu(ts, mode="swap")
        return

    if action == "streams_done":
        mid = _safe_int(data[3]) if len(data) > 3 else None
        ts, _ = _get_vt_session_for_callback(owner, mid)
        if ts:
            ts.signal()
        return

    if action == "ext_done":
        mid = _safe_int(data[3]) if len(data) > 3 else None
        ts, _ = _get_vt_session_for_callback(owner, mid)
        if ts:
            ts.signal()
        return

    if action == "ext_all":
        mid = _safe_int(data[3]) if len(data) > 3 else None
        ts, _ = _get_vt_session_for_callback(owner, mid)
        if not ts:
            await query.answer("Session not found!", show_alert=True)
            return
        if not ts.streams_info:
            await query.answer("No streams available!", show_alert=True)
            return
        extract_mode = ts.extract_mode
        if extract_mode in ("audio", "both"):
            all_audio = set()
            seen_stream_indices = set()
            ai = 0
            for s in ts.streams_info:
                raw_index = s.get("index")
                if raw_index in seen_stream_indices:
                    continue
                seen_stream_indices.add(raw_index)
                if s.get("codec_type") == "audio":
                    all_audio.add(ai)
                    ai += 1
            ts.extract_audio_idx = (
                set() if ts.extract_audio_idx == all_audio else all_audio
            )
        if extract_mode in ("subtitles", "both"):
            all_subs = set()
            seen_stream_indices = set()
            si = 0
            for s in ts.streams_info:
                raw_index = s.get("index")
                if raw_index in seen_stream_indices:
                    continue
                seen_stream_indices.add(raw_index)
                if s.get("codec_type") == "subtitle":
                    all_subs.add(si)
                    si += 1
            ts.extract_sub_idx = set() if ts.extract_sub_idx == all_subs else all_subs
        await _stream_menu(ts, mode="extract", extract_mode=extract_mode)
        return

    if action in ("mg_all", "mg_edit", "mg_cancel"):
        sel = _selector_futures.get(msg.id)
        if not sel:
            await query.answer("Session expired.", show_alert=True)
            return
        if action == "mg_all":
            if not sel.future.done():
                sel.future.set_result([list(sel.video_files)])
            return
        if action == "mg_cancel":
            if not sel.future.done():
                sel.future.set_result(None)
            return
        if action == "mg_edit":
            if sel._waiting:
                await query.answer("Already waiting…", show_alert=True)
                return
            await sel.start_input(client)
            return


async def vt_text_input(*a, **k):
    pass
