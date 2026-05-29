from dataclasses import dataclass

from synology_photos_ai.config import Space


@dataclass(frozen=True)
class ApiNames:
    browse_item: str
    browse_general_tag: str
    browse_recently_added: str
    thumbnail: str


_PERSONAL = ApiNames(
    browse_item="SYNO.Foto.Browse.Item",
    browse_general_tag="SYNO.Foto.Browse.GeneralTag",
    browse_recently_added="SYNO.Foto.Browse.RecentlyAdded",
    thumbnail="SYNO.Foto.Thumbnail",
)

_SHARED = ApiNames(
    browse_item="SYNO.FotoTeam.Browse.Item",
    browse_general_tag="SYNO.FotoTeam.Browse.GeneralTag",
    browse_recently_added="SYNO.FotoTeam.Browse.RecentlyAdded",
    thumbnail="SYNO.FotoTeam.Thumbnail",
)


def api_names_for(space: Space) -> ApiNames:
    return _PERSONAL if space == Space.PERSONAL else _SHARED
