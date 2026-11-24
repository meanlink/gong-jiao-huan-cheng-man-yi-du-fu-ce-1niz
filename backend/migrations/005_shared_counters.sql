CREATE TABLE IF NOT EXISTS support_counter (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    desk text NOT NULL CHECK (desk IN ('access', 'station', 'dispatch')),
    location text NOT NULL CHECK (char_length(location) BETWEEN 2 AND 100),
    capacity smallint NOT NULL CHECK (capacity BETWEEN 1 AND 30),
    available boolean NOT NULL,
    updated_by text NOT NULL REFERENCES staff_account(email),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, desk)
);

CREATE TABLE IF NOT EXISTS support_counter_member (
    plan_id bigint NOT NULL REFERENCES route_handover(plan_id),
    desk text NOT NULL CHECK (desk IN ('access', 'station', 'dispatch')),
    counter_id bigint NOT NULL,
    PRIMARY KEY (plan_id, desk),
    FOREIGN KEY (counter_id, desk) REFERENCES support_counter(id, desk)
);

-- 既有的每线路服务点各自迁为一个独立柜台，之后主管可把多条线路并入同一个柜台。
DO $$
DECLARE
    desk_row record;
    new_counter_id bigint;
BEGIN
    IF to_regclass('support_desk') IS NOT NULL THEN
        FOR desk_row IN SELECT * FROM support_desk LOOP
            INSERT INTO support_counter (desk, location, capacity, available, updated_by, updated_at)
            VALUES (desk_row.desk, desk_row.location, desk_row.capacity, desk_row.available,
                    desk_row.updated_by, desk_row.updated_at)
            RETURNING id INTO new_counter_id;
            INSERT INTO support_counter_member (plan_id, desk, counter_id)
            VALUES (desk_row.plan_id, desk_row.desk, new_counter_id);
        END LOOP;
        ALTER TABLE support_case DROP CONSTRAINT IF EXISTS support_case_plan_id_desk_fkey;
        ALTER TABLE support_case ADD CONSTRAINT support_case_plan_id_desk_fkey
            FOREIGN KEY (plan_id, desk) REFERENCES support_counter_member(plan_id, desk);
        ALTER TABLE support_case_event DROP CONSTRAINT IF EXISTS support_case_event_plan_id_desk_fkey;
        ALTER TABLE support_case_event ADD CONSTRAINT support_case_event_plan_id_desk_fkey
            FOREIGN KEY (plan_id, desk) REFERENCES support_counter_member(plan_id, desk);
        DROP TABLE support_desk;
    END IF;
END $$;
