-- Restore the original schema.sql/database.dump captured before migration.
-- This file intentionally performs no automated destructive rollback because
-- the exact pre-cutover view definition is bound to the verified backup.
SELECT 'Use the verified Windows database.dump or pre-load schema.sql to restore the prior active view.' AS instruction;
