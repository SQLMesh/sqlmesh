# Fabric catalog state

Fabric does not persist `USE` statements between independent sessions. The
adapter therefore keeps the logical target catalog separate from the catalog
used by the open connection. Restoring a scoped operation to the neutral state
updates the logical target without unnecessarily closing a connection; an
actual catalog switch closes the thread-local connection before it is reused.

Tests for this behaviour are in `tests/core/engine_adapter/test_fabric.py`.
When changing catalog creation or deletion, cover both the configured default
catalog and a non-default warehouse.
