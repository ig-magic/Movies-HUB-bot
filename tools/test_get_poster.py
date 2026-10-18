"""Test harness: exercises utils.get_poster() with stubbed heavy deps.

It verifies the real code path of the bot (search -> filter -> details)
without needing MongoDB or pyrogram to be installed.
"""
import asyncio
import os
import sys
import types

REPO = "/home/user/Movies-HUB-bot"
sys.path.insert(0, REPO)

# --- environment required by info.py -------------------------------------
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.setdefault("LOG_CHANNEL", "-1001234567890")
os.environ.setdefault("CHANNELS", "-1001234567890")
os.environ.setdefault("SUPPORT_CHAT", "testsupport")
os.environ.setdefault("SUPPORT_CHAT_ID", "-1001234567890")
os.environ.setdefault("DATABASE_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DATABASE_NAME", "testdb")
os.environ.setdefault("COLLECTION_NAME", "testcol")
os.environ.setdefault("FILE_STORE_CHANNEL", "-1001234567890")
os.environ.setdefault("DELETE_CHANNELS", "0")
os.environ.setdefault("ADMIN", "123456789")

# --- stubs ---------------------------------------------------------------
pyrogram = types.ModuleType("pyrogram")
pyrogram_types = types.ModuleType("pyrogram.types")
pyrogram_errors = types.ModuleType("pyrogram.errors")
pyrogram_enums = types.ModuleType("pyrogram.enums")


class _Msg:
    def __init__(self, *a, **k):
        pass


class _Btn:
    def __init__(self, *a, **k):
        self.args = a


class _Markup:
    def __init__(self, *a, **k):
        pass


pyrogram_types.Message = _Msg
pyrogram_types.InlineKeyboardButton = _Btn
pyrogram_types.InlineKeyboardMarkup = _Markup


class _Status:
    MEMBER = "member"
    ADMINISTRATOR = "administrator"
    OWNER = "owner"
    BANNED = "banned"


pyrogram_enums.ChatMemberStatus = _Status


class _Err(Exception):
    pass


for name in (
    "UserNotParticipant",
    "UserIsBlocked",
    "InputUserDeactivated",
    "FloodWait",
    "MediaEmpty",
    "PhotoInvalidDimensions",
    "WebpageMediaEmpty",
    "MessageNotModified",
    "QueryIdInvalid",
):
    setattr(pyrogram_errors, name, type(name, (_Err,), {}))
pyrogram.errors = pyrogram_errors
pyrogram.types = pyrogram_types
pyrogram.enums = pyrogram_enums
sys.modules["pyrogram"] = pyrogram
sys.modules["pyrogram.types"] = pyrogram_types
sys.modules["pyrogram.errors"] = pyrogram_errors
sys.modules["pyrogram.enums"] = pyrogram_enums

database_pkg = types.ModuleType("database")
database_pkg.__path__ = []
users_chats_db = types.ModuleType("database.users_chats_db")
users_chats_db.db = object()
join_reqs = types.ModuleType("database.join_reqs")
join_reqs.JoinReqs = object
sys.modules["database"] = database_pkg
sys.modules["database.users_chats_db"] = users_chats_db
sys.modules["database.join_reqs"] = join_reqs

shortzy = types.ModuleType("shortzy")
shortzy.Shortzy = object
sys.modules["shortzy"] = shortzy

# --- the real test -------------------------------------------------------
import utils  # noqa: E402

print("module imported OK, imdb backend =", type(utils.imdb).__name__)


async def main():
    for query in ["the matrix", "breaking bad 2008", "iron man 2008", "not_a_real_movie_xyz"]:
        poster = await utils.get_poster(query, file="Iron.Man.2008.1080p.BluRay.mkv")
        if not poster:
            print("%-22s -> no data (graceful)" % query)
            continue
        print("%-22s -> %s (%s) | rating %s | votes %s | %s" % (
            query, poster["title"], poster["year"], poster["rating"],
            poster["votes"], poster["poster"]))
        assert poster["title"], "title missing"
        assert poster["url"].startswith("https://www.imdb.com/title/tt"), poster["url"]
        assert poster["genres"] != "N/A", "genres missing"
        assert poster["runtime"] != "N/A", "runtime missing"
    # bulk mode
    bulk = await utils.get_poster("the matrix", bulk=True)
    print("bulk results:", [(b.movieID, b.get("title"), b.get("kind")) for b in bulk])
    print("ALL GOOD")


asyncio.run(main())
