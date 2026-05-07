-- Drop lowercase duplicates for indicators_rsi_historical (keep UPPERCASE)
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.column_name
    FROM information_schema.columns c
    WHERE c.table_schema='public' AND c.table_name='indicators_rsi_historical'
    GROUP BY UPPER(c.column_name), c.column_name
    HAVING COUNT(*) FILTER (WHERE c.column_name = UPPER(c.column_name)) > 0
       AND c.column_name <> UPPER(c.column_name)
  LOOP
    EXECUTE format('ALTER TABLE public.indicators_rsi_historical DROP COLUMN IF EXISTS %I', r.column_name);
  END LOOP;
END $$;
