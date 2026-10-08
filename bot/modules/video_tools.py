from pyrogram.filters import regex
from pyrogram.handlers import CallbackQueryHandler

from bot import bot
from ..videotools import (
    proceed_video_tool,
    video_tool_pre_config,
    vt_callback,
    vt_text_input,
)

bot.add_handler(CallbackQueryHandler(vt_callback, filters=regex(r"^vtmenu")))

__all__ = [
    "proceed_video_tool",
    "video_tool_pre_config",
    "vt_callback",
    "vt_text_input",
]
