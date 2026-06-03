-- One schedule row per user (allow ON CONFLICT upsert in the CLI)
ALTER TABLE schedules ADD CONSTRAINT schedules_user_id_unique UNIQUE (user_id);
