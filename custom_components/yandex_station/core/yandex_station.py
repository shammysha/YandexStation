import asyncio
import binascii
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import yaml
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.components.media_source.models import BrowseMediaSource
from homeassistant.core import callback, split_entity_id, Event
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceRegistry
from homeassistant.helpers.entity_registry import EntityRegistry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoreEntity,
    RestoredExtraData,
)
from homeassistant.helpers.template import Template
from homeassistant.helpers.event import (
    TrackStates, 
    EventStateChangedData, 
    EventEntityRegistryUpdatedData, 
    async_track_state_change_filtered,
    async_track_entity_registry_updated_event
)
from homeassistant.util import dt
from . import stream, utils
from .const import DATA_CONFIG, DOMAIN
from .yandex_glagol import YandexGlagol
from .yandex_music import get_file_info
from .yandex_quasar import YandexQuasar
from ..hass import shopping_list

_LOGGER = logging.getLogger(__name__)

RE_MUSIC_ID = re.compile(r"^\d+(:\d+)?$")


BASE_FEATURES = (
    MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.SELECT_SOUND_MODE
    | MediaPlayerEntityFeature.BROWSE_MEDIA
)

CLOUD_FEATURES = (
    BASE_FEATURES
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.NEXT_TRACK
)
LOCAL_FEATURES = (
    BASE_FEATURES
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.SHUFFLE_SET
)

SOUND_MODE1 = "Произнеси текст"
SOUND_MODE2 = "Выполни команду"

MEDIA_DEFAULT = [
    {
        "title": "Произнеси текст",
        "media_content_type": "text",
        "thumbnail": "https://brands.home-assistant.io/_/tts/icon.png",
    },
    {
        "title": "Выполни команду",
        "media_content_type": "command",
        "thumbnail": "https://brands.home-assistant.io/_/automation/icon.png",
    },
    {
        "title": "Медиа",
        "thumbnail": "https://brands.home-assistant.io/_/media_source/logo.png",
        "domain": "media_source",
    },
]

SOURCE_STATION = "Станция"
SOURCE_HDMI = "HDMI"

# Thanks to: https://github.com/iswitch/ha-yandex-icons
CUSTOM = {
    # колонки Яндекса
    "yandexstation": ["yandex:station", "Яндекс", "Станция (2018)"],
    "yandexstation_2": ["yandex:station-max", "Яндекс", "Станция Макс (2020)"],
    "yandexmini": ["yandex:station-mini", "Яндекс", "Станция Мини (2019)"],
    "yandexmini_2": ["yandex:station-mini-2", "Яндекс", "Станция Мини 2 (2021)"],
    "bergamot": ["yandex:station-mini-3", "Яндекс", "Станция Мини 3 (2024)"],
    "yandexmicro": ["yandex:station-lite", "Яндекс", "Станция Лайт (2021)"],
    "plum": ["yandex:station-lite-2", "Яндекс", "Станция Лайт 2 (2024)"],
    "yandexmidi": ["yandex:station-2", "Яндекс", "Станция 2 (2022)"],  # zigbee
    "cucumber": ["yandex:station-midi", "Яндекс", "Станция Миди (2023)"],  # zigbee
    "chiron": ["yandex:station-duo-max", "Яндекс", "Станция Дуо Макс (2023)"],  # zigbee
    # платформа Яндекс.ТВ (без облачного управления!)
    "yandexmodule": ["yandex:module", "Яндекс", "Модуль (2019)"],
    "yandexmodule_2": ["yandex:module-2", "Яндекс", "Модуль 2 (2021)"],
    "yandex_tv": ["mdi:television-classic", "Unknown", "ТВ с Алисой"],
    # ТВ с Алисой
    "goya": ["mdi:television-classic", "Яндекс", "ТВ (2022)"],
    "magritte": ["mdi:television-classic", "Яндекс", "ТВ Станция (2023)"],
    "monet": ["mdi:television-classic", "Яндекс", "ТВ Станция Бейсик (2024)"],
    # колонки НЕ Яндекса
    "lightcomm": ["yandex:dexp-smartbox", "DEXP", "Smartbox"],
    "elari_a98": ["yandex:elari-smartbeat", "Elari", "SmartBeat"],
    "linkplay_a98": ["yandex:irbis-a", "IRBIS", "A"],
    "wk7y": ["yandex:lg-xboom-wk7y", "LG", "XBOOM AI ThinQ WK7Y"],
    "prestigio_smart_mate": ["yandex:prestigio-smartmate", "Prestigio", "Smartmate"],
    "jbl_link_music": ["yandex:jbl-link-music", "JBL", "Link Music"],
    "jbl_link_portable": ["yandex:jbl-link-portable", "JBL", "Link Portable"],
    # экран с Алисой
    "quinglong": ["yandex:display-xiaomi", "Xiaomi", "Smart Display 10R X10G (2023)"],
    # не колонки
    "saturn": ["yandex:hub", "Яндекс", "Хаб (2023)"],
    "mike": ["yandex:lg-xboom-wk7y", "Яндекс", "IP камера (2025)"],
}

STATES_OUT_OF_USE = [ MediaPlayerState.OFF, STATE_UNAVAILABLE, STATE_UNKNOWN ]

# noinspection PyAbstractClass
class YandexSource(BrowseMediaSource):
    def __init__(self, **kwargs):
        query = {}
        if kwargs.get("media_content_id"):
            query["message"] = kwargs.pop("media_content_id")
            kwargs.setdefault("can_expand", False)
        if kwargs.get("media_content_type"):
            query["type"] = kwargs.pop("media_content_type")
        if kwargs.get("template"):
            query["template"] = template = kwargs.pop("template")
            kwargs.setdefault("can_expand", "message" in template)
        if kwargs.get("extra"):
            extra = kwargs.pop("extra")
            query["volume_level"] = extra["volume_level"]
        if query:
            kwargs["identifier"] = utils.encode_media_source(query)

        kwargs = {
            "domain": "tts",  # will show message/say dialog
            "identifier": None,
            "media_class": MediaClass.APP,  # needs for icon
            "media_content_type": MediaType.APP,  # important for HA v2025.6
            "can_play": False,  # show play button in
            "can_expand": True,  # true - show window with text input
            **kwargs,  # override all default values
        }
        super().__init__(**kwargs)


# noinspection PyAbstractClass
class MediaBrowser(MediaPlayerEntity):
    media_cache: list = None

    async def async_browse_media(
        self,
        media_content_type: str = None,
        media_content_id: str = None,
    ) -> BrowseMedia:
        if not MediaBrowser.media_cache:
            conf = self.hass.data[DOMAIN][DATA_CONFIG]
            conf = conf.get("media_source") or MEDIA_DEFAULT
            MediaBrowser.media_cache = [YandexSource(**item) for item in conf]

        if media_content_id:
            if not media_content_id.startswith("media-source://tts"):
                return await media_source.async_browse_media(
                    self.hass, media_content_id
                )

            for media in MediaBrowser.media_cache:
                if media.media_content_id == media_content_id:
                    return media

        return BrowseMediaSource(
            title=self.name,
            children=MediaBrowser.media_cache,
            domain=None,
            identifier=None,
            media_class=None,
            media_content_type=None,
            can_play=False,
            can_expand=True,
        )


# noinspection PyAbstractClass
class YandexStationBase(MediaBrowser, RestoreEntity):
    local_state: Optional[dict] = None
    # для управления громкостью Алисы
    alice_volume: Optional[dict] = None

    # true of false if device has HDMI
    hdmi_audio: Optional[bool] = None

    glagol: YandexGlagol = None

    def __init__(self, quasar: YandexQuasar, device: dict):
        self.quasar = quasar
        self.device = device
        self.requests = {}

        self._attr_assumed_state = True
        self._attr_is_volume_muted = False
        self._attr_media_image_remotely_accessible = True
        self._attr_name = device["name"]
        self._attr_should_poll = True
        self._attr_state = MediaPlayerState.IDLE
        self._attr_sound_mode_list = [SOUND_MODE1, SOUND_MODE2]
        self._attr_sound_mode = SOUND_MODE1
        self._attr_supported_features = CLOUD_FEATURES
        self._attr_volume_level = 0.5
        self._attr_unique_id = device["quasar_info"]["device_id"]

        self._attr_device_info = info = DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            name=self.device["name"],
        )
        if custom := CUSTOM.get(self.device_platform):
            info["manufacturer"] = custom[1]
            info["model"] = custom[2]
        if mac := device.get("mac"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}

        # backward compatibility
        self.entity_id = "media_player."
        if self.device_platform in ("yandexmodule", "yandexmodule_2"):
            self.entity_id += "yandex_module"
        elif self.device_platform in ("yandex_tv", "goya", "magritte"):
            self.entity_id += "yandex_tv"
        else:
            self.entity_id += "yandex_station"
        self.entity_id += f"_{self._attr_unique_id.lower()}"

        quasar.subscribe_update(device["id"], self.on_update)

    @property
    def extra_state_attributes(self):
        if self.local_state:
            return {"alice_state": self.local_state["aliceState"]}
        return None

    def on_update(self, device: dict):
        if not self.hass:
            return

        if "scenario_name" in device:
            for item in device["capabilities"]:
                event_data = item["state"]
                event_data["entity_id"] = self.entity_id
                event_data["name"] = self.name
                event_data["scenario_name"] = device["scenario_name"]
                self.debug(f"yandex_scenario: {event_data}")
                self.hass.bus.async_fire("yandex_scenario", event_data)
        else:
            for item in device["capabilities"]:
                if (
                    item["type"] != "devices.capabilities.quasar.server_action"
                    or not item["state"]
                ):
                    continue

                event_data = item["state"]
                event_data["entity_id"] = self.entity_id
                event_data["name"] = self.name
                self.debug(f"yandex_speaker: {event_data}")
                self.hass.bus.async_fire("yandex_speaker", event_data)

    # ADDITIONAL CLASS FUNCTION

    @property
    def device_platform(self) -> str:
        platform: str = self.device["quasar_info"]["platform"]
        if platform.startswith("yandex_tv"):
            platform = "yandex_tv"
        return platform

    def debug(self, text: str):
        _LOGGER.debug(f"{self.name} | {text}")

    async def init_local_mode(self):
        # self.debug(f"Init local mode (hass: {self.hass is not None})")
        if not self.glagol:
            self.glagol = YandexGlagol(self.quasar.session, self.device)
            self.glagol.update_handler = self.async_set_state

        await self.glagol.start_or_restart()

        await self.init_hdmi_audio()

    async def init_hdmi_audio(self):
        if self._attr_source_list:
            return

        if self.device_platform not in ("yandexstation", "yandexstation_2"):
            return

        try:
            config, _ = await self.quasar.get_device_config(self.device)
            self.hdmi_audio = config.get("hdmiAudio", False)
        except:
            _LOGGER.warning("Не получается получить настройки HDMI")
            return

        # for HomeKit source list support
        self._attr_device_class = MediaPlayerDeviceClass.TV
        self._attr_source = SOURCE_HDMI if self.hdmi_audio else SOURCE_STATION
        self._attr_source_list = [SOURCE_STATION, SOURCE_HDMI]

    async def sync_hdmi_audio(self):
        # if HDMI supported and state loaded
        if self.hdmi_audio is None:
            return

        if self._attr_source == SOURCE_STATION:
            enabled = False
        elif self._attr_source == SOURCE_HDMI:
            enabled = True
        else:
            return

        # check if something changed
        if self.hdmi_audio == enabled:
            return

        try:
            config, version = await self.quasar.get_device_config(self.device)
            if enabled:
                config["hdmiAudio"] = True
            else:
                config.pop("hdmiAudio", None)
            await self.quasar.set_device_config(self.device, config, version)
        except:
            _LOGGER.warning("Не получается изменить настройки HDMI")
            return

        self.hdmi_audio = enabled

    async def response(self, card: dict, request_id: str):
        if not card:
            self.debug(f"Empty response on request: {request_id}")
            return

        self.debug(f"{card['text']} | {request_id}")

        if card["type"] == "simple_text":
            text = card["text"]

        elif card["type"] == "text_with_button":
            text = card["text"]

            for button in card["buttons"]:
                assert button["type"] == "action"
                for directive in button["directives"]:
                    if directive["name"] == "open_uri":
                        title = button["title"]
                        uri = directive["payload"]["uri"]
                        text += f"\n[{title}]({uri})"

        else:
            _LOGGER.error(f"Неизвестный тип ответа: {card['type']}")
            return

        self.hass.bus.async_fire(
            f"{DOMAIN}_response",
            {
                "entity_id": self.entity_id,
                "name": self.name,
                "text": text,
                "request_id": request_id,
            },
        )

    async def _set_brightness(self, value: str):
        if self.device_platform not in (
            "yandexstation_2",
            "yandexmini_2",
            "cucumber",
            "plum",
            "bergamot",
        ):
            _LOGGER.warning("Поддерживаются только станции с экраном")
            return

        try:
            value = float(value)
        except:
            _LOGGER.exception(f"Недопустимое значение яркости: {value}")
            return

        config, version = await self.quasar.get_device_config(self.device)

        if "led" not in config:
            config["led"] = {"brightness": {"auto": True, "value": 0.5}}

        if 0 <= value <= 1:
            config["led"]["brightness"]["auto"] = False
            config["led"]["brightness"]["value"] = value
        else:
            config["led"]["brightness"]["auto"] = True

        await self.quasar.set_device_config(self.device, config, version)

    async def _set_beta(self, value: str):
        if value == "True":
            value = True
        elif value == "False":
            value = False
        else:
            return

        config, version = await self.quasar.get_device_config(self.device)
        config["beta"] = value
        await self.quasar.set_device_config(self.device, config, version)

    async def _set_locale(self, value: str):
        assert value in ("ru-RU", "en-US", "ar-SA", "kk-KZ", "tr-TR")

        config, version = await self.quasar.get_device_config(self.device)
        config["locale"] = value
        await self.quasar.set_device_config(self.device, config, version)

    async def _set_settings(self, value: str):
        data = yaml.safe_load(value)
        for k, v in data.items():
            await self.quasar.set_account_config(k, v)

    def _check_set_alice_volume(self, volume: int):
        # если уже есть активная громкость, или громкость голоса равна текущей
        # громкости колонки - ничего не делаем
        if self.alice_volume or volume == self.volume_level:
            return

        self.alice_volume = {
            "prev_volume": self.volume_level,
            "wait_state": "SPEAKING",
            "wait_ts": time.time() + 30,
        }

        self.hass.create_task(self.async_set_volume_level(volume))

    def _process_alice_volume(self, alice_state: str):
        volume = None

        # если что-то пошло не так, через 30 секунд возвращаем громкость
        if time.time() > self.alice_volume["wait_ts"]:
            if "prev_volume" in self.alice_volume:
                volume = self.alice_volume["prev_volume"]
            self.alice_volume = None

        elif self.alice_volume["wait_state"] == alice_state:
            if alice_state == "SPEAKING":
                self.alice_volume["wait_state"] = "IDLE"

            elif alice_state == "IDLE":
                volume = self.alice_volume["prev_volume"]
                self.alice_volume = None

        if volume:
            self.hass.create_task(self.async_set_volume_level(volume))

    @callback
    def yandex_dialog(self, media_type: str, media_id: str):
        """Passes TTS data to YandexDialogs component and return text command to
        start dialog with data CRC-hash as ID.
        """
        if media_type.startswith("dialog"):
            _, name, tag = media_type.split(":")
            payload = {
                "tts": media_id,
                "session": {"dialog": tag},
                "end_session": False,
            }
        else:
            _, name = media_type.split(":")
            payload = {"tts": media_id}

        crc = str(binascii.crc32(f"{self.entity_id}.{media_id}".encode()))
        try:
            dialog = self.hass.data["yandex_dialogs"]
            dialog.dialogs[crc] = payload
        except:
            _LOGGER.warning("Компонент Яндекс Диалогов не подключен")

        return f"СКАЖИ НАВЫКУ {name} {crc}"

    @callback
    def update_device_info(self, sw_version: str):
        if not self.hass:
            return
        registry: DeviceRegistry = self.hass.data["device_registry"]
        device = registry.async_get_device({(DOMAIN, self._attr_unique_id)}, None)
        registry.async_update_device(device.id, sw_version=sw_version)

    @callback
    def async_set_state(self, data: dict):
        if data is None:
            if self._attr_assumed_state:
                return

            self.debug("Возврат в облачный режим")
            self.local_state = None

            self._attr_assumed_state = True
            self._attr_media_artist = None
            self._attr_media_channel = None
            self._attr_media_content_id = None
            self._attr_media_content_type = None
            self._attr_media_duration = None
            self._attr_media_image_url = None
            self._attr_media_playlist = None
            self._attr_media_position = None
            self._attr_media_position_updated_at = None
            self._attr_media_series_title = None
            self._attr_media_title = None
            self._attr_repeat = None
            self._attr_should_poll = True
            self._attr_shuffle = None
            self._attr_supported_features = CLOUD_FEATURES

            self.async_write_ha_state()
            return

        state = data["state"]
        state.pop("timeSinceLastVoiceActivity", None)

        # skip same state
        if self.local_state == state:
            return

        self.local_state = state

        if "softwareVersion" in data:
            self.update_device_info(data["softwareVersion"])

        # возвращаем из состояния mute, если нужно
        # if self.prev_volume and state['volume']:
        #     self.prev_volume = None

        if self.alice_volume:
            self._process_alice_volume(state["aliceState"])

        # default attributes for local mode
        self._attr_assumed_state = False
        self._attr_available = True
        self._attr_should_poll = False
        self._attr_supported_features = LOCAL_FEATURES

        # optional attributes for local mode
        self._attr_media_artist = None
        self._attr_media_channel = None
        self._attr_media_content_type = None
        self._attr_media_image_url = None
        self._attr_media_playlist = None
        self._attr_media_series_title = None
        self._attr_repeat = None
        self._attr_shuffle = None

        if player_state := state.get("playerState"):
            if player_state["hasPrev"]:
                self._attr_supported_features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
            if player_state["hasNext"]:
                self._attr_supported_features |= MediaPlayerEntityFeature.NEXT_TRACK
            if player_state["duration"]:
                self._attr_supported_features |= MediaPlayerEntityFeature.SEEK

            # https://github.com/home-assistant/frontend/blob/dev/src/data/media-player.ts
            # supported computeMediaDescription: music/image/playlist/tvshow/channel
            if player_state["type"] == "Track":
                self._attr_media_artist = player_state["subtitle"] or None
                self._attr_media_content_type = MediaType.MUSIC
            elif player_state["type"] == "FmRadio":
                self._attr_media_content_type = "radio"
            elif player_state["liveStreamText"] == "Прямой эфир":
                self._attr_media_channel = player_state["subtitle"] or None
                self._attr_media_content_type = MediaType.CHANNEL
            elif player_state["playerType"] == "ru.yandex.quasar.app":
                self._attr_media_content_type = MediaType.TVSHOW
                self._attr_media_series_title = player_state["subtitle"] or None

            if player_state["playlistType"] == "Track":
                self._attr_media_playlist = MediaType.TRACK
            elif player_state["playlistType"] == "Artist":
                self._attr_media_playlist = MediaType.ARTIST
            elif player_state["playlistType"] == "Album":
                self._attr_media_playlist = MediaType.ALBUM
            elif player_state["playlistType"] == "Playlist":
                self._attr_media_playlist = MediaType.PLAYLIST
            elif player_state["playlistType"] == "FmRadio":
                self._attr_media_playlist = "radio"

            if extra := player_state["extra"]:
                if url := extra.get("coverURI"):
                    url = "https://" + url.replace("%%", "400x400")
                    self._attr_media_image_url = url

            if repeat := player_state["entityInfo"].get("repeatMode"):
                if repeat == "None":
                    self._attr_repeat = RepeatMode.OFF
                elif repeat == "All":
                    self._attr_repeat = RepeatMode.ALL
                elif repeat == "One":
                    self._attr_repeat = RepeatMode.ONE

            if "shuffled" in player_state["entityInfo"]:
                self._attr_shuffle = player_state["entityInfo"]["shuffled"]

            # main attributes for local mode
            self._attr_media_content_id = player_state["id"]
            self._attr_media_duration = player_state["duration"] or None
            self._attr_media_position = player_state["progress"]
            self._attr_media_position_updated_at = datetime.now(timezone.utc)
            self._attr_media_title = player_state["title"]
            self._attr_state = (
                MediaPlayerState.PLAYING
                if state["playing"]
                else MediaPlayerState.PAUSED
            )
        else:
            self._attr_media_content_id = None
            self._attr_media_duration = None
            self._attr_media_position = None
            self._attr_media_position_updated_at = None
            self._attr_media_title = None
            self._attr_state = MediaPlayerState.IDLE

        if isinstance(state["volume"], float):
            if state["volume"] > 0:
                self._attr_is_volume_muted = False
                self._attr_volume_level = state["volume"]
            else:
                self._attr_is_volume_muted = True

        if self.hass:
            self.async_write_ha_state()

    # BASE MEDIA PLAYER FUNCTIONS

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return RestoredExtraData({"sound_mode": self._attr_sound_mode})

    async def async_added_to_hass(self):
        if extra_data := await self.async_get_last_extra_data():
            data = extra_data.as_dict()
            self._attr_sound_mode = data["sound_mode"]

        if await utils.has_custom_icons(self.hass) and self.device_platform in CUSTOM:
            self._attr_icon = CUSTOM[self.device_platform][0]
            self.debug(f"Установка кастомной иконки: {self._attr_icon}")

        if "host" in self.device:
            await self.init_local_mode()

    async def async_will_remove_from_hass(self):
        if self.glagol:
            await self.glagol.stop()

    async def async_select_sound_mode(self, sound_mode: str):
        self._attr_sound_mode = sound_mode
        self.async_write_ha_state()

    async def async_select_source(self, source):
        self.debug(f"Change source to {source}")

        self._attr_source = source
        self.async_write_ha_state()

        await self.sync_hdmi_audio()

    async def async_mute_volume(self, mute: bool):
        volume = 0 if mute else self._attr_volume_level
        await self.async_set_volume_level(volume)

    async def async_set_volume_level(self, volume: float):
        # https://github.com/AlexxIT/YandexStation/issues/324
        if isinstance(volume, str):
            try:
                volume = float(volume)
            except Exception:
                return

        if self.local_state:
            # у станции округление громкости до десятых
            await self.glagol.send({"command": "setVolume", "volume": round(volume, 1)})

        else:
            # на Яндекс ТВ Станция (2023) громкость от 0 до 100
            # на колонках - от 0 до 10
            k = 100 if self.platform in ["magritte", "monet"] else 10
            await self.quasar.send(self.device, f"громкость на {round(k * volume)}")
            if volume > 0:
                self._attr_is_volume_muted = False
                self._attr_volume_level = round(volume, 2)
            else:
                # don't change volume_level so can back to previous value
                self._attr_is_volume_muted = True
            self.async_write_ha_state()

    async def async_volume_up(self):
        if self.local_state:
            await self.glagol.send(utils.external_command("sound_louder"))
        else:
            await super().async_volume_up()

    async def async_volume_down(self):
        if self.local_state:
            await self.glagol.send(utils.external_command("sound_quiter"))
        else:
            await super().async_volume_up()

    async def async_media_seek(self, position):
        if self.local_state:
            await self.glagol.send({"command": "rewind", "position": position})

    async def async_media_play(self):
        if self.local_state:
            await self.glagol.send({"command": "play"})

        else:
            await self.quasar.send(self.device, "продолжить")
            self._attr_state = MediaPlayerState.PLAYING
            self.async_write_ha_state()

    async def async_media_pause(self):
        if self.local_state:
            await self.glagol.send({"command": "stop"})

        else:
            await self.quasar.send(self.device, "пауза")
            self._attr_state = MediaPlayerState.PAUSED
            self.async_write_ha_state()

    async def async_media_stop(self):
        await self.async_media_pause()

    async def async_media_previous_track(self):
        if self.local_state:
            await self.glagol.send({"command": "prev"})
        else:
            await self.quasar.send(self.device, "прошлый трек")

    async def async_media_next_track(self):
        if self.local_state:
            await self.glagol.send({"command": "next"})
        else:
            await self.quasar.send(self.device, "следующий трек")

    async def async_turn_on(self):
        if self.local_state:
            await self.glagol.send(
                utils.update_form("personal_assistant.scenarios.player_continue")
            )
        else:
            await self.async_media_play()

    async def async_turn_off(self):
        if self.local_state:
            await self.glagol.send(
                utils.update_form("personal_assistant.scenarios.quasar.go_home")
            )
        else:
            await self.async_media_pause()

    async def async_set_repeat(self, repeat: RepeatMode):
        modes = {RepeatMode.ALL: "All", RepeatMode.ONE: "One"}
        mode = modes.get(repeat, "None")
        await self.glagol.send({"command": "repeat", "mode": mode})

    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self.glagol.send({"command": "shuffle", "enable": shuffle})

    async def async_update(self):
        # update online only while cloud connected
        if self.local_state:
            return
        await self.quasar.update_online_stats()
        self._attr_available = self.device.get("online", False)

    async def async_play_media(
        self, media_type: str, media_id: str, extra: dict = None, **kwargs
    ):
        # Format:  media-source://{domain}/{identifier}?message={user_input}
        # Example: media-source://tts/747970653d74657874?message=123
        if media_id.startswith(f"media-source://tts/"):
            # starting from HA v2025.5, "media_type" will always be "audio/mp3"
            query = utils.decode_media_source(media_id)
            if template := query.pop("template", ""):
                media_id = Template(template, self.hass).async_render(query)
            else:
                media_id = query["message"]
            if volume_level := query.get("volume_level"):
                extra.setdefault("volume_level", float(volume_level))
            if query_type := query.get("type"):
                media_type = query_type
            else:
                media_type = "text"  # for support Google TTS, etc.

        if not media_id:
            _LOGGER.warning("Получено пустое media_id")
            return

        # tts for backward compatibility and mini-media-player support
        if media_type == "tts":
            media_type = "text" if self._attr_sound_mode == SOUND_MODE1 else "command"
        elif media_type == "brightness":
            await self._set_brightness(media_id)
            return
        elif media_type == "beta":
            await self._set_beta(media_id)
            return
        elif media_type == "locale":
            await self._set_locale(media_id)
            return
        elif media_type == "settings":
            await self._set_settings(media_id)
            return
        elif media_type == "update_scenario":
            await self.quasar.update_scenario(media_id)
            return

        if self.local_state:
            if media_source.is_media_source_id(media_id):
                sourced_media = await media_source.async_resolve_media(
                    self.hass, media_id, self.entity_id
                )
                # we use the sourced_media.url to reduce the link size
                payload = utils.get_stream_url(
                    sourced_media.url, media_type, extra.get("metadata")
                )

            elif "https://" in media_id or "http://" in media_id:
                payload = utils.get_stream_url(
                    media_id, media_type, extra.get("metadata")
                )
                if not payload:
                    payload = await utils.get_media_payload(
                        self.quasar.session, media_id
                    )

            elif media_type.startswith(("text:", "dialog:")):
                payload = {
                    "command": "sendText",
                    "text": self.yandex_dialog(media_type, media_id),
                }

            elif media_type == "text":
                if extra and extra.get("volume_level") is not None:
                    self._check_set_alice_volume(extra["volume_level"])
                payload = utils.update_form(
                    "personal_assistant.scenarios.quasar.iot.repeat_phrase",
                    phrase_to_repeat=media_id,
                )

            elif media_type == "command":
                payload = {"command": "sendText", "text": media_id}

            elif media_type == "dialog":
                if extra and extra.get("volume_level") is not None:
                    self._check_set_alice_volume(extra["volume_level"])
                payload = utils.update_form(
                    "personal_assistant.scenarios.repeat_after_me",
                    request=utils.fix_dialog_text(media_id),
                )

            elif media_type == "draw_animation":
                payload = utils.draw_animation_command(media_id)

            elif media_type == "json":
                payload = json.loads(media_id)

            elif media_type == "shopping_list":
                coro = shopping_list.shopping_sync(self.hass, self.glagol)
                await self.hass.async_create_background_task(coro, self.name)
                return

            elif media_type.startswith("question"):
                card = await self.glagol.send({"command": "sendText", "text": media_id})
                request_id = media_type.split(":", 1)[1] if ":" in media_type else None
                await self.response(card, request_id)
                return

            elif media_type == "restart":
                await self.glagol.stop()
                await asyncio.sleep(float(media_id))
                await self.glagol.start_or_restart()
                return

            elif RE_MUSIC_ID.match(media_id):
                payload = {"command": "playMusic", "id": media_id, "type": media_type}

            else:
                payload = None

            if not payload:
                _LOGGER.warning(f"Unsupported local media: {media_id} {media_type}")
                return

            await self.glagol.send(payload)

        else:
            if media_type.startswith(("text:", "dialog:")):
                media_id = self.yandex_dialog(media_type, media_id)
                await self.quasar.send(self.device, media_id)

            elif media_type == "text":
                media_id = utils.fix_cloud_text(media_id)
                await self.quasar.send(self.device, media_id, is_tts=True)

            elif media_type == "command":
                media_id = utils.fix_cloud_text(media_id)
                await self.quasar.send(self.device, media_id)

            elif media_type == "brightness":
                await self._set_brightness(media_id)
                return

            else:
                _LOGGER.warning(f"Unsupported cloud media: {media_type}")
                return


class YandexStation(YandexStationBase):
    # {name: entity_id} pairs
    sync_sources: dict = None
    sync_enabled: bool = False
    
    sync_id: Optional[str] = None
    sync_playing: Optional[bool] = None
    sync_volume: Optional[float] = None
    sync_mute: Optional[bool] = None

    all_players: Optional[str] = None
    
    async def init_local_mode(self):
        await super().init_local_mode()
        
        if self.all_players is None:
            self.all_players = utils.get_all_media_player_entities(self.hass)
            
        if self.sync_sources is None:
            await self.async_build_source_list()
            self._attr_source = SOURCE_STATION

    async def async_select_source(self, source):
        if self.sync_mute is True:
            # включаем звук колонке, если выключали его
            self.hass.create_task(self.async_mute_volume(False))

        if self.sync_playing:
            # сбрасываем синхронизацию
            self.sync_playing = self.sync_id = self.sync_volume = self.sync_mute = None
            # останавливаем внешний медиаплеер
            self.sync_service_call("media_pause")

        await super().async_select_source(source)

        if self.sync_sources and (source := self.sync_sources.get(source)):
            self.sync_enabled = True
            if "platform" not in source:
                if entity := utils.get_entity(self.hass, source["entity_id"]):
                    source["platform"] = entity.platform.platform_name
        else:
            self.sync_enabled = False

    async def async_media_seek(self, position):
        await super().async_media_seek(position)

        if self.sync_enabled:
            entity_id = self.sync_sources[self._attr_source]["entity_id"]
            if entity := utils.get_entity(self.hass, entity_id):
                if entity.supported_features & MediaPlayerEntityFeature.SEEK:
                    await self.hass.services.async_call(
                        "media_player",
                        "media_seek",
                        {"entity_id": entity_id, "seek_position": position},
                    )

    async def async_build_source_list(self) -> None:
        self.sync_sources = {}
            
        for src in utils.get_media_players(self.hass, self.entity_id):
            if src.get("name"):
                self.sync_sources[src.get("name")] = src

        self._attr_device_class = MediaPlayerDeviceClass.TV
        self._attr_source_list = [SOURCE_STATION] + list(self.sync_sources.keys())
    
    @callback    
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        
        # Add listener
        self.async_on_remove(
            async_track_state_change_filtered(
                self.hass, TrackStates(False, set(), {"media_player"}), self._media_player_state_change_listener
            ).async_remove
        )    
        
        if self.all_players is not None:
            self.async_on_remove(
                async_track_entity_registry_updated_event(
                    self.hass, iter(self.all_players), self._media_player_registry_change_listener
                )
            )           

    async def _media_player_state_change_listener(self, event: Event[EventStateChangedData]) -> None:
        if (
            (
                event.data["old_state"] is None 
                or event.data["old_state"].state in STATES_OUT_OF_USE
            ) 
            and event.data["new_state"] is not None 
            and event.data["new_state"].state not in STATES_OUT_OF_USE
        ) or (
            (
                event.data["new_state"] is None 
                or event.data["new_state"].state in STATES_OUT_OF_USE
            ) 
            and event.data["old_state"] is not None 
            and event.data["old_state"].state not in STATES_OUT_OF_USE            
        ):
            await self.async_build_source_list()

            if self._attr_source not in self._attr_source_list:
                await self.async_select_source(SOURCE_STATION)
            else:
                self.async_write_ha_state()
                
    async def _media_player_registry_change_listener(self, event: Event[EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] != "update":
            return

        er: EntityRegistry = self.hass.data['entity_registry']
        ent_reg = er.async_get(event.data["entity_id"])

        if ent_reg.supported_features & MediaPlayerEntityFeature.PLAY_MEDIA:
            await self.async_build_source_list()

            if self._attr_source not in self._attr_source_list:
                await self.async_select_source(SOURCE_STATION)
            else:
                self.async_write_ha_state()
    
    def async_set_state(self, data: dict):
        super().async_set_state(data)

        if not self.sync_enabled or data is None or "playerState" not in data["state"]:
            return

        state = data["state"]
        player_state = state["playerState"]

        if self.sync_playing != data["state"]["playing"]:
            self.sync_playing = data["state"]["playing"]
            if self.sync_playing:
                if self.sync_id == player_state["id"]:
                    # продолжаем играть, если ID не изменился
                    self.sync_service_call("media_play")
            else:
                # останавливаем, если ничего не играет
                self.sync_service_call("media_pause")

        if self.sync_id != player_state["id"]:
            self.sync_id = player_state["id"]
            # запускаем новую песню, если ID изменился
            self.hass.create_task(self.sync_play_media(data))

        if state["volume"] and self.sync_volume != state["volume"]:
            self.sync_volume = state["volume"]
            self.sync_mute = None
            self.sync_service_call("volume_set", volume_level=state["volume"])

        # если музыка играет - глушим колонку Яндекса
        if self.sync_mute is True:
            # включаем громкость колонки, когда с ней разговариваем
            if state["aliceState"] != "IDLE":
                self.sync_mute = False
                self.hass.create_task(self.async_mute_volume(False))
        elif state["aliceState"] == "IDLE":
            self.sync_mute = True
            self.hass.create_task(self.async_mute_volume(True))

    async def sync_play_media(self, data: dict):
        self.debug("Sync state: play_media")

        source = self.sync_sources[self._attr_source]

        if source.get("platform") == "apple_tv":
            # For AirPlay receivers is not possible to change media_content_id, while
            # streaming to device is in progress. So we need to send media_stop command
            # to media_player instance and after streaming is stopped we can send to
            # device new media_content_id. If we don't do this we got error "already
            # streaming to device".
            # Error provided by pyatv component https://github.com/postlund/pyatv
            await self.hass.services.async_call(
                "media_player",
                "media_stop",
                {"entity_id": source["entity_id"]},
            )
            # After command is sended, we need to wait while receiver accept command and
            # stop streaming.
            await asyncio.sleep(1)

        try:
            player_state = data["state"]["playerState"]

            if player_state["type"] == "FmRadio":
                info = utils.get_radio_info(data)
            else:
                info = await get_file_info(
                    self.quasar.session,
                    player_state["id"],
                    source.get("quality", "lossless"),
                    source.get("codecs", "mp3"),
                )

            data = {
                "media_content_id": stream.get_url(info["url"], info["codec"]),
                "media_content_type": source.get("media_content_type", "music"),
                "entity_id": source["entity_id"],
            }

            if source.get("platform") == "cast":
                if data["media_content_id"].endswith(".m3u8"):
                    data["media_content_type"] = "application/vnd.apple.mpegurl"

                data["extra"] = {
                    "stream_type": "BUFFERED",
                    "metadata": {
                        "metadataType": 3,
                        "title": self._attr_media_title,
                        "artist": self._attr_media_artist,
                        "images": [{"url": self._attr_media_image_url}],
                    },
                }

        except Exception as e:
            self.debug("Failed to get track url: " + str(e))
            return

        await self.async_media_seek(0)
        await self.hass.services.async_call("media_player", "play_media", data)

    def sync_service_call(self, service: str, **kwargs):
        source = self.sync_sources[self._attr_source]

        kwargs["entity_id"] = source["entity_id"]

        if service == "volume_set" and "sync_volume" in source:
            if source["sync_volume"] is False:
                return
            if isinstance(source["sync_volume"], str):
                source["sync_volume"] = Template(source["sync_volume"], self.hass)
            if isinstance(source["sync_volume"], Template):
                v = source["sync_volume"].async_render(kwargs, False)
                kwargs["volume_level"] = float(v)

        self.debug(f"Sync state: {service}")

        self.hass.create_task(
            self.hass.services.async_call("media_player", service, kwargs)
        )


# noinspection PyAbstractClass
class YandexModule(YandexStationBase):
    """YandexModule support only local control."""

    def __init__(self, quasar: YandexQuasar, device: dict):
        super().__init__(quasar, device)

        self._attr_available = False
        self._attr_should_poll = False

        # both yandex modules don't support music sync
        if self.device_platform == "yandexmodule":
            self.sync_sources = {}

        try:
            self.support_on = any(
                cap["state"]["instance"] == "on" for cap in self.device["capabilities"]
            )
        except Exception:
            self.support_on = False

    def async_set_state(self, data: dict):
        super().async_set_state(data)

        if self._attr_available and self.local_state is None:
            self._attr_available = False

    async def async_update(self):
        pass

    async def async_media_play(self):
        if self.device_platform != "yandexmodule":
            await self.glagol.send({"command": "sendText", "text": "продолжить"})
        else:
            await super().async_media_play()

    async def async_play_media(self, media_type: str, media_id: str, **kwargs):
        kwargs["extra"].setdefault("force_local", True)
        await super().async_play_media(media_type, media_id, **kwargs)

    async def async_turn_on(self):
        if self.support_on:
            await self.quasar.device_actions(self.device, on=True)
        else:
            await super().async_turn_on()

    async def async_turn_off(self):
        if self.support_on:
            await self.quasar.device_actions(self.device, on=False)
        else:
            await super().async_turn_on()
