# Back up the application database before existing-schema changes

The provider creates a full application-database backup before applying reviewed
changes to existing schemas, and restoration targets a new database rather than
overwriting live data automatically. A database-wide dump gives a clearer recovery
boundary than schema-only dumps that may omit dependencies, at the cost of backup
time and storage as the application grows. Provisioning restores required roles
and grants separately, and Core state and application secrets remain outside the
PostgreSQL backup's scope.
