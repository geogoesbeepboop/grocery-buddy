-- 005_checkout_ready_status.sql
--
-- The agent never actually completes a purchase on the user's behalf. After the
-- user approves a cart, the automation stages everything in the real Amazon cart
-- and hands the user a checkout link to finish themselves. Add a 'checkout_ready'
-- status to both carts and purchases so the data model reflects "staged, not
-- bought" instead of the misleading 'purchased'/'completed'.

ALTER TABLE carts DROP CONSTRAINT IF EXISTS carts_status_check;
ALTER TABLE carts ADD CONSTRAINT carts_status_check
    CHECK (status IN (
        'draft','pending_approval','approved','checkout_ready',
        'purchased','failed','rejected','expired'
    ));

ALTER TABLE purchases DROP CONSTRAINT IF EXISTS purchases_status_check;
ALTER TABLE purchases ADD CONSTRAINT purchases_status_check
    CHECK (status IN ('pending','checkout_ready','completed','failed'));
