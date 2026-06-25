import aiocache
import asyncio
import coloredlogs
import csv
import datetime
import dotenv
import hashlib
import logging
import music_assistant_client
import os
import rapidfuzz
import re
import requests
import requests_cache
import zoneinfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.enums import QueueOption

coloredlogs.install(level=os.environ.get("LOG_LEVEL", "INFO"))
dotenv.load_dotenv()

env_vars = [
    "MASS_URL",
    "MASS_TOKEN",
    "ALARMS_URL",
    "TZ",
]

missing_env_vars = [var for var in env_vars if not os.environ.get(var)]
if missing_env_vars:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing_env_vars)}")

if os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"):
    logging.getLogger("mass_alarms").setLevel(logging.DEBUG)
    DEBUG = True
else:
    DEBUG = False

for var in env_vars:
    globals()[var] = os.environ.get(var)

try:
    APP_TZ = zoneinfo.ZoneInfo(TZ)
except Exception:  # noqa: BLE001
    logging.warning("Invalid TZ '%s', defaulting to UTC", TZ)
    APP_TZ = datetime.timezone.utc

job_run_mutex = asyncio.Lock()

# Cache requests to the google sheet between runs
requests_cache.install_cache("mass_alarms_cache", backend="sqlite", expire_after=300)

# Times out relatively because we're not keeping the music assistant connective alive
# Also this is (currently) the only one we actually re-call, everything else is cache in memory in the job scheduler args
@aiocache.cached(ttl=30)
async def get_music_assistant_client():
    logging.info(f"Connecting to Music Assistant at {MASS_URL}")
    client = music_assistant_client.MusicAssistantClient(MASS_URL, None, token=MASS_TOKEN)
    await client.connect()
    return client


@aiocache.cached()
async def get_players():
    logging.info("Fetching players from Music Assistant")
    client = await get_music_assistant_client()
    await client.players.fetch_state()
    return [
        {
            "display_name": getattr(player, "display_name", None) or player.name,
            "name": player.name,
            "player_id": player.player_id
        }
        for player in client.players
    ]

@aiocache.cached()
async def get_media(kind):
    logging.info(f"Fetching {kind}s from Music Assistant")
    client = await get_music_assistant_client()
    func = getattr(client.music, f"get_library_{kind}s", None)

    if func is None:
        raise ValueError(f"Unsupported media type: {kind}")
    
    return [
        {
            "uri": item.uri,
            "provider": item.provider,
            "name": item.name
        }
        for item in await func()
    ]

@aiocache.cached()
async def load_google_sheet_alarms():
    logging.info(f"Fetching alarms from Google Sheets")
    response = requests.get(ALARMS_URL)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch alarms from {ALARMS_URL}: {response.status_code} {response.text}")

    alarms = []
    for i, row in enumerate(csv.DictReader(response.text.splitlines())):
        slug = re.sub(r'\W+', '_', row.get("Playlist", "unknown").lower())[0:20]
        row["ID"] = f"alarm_{i+1}_{slug}"
        alarms.append(row)

    if DEBUG:
        alarms.append({
            "ID": "alarm_debug",
            "Playlist": "Disney",
            "Days": "All",
            "Start Time": (datetime.datetime.now(tz=APP_TZ) + datetime.timedelta(minutes=1)).strftime("%I:%M %p"),
            "End Time": (datetime.datetime.now(tz=APP_TZ) + datetime.timedelta(minutes=2)).strftime("%I:%M %p"),
            "Volume": 20,
            "Shuffle?": "Y",
            "Speakers": "Living Room, Kitchen"
        })

    return alarms

@aiocache.cached()
async def get_matched_alarms():
    alarms = await load_google_sheet_alarms()
    players = await get_players()

    logging.info("Matching alarms with Music Assistant media")
    unmatched_speakers = set()

    for alarm in alarms:
        logging.info(f"Scheduling alarm '{alarm['ID']}' for playlist '{alarm['Playlist']}' on days '{alarm['Days']}' from {alarm['Start Time']} to {alarm['End Time']}")

        target_playlist = alarm["Playlist"]
        matches = []

        bonus = 10
        for kind in ["playlist", "album", "radio"]:
            uri_list = await get_media(kind)
            bonus -= 1
            for item in uri_list:
                if item["name"] == target_playlist:
                    matches.append((kind, item, 200 + bonus))

                elif target_playlist in item["name"]:
                    matches.append((kind, item, 100 + bonus))

                else:
                    score = rapidfuzz.fuzz.ratio(target_playlist, item["name"])
                    if score > 80:
                        matches.append((kind, item, score + bonus))

        matches.sort(key=lambda x: x[2], reverse=True)

        alarm["playlist_kind"] = matches[0][0] if matches else None
        alarm["playlist_name"] = matches[0][1]["name"] if matches else None
        alarm["playlist_uri"] = matches[0][1]["uri"] if matches else None

        alarm["play_on"] = []

        for speaker in alarm["Speakers"].split(","):
            speaker = speaker.strip()
            matched = None

            for player in players:
                if speaker.lower() in (player["display_name"] or player["name"]).lower():
                    alarm["play_on"].append(player)
                    matched = True

            if not matched:
                unmatched_speakers.add(speaker)
                    
    if unmatched_speakers:
        logging.warning(f"Unmatched speakers: {', '.join(unmatched_speakers)}")

    return alarms

async def start_alarm_playback(alarm):
    logging.info(f"Starting alarm playback for {alarm['ID']}")
    async with job_run_mutex:
        client = await get_music_assistant_client()
        pids = [s["player_id"] for s in alarm["play_on"]]

        for pid in pids:
            try:
                await client.players.player_command_power(pid, True)
            except MusicAssistantError:
                logging.warning(f"Could not power on player {pid}", exc_info=True)

        for pid in pids:
            try:
                await client.players.player_command_ungroup(pid)
            except MusicAssistantError:
                logging.debug(f"Ungroup of {pid} was a no-op", exc_info=True)

        if len(pids) == 1:
            leader_id = pids[0]
            member_ids = []
        else:
            leader_id = pids[0]
            member_ids = pids[1:]

        if member_ids:
            await client.players.player_command_group_many(leader_id, member_ids)

        for pid in pids:
            try:
                await client.players.player_command_volume_set(pid, int(alarm["Volume"]))
            except MusicAssistantError:
                logging.warning(f"Could not set volume on player {pid}", exc_info=True)

        await client.player_queues.play_media(
            leader_id,
            media=[alarm["playlist_uri"]],
            option=QueueOption.REPLACE,
            radio_mode=False,
        )

        if str(alarm.get("Shuffle?", False)).lower()[:1] in ("1", "t", "y"):
            try:
                await client.player_queues.queue_command_shuffle(leader_id, True)
            except MusicAssistantError:
                logging.warning(f"Could not shuffle queue for player {leader_id}", exc_info=True)

async def stop_alarm_playback(alarm):
    logging.info(f"Stopping alarm playback for {alarm['ID']}")  
    async with job_run_mutex:
        client = await get_music_assistant_client()
        pids = [s["player_id"] for s in alarm["play_on"]]

        for pid in pids:
            try:
                await client.players.player_command_stop(pid)
            except MusicAssistantError:
                logging.warning(f"Could not stop playback on player {pid}", exc_info=True)

            try:
                await client.players.player_command_ungroup(pid)
            except MusicAssistantError:
                logging.debug(f"Ungroup of {pid} was a no-op", exc_info=True)


ALARM_HASH = None

async def add_alarm_jobs(scheduler):
    logging.info("Running add_alarm_jobs to fetch alarms and schedule jobs")
    async with job_run_mutex:
        alarms = await get_matched_alarms()
        logging.info(f"Found {len(alarms)} alarms in Google Sheets")

        global ALARM_HASH
        new_hash = hashlib.md5(str(alarms).encode("utf-8")).hexdigest()
        if ALARM_HASH == new_hash:
            logging.info("No changes in alarms, skipping job scheduling")
            return
        else:
            ALARM_HASH = new_hash
            logging.info(f"Alarms have changed, updating job scheduling (hash={ALARM_HASH})")
            scheduler.remove_all_jobs()

        for alarm in alarms:
            logging.debug(f"Configuring alarm: {alarm['ID']}")

            if alarm.get("When?", False):
                logging.warning(f"TODO: Alarm '{alarm['ID']}' has a 'When?' condition.")
                continue

            if not alarm["playlist_uri"]:
                logging.warning(
                    f"Skipping alarm '{alarm['ID']}' because no matching playlist was found for '{alarm['Playlist']}'."
                )
                continue

            start_time = datetime.datetime.strptime(alarm["Start Time"], "%I:%M %p").time()
            end_time = datetime.datetime.strptime(alarm["End Time"], "%I:%M %p").time()

            day_map = {
                "Mon": 0,
                "Monday": 0,
                "Tue": 1,
                "Tuesday": 1,
                "Wed": 2,
                "Wednesday": 2,
                "Thu": 3,
                "Thursday": 3,
                "Fri": 4,
                "Friday": 4,
                "Sat": 5,
                "Saturday": 5,
                "Sun": 6,
                "Sunday": 6,
            }

            if alarm["Days"] == "All":
                days_of_week = list(range(7))
            elif "-" in alarm["Days"]:
                start_day, end_day = alarm["Days"].split("-")
                start_index = day_map[start_day.strip().capitalize()]
                end_index = day_map[end_day.strip().capitalize()]
                if start_index <= end_index:
                    days_of_week = list(range(start_index, end_index + 1))
                else:
                    days_of_week = list(range(start_index, 7)) + list(
                        range(0, end_index + 1)
                    )
            elif "," in alarm["Days"]:
                days_of_week = [
                    day_map[d.strip().capitalize()] for d in alarm["Days"].split(",")
                ]
            elif "&" in alarm["Days"]:
                days_of_week = [
                    day_map[d.strip().capitalize()] for d in alarm["Days"].split("&")
                ]
            else:
                days_of_week = [day_map[alarm["Days"].strip().capitalize()]]

            scheduler.add_job(
                func=start_alarm_playback,
                trigger=CronTrigger(
                    hour=start_time.hour,
                    minute=start_time.minute,
                    second=1,
                    day_of_week=",".join(str(d) for d in days_of_week),
                    timezone=APP_TZ,
                ),
                args=[alarm],
                id=f"alarm_start({alarm['Playlist']}, {alarm['Days']}, {start_time})",
                replace_existing=True,
            )

            scheduler.add_job(
                func=stop_alarm_playback,
                trigger=CronTrigger(
                    hour=end_time.hour,
                    minute=end_time.minute,
                    second=0,
                    day_of_week=",".join(str(d) for d in days_of_week),
                    timezone=APP_TZ,
                ),
                args=[alarm],
                id=f"alarm_stop({alarm['Playlist']}, {alarm['Days']}, {end_time})",
                replace_existing=True,
            )

        # Automatically reload alarms (if changed) every hour
        scheduler.add_job(
            func=add_alarm_jobs,
            trigger=CronTrigger(
                hour="*",
                minute=5,
                second=30,
                timezone=APP_TZ,
            ),
            args=[scheduler],
            id="reload_alarms",
            replace_existing=True,
        )

async def main():
    if DEBUG:
        alarms = await get_matched_alarms()
        with open("alarms_debug.json", "w", encoding="utf-8") as f:
            import json
            json.dump(alarms, f, indent=2)

    scheduler = AsyncIOScheduler(timezone=APP_TZ)
    scheduler.start()

    await add_alarm_jobs(scheduler)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
