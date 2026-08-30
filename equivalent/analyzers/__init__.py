"""The analyzers a strategy may name, run as subprocesses by the gateway.

Trust role: an analyzer's verdict becomes a claim and its file list becomes
the region's allow-list, so what these modules return decides which code is
unfrozen. They read a tree the gateway materialized for them and write
nothing.
"""
