from typing import TypedDict, List


class Player(TypedDict):
    steamid: str
    communityvisibilitystate: int
    profilestate: int
    personaname: str
    profileurl: str
    avatar: str
    avatarmedium: str
    avatarfull: str
    avatarhash: str
    lastlogoff: int
    personastate: int
    realname: str
    primaryclanid: str
    timecreated: int
    personastateflags: int
    # gameextrainfo: str
    # gameid: str


class PlayerSummariesResponse(TypedDict):
    players: List[Player]


class PlayerSummaries(TypedDict):
    response: PlayerSummariesResponse


class Achievements(TypedDict):
    name: str
    image: bytes


class GameData(TypedDict):
    game_name: str
    play_time: str
    last_played: str
    game_image: bytes
    achievements: List[Achievements]
    completed_achievement_number: int
    total_achievement_number: int


class PlayerData(TypedDict):
    steamid: str
    player_name: str
    background: bytes
    avatar: bytes
    description: str
    recent_2_week_play_time: str
    game_data: List[GameData]


class DrawPlayerStatusData(TypedDict):
    game_name: str
    game_time: str
    last_play_time: str
    game_header: bytes
    achievements: List[Achievements]
    completed_achievement_number: int
    total_achievement_number: int
