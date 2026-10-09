from asyncio import Future, TimeoutError as AIOTimeoutError, wait_for, sleep
from contextlib import suppress
from functools import partial
from html import escape
from os import path as ospath
from time import monotonic

from aiofiles.os import path as aiopath
from langcodes import Language
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler

from .. import LOGGER, bot, config_dict, user_data
from ..helper.ext_utils.bot_utils import new_task, update_user_ldata
from ..helper.ext_utils.db_handler import DbManger
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from .session import (
    VT_MRES_DEFAULT,
    VT_FUNCS,
    _OPTS,
    _RES_PRESETS,
    _selector_futures,
    clear_handler_token,
    handler_token_active,
    new_handler_token,
)


async def _main_menu(sess) -> tuple:
    uid = sess.user_id
    btns = ButtonMaker()
    user = sess.listener.message.from_user
    tag = user.mention(style="html") if user else "User"

    enabled_funcs = {
        fkey
        for fkey, (_, _, cfg) in VT_FUNCS.items()
        if config_dict.get(cfg, True)
    }
    if sess.func_key and sess.func_key not in enabled_funcs:
        sess.func_key = None

    for fkey, (label, _, cfg) in VT_FUNCS.items():
        if not config_dict.get(cfg, True):
            continue
        if sess.func_key == fkey:
            btns.data_button(f"✓ {label}", f"vtmenu {uid} fn {fkey}")
        else:
            btns.data_button(f"{label}", f"vtmenu {uid} fn_enable {fkey}")

    if sess.func_key:
        btns.data_button("✓ Proceed", f"vtmenu {uid} proceed")
    btns.data_button("✗ Cancel", f"vtmenu {uid} cancel", "footer")

    if sess.func_key:
        sel = VT_FUNCS[sess.func_key][0]
        body = (
            f"<b>├</b> Selected : <b>{sel}</b>\n"
            f"<b>╰</b> Click <b>✓ Proceed</b> To Start, Or Pick Another Function."
        )
    else:
        if not sess.pre_config and sess.files:
            flist = "\n".join(
                f"  {i + 1}. <code>{ospath.basename(f)}</code>"
                for i, f in enumerate(sess.files[:8])
            )
            if len(sess.files) > 8:
                flist += f"\n  … and {len(sess.files) - 8} more"
            body = f"<b>├</b> Files :\n{flist}\n<b>╰</b> <b>Select a function:</b>"
        else:
            body = "<b>╰</b> <b>Select a function:</b>"

    text = f"⌬ <b>Video Tool</b>\n<b>├</b> User : {tag}\n{body}"
    if sess.selection_deadline:
        left = max(0.0, sess.selection_deadline - monotonic())
        text += f"\n<b>╰</b> <b>Time Left :</b> <code>{left:.1f} sec</code>"
    return text, btns.build_menu(2)


async def _fn_menu(sess) -> tuple:
    uid = sess.user_id
    fkey = sess.browsed_func
    label, desc, _ = VT_FUNCS[fkey]
    ud = user_data.get(uid, {})

    def cv(k):
        return ud.get(k, _OPTS[k][1])

    btns = ButtonMaker()
    lines = [f"⌬ <b>{label} Settings</b>", f"<b>╭</b> {desc}"]

    if fkey == "encode":
        lines += [
            f"<b>├</b> Video Codec : <code>{cv('VT_VCODEC')}</code>",
            f"<b>├</b> Audio Codec : <code>{cv('VT_ACODEC')}</code>",
            f"<b>├</b> CRF         : <code>{cv('VT_CRF')}</code>",
            f"<b>├</b> Preset      : <code>{cv('VT_PRESET')}</code>",
            f"<b>├</b> Resolution  : <code>{cv('VT_RES') or 'Original'}</code>",
            f"<b>├</b> FPS         : <code>{cv('VT_FPS') or 'Original'}</code>",
            f"<b>╰</b> Output Ext  : <code>{cv('VT_EXT')}</code>",
        ]
        for lbl, k in [
            ("Video Codec", "VT_VCODEC"),
            ("Audio Codec", "VT_ACODEC"),
            ("CRF", "VT_CRF"),
            ("Preset", "VT_PRESET"),
            ("Resolution", "VT_RES"),
            ("FPS", "VT_FPS"),
            ("Ext", "VT_EXT"),
        ]:
            btns.data_button(lbl, f"vtmenu {uid} sub {fkey} {k}")

    elif fkey == "convert":
        lines += [
            f"<b>├</b> Output Ext  : <code>{cv('VT_CONV_EXT')}</code>",
            f"<b>├</b> Video Codec : <code>{cv('VT_CONV_VCODEC')}</code>",
            f"<b>╰</b> Audio Codec : <code>{cv('VT_CONV_ACODEC')}</code>",
        ]
        for lbl, k in [
            ("Output Ext", "VT_CONV_EXT"),
            ("Video Codec", "VT_CONV_VCODEC"),
            ("Audio Codec", "VT_CONV_ACODEC"),
        ]:
            btns.data_button(lbl, f"vtmenu {uid} sub {fkey} {k}")

    elif fkey == "multi_res":
        mode = ud.get("VT_MRES_MODE", "quality")
        sel_str = ud.get("VT_MRES_LIST", VT_MRES_DEFAULT)
        selected = {x.strip() for x in sel_str.split(",") if x.strip()}
        lines.append(f"<b>├</b> Mode        : <code>{mode.title()}</code>")
        lines.append(f"<b>╰</b> Resolutions : <code>{sel_str}</code>")
        btns.data_button(
            "✓ Quality" if mode == "quality" else "Quality",
            f"vtmenu {uid} setval multi_res VT_MRES_MODE quality",
        )
        btns.data_button(
            "✓ Compressed" if mode == "compressed" else "Compressed",
            f"vtmenu {uid} setval multi_res VT_MRES_MODE compressed",
        )
        for rlbl in _RES_PRESETS:
            icon = "✓" if rlbl in selected else ""
            btns.data_button(f"{icon}{rlbl}", f"vtmenu {uid} res_tog {rlbl}")

    elif fkey == "merge_video":
        lines += [
            "<b>├</b> Naturally Sorted Before Merge.",
            "<b>╰</b> All Collected Files Concatenated Into One.",
        ]

    elif fkey == "mux_audio":
        rep = cv("VT_MUX_REPLACE")
        lines += [
            f"<b>├</b> Mode      : <code>{'Replace Existing' if rep == 'true' else 'Add Alongside'}</code>",
            f"<b>╰</b> Language  : <code>{cv('VT_ALL_LANG')}</code>",
        ]
        btns.data_button(
            "✓ Replace" if rep == "true" else "Replace",
            f"vtmenu {uid} setval {fkey} VT_MUX_REPLACE true",
        )
        btns.data_button(
            "✓ Add Alongside" if rep == "false" else "Add Alongside",
            f"vtmenu {uid} setval {fkey} VT_MUX_REPLACE false",
        )
        btns.data_button("Audio Lang", f"vtmenu {uid} sub {fkey} VT_ALL_LANG")

    elif fkey == "softsub":
        lines.append(f"<b>╰</b> Sub Lang : <code>{cv('VT_SUB_LANG')}</code>")
        btns.data_button("Sub Lang", f"vtmenu {uid} sub {fkey} VT_SUB_LANG")

    elif fkey == "mux_all":
        lines += [
            f"<b>├</b> Audio Lang    : <code>{cv('VT_ALL_LANG')}</code>",
            f"<b>╰</b> Subtitle Lang : <code>{cv('VT_ALL_SUB_LANG')}</code>",
        ]
        btns.data_button("Audio Lang", f"vtmenu {uid} sub {fkey} VT_ALL_LANG")
        btns.data_button("Subtitle Lang", f"vtmenu {uid} sub {fkey} VT_ALL_SUB_LANG")

    elif fkey == "introsub":
        lines += [
            f"<b>├</b> Text     : <code>{escape(cv('VT_ISUB_TEXT'))}</code>",
            f"<b>├</b> Sub Lang : <code>{cv('VT_ISUB_LANG')}</code>",
            f"<b>├</b> Duration : <code>{cv('VT_ISUB_DURATION')}s</code>",
            f"<b>├</b> Font Size: <code>{cv('VT_ISUB_SIZE')}</code>",
            f"<b>├</b> Color    : <code>{cv('VT_ISUB_COLOR')}</code>",
            f"<b>╰</b> Position : <code>{cv('VT_ISUB_POS')}</code>",
        ]
        for lbl, k in [
            ("Text", "VT_ISUB_TEXT"),
            ("Sub Lang", "VT_ISUB_LANG"),
            ("Duration", "VT_ISUB_DURATION"),
            ("Size", "VT_ISUB_SIZE"),
            ("Color", "VT_ISUB_COLOR"),
            ("Position", "VT_ISUB_POS"),
        ]:
            btns.data_button(lbl, f"vtmenu {uid} sub {fkey} {k}")

    elif fkey in ("rm_sub", "rm_audio", "strip_metadata"):
        lines.append("<b>╰</b> No settings needed. Runs automatically.")

    elif fkey == "hardsub":
        lines += [
            f"<b>├</b> Font Size  : <code>{cv('VT_HS_SIZE')}</code>",
            f"<b>├</b> Font Color : <code>{cv('VT_HS_COLOR')}</code>",
            f"<b>╰</b> Output Ext : <code>{cv('VT_HS_EXT')}</code>",
        ]
        for lbl, k in [
            ("Font Size", "VT_HS_SIZE"),
            ("Font Color", "VT_HS_COLOR"),
            ("Ext", "VT_HS_EXT"),
        ]:
            btns.data_button(lbl, f"vtmenu {uid} sub {fkey} {k}")

    elif fkey == "rm_streams":
        lines += [
            "<b>├</b> Post-download: bot lists every audio/subtitle track.",
            "<b>╰</b> Toggle each track then confirm.",
        ]

    elif fkey == "extract":
        mode = cv("VT_EXTRACT_MODE")
        lines.append(f"<b>╰</b> Default Mode : <code>{mode}</code>")
        for opt, lbl in [
            ("audio", "Audio"),
            ("subtitles", "Subtitles"),
            ("both", "Both"),
        ]:
            icon = "✓ " if mode == opt else ""
            btns.data_button(
                f"{icon}{lbl}", f"vtmenu {uid} setval {fkey} VT_EXTRACT_MODE {opt}"
            )
        # Select all button (streams only exist post-download)
        if sess.streams_info:
            btns.data_button(
                "Select All Tracks", f"vtmenu {uid} ext_all {sess.mid}", "footer"
            )

    elif fkey == "swap_audio":
        lines += [
            "<b>├</b> Post-download: pick two audio tracks to swap.",
            "<b>╰</b> Stream remux — no re-encode.",
        ]

    elif fkey == "convert_audio":
        ext = cv("VT_CONV_AUDIO_EXT")
        bitrate = cv("VT_CONV_AUDIO_BITRATE")
        lines += [
            f"<b>├</b> Format  : <code>{ext}</code>",
            f"<b>╰</b> Bitrate : <code>{bitrate}</code>",
        ]
        for lbl, k in [
            ("Format", "VT_CONV_AUDIO_EXT"),
            ("Bitrate", "VT_CONV_AUDIO_BITRATE"),
        ]:
            btns.data_button(lbl, f"vtmenu {uid} sub {fkey} {k}")

    elif fkey == "aspect_ratio":
        ratio = cv("VT_ASPECT_RATIO_VALUE")
        lines += [
            f"<b>╰</b> Ratio : <code>{ratio}</code>",
        ]
        btns.data_button("Ratio", f"vtmenu {uid} sub {fkey} VT_ASPECT_RATIO_VALUE")

    elif fkey == "watermark":
        wt = cv("VT_WM_TYPE")
        lines += [f"<b>├</b> Type    : <code>{wt.title()}</code>"]
        btns.data_button(
            "✓ Text" if wt == "text" else "Text", f"vtmenu {uid} wm_type text"
        )
        btns.data_button(
            "✓ Image" if wt == "image" else "Image", f"vtmenu {uid} wm_type image"
        )
        if wt == "text":
            lines += [
                f"<b>├</b> Text    : <code>{escape(cv('VT_WM_TEXT'))}</code>",
                "<b>╰</b> Tap <b>Text</b> Again To Edit.",
                f"<b>├</b> Pos     : <code>{cv('VT_WM_POS')}</code>",
                f"<b>├</b> Size    : <code>{cv('VT_WM_SIZE')}</code>",
                f"<b>├</b> Color   : <code>{cv('VT_WM_COLOR')}</code>",
                f"<b>╰</b> Opacity : <code>{cv('VT_WM_OPACITY')}</code>",
            ]
            for lbl, k in [
                ("Pos", "VT_WM_POS"),
                ("Size", "VT_WM_SIZE"),
                ("Color", "VT_WM_COLOR"),
                ("Opacity", "VT_WM_OPACITY"),
            ]:
                btns.data_button(lbl, f"vtmenu {uid} sub {fkey} {k}")
        else:
            wm_image_path = cv("VT_WM_IMAGE")
            has_wm_image = bool(wm_image_path and await aiopath.exists(wm_image_path))
            wm_image_name = (
                escape(ospath.basename(wm_image_path)) if has_wm_image else "Not Set"
            )
            lines += [
                f"<b>├</b> Image   : <code>{wm_image_name}</code>",
                f"<b>├</b> Pos     : <code>{cv('VT_WM_POS')}</code>",
                f"<b>╰</b> Opacity : <code>{cv('VT_WM_OPACITY')}</code>",
            ]
            btns.data_button(
                "Change Image" if has_wm_image else "Set Image",
                f"vtmenu {uid} sub {fkey} VT_WM_IMAGE",
            )
            if has_wm_image:
                btns.data_button("View Image", f"vtmenu {uid} wm_view")
                btns.data_button("Remove Image", f"vtmenu {uid} wm_remove")
            for lbl, k in [("Pos", "VT_WM_POS"), ("Opacity", "VT_WM_OPACITY")]:
                btns.data_button(lbl, f"vtmenu {uid} sub {fkey} {k}")

    # Header select button removed to avoid confusion; selection
    # is managed from the main menu instead.
    btns.data_button("✦ Reset", f"vtmenu {uid} fn_reset {fkey}", "footer")
    btns.data_button("« Back", f"vtmenu {uid} main", "footer")
    btns.data_button("✗ Cancel", f"vtmenu {uid} cancel", "footer")
    user = sess.listener.message.from_user
    tag = user.mention(style="html") if user else "User"
    lines.append(f"<b>├</b> User : {tag}")
    if sess.selection_deadline:
        left = max(0.0, sess.selection_deadline - monotonic())
        lines.append(f"<b>╰</b> <b>Time Left :</b> <code>{left:.1f} sec</code>")
    return "\n".join(lines), btns.build_menu(2)


async def _sub_menu(uid: int, fkey: str, skey: str, msg) -> bool:
    ud = user_data.get(uid, {})
    opts, default = _OPTS[skey]
    cur = ud.get(skey, default)
    nice = skey.replace("VT_", "").replace("_", " ").title()
    btns = ButtonMaker()

    if opts is None:
        btns.data_button("Stop Input", f"vtmenu {uid} sub_stop {fkey} {skey}")
        btns.data_button("« Back", f"vtmenu {uid} fn {fkey}", "footer")
        text = (
            f"⌬ <b>Set {nice}</b>\n\n"
            f"<b>├</b> Current : <code>{escape(cur) if cur else 'Not Set'}</code>\n"
            f"<b>╰</b> Default : <code>{escape(default)}</code>\n\n"
            f"<i>Send new value now.</i>\n"
            f"<b>╰</b> <b>Time Left :</b> <code>60 sec</code>"
        )
        await edit_message(msg, text, btns.build_menu(1))
        return True

    for opt in opts:
        display = "Keep Original" if opt == "" else opt
        prefix = "✓ " if opt == cur else ""
        btns.data_button(
            f"{prefix}{display}", f"vtmenu {uid} setval {fkey} {skey} {opt}"
        )
    btns.data_button("« Back", f"vtmenu {uid} fn {fkey}", "footer")
    text = (
        f"⌬ <b>Set {nice}</b>\n\n"
        f"<b>├</b> Current : <code>{escape(cur) if cur else 'Not Set'}</code>\n"
        f"<b>╰</b> Default : <code>{escape(default)}</code>\n\n"
        f"<i>Tap An Option:</i>"
    )
    await edit_message(msg, text, btns.build_menu(2))
    return False


PER_PAGE = 10


async def _stream_menu(sess, mode: str = "rm", extract_mode: str = None):
    btns = ButtonMaker()
    uid = sess.user_id
    ai = si = 0
    seen_stream_indices = set()
    audio_tracks = []
    sub_tracks = []

    for s in sess.streams_info:
        raw_index = s.get("index")
        if raw_index in seen_stream_indices:
            continue
        seen_stream_indices.add(raw_index)

        ct = s.get("codec_type", "")
        codec = s.get("codec_name", "?")
        lang = (s.get("tags") or {}).get("language", "und - Mirror Nexus")
        with suppress(Exception):
            lang = Language.get(lang).display_name()
        title = (s.get("tags") or {}).get("title", "")
        suffix = f" - {title}" if title else ""

        if ct == "audio":
            audio_tracks.append(
                {"lang": lang, "codec": codec, "suffix": suffix, "idx": ai}
            )
            ai += 1
        elif ct == "subtitle":
            sub_tracks.append(
                {"lang": lang, "codec": codec, "suffix": suffix, "idx": si}
            )
            si += 1

    audio_total = len(audio_tracks)
    sub_total = len(sub_tracks)
    audio_pages = max(1, (audio_total + PER_PAGE - 1) // PER_PAGE) if audio_total else 1
    sub_pages = max(1, (sub_total + PER_PAGE - 1) // PER_PAGE) if sub_total else 1

    # Cap current page to valid range
    sess.audio_page = max(0, min(sess.audio_page, audio_pages - 1))
    sess.sub_page = max(0, min(sess.sub_page, sub_pages - 1))

    a_start = sess.audio_page * PER_PAGE
    a_end = min(a_start + PER_PAGE, audio_total)
    s_start = sess.sub_page * PER_PAGE
    s_end = min(s_start + PER_PAGE, sub_total)

    # Compute removed items from all tracks (not just current page)
    removed_audio_langs = [
        t["lang"].title() for t in audio_tracks if t["idx"] in sess.remove_audio_idx
    ]
    removed_sub_langs = [
        t["lang"].title() for t in sub_tracks if t["idx"] in sess.remove_sub_idx
    ]

    def _add_audio_btn(track, mode_):
        idx = track["idx"]
        if mode_ == "swap":
            mark = (
                " [A]"
                if sess.swap_a_idx == idx
                else (" [B]" if sess.swap_b_idx == idx else "")
            )
            lbl = (
                f"A{idx + 1} [{track['codec']}][{track['lang']}]{track['suffix']}{mark}"
            )
            btns.data_button(lbl, f"vtmenu {uid} swap_pick {sess.mid} {idx}")
            return
        toggled = (
            idx in sess.remove_audio_idx
            if mode_ == "rm"
            else idx in sess.extract_audio_idx
        )
        action = "tog_a" if mode_ == "rm" else "ext_a"
        btns.data_button(
            f"{'✓ ' if toggled else ''}Audio - {track['lang'].title()}",
            f"vtmenu {uid} {action} {sess.mid} {idx}",
        )

    def _add_sub_btn(track, mode_):
        idx = track["idx"]
        toggled = (
            idx in sess.remove_sub_idx if mode_ == "rm" else idx in sess.extract_sub_idx
        )
        action = "tog_s" if mode_ == "rm" else "ext_s"
        btns.data_button(
            f"{'✓ ' if toggled else ''}Subtitle - {track['lang'].title()}",
            f"vtmenu {uid} {action} {sess.mid} {idx}",
        )

    # Determine which track types to show
    show_audio = (
        mode == "swap"
        or mode == "rm"
        or (mode == "extract" and extract_mode in ("audio", "both"))
    )
    show_sub = mode == "rm" or (
        mode == "extract" and extract_mode in ("subtitles", "both")
    )

    # Render visible tracks
    if show_audio:
        for track in audio_tracks[a_start:a_end]:
            _add_audio_btn(track, mode)
    if show_sub:
        for track in sub_tracks[s_start:s_end]:
            _add_sub_btn(track, mode)

    # Build page info for text
    a_pg_str = (
        f" Pg {sess.audio_page + 1}/{audio_pages}"
        if show_audio and audio_pages > 1
        else ""
    )
    s_pg_str = (
        f" Pg {sess.sub_page + 1}/{sub_pages}" if show_sub and sub_pages > 1 else ""
    )

    # Audio page nav
    if show_audio and audio_pages > 1:
        prev_a = (sess.audio_page - 1) % audio_pages
        next_a = (sess.audio_page + 1) % audio_pages
        btns.data_button("‹", f"vtmenu {uid} aud_page {sess.mid} {prev_a}", "f_body")
        btns.data_button(
            f"{sess.audio_page + 1}/{audio_pages}",
            f"vtmenu {uid} aud_page {sess.mid} {sess.audio_page}",
            "f_body",
        )
        btns.data_button("›", f"vtmenu {uid} aud_page {sess.mid} {next_a}", "f_body")

    # Sub page nav
    if show_sub and sub_pages > 1:
        prev_s = (sess.sub_page - 1) % sub_pages
        next_s = (sess.sub_page + 1) % sub_pages
        btns.data_button("‹", f"vtmenu {uid} sub_page {sess.mid} {prev_s}", "f_body")
        btns.data_button(
            f"{sess.sub_page + 1}/{sub_pages}",
            f"vtmenu {uid} sub_page {sess.mid} {sess.sub_page}",
            "f_body",
        )
        btns.data_button("›", f"vtmenu {uid} sub_page {sess.mid} {next_s}", "f_body")

    if mode == "rm":
        ra_str = ", ".join(removed_audio_langs) if removed_audio_langs else "none"
        rs_str = ", ".join(removed_sub_langs) if removed_sub_langs else "none"
        btns.data_button("✓ Confirm", f"vtmenu {uid} streams_done {sess.mid}", "footer")
        btns.data_button("✗ Cancel", f"vtmenu {uid} cancel", "footer")
        text = (
            "<b>Remove Streams</b>\n"
            "Tap To Toggle, Then Confirm.\n\n"
            f"Remove Audio : {ra_str}{a_pg_str}\n"
            f"Remove Subs  : {rs_str}{s_pg_str}"
        )
    elif mode == "extract":
        ea = len(sess.extract_audio_idx)
        es = len(sess.extract_sub_idx)
        btns.data_button("Select All", f"vtmenu {uid} ext_all {sess.mid}", "footer")
        btns.data_button("✓ Confirm", f"vtmenu {uid} ext_done {sess.mid}", "footer")
        btns.data_button("✗ Cancel", f"vtmenu {uid} cancel", "footer")
        if extract_mode == "audio":
            text = f"<b>Extract Audio</b>\nTap To Toggle Tracks, Then Confirm.\n\nAudio Selected : {ea}/{audio_total}{a_pg_str}"
        elif extract_mode == "subtitles":
            text = f"<b>Extract Subtitles</b>\nTap To Toggle Subs, Then Confirm.\n\nSubs Selected  : {es}/{sub_total}{s_pg_str}"
        else:
            text = (
                "<b>Extract Subs / Audio</b>\n"
                "Tap To Toggle Tracks, Then Confirm.\n\n"
                f"Audio Selected : {ea}/{audio_total}{a_pg_str}\n"
                f"Subs Selected  : {es}/{sub_total}{s_pg_str}"
            )
    else:
        a = sess.swap_a_idx
        b = sess.swap_b_idx
        btns.data_button(
            "✓ Confirm Swap", f"vtmenu {uid} streams_done {sess.mid}", "footer"
        )
        btns.data_button("✗ Cancel", f"vtmenu {uid} cancel", "footer")
        text = (
            "<b>Swap Audio Tracks</b>\n\n"
            f"Track A : {'Audio ' + str(a + 1) if a is not None else 'Not selected'}\n"
            f"Track B : {'Audio ' + str(b + 1) if b is not None else 'Not selected'}\n\n"
            "<i>Tap Two Tracks Then Confirm.</i>"
        )

    user = sess.listener.message.from_user
    tag = user.mention(style="html") if user else "User"
    text += f"\n<b>├</b> User : {tag}"
    if sess.selection_deadline:
        left = max(0.0, sess.selection_deadline - monotonic())
        text += f"\n<b>╰</b> <b>Time Left :</b> <code>{left:.1f} sec</code>"

    if sess.menu_msg:
        cols = 2
        sess.menu_msg = await edit_message(
            sess.menu_msg, text, btns.build_menu(b_cols=cols, fb_cols=3)
        )


class MergeGroupSelector:
    def __init__(self, client, message, video_files):
        self.client = client
        self.message = message
        self.video_files = list(video_files)
        self.future = Future()
        self.msg = None
        self._waiting = False
        self._handler = None

    def _msg_text(self, extra=""):
        lines = ["<b>🎬 Merge Groups — Select Which Files To Combine:</b>\n"]
        for i, p in enumerate(self.video_files, 1):
            lines.append(f"<code>{i}.</code>{ospath.basename(p)}\n")
        lines += [
            "",
            "<b>Send Groups Separated By</b> <code>|</code>",
            "<b>Example:</b> <code>1,2,3 | 4,5 | 6</code>",
            "  <code>1,2,3</code> → Part 1   <code>4,5</code> → Part 2   <code>6</code> → Solo",
            "💡 Unlisted Files Are Grouped Automatically.",
        ]
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    def _btns(self, waiting=False):
        uid = self.message.from_user.id if self.message.from_user else 0
        btns = ButtonMaker()
        btns.data_button("✓ Merge All", f"vtmenu {uid} mg_all")
        btns.data_button(
            "✏️ Waiting…" if waiting else "✏️ Custom Plan", f"vtmenu {uid} mg_edit"
        )
        btns.data_button("✗ Cancel", f"vtmenu {uid} mg_cancel")
        return btns.build_menu(1)

    def _parse(self, text):
        total = len(self.video_files)
        seen = set()
        groups = []
        for part in text.split("|"):
            part = part.strip()
            if not part:
                continue
            indices = []
            for token in part.split(","):
                token = token.strip()
                if not token.isdigit():
                    return None, f"✗ Invalid token: <code>{token}</code>"
                idx = int(token)
                if idx < 1 or idx > total:
                    return None, f"✗ Number {idx} out of range (1–{total})"
                if idx in seen:
                    return None, f"✗ Number {idx} repeated"
                seen.add(idx)
                indices.append(idx)
            if indices:
                groups.append([self.video_files[i - 1] for i in indices])
        unlisted = [
            self.video_files[i - 1] for i in range(1, total + 1) if i not in seen
        ]
        if unlisted:
            groups.append(unlisted)
        if not groups:
            return None, "✗ No valid groups"
        return groups, None

    async def get_groups(self):
        self.msg = await send_message(self.message, self._msg_text(), self._btns())
        _selector_futures[self.msg.id] = self
        try:
            return await wait_for(self.future, timeout=120)
        except AIOTimeoutError:
            LOGGER.warning("[MergeGroupSelector] Timed out — merging all.")
            return [list(self.video_files)]
        finally:
            _selector_futures.pop(self.msg.id, None)
            await self._rm_handler()
            await delete_message(self.msg)

    async def _rm_handler(self):
        if self._handler:
            try:
                bot.remove_handler(*self._handler)
            except Exception:
                pass
            self._handler = None
        self._waiting = False

    async def start_input(self, client):
        self._waiting = True
        await edit_message(
            self.msg,
            self._msg_text("\n\n💡 Reply This Message With Your Group Plan…</b>"),
            self._btns(waiting=True),
        )

        async def _flt(_, __, ev):
            u = ev.from_user or ev.sender_chat
            return (
                u
                and u.id == self.message.from_user.id
                and ev.chat.id == self.message.chat.id
                and bool(ev.text)
                and ev.reply_to_message is not None
                and ev.reply_to_message.id == self.msg.id
            )

        async def _handle(_, m):
            if not self._waiting:
                return
            self._waiting = False
            await self._rm_handler()
            await delete_message(m)
            groups, err = self._parse(m.text.strip())
            if err:
                await edit_message(self.msg, self._msg_text(f"\n\n{err}"), self._btns())
                return
            if not self.future.done():
                self.future.set_result(groups)

        self._handler = bot.add_handler(
            MessageHandler(_handle, filters=create(_flt)), group=-1
        )


async def _text_event_handler(
    client,
    query,
    pfunc,
    rfunc,
    timeout=60,
    photo=False,
    document=False,
):
    from time import time as _t

    uid = query.from_user.id
    token = new_handler_token(uid)
    start = upd = _t()

    async def _flt(_, __, ev):
        u = ev.from_user or ev.sender_chat
        return bool(
            u and u.id == uid and ev.chat.id == query.message.chat.id and ev.text
        )

    async def _media_flt(_, __, ev):
        u = ev.from_user or ev.sender_chat
        return bool(
            u
            and u.id == uid
            and ev.chat.id == query.message.chat.id
            and ((photo and ev.photo) or (document and ev.document))
        )

    handler_filter = _media_flt if (photo or document) else _flt
    bound_pfunc = partial(pfunc, token=token)
    h = client.add_handler(
        MessageHandler(bound_pfunc, filters=create(handler_filter)), group=-1
    )

    try:
        while handler_token_active(uid, token):
            await sleep(0.5)
            elapsed = _t() - start
            if elapsed > timeout:
                clear_handler_token(uid, token)
                await rfunc()
            elif _t() - upd > 8 and handler_token_active(uid, token):
                upd = _t()
                try:
                    m = await client.get_messages(
                        query.message.chat.id, query.message.id
                    )
                    lines = m.text.split("\n")
                    lines[-1] = (
                        f"<b>╰</b> <b>Time Left :</b> <code>{round(timeout - elapsed, 1)} sec</code>"
                    )
                    await edit_message(m, "\n".join(lines), m.reply_markup)
                except Exception:
                    pass
    finally:
        clear_handler_token(uid, token)
        client.remove_handler(*h)


@new_task
async def _text_msg_handler(_, message, skey, rfunc, token=None):
    uid = message.from_user.id
    clear_handler_token(uid, token)
    update_user_ldata(uid, skey, message.text.strip())
    await delete_message(message)
    await DbManger().update_user_data(uid)
    await rfunc()
