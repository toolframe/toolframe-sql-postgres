# Separate provisioning privileges from normal execution

A setup tool provisions the application database and store identities using
explicit administrative credentials; the running provider does not retain
cluster-wide database or role creation privileges. Business access is scoped to
the owning store, with host-controlled migration and backup responsibilities
separate from ordinary SQL calls. This adds an explicit provisioning step when
new stores are introduced, in exchange for a bounded runtime privilege model.
