"""终末地插件内各服务与存储的唯一实例；其他模块从这里 import，不要另建。"""

from __future__ import annotations

from .account.client import EndfieldOfficialClient
from .account.store import EndfieldStore
from .archives.store import ArchiveSnapshotStore
from .calendar.akedata import AkeDataVersionCalendarSource
from .calendar.official import OfficialVersionCalendarSource
from .catalog.random_wiki import RandomWikiService
from .catalog.refresh import CollectionSnapshotRefresher
from .catalog.service import EndfieldService
from .gacha.assets import EndfieldGachaAssetCache
from .medals.store import MedalSnapshotStore
from .ownership.draw import draw_ownership_stats
from .ownership.service import OwnershipStatsService, register_ownership_stats_renderer
from .providers.warfarin import WarfarinClient
from .stages.service import EndfieldStageService
from .usage.service import UsageStatsService


client = WarfarinClient()
service = EndfieldService(client)
stage_service = EndfieldStageService(client)
gacha_asset_cache = EndfieldGachaAssetCache(service)
account_store = EndfieldStore()
official_client = EndfieldOfficialClient()
ownership_stats_service = OwnershipStatsService(account_store, official_client)
register_ownership_stats_renderer(draw_ownership_stats)
usage_stats_service = UsageStatsService(account_store, official_client)
calendar_source = AkeDataVersionCalendarSource(client)
official_calendar_source = OfficialVersionCalendarSource()
medal_store = MedalSnapshotStore()
archive_store = ArchiveSnapshotStore()
collection_refresher = CollectionSnapshotRefresher(service, medal_store, archive_store)
random_wiki_service = RandomWikiService(service, stage_service, lambda: archive_store.load_current_view())
