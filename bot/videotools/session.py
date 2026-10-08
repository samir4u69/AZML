from asyncio import Event, TimeoutError as AIOTimeoutError, wait_for
from os import path as ospath, walk
from uuid import uuid4

from aiofiles.os import path as aiopath
from natsort import natsorted

from .. import config_dict, user_data
from ..helper.ext_utils.bot_utils import sync_to_async, update_user_ldata
from ..helper.ext_utils.db_handler import DbManger
from ..helper.ext_utils.leech_utils import get_document_type

_sessions: dict = {}
_msg_id_to_mid: dict = {}  # menu_msg.id -> listener.mid
handler_dict: dict = {}
_selector_futures: dict = {}


def _register_menu_msg(msg_id: int, mid: str):
    _msg_id_to_mid[msg_id] = mid


def _unregister_menu_msg(msg_id: int):
    _msg_id_to_mid.pop(msg_id, None)


VT_MRES_DEFAULT = "1080p,720p,480p"

VT_FUNCS = {
    "encode": ("Encode", "Re-Encode With Custom Codec/CRF/Preset", "VT_ENCODE_ENABLED"),
    "convert": ("Convert", "Change Container Or Remux Streams", "VT_CONVERT_ENABLED"),
    "multi_res": (
        "Multi-Resolution",
        "Generate 480p/720p/1080p Variants",
        "VT_MULTI_RES_ENABLED",
    ),
    "merge_video": (
        "Video + Video",
        "Combined Multiple Video Clips Into One File",
        "VT_MERGE_VIDEO_ENABLED",
    ),
    "mux_audio": (
        "Video + Audio",
        "Mux External Audio Into Video",
        "VT_MUX_AUDIO_ENABLED",
    ),
    "softsub": ("Video + Subtitle", "Add Subtitle As Soft Track", "VT_SOFTSUB_ENABLED"),
    "mux_all": (
        "Video+Audio+Sub",
        "Combine Video, Audio And Subtitle",
        "VT_MUX_ALL_ENABLED",
    ),
    "introsub": (
        "IntroSub",
        "Add Subtitle As Introsub Soft Track",
        "VT_INTROSUB_ENABLED",
    ),
    "hardsub": ("HardSub", "Burn Subtitle Into Video Picture", "VT_HARDSUB_ENABLED"),
    "rm_sub": ("Remove Subs", "Strip All Subtitle Tracks", "VT_RM_SUB_ENABLED"),
    "rm_audio": ("Remove Audio", "Strip All Audio Tracks", "VT_RM_AUDIO_ENABLED"),
    "rm_streams": (
        "Remove Streams",
        "Selectively Remove Audio/Subtitle Streams",
        "VT_RM_STREAMS_ENABLED",
    ),
    "strip_metadata": (
        "Strip Metadata",
        "Remove All Metadata And Chapters",
        "VT_STRIP_META_ENABLED",
    ),
    "extract": (
        "Extract Subs/Audio",
        "Extract Audio, Subtitles, Or Both",
        "VT_EXTRACT_ENABLED",
    ),
    "swap_audio": (
        "Swap Audio",
        "Swap Position Of Two Audio Tracks",
        "VT_SWAP_AUDIO_ENABLED",
    ),
    "watermark": (
        "Watermark",
        "Overlay Image Or Text On Every Frame",
        "VT_WATERMARK_ENABLED",
    ),
    "convert_audio": (
        "Convert Audio",
        "Convert Audio File Format (mp3/aac/flac/ogg/opus/wav)",
        "VT_CONVERT_AUDIO_ENABLED",
    ),
    "aspect_ratio": (
        "Aspect Ratio",
        "Change Video Aspect Ratio (e.g. 16:9, 4:3)",
        "VT_ASPECT_RATIO_ENABLED",
    ),
}

_OPTS = {
    "VT_VCODEC": (
        ["libx264", "libx265", "libvpx-vp9", "libaom-av1", "copy"],
        "libx264",
    ),
    "VT_ACODEC": (["aac", "ac3", "libopus", "libmp3lame", "copy"], "aac"),
    "VT_CRF": (["16", "18", "20", "23", "26", "28", "30", "35"], "23"),
    "VT_PRESET": (
        ["ultrafast", "superfast", "veryfast", "fast", "medium", "slow", "veryslow"],
        "medium",
    ),
    "VT_RES": (["", "1920:1080", "1280:720", "854:480", "640:360", "3840:2160"], ""),
    "VT_FPS": (["", "24", "25", "30", "60"], ""),
    "VT_EXT": (["mkv", "mp4", "avi", "webm", "ts", "mov"], "mkv"),
    "VT_CONV_EXT": (["mkv", "mp4", "avi", "webm", "ts", "m4v"], "mkv"),
    "VT_CONV_VCODEC": (["copy", "libx264", "libx265", "libvpx-vp9"], "copy"),
    "VT_CONV_ACODEC": (["copy", "aac", "ac3", "opus"], "copy"),
    "VT_MRES_LIST": (None, VT_MRES_DEFAULT),
    "VT_MRES_MODE": (["quality", "compressed"], "quality"),
    "VT_MUX_REPLACE": (["true", "false"], "true"),
    "VT_SUB_LANG": (None, "und - Downloader Zone"),
    "VT_ALL_LANG": (None, "und - Downloader Zone"),
    "VT_ALL_SUB_LANG": (None, "und - Downloader Zone"),
    "VT_ISUB_TEXT": (None, "Intro"),
    "VT_ISUB_LANG": (None, "und - Downloader Zone"),
    "VT_ISUB_DURATION": (["30", "60", "90", "120", "150", "180", "300"], "90"),
    "VT_ISUB_SIZE": (
        ["14", "16", "18", "20", "22", "24", "28", "32", "36", "48"],
        "20",
    ),
    "VT_ISUB_COLOR": (
        ["white", "yellow", "cyan", "green", "orange", "red", "black"],
        "white",
    ),
    "VT_ISUB_POS": (
        [
            "bottom_left",
            "bottom_center",
            "bottom_right",
            "middle_left",
            "middle_center",
            "middle_right",
            "top_left",
            "top_center",
            "top_right",
        ],
        "bottom_center",
    ),
    "VT_HS_SIZE": (["14", "16", "18", "20", "22", "24", "28"], "18"),
    "VT_HS_COLOR": (["white", "yellow", "cyan", "orange", "red", "black"], "white"),
    "VT_HS_EXT": (["mkv", "mp4"], "mkv"),
    "VT_EXTRACT_MODE": (["audio", "subtitles", "both"], "audio"),
    "VT_WM_TYPE": (["text", "image"], "text"),
    "VT_WM_TEXT": (None, "Downloader Zone"),
    "VT_WM_IMAGE": (None, ""),
    "VT_WM_POS": (
        ["bottom_right", "bottom_left", "top_right", "top_left", "center"],
        "bottom_right",
    ),
    "VT_WM_SIZE": (["16", "18", "20", "24", "28", "32", "36", "48"], "24"),
    "VT_WM_COLOR": (["white", "yellow", "cyan", "orange", "red", "black"], "white"),
    "VT_WM_OPACITY": (["0.3", "0.5", "0.6", "0.7", "0.8", "0.9", "1.0"], "0.8"),
    "VT_WM_FONT": (None, ""),
    "VT_CONV_AUDIO_EXT": (
        ["mp3", "aac", "flac", "ogg", "opus", "wav", "m4a", "wma"],
        "mp3",
    ),
    "VT_CONV_AUDIO_BITRATE": (
        ["128k", "192k", "256k", "320k", "lossless"],
        "192k",
    ),
    "VT_ASPECT_RATIO_VALUE": (
        ["16:9", "4:3", "21:9", "1:1", "3:2", "5:4", "16:10"],
        "16:9",
    ),
}

_FUNC_KEYS = {
    "encode": [
        "VT_VCODEC",
        "VT_ACODEC",
        "VT_CRF",
        "VT_PRESET",
        "VT_RES",
        "VT_FPS",
        "VT_EXT",
    ],
    "convert": ["VT_CONV_EXT", "VT_CONV_VCODEC", "VT_CONV_ACODEC"],
    "multi_res": ["VT_MRES_LIST", "VT_MRES_MODE"],
    "mux_audio": ["VT_MUX_REPLACE", "VT_ALL_LANG"],
    "softsub": ["VT_SUB_LANG"],
    "mux_all": ["VT_ALL_LANG", "VT_ALL_SUB_LANG"],
    "introsub": [
        "VT_ISUB_TEXT",
        "VT_ISUB_LANG",
        "VT_ISUB_DURATION",
        "VT_ISUB_SIZE",
        "VT_ISUB_COLOR",
        "VT_ISUB_POS",
    ],
    "hardsub": ["VT_HS_SIZE", "VT_HS_COLOR", "VT_HS_EXT"],
    "extract": ["VT_EXTRACT_MODE"],
    "watermark": [
        "VT_WM_TYPE",
        "VT_WM_TEXT",
        "VT_WM_IMAGE",
        "VT_WM_FONT",
        "VT_WM_POS",
        "VT_WM_SIZE",
        "VT_WM_COLOR",
        "VT_WM_OPACITY",
    ],
    "convert_audio": ["VT_CONV_AUDIO_EXT", "VT_CONV_AUDIO_BITRATE"],
    "aspect_ratio": ["VT_ASPECT_RATIO_VALUE"],
}

_RES_PRESETS = {
    "4K": "3840:2160",
    "1080p": "1920:1080",
    "720p": "1280:720",
    "480p": "854:480",
    "360p": "640:360",
    "240p": "426:240",
}


def _uv(uid: int, key: str) -> str:
    return user_data.get(uid, {}).get(key, _OPTS.get(key, (None, ""))[1])


class VTConfiguration:
    def __init__(self, user_id: int, func_key: str = None):
        self.user_id = user_id
        self.func_key = func_key
        self.config_data = {}
        self._load_config()

    def _load_config(self):
        ud = user_data.get(self.user_id, {})
        if self.func_key:
            for key in _FUNC_KEYS.get(self.func_key, []):
                self.config_data[key] = ud.get(key, _OPTS.get(key, (None, ""))[1])

    def get(self, key: str, default=None):
        return self.config_data.get(key, default or _OPTS.get(key, (None, ""))[1])

    def update(self, key: str, value):
        self.config_data[key] = value
        update_user_ldata(self.user_id, key, value)

    async def save(self):
        await DbManger().update_user_data(self.user_id)

    def to_dict(self):
        return dict(self.config_data)


class VTSession:
    def __init__(self, listener, dl_path, gid):
        self.listener = listener
        self.dl_path = dl_path
        self.gid = gid
        self.mid = listener.mid
        self.user_id = listener.user_id
        self.files = []
        self.menu_msg = None
        self.func_key = None
        self.browsed_func = None
        self.pre_config = False
        self._ready = Event()
        self.media_type_cache = {}
        self.merge_groups = None
        self.merge_name = None
        self.streams_info = []
        self.remove_audio_idx = set()
        self.remove_sub_idx = set()
        self.swap_a_idx = None
        self.swap_b_idx = None
        self.extract_audio_idx = set()
        self.extract_sub_idx = set()
        self.extract_use_selection = False
        self.extract_mode = "both"  # audio, subtitles, or both
        self.audio_page = 0
        self.sub_page = 0
        self.selection_deadline = 0.0

    def signal(self):
        self._ready.set()

    async def wait(self, timeout=300) -> bool:
        try:
            await wait_for(self._ready.wait(), timeout)
            self._ready.clear()
            return True
        except AIOTimeoutError:
            return False

    async def _get_streams(self, file):
        self.streams_info = await get_streams(file)
        if not self.streams_info:
            raise ValueError("No streams found")

    async def _init_session(self, files):
        self.files = files
        self.streams_info = []
        self.remove_audio_idx = set()
        self.remove_sub_idx = set()
        self.extract_audio_idx = set()
        self.extract_sub_idx = set()
        self.swap_a_idx = None
        self.swap_b_idx = None
        self.func_key = None
        self.browsed_func = None
        self._selector = None
        self._last_msg = None
        self.menu_msg = None
        self._handler = None
        self.audio_page = 0
        self.sub_page = 0
        self.selection_deadline = 0
        self.pre_config = False


def _is_sub(path: str) -> bool:
    return ospath.splitext(path)[1].lower() in (".srt", ".ass", ".ssa", ".vtt", ".sub")


def _func_enabled(fkey: str) -> bool:
    return config_dict.get(VT_FUNCS[fkey][2], True)


def new_handler_token(uid: int):
    token = uuid4().hex
    handler_dict[uid] = token
    return token


def clear_handler_token(uid: int, token) -> None:
    if handler_dict.get(uid) == token:
        handler_dict.pop(uid, None)


def handler_token_active(uid: int, token) -> bool:
    return handler_dict.get(uid) == token


async def _collect_files(dl_path: str) -> list:
    if await aiopath.isfile(dl_path):
        if _is_sub(dl_path):
            return [dl_path]
        is_v, is_a, _ = await get_document_type(dl_path)
        return [dl_path] if (is_v or is_a) else []
    files = []
    for dirp, _, fnames in await sync_to_async(walk, dl_path, topdown=False):
        for fn in natsorted(fnames):
            fp = ospath.join(dirp, fn)
            if _is_sub(fp):
                files.append(fp)
                continue
            is_v, is_a, _ = await get_document_type(fp)
            if is_v or is_a:
                files.append(fp)
    return files


def _get_vt_session_for_callback(owner: int, mid: int = None, msg_id: int = None):
    if mid is not None:
        sess = _sessions.get(mid)
        if not sess:
            return None, "Session not found!"
        if getattr(sess, "user_id", None) != owner:
            return None, "Session owner mismatch!"
        return sess, None

    if msg_id is not None:
        mid = _msg_id_to_mid.get(msg_id)
        if mid is None:
            return None, "Session not found!"
        sess = _sessions.get(mid)
        if not sess:
            return None, "Session not found!"
        if getattr(sess, "user_id", None) != owner:
            return None, "Session owner mismatch!"
        return sess, None

    for s in _sessions.values():
        if s and getattr(s, "user_id", None) == owner:
            return s, None

    return None, "Session expired or not found."
