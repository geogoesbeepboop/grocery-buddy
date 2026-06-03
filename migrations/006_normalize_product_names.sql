-- 006_normalize_product_names.sql
--
-- Products are keyed by their name across inventory, consumption profiles, and
-- events. Case/whitespace variants ("Milk" vs "milk") were stored as separate
-- rows, causing duplicate pantry entries and double-ordering in a single run.
--
-- This migration (1) merges duplicates, keeping the most-recently-updated row
-- per (user, canonical name), and (2) canonicalizes every product name to the
-- normalized lowercase form, matching grocery_buddy.products.normalize_product
-- so future upserts dedupe via the existing UNIQUE (user_id, product) constraint.

-- ── inventory_items ───────────────────────────────────────────────────────────
DELETE FROM inventory_items t
USING (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id, lower(regexp_replace(btrim(product), '\s+', ' ', 'g'))
               ORDER BY updated_at DESC, id DESC
           ) AS rn
    FROM inventory_items
) d
WHERE t.id = d.id AND d.rn > 1;

UPDATE inventory_items
SET product = lower(regexp_replace(btrim(product), '\s+', ' ', 'g'))
WHERE product <> lower(regexp_replace(btrim(product), '\s+', ' ', 'g'));

-- ── consumption_profile ───────────────────────────────────────────────────────
DELETE FROM consumption_profile t
USING (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id, lower(regexp_replace(btrim(product), '\s+', ' ', 'g'))
               ORDER BY updated_at DESC, id DESC
           ) AS rn
    FROM consumption_profile
) d
WHERE t.id = d.id AND d.rn > 1;

UPDATE consumption_profile
SET product = lower(regexp_replace(btrim(product), '\s+', ' ', 'g'))
WHERE product <> lower(regexp_replace(btrim(product), '\s+', ' ', 'g'));

-- ── consumption_events (time-series — just canonicalize so they join) ──────────
UPDATE consumption_events
SET product = lower(regexp_replace(btrim(product), '\s+', ' ', 'g'))
WHERE product <> lower(regexp_replace(btrim(product), '\s+', ' ', 'g'));
