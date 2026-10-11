"""Daily backup of the database, plus a weekly archive of uploaded photos.

Uses only the standard library, so it runs with the server's normal python3.
Backups go to ~/backups. Old ones are deleted automatically.
"""
import datetime
import sqlite3
import tarfile
from pathlib import Path

APP = Path.home() / "import-tracker"
BACKUPS = Path.home() / "backups"
KEEP_DATABASE_COPIES = 14   # one per day
KEEP_PHOTO_ARCHIVES = 4     # one per week


def prune(pattern, keep):
    files = sorted(BACKUPS.glob(pattern))
    for old in files[:-keep]:
        old.unlink()


def main():
    BACKUPS.mkdir(exist_ok=True)
    today = datetime.date.today()

    # sqlite's own backup call gives a consistent copy even while the site is running
    target = BACKUPS / f"db-{today.isoformat()}.sqlite3"
    source = sqlite3.connect(APP / "db.sqlite3")
    destination = sqlite3.connect(target)
    with destination:
        source.backup(destination)
    destination.close()
    source.close()
    prune("db-20*.sqlite3", KEEP_DATABASE_COPIES)
    print("Database backed up:", target.name)

    # photos: once a week, on Sundays
    media = APP / "media"
    if today.weekday() == 6 and media.exists():
        archive = BACKUPS / f"photos-{today.isoformat()}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(media, arcname="media")
        prune("photos-20*.tar.gz", KEEP_PHOTO_ARCHIVES)
        print("Photos backed up:", archive.name)


main()