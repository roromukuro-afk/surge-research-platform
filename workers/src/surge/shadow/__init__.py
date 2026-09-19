"""The Phase B web shadow (D-279): the same evaluation, run on Vercel next to the official one.

The official Phase B is the Windows Task Scheduler on the operator's PC, from a
frozen worktree (D-277, D-278). The shadow runs the same frozen code in the
cloud, stores what it produced in a private Vercel Blob store, and is compared
with the official system day by day. It never replaces it on its own: the
switch is the operator's decision.

``artifacts`` is the storage format (one write-once object per artifact, an
integrity record per day as its commit point), ``export`` turns a cohort the
PC wrote into that format - for the web's fixtures and for comparisons.
"""
