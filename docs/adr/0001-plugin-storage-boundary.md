# Keep PostgreSQL plugin storage separate from Core management state

The PostgreSQL provider supplies plugin-owned stores for the existing business
and authentication consumers, while Core retains its independent management
state. The initial support boundary is one Core per application with fresh
PostgreSQL stores, preserving the existing SQLite application. This keeps storage
adoption independent of distributed host coordination and avoids making Core's
own recovery depend on a provider whose lifecycle it manages.
