-- Текстовый MVP: пользователи, история, память, учёт расходов, дедупликация апдейтов.

CREATE TABLE users (
    id                      BIGSERIAL PRIMARY KEY,
    tg_user_id              BIGINT NOT NULL UNIQUE,
    first_name              TEXT,
    language_code           TEXT,
    age_confirmed_at        TIMESTAMPTZ,
    -- /reset: в контекст попадают только сообщения с id больше этой границы
    context_from_message_id BIGINT NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    model      TEXT,
    -- для ответа: id сообщения пользователя, на которое он отвечает (порядок в истории)
    reply_to_id BIGINT,
    summarized BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX messages_user_id_id ON messages (user_id, id);
CREATE INDEX messages_user_unsummarized ON messages (user_id, id) WHERE NOT summarized;

CREATE TABLE conversation_summaries (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content          TEXT NOT NULL,
    first_message_id BIGINT NOT NULL,
    last_message_id  BIGINT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX conversation_summaries_user_id_id ON conversation_summaries (user_id, id);

CREATE TABLE memory_facts (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category   TEXT NOT NULL CHECK (category IN ('name', 'interest', 'preference', 'fact', 'thread')),
    fact       TEXT NOT NULL,
    fact_norm  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, fact_norm)
);
CREATE INDEX memory_facts_user_id ON memory_facts (user_id, category);

CREATE TABLE api_usage (
    id                 BIGSERIAL PRIMARY KEY,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    operation          TEXT NOT NULL,
    user_id            BIGINT REFERENCES users(id) ON DELETE SET NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           NUMERIC(12, 6) NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX api_usage_provider_created ON api_usage (provider, created_at);

-- Защита от повторной обработки одного и того же update_id (например, после рестарта).
CREATE TABLE processed_updates (
    update_id  BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
