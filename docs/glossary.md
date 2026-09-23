# Plugin-owned storage

Storage preserves data owned by a stable plugin within a Toolframe application,
independently of that plugin's installation and execution lifecycle.

## Language

**Application Environment**:
An independently configured deployment of a Toolframe application, with its own
data identity and lifecycle; development and production are distinct environments.

**Storage Provider**:
A provider plugin that supplies persistence for data owned by other plugins,
respecting their ownership boundaries.

**Business Store**:
The durable data owned by one stable plugin identity in one application, retained
independently of package updates and worker replacement.
_Avoid_: Core State when referring to plugin-owned data.

**Store Owner**:
The stable plugin identity that owns a Business Store and defines its meaning.
_Avoid_: Worker or package version as the data owner's identity.

**Core State**:
Durable information used to manage plugins, separate from the data owned by those
plugins.

**Data Schema Version**:
The identity of the data format expected by a plugin capability.
_Avoid_: Plugin Code Version as a synonym for data compatibility.

**Application Data Backup**:
A recoverable snapshot of the plugin-owned data and schema history belonging to
one Application Environment. It does not include Core State or application secrets.
_Avoid_: Complete application backup when those separate assets are absent.

**Active Generation**:
A particular live execution of a captured plugin implementation, distinct from
its stable identity and other executions of the same package.
