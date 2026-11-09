-- Static Redis admission program. Every accessed key is explicitly in KEYS;
-- the Python boundary gives them one hash tag for Redis Cluster compatibility.
-- TIME is in integer milliseconds, safely below Lua's exact integer limit.
local operation = ARGV[1]
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local minute, day, batch = 60000, 86400000, 128
local names = {'requests_per_minute', 'requests_per_day', 'tokens_per_minute', 'max_concurrent'}
local events = {}
local total = 0
local cooldown = 0
local poisoned = false
local max_integer = 9007199254740991
local policy = nil
local bound = redis.call('GET', KEYS[1])

local function integer(value)
    -- tostring/cjson round after fourteen significant digits by default.
    return string.format('%.0f', value)
end

local function snapshot()
    return {requests_in_minute=integer(redis.call('ZCARD', KEYS[2])),
            requests_in_day=integer(redis.call('ZCARD', KEYS[3])), tokens_in_minute=integer(total),
            in_flight=integer(redis.call('ZCARD', KEYS[4])),
            cooldown_seconds=math.max(0, cooldown-now)/1000}
end

local function reply(status, code, retry, retire_after)
    if policy then
        redis.call('SET', KEYS[7], poisoned and 'unavailable' or integer(total))
        local ttl = math.max(day + policy[10] + minute, cooldown-now+day)
        for _, key in ipairs(KEYS) do redis.call('PEXPIRE', key, math.ceil(ttl)) end
    end
    return cjson.encode({version=2, status=status, code=code or cjson.null,
                         retry_after_seconds=retry or cjson.null, snapshot=snapshot(), events=events,
                         retire_after_ms=retire_after and integer(retire_after) or cjson.null})
end

local function can_retire()
    -- The successful admission reply issued this immutable server deadline.
    -- A whole missing bucket before then can indicate eviction, never expiry.
    -- The extra possible cooldown horizon is deliberately conservative: every
    -- admitted operation must complete within its configured lease.
    local deadline, duration = tonumber(ARGV[6]), tonumber(ARGV[5]) or 0
    return deadline and deadline > 0 and deadline <= max_integer and
           deadline == math.floor(deadline) and duration >= 0 and
           duration <= max_integer - deadline and now >= deadline + duration
end

local function event(kind, dimension, used, limit, retry, provider, model, log)
    events[#events+1] = {kind=kind, dimension=dimension, used=integer(used), limit=integer(limit),
                         retry_after_seconds=retry or cjson.null, provider=provider, model_id=model, log=log}
end

local function dimensions()
    return {redis.call('ZCARD', KEYS[2]), redis.call('ZCARD', KEYS[3]),
            total, redis.call('ZCARD', KEYS[4])}
end

local function warnings(provider, model, emit)
    local used = dimensions()
    for i, dimension in ipairs(names) do
        if used[i] < policy[i+4] then
            redis.call('SREM', KEYS[9], dimension)
        elseif emit and redis.call('SADD', KEYS[9], dimension) == 1 then
            event('near_limit', dimension, used[i], policy[i], nil, provider, model, true)
        end
    end
end

-- A corrupt or partially evicted bucket must never become a fresh budget.
if not bound then
    for i=2,#KEYS do
        if redis.call('EXISTS', KEYS[i]) == 1 then return reply('unavailable') end
    end
    if operation == 'snapshot' then return reply('snapshot') end
    if operation == 'finish' then
        return reply(can_retire() and 'retired' or 'unavailable')
    end
else
    policy = cjson.decode(bound)
    local rawtotal = redis.call('GET', KEYS[7])
    total = tonumber(rawtotal)
    cooldown = tonumber(redis.call('GET', KEYS[8]) or '0')
    if not total or total < 0 or total > max_integer or not cooldown then
        total, cooldown, poisoned = 0, 0, true
        return reply('unavailable')
    end
    if operation == 'acquire' and bound ~= ARGV[2] then return reply('conflict') end

    redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now-minute)
    redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now-day)

    -- Clean small reservation records in bounded batches, never the day log.
    local expired = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now, 'LIMIT', 0, batch)
    for _, id in ipairs(expired) do
        local raw = redis.call('HGET', KEYS[5], id)
        if not raw then return reply('unavailable') end
        local record = cjson.decode(raw)
        if record.s ~= 'a' then return reply('unavailable') end
        record.s = 'l'
        redis.call('ZREM', KEYS[4], id)
        if record.a + minute > now then
            redis.call('ZADD', KEYS[6], record.a+minute, id)
        else
            total = total-tonumber(record.t)
            record.t = '0'
        end
        redis.call('HSET', KEYS[5], id, cjson.encode(record))
    end
    local settled = redis.call('ZRANGEBYSCORE', KEYS[6], '-inf', now, 'LIMIT', 0, batch)
    for _, id in ipairs(settled) do
        local raw = redis.call('HGET', KEYS[5], id)
        if not raw then return reply('unavailable') end
        local record = cjson.decode(raw)
        if record.s == 'a' then return reply('unavailable') end
        total = total-tonumber(record.t)
        record.t = '0'
        redis.call('ZREM', KEYS[6], id)
        redis.call('HSET', KEYS[5], id, cjson.encode(record))
    end
    local forgotten = redis.call('ZRANGEBYSCORE', KEYS[11], '-inf', now, 'LIMIT', 0, batch)
    for _, id in ipairs(forgotten) do
        redis.call('HDEL', KEYS[5], id)
        redis.call('ZREM', KEYS[11], id)
    end
    if (redis.call('ZCOUNT', KEYS[4], '-inf', now) > 0 or
            redis.call('ZCOUNT', KEYS[6], '-inf', now) > 0 or total < 0) then
        return reply('unavailable')
    end
    warnings('', '', false)
end

if operation == 'snapshot' then return reply('snapshot') end

if operation == 'finish' then
    local id = ARGV[3]
    local raw = redis.call('HGET', KEYS[5], id)
    if not raw then return reply(can_retire() and 'retired' or 'unavailable') end
    local record = cjson.decode(raw)
    if record.s == 'f' then return reply('finished') end
    if record.s == 'x' then return reply('expired_finished') end
    local expired = record.s == 'l'
    if record.s ~= 'a' and not expired then return reply('unavailable') end
    if not expired then
        local deadline = tonumber(redis.call('ZSCORE', KEYS[4], id))
        if not deadline or deadline <= now then return reply('unavailable') end
    end
    local charge = 0
    if record.a+minute > now then
        charge = tonumber(ARGV[4] ~= '' and ARGV[4] or record.e)
    end
    local revised = total-tonumber(record.t)+charge
    if revised > max_integer then
        -- A counter outside Lua's exact range must not later release rounded
        -- capacity. Keep this bucket unavailable until its retention expires.
        poisoned = true
        return reply('unavailable')
    end
    redis.call('ZREM', KEYS[4], id)
    total = revised
    record.t = '0'
    if record.a+minute > now then
        record.t = integer(charge)
        redis.call('ZADD', KEYS[6], record.a+minute, id)
    end
    -- Expired completion is accounting only. Its ID can never replay an
    -- admission, even though subsequent accounting completion is idempotent.
    record.s = expired and 'x' or 'f'
    redis.call('HSET', KEYS[5], id, cjson.encode(record))
    local duration = tonumber(ARGV[5])
    if duration and duration > 0 and now+duration > cooldown then
        cooldown = now+duration
        redis.call('SET', KEYS[8], integer(cooldown))
        event('cooldown', 'cooldown', 0, 0, duration/1000, record.p, record.m, true)
    end
    warnings(record.p, record.m, true)
    return reply(expired and 'expired_finished' or 'finished')
end

if operation ~= 'acquire' then return reply('unavailable') end
local requested = cjson.decode(ARGV[2])
local id, estimate, provider, model = ARGV[3], tonumber(ARGV[4]), ARGV[5], ARGV[6]
if bound then
    local previous = redis.call('HGET', KEYS[5], id)
    if previous then
        local record = cjson.decode(previous)
        if tonumber(record.e) ~= estimate or record.p ~= provider or record.m ~= model then
            return reply('unavailable')
        end
        if record.s == 'a' or record.s == 'f' then
            return reply('admitted', nil, nil, record.a+day+policy[10])
        end
        return reply('unavailable')
    end
end

local function deny(dimension, used, limit, retry, code)
    local log = true
    if bound then log = redis.call('SADD', KEYS[10], dimension) == 1 end
    event('blocked', dimension, used, limit, retry, provider, model, log)
    return reply('denied', code or 'local_rate_limit', retry)
end

if estimate > requested[3] then
    return deny('tokens_per_minute', estimate, requested[3], nil, 'request_budget_exceeded')
end
if bound then
    if cooldown > now then return deny('cooldown', 0, 0, (cooldown-now)/1000) end
    local used = dimensions()
    if used[4] >= policy[4] then return deny(names[4], used[4], policy[4], 1) end
    for i=1,2 do
        if used[i] >= policy[i] then
            local first = redis.call('ZRANGE', KEYS[i+1], 0, 0, 'WITHSCORES')
            local window = i == 1 and minute or day
            return deny(names[i], used[i], policy[i], (tonumber(first[2])+window-now)/1000)
        end
    end
    if total+estimate > policy[3] then
        -- At most one minute's settled charges are inspected, never day history.
        local charges = redis.call('ZRANGE', KEYS[6], 0, -1, 'WITHSCORES')
        local released = 0
        for i=1,#charges,2 do
            local raw = redis.call('HGET', KEYS[5], charges[i])
            if not raw then return reply('unavailable') end
            released = released+tonumber(cjson.decode(raw).t)
            if total+estimate-released <= policy[3] then
                return deny(names[3], total, policy[3], (tonumber(charges[i+1])-now)/1000)
            end
        end
        return deny(names[3], total, policy[3], 1)
    end
else
    policy = requested
    redis.call('SET', KEYS[1], ARGV[2])
end

redis.call('DEL', KEYS[10])
local record = {a=now, t=ARGV[4], e=ARGV[4], p=provider, m=model, s='a'}
redis.call('ZADD', KEYS[2], now, id)
redis.call('ZADD', KEYS[3], now, id)
redis.call('ZADD', KEYS[4], now+policy[10], id)
redis.call('HSET', KEYS[5], id, cjson.encode(record))
redis.call('ZADD', KEYS[11], now+day+policy[10], id)
total = total+estimate
warnings(provider, model, true)
return reply('admitted', nil, nil, now+day+policy[10])
