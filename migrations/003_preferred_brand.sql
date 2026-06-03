-- Brand preferences per product, used for brand-aware Amazon selection.
--
-- preferred_brand   : the brand the user likes for this product (NULL = no preference)
-- brand_flexibility : how strictly to honor the preference when picking a result
--                     'strict'  → only buy the preferred brand
--                     'prefer'  → prefer it, but a cheaper/available alternative is OK
--                     'any'     → brand doesn't matter (default)

ALTER TABLE consumption_profile
    ADD COLUMN IF NOT EXISTS preferred_brand    TEXT,
    ADD COLUMN IF NOT EXISTS brand_flexibility  TEXT NOT NULL DEFAULT 'any'
        CHECK (brand_flexibility IN ('strict', 'prefer', 'any'));
