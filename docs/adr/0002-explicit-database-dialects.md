# Support database dialects explicitly

The SDK and first-party consumers will declare the SQL and schema differences
needed by SQLite and PostgreSQL, preserving existing SQLite behavior and recorded
migration history. PostgreSQL support will not promise transparent execution of
arbitrary SQLite SQL. Explicit adaptation makes generated identifiers, numeric
types and migrations testable without hiding a general SQL translation layer
inside the PostgreSQL provider.
