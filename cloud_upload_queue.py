"""Track library changes and index only snapshots awaiting server confirmation."""


def initialize_upload_queue(db):
    db.execute("CREATE TABLE IF NOT EXISTS cloud_upload_dirty (gid INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS cloud_upload_queue (gid INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
    for table in ("favorites", "rating_counts"):
        for event in ("INSERT", "UPDATE", "DELETE"):
            row = "OLD" if event == "DELETE" else "NEW"
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS cloud_dirty_{table}_{event.lower()}
                AFTER {event} ON {table} BEGIN
                INSERT INTO cloud_upload_dirty SELECT {row}.gid
                    WHERE NOT EXISTS(SELECT 1 FROM cloud_upload_dirty WHERE gid={row}.gid);
                END""")
    for event in ("INSERT", "UPDATE"):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS cloud_queue_snapshot_{event.lower()}
            AFTER {event} ON cloud_upload_snapshots BEGIN
            INSERT INTO cloud_upload_queue SELECT NEW.gid,NEW.payload
                WHERE NOT EXISTS(SELECT 1 FROM cloud_upload_ack WHERE gid=NEW.gid AND payload=NEW.payload)
                ON CONFLICT(gid) DO UPDATE SET payload=excluded.payload;
            DELETE FROM cloud_upload_queue WHERE gid=NEW.gid
                AND EXISTS(SELECT 1 FROM cloud_upload_ack WHERE gid=NEW.gid AND payload=NEW.payload);
            END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS cloud_queue_ack_{event.lower()}
            AFTER {event} ON cloud_upload_ack BEGIN
            DELETE FROM cloud_upload_queue WHERE gid=NEW.gid AND payload=NEW.payload;
            INSERT INTO cloud_upload_queue SELECT gid,payload FROM cloud_upload_snapshots
                WHERE gid=NEW.gid AND payload IS NOT NEW.payload
                ON CONFLICT(gid) DO UPDATE SET payload=excluded.payload;
            END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS cloud_queue_snapshot_delete AFTER DELETE ON cloud_upload_snapshots
        BEGIN DELETE FROM cloud_upload_queue WHERE gid=OLD.gid; END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS cloud_queue_ack_delete AFTER DELETE ON cloud_upload_ack BEGIN
        INSERT INTO cloud_upload_queue SELECT gid,payload FROM cloud_upload_snapshots WHERE gid=OLD.gid
            ON CONFLICT(gid) DO UPDATE SET payload=excluded.payload;
        END""")
    if not db.execute("SELECT 1 FROM app_settings WHERE key='cloud_upload_queue_initialized'").fetchone():
        db.execute("INSERT OR IGNORE INTO cloud_upload_dirty SELECT gid FROM favorites")
        db.execute("INSERT OR IGNORE INTO cloud_upload_dirty SELECT gid FROM cloud_upload_snapshots")
        db.execute("""INSERT OR REPLACE INTO cloud_upload_queue SELECT s.gid,s.payload FROM cloud_upload_snapshots s
            LEFT JOIN cloud_upload_ack a ON a.gid=s.gid WHERE s.payload IS NOT a.payload""")
        db.execute("INSERT INTO app_settings VALUES('cloud_upload_queue_initialized','1')")
