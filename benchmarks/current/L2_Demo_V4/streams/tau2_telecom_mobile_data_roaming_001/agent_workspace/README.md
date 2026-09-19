# Persistent Stream Workspace

This is the initial Workspace for `tau2_telecom_mobile_data_roaming_001`.

It contains no private initial state, target assertion, or answer. The runtime owns one copy of this Workspace for the whole Stream and may add model-response envelopes and learner memory under `.l2_state/`. Telecom task initialization is benchmark-private verifier state loaded from the materialized Episode fixture; it does not clear or replace the Stream Workspace.
