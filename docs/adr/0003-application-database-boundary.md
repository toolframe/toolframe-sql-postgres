# Give each application environment its own database

Each Toolframe application environment uses a dedicated PostgreSQL database,
which may share a PostgreSQL server with other application databases. Within it,
plugin-owned stores use separate schemas and access restrictions, so identical
plugin identities in different applications do not share data. Schema selection
and search_path are routing mechanisms, not substitutes for permissions.
