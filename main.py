import httpx
import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from pid import PidFile, PidFileError
from syftbox.lib import Client
from tqdm import tqdm
from typing import Any, Optional

# Initialize logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

client: Client = Client.load()

DATA_DIR: Path = client.api_data()
SYNC_STATE_ENDPOINT: str = f"{client.config.client_url}sync/state"

state_path: Path = DATA_DIR / "state.json"


class SyncStatus(Enum):
    QUEUED = "queued"
    ERRORED = "error"
    SYNCED = "synced"
    IGNORED = "ignored"

    @property
    def color_code(self) -> int:
        return {
            SyncStatus.QUEUED: 1,
            SyncStatus.ERRORED: 2,
            SyncStatus.SYNCED: 6,
            SyncStatus.IGNORED: 7,
        }[self]


def apply_sync_status_indicator(path: Path, status: SyncStatus) -> bool:
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "Finder" to set label index of (POSIX file "{path}" as alias) to {status.color_code}',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to apply sync status indicator: {e}")
        return False


def fetch_sync_state() -> list[dict[str, Any]]:
    try:
        response = httpx.get(SYNC_STATE_ENDPOINT)
        response.raise_for_status()
        return response.json()
    except httpx.RequestError as e:
        logging.error(f"Request error while fetching sync state: {e}")
    except httpx.HTTPStatusError as e:
        logging.error(f"HTTP error while fetching sync state: {e}")
    return []


def load_last_synced() -> Optional[datetime]:
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
            last_synced = data.get("last_synced")
            return datetime.fromisoformat(last_synced)
    except FileNotFoundError:
        logging.warning("State file not found. Assuming first run.")
    except ValueError:
        logging.error("Invalid timestamp format in state file. Deleting file.")
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from state file: {e}. Deleting file.")

    state_path.unlink(missing_ok=True)
    return None


def update_last_synced(timestamp: datetime) -> None:
    data = {"last_synced": timestamp.isoformat()}
    try:
        with open(state_path, "w") as f:
            json.dump(data, f)
        logging.info("Sync state updated successfully.")
    except IOError as e:
        logging.error(f"Failed to update sync state: {e}")


def process_item(item: dict[str, Any]) -> None:
    try:
        path = client.datasites / item["path"]
        status = SyncStatus(item["status"])
        apply_sync_status_indicator(path, status)
    except KeyError as e:
        logging.error(f"Missing expected key in sync state item: {e}")
    except ValueError as e:
        logging.error(f"Invalid status value: {e}")


def apply() -> None:
    try:
        with PidFile(pidname="sync_status_indicators.pid", piddir=DATA_DIR):
            sync_state = fetch_sync_state()
            sync_state_fetch_timestamp = datetime.now()
            if not sync_state:
                return

            last_synced = load_last_synced()
            buffer = timedelta(seconds=2)

            if last_synced:
                last_synced_dt = datetime.fromisoformat(last_synced)
                sync_state = [
                    i
                    for i in sync_state
                    if datetime.fromisoformat(i["timestamp"]) >= last_synced_dt - buffer
                ]

            with ThreadPoolExecutor() as executor:
                futures = [executor.submit(process_item, item) for item in sync_state]
                for future in tqdm(as_completed(futures), total=len(futures)):
                    future.result()

            update_last_synced(timestamp=sync_state_fetch_timestamp)
    except PidFileError:
        # A previous instance is still running, skip this run
        pass


apply()
