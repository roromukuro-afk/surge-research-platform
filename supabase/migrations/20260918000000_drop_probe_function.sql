-- Remove a function that exists in the cloud database and in no migration.
--
-- `ref.tmp_trigger_probe()` was `select 1`, created while checking that the
-- event trigger which revokes PUBLIC EXECUTE actually fires on a new function.
-- The probe was never dropped, so the database carried an object the migrations
-- do not describe - the same class of divergence as a drifted body, only in the
-- other direction: not "the file says something else" but "the file says
-- nothing at all".
--
-- Nothing referenced it (no trigger, no event trigger, no default, no view), so
-- dropping it removes a stray, not a dependency.

drop function if exists ref.tmp_trigger_probe();
