-- get_iomete_maintenance_config — discoverable maintenance configuration entries.
-- The maintenance table is configured at deployment time; this tool is not
-- snapshot-scoped because configuration is an operational property.
SELECT key, value, source
FROM :maintenance_table
ORDER BY source, key
