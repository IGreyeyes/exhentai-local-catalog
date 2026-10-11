CREATE TABLE IF NOT EXISTS sync_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  dataset_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);
INSERT OR IGNORE INTO sync_meta VALUES (1, lower(hex(randomblob(16))), 1);

CREATE TABLE IF NOT EXISTS contributors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  token_hash TEXT NOT NULL UNIQUE,
  collector_id TEXT,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER,
  window_start INTEGER NOT NULL DEFAULT 0,
  window_requests INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public_snapshots (
  revision INTEGER PRIMARY KEY AUTOINCREMENT,
  gid INTEGER NOT NULL UNIQUE CHECK (gid > 0),
  favorite_count INTEGER NOT NULL CHECK (favorite_count >= 0),
  rating_count INTEGER CHECK (rating_count >= 0),
  checked_at INTEGER NOT NULL CHECK (checked_at > 0),
  source TEXT NOT NULL CHECK (source IN ('e-hentai.org', 'exhentai.org')),
  collector_id TEXT NOT NULL,
  collector_name TEXT NOT NULL DEFAULT '',
  contributor_id TEXT NOT NULL,
  received_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gid INTEGER NOT NULL,
  favorite_count INTEGER NOT NULL,
  rating_count INTEGER,
  checked_at INTEGER NOT NULL,
  source TEXT NOT NULL,
  collector_id TEXT NOT NULL,
  collector_name TEXT NOT NULL,
  contributor_id TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  UNIQUE (contributor_id, gid)
);
CREATE INDEX IF NOT EXISTS pending_received ON pending_snapshots(received_at, id);

CREATE TABLE IF NOT EXISTS upload_controls (
  id INTEGER PRIMARY KEY CHECK(id=1),
  upload_enabled INTEGER NOT NULL DEFAULT 0 CHECK(upload_enabled IN (0,1)),
  pending_count INTEGER NOT NULL DEFAULT 0,
  migration_done INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO upload_controls(id) VALUES(1);
CREATE TABLE IF NOT EXISTS upload_policy (
  id INTEGER PRIMARY KEY CHECK(id=1),trusted_daily_records INTEGER NOT NULL DEFAULT 5000,
  ip_daily_records INTEGER NOT NULL DEFAULT 10000,global_daily_records INTEGER NOT NULL DEFAULT 50000
);
CREATE TABLE IF NOT EXISTS upload_guard (id INTEGER PRIMARY KEY CHECK(id=1), allowed INTEGER NOT NULL CHECK(allowed=1));
CREATE TABLE IF NOT EXISTS upload_sources (
  id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, collector_id TEXT NOT NULL,
  name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'review' CHECK(status IN ('review','trusted','blocked')),
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS upload_limits (
  key TEXT PRIMARY KEY, window_start INTEGER NOT NULL, used INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS upload_limits_expiry ON upload_limits(window_start);
CREATE TABLE IF NOT EXISTS cloud_public (
  gid INTEGER PRIMARY KEY, payload TEXT, checked_at INTEGER NOT NULL,
  source_id TEXT NOT NULL, action TEXT NOT NULL DEFAULT 'snapshot',
  revision INTEGER NOT NULL DEFAULT 0, received_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cloud_history (
  revision INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER NOT NULL, payload TEXT,
  source_id TEXT NOT NULL, action TEXT NOT NULL, received_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cloud_history_gid ON cloud_history(gid,revision);
CREATE TABLE IF NOT EXISTS cloud_pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER NOT NULL, payload TEXT NOT NULL,
  checked_at INTEGER NOT NULL, source_id TEXT NOT NULL, reason TEXT NOT NULL,
  received_at INTEGER NOT NULL, UNIQUE(source_id,gid)
);
CREATE INDEX IF NOT EXISTS cloud_pending_received ON cloud_pending(received_at,id);
CREATE TABLE IF NOT EXISTS cloud_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, target TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS cloud_public_insert AFTER INSERT ON cloud_public BEGIN
  INSERT INTO cloud_history(gid,payload,source_id,action,received_at)
    VALUES(NEW.gid,NEW.payload,NEW.source_id,NEW.action,NEW.received_at);
  UPDATE cloud_public SET revision=last_insert_rowid() WHERE gid=NEW.gid;
END;
CREATE TRIGGER IF NOT EXISTS cloud_public_update AFTER UPDATE OF payload,checked_at,source_id,action,received_at ON cloud_public BEGIN
  INSERT INTO cloud_history(gid,payload,source_id,action,received_at)
    VALUES(NEW.gid,NEW.payload,NEW.source_id,NEW.action,NEW.received_at);
  UPDATE cloud_public SET revision=last_insert_rowid() WHERE gid=NEW.gid;
END;
CREATE TRIGGER IF NOT EXISTS cloud_pending_insert AFTER INSERT ON cloud_pending BEGIN
  UPDATE upload_controls SET pending_count=pending_count+1 WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS cloud_pending_delete AFTER DELETE ON cloud_pending BEGIN
  UPDATE upload_controls SET pending_count=pending_count-1 WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS upload_source_insert AFTER INSERT ON upload_sources BEGIN
  UPDATE upload_controls SET source_count=source_count+1 WHERE id=1;
END;
INSERT OR IGNORE INTO upload_sources(id,token_hash,collector_id,name,status,created_at)
  SELECT id,token_hash,COALESCE(collector_id,''),name,
    CASE WHEN revoked_at IS NULL THEN 'review' ELSE 'blocked' END,created_at FROM contributors WHERE (SELECT migration_done FROM upload_controls WHERE id=1)=0;
INSERT OR IGNORE INTO cloud_public(gid,payload,checked_at,source_id,received_at)
  SELECT gid,json_object('gid',gid,'favorite_count',favorite_count,'rating_count',rating_count,
    'checked_at',checked_at,'source',source,'collector_id',collector_id,'collector_name',collector_name),
    checked_at,contributor_id,received_at FROM public_snapshots WHERE (SELECT migration_done FROM upload_controls WHERE id=1)=0;
INSERT OR IGNORE INTO cloud_pending(gid,payload,checked_at,source_id,reason,received_at)
  SELECT gid,json_object('gid',gid,'favorite_count',favorite_count,'rating_count',rating_count,
    'checked_at',checked_at,'source',source,'collector_id',collector_id,'collector_name',collector_name),
    checked_at,contributor_id,'旧版待审',received_at FROM pending_snapshots WHERE (SELECT migration_done FROM upload_controls WHERE id=1)=0;
UPDATE upload_controls SET migration_done=1 WHERE id=1;
