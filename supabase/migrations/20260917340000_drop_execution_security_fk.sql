-- Drop a foreign key that no sibling table has.
--
-- prod.entry_analysis_executions was created referencing ref.securities, and
-- prod.setups, prod.watches, prod.entry_attempts, prod.predictions and
-- prod.episodes all reference a security by id without one. That is not an
-- oversight in those tables: an identity rebuild remaps security ids
-- (CLAUDE.md 1-10b), and a hard reference from the production record would
-- either block the migration or cascade into rewriting history.
--
-- A no-op on a database built from the current migration, which no longer
-- creates it. Kept so a database that was built before this correction reaches
-- the same state as one built after.

set search_path = '';

alter table prod.entry_analysis_executions
  drop constraint if exists entry_analysis_executions_security_id_fkey;
