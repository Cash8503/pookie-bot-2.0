import time
from types import SimpleNamespace

from cogs import _rank_tracking as state
from cogs.tasks import rank_tracker


class FakeSettings:
    def __init__(self):
        self._cache = {}
        self._user_cache = {}

    def get(self, guild_id, namespace, key, default=None):
        return self._cache.get(guild_id, {}).get(namespace, {}).get(key, default)

    def get_user(self, user_id, namespace, key, default=None):
        return self._user_cache.get(user_id, {}).get(namespace, {}).get(key, default)

    async def set_user(self, user_id, namespace, key, value):
        self._user_cache.setdefault(user_id, {}).setdefault(namespace, {})[key] = value


class FakeGuild:
    def __init__(self, guild_id, members):
        self.id = guild_id
        self._members = {member.id: member for member in members}

    @property
    def members(self):
        return list(self._members.values())

    def get_member(self, user_id):
        return self._members.get(user_id)

    def get_channel(self, _channel_id):
        return None


class FakeBot:
    def __init__(self, settings, guilds):
        self.settings = settings
        self.guilds = guilds

    def get_guild(self, guild_id):
        return next((guild for guild in self.guilds if guild.id == guild_id), None)


def _member(user_id):
    return SimpleNamespace(id=user_id, bot=False, display_name=f"User {user_id}")


def _snapshot():
    return {role: None for role in rank_tracker.ROLES}


def _player_data():
    return {"summary": {"competitive": {"pc": {}}}}


def test_unavailable_accounts_use_daily_cadence():
    now = 100_000.0
    unavailable = {
        "status": state.STATUS_UNAVAILABLE,
        "last_attempt_at": now,
    }

    assert state.check_is_due({"status": state.STATUS_KNOWN_GOOD}, now=now)
    assert state.check_is_due({"status": state.STATUS_PENDING}, now=now)
    assert not state.check_is_due(unavailable, now=now + state.DAILY_RETRY_SECONDS - 1)
    assert state.check_is_due(unavailable, now=now + state.DAILY_RETRY_SECONDS)
    assert not state.check_is_due({"status": state.STATUS_ARCHIVED}, now=now + 999_999)


async def test_archive_preserves_connection_and_restores_prior_cadence():
    settings = FakeSettings()
    settings._user_cache[42] = {
        "ow": {
            "battletag": "Pookie#1234",
            "rank_snapshot": _snapshot(),
        }
    }
    await state.mark_unavailable(settings, 42, now=100.0)

    assert await state.archive_account(settings, 42, now=200.0)
    archived = state.get_tracker_state(settings, 42)
    assert archived["status"] == state.STATUS_ARCHIVED
    assert archived["resume_status"] == state.STATUS_UNAVAILABLE
    assert settings.get_user(42, "ow", "battletag") == "Pookie#1234"
    assert settings.get_user(42, "ow", "rank_snapshot") == _snapshot()

    assert await state.reactivate_account(settings, 42, now=300.0)
    restored = state.get_tracker_state(settings, 42)
    assert restored["status"] == state.STATUS_UNAVAILABLE
    assert restored["last_attempt_at"] == 100.0

    await state.mark_success(settings, 42, now=400.0)
    assert state.get_tracker_state(settings, 42)["status"] == state.STATUS_KNOWN_GOOD


async def test_scheduler_checks_only_due_shared_accounts(monkeypatch):
    settings = FakeSettings()
    settings._cache[1] = {"rank_tracker": {"channel": 99}}
    current_time = time.time()
    settings._user_cache = {
        1: {
            "ow": {
                "battletag": "Good#1234",
                "rank_snapshot": _snapshot(),
                state.STATE_KEY: {"status": state.STATUS_KNOWN_GOOD},
            }
        },
        2: {
            "ow": {
                "battletag": "Private#1234",
                "rank_snapshot": _snapshot(),
                state.STATE_KEY: {
                    "status": state.STATUS_UNAVAILABLE,
                    "last_attempt_at": current_time,
                },
            }
        },
        3: {
            "ow": {
                "battletag": "Departed#1234",
                "rank_snapshot": _snapshot(),
                state.STATE_KEY: {"status": state.STATUS_KNOWN_GOOD},
            }
        },
    }

    guild = FakeGuild(1, [_member(1), _member(2)])
    tracker = rank_tracker.RankTracker(FakeBot(settings, [guild]))
    tracker.session = object()
    calls = []

    async def fake_fetch(_session, player_id):
        calls.append(player_id)
        return _player_data()

    monkeypatch.setattr(rank_tracker, "fetch_player", fake_fetch)
    await tracker._run_check()

    assert calls == ["Good-1234"]
    assert state.get_tracker_state(settings, 1)["status"] == state.STATUS_KNOWN_GOOD
    assert state.get_tracker_state(settings, 2)["status"] == state.STATUS_UNAVAILABLE
    assert state.get_tracker_state(settings, 3)["status"] == state.STATUS_ARCHIVED
    assert settings.get_user(3, "ow", "battletag") == "Departed#1234"
