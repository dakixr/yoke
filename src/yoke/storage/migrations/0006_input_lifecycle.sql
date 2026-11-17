-- Durable input phases and explicit queue holds. Legacy payloads stay readable.
ALTER TABLE inputs ADD COLUMN phase TEXT;
ALTER TABLE session_queues ADD COLUMN held_reason TEXT;
ALTER TABLE session_queues ADD COLUMN input_revision INTEGER NOT NULL DEFAULT 0;

-- Older CLI queues may have a pending payload without an admission identity.
-- Preserve the payload and give those inputs the same durable owner as HTTP inputs.
UPDATE inputs SET admission = json_object(
    'id', id, 'session_id', session_id, 'prompt', prompt,
    'attachments', json(coalesce(json_extract(pending, '$.attachments'), '[]')),
    'continuation', json(coalesce(json_extract(pending, '$.continuation'), 'false')),
    'skill', json_extract(pending, '$.skill'),
    'delivery', CASE WHEN json_extract(pending, '$.kind') = 'steering' THEN 'steer' ELSE 'queue' END,
    'fingerprint', yoke_sha256(CAST(json_object(
        'delivery', CASE WHEN json_extract(pending, '$.kind') = 'steering' THEN 'steer' ELSE 'queue' END,
        'prompt', prompt, 'sessionID', session_id) AS BLOB)),
    'time_created', coalesce(json_extract(pending, '$.created_at'), ''),
    'admitted_seq', coalesce(admitted_seq, 0), 'state', 'admitted'
), state = 'admitted'
WHERE admission IS NULL AND pending IS NOT NULL;

-- Complete a crash-interrupted workspace pause in the migration transaction.
UPDATE inputs SET pending = json_object(
    'id', id, 'prompt', prompt,
    'attachments', json(coalesce(json_extract(admission, '$.attachments'), '[]')),
    'continuation', json(coalesce(json_extract(admission, '$.continuation'), 'false')),
    'skill', json_extract(admission, '$.skill'),
    'kind', CASE WHEN json_extract(admission, '$.delivery') = 'steer' THEN 'steering' ELSE 'queued' END,
    'created_at', coalesce(json_extract(admission, '$.time_created'), ''),
    'paused', json('true'), 'pause_cause', 'workspace'
), position = coalesce(json_extract(admission, '$.queue_position'), 0), paused = 1,
   state = 'admitted', admission = json_set(admission, '$.state', 'admitted')
WHERE json_extract(admission, '$.workspace_blocked') = 1
  AND coalesce(json_extract(admission, '$.settled'), 0) = 0 AND state != 'removed';
WITH positions AS (
    SELECT session_id, id, row_number() OVER (
        PARTITION BY session_id ORDER BY position,
        CASE WHEN json_extract(admission, '$.workspace_blocked') THEN 0 ELSE 1 END,
        admitted_seq, id) - 1 AS new_position
    FROM inputs WHERE position IS NOT NULL
)
UPDATE inputs SET position = (
    SELECT new_position FROM positions p WHERE p.session_id = inputs.session_id AND p.id = inputs.id
) WHERE position IS NOT NULL;

UPDATE inputs SET phase = CASE
    WHEN state = 'removed' THEN 'removed'
    WHEN coalesce(json_extract(admission, '$.settled'), 0) THEN 'settled'
    WHEN state = 'promoted' AND NOT coalesce(json_extract(admission, '$.workspace_blocked'), 0)
        THEN 'promoted'
    WHEN paused OR coalesce(json_extract(admission, '$.workspace_blocked'), 0) THEN 'paused'
    ELSE 'queued' END;
UPDATE inputs SET admission = json_set(admission, '$.phase', json(CASE phase
    WHEN 'removed' THEN json_object('kind', 'removed')
    WHEN 'settled' THEN json_object('kind', 'settled',
        'outcome', coalesce(json_extract(admission, '$.outcome'), 'recovered'),
        'settled_at', coalesce(json_extract(admission, '$.settled_at'), ''))
    WHEN 'promoted' THEN json_object('kind', 'promoted',
        'promoted_seq', coalesce(promoted_seq, 0),
        'queue_position', coalesce(json_extract(admission, '$.queue_position'), 0))
    WHEN 'paused' THEN json_object('kind', 'paused', 'position', coalesce(position, 0),
        'cause', CASE WHEN json_extract(admission, '$.workspace_blocked') THEN 'workspace' ELSE 'user' END)
    ELSE json_object('kind', 'queued', 'position', coalesce(position, 0)) END))
WHERE admission IS NOT NULL;
CREATE INDEX inputs_by_phase ON inputs (session_id, phase);
