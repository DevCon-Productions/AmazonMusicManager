from dataclasses import dataclass, field


@dataclass
class Song:
    title: str
    artist: str
    album: str = ""
    release_date: str = ""
    genre: str = ""
    duration: str = ""
    asin: str = ""
    album_asin: str = ""


@dataclass
class Playlist:
    name: str
    songs: list[Song] = field(default_factory=list)
