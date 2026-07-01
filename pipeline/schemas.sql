-- stop_events: one row per (service_date, trip_id, stop_sequence)
-- delay_sec is NULL for ghost trips; is_ghost=1 for canceled or never-observed trips.
-- PRIMARY KEY enforces idempotent re-runs: build_stop_events.py deletes then re-inserts.

CREATE TABLE IF NOT EXISTS stop_events (
  service_date  TEXT     NOT NULL,
  route_id      TEXT     NOT NULL,
  direction_id  INTEGER  NOT NULL,
  trip_id       TEXT     NOT NULL,
  stop_id       TEXT     NOT NULL,
  stop_sequence INTEGER  NOT NULL,
  scheduled_ts  INTEGER  NOT NULL,   -- Unix epoch (Europe/Sofia, 04:00 service-day boundary)
  delay_sec     INTEGER,             -- NULL when is_ghost = 1
  is_ghost      INTEGER  NOT NULL DEFAULT 0,
  PRIMARY KEY (service_date, trip_id, stop_sequence)
);

CREATE INDEX IF NOT EXISTS idx_se_route
  ON stop_events (route_id, direction_id, service_date);

-- vehicle_arrivals: one row per (service_date, trip_id, stop_sequence)
-- delay_sec computed from GPS actual_arrival, not from TripUpdates prediction.
-- source='vp' for GPS-derived arrivals; 'tu' for TripUpdates fallback.
-- gps_distance_m is NULL when source='tu' (no GPS position available).
-- PRIMARY KEY enforces idempotent re-runs: build_vehicle_arrivals.py deletes then re-inserts.

CREATE TABLE IF NOT EXISTS vehicle_arrivals (
  service_date    TEXT     NOT NULL,
  route_id        TEXT     NOT NULL,
  direction_id    INTEGER  NOT NULL,
  trip_id         TEXT     NOT NULL,
  stop_id         TEXT     NOT NULL,
  stop_sequence   INTEGER  NOT NULL,
  vehicle_id      TEXT,
  scheduled_ts    INTEGER  NOT NULL,       -- Unix epoch (Europe/Sofia, 04:00 service-day boundary)
  actual_arrival  INTEGER,                 -- NULL if vehicle never reached this stop (ghost/absent)
  delay_sec       INTEGER,                 -- actual_arrival - scheduled_ts; NULL if no arrival
  gps_distance_m  REAL,                    -- haversine distance from position to stop coords; NULL if no GPS
  source          TEXT     NOT NULL DEFAULT 'vp',
  PRIMARY KEY (service_date, trip_id, stop_sequence)
);

CREATE INDEX IF NOT EXISTS idx_va_route
  ON vehicle_arrivals (route_id, direction_id, service_date);
